#!/usr/bin/env python3
"""backtest/run_protective_vol_gate.py — orchestration de la candidate
`protective_vol_gate_6majors` (`backtest/results/protective_vol_gate_6majors/SPEC.md`,
pré-enregistrée 2026-09-14, backlog P1#6, session hebdomadaire #7). Moteur commun
`backtest/engine.py` UNIQUEMENT (`docs/PROMOTION-RULES.md` §1.1) -- ce script ne réimplémente
AUCUNE logique de simulation, seulement l'orchestration walk-forward + le calcul des analyses
d'honnêteté demandées par la SPEC (jamais présentes dans `backtest/engine.py`, qui n'a pas à
connaître ce concept spécifique à une candidate).

Usage :
    python3 -m backtest.run_protective_vol_gate [--data-dir _data/crypto] [--output-dir ...]
    python3 -m backtest.run_protective_vol_gate --smoke   # calendrier tronqué aux 18 premiers
                                                            # mois, sortie dans results/.../smoke/

AUCUNE grille hors `backtest/strategies/protective_vol_gate.PARAM_GRID` n'est testée ici (import
direct de la constante, jamais une valeur ad hoc) : V in {24,72} x p_in in {0.95,0.98}, 4
combinaisons, rien d'autre (SPEC.md §4).

Portage inter-fenêtres (SPEC.md §2, "standard depuis la session #6") : `carry_across_windows=
True` est ACTIF PAR DÉFAUT pour le run NOMINAL de CETTE candidate (contrairement à
`run_funding_carry.py` où il restait un flag opt-in informatif hérité d'une session antérieure
à ce changement de standard) -- appliqué IDENTIQUEMENT à la candidate ET au contrôle apparié
(SPEC.md §5 : "même moteur, même overlay, mêmes coûts, mêmes fenêtres" -- le portage fait partie
de ce pipeline commun, sinon la comparaison candidate/contrôle ne serait plus appariée sur un
pipeline réellement identique). Le benchmark B&H (sans coûts ni overlay) et le proxy quasi-passif
SMA200 n'utilisent PAS le portage : ni l'un ni l'autre ne passent par la surcouche de risque du
moteur (poids constants ou déterministes, capital simplement remis à plat comme des repères
statiques, cf. `backtest/run_vol_breakout.py`/`backtest/run_funding_carry.py`, même convention).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtest import data_hourly as bt_data  # noqa: E402
from backtest import engine  # noqa: E402
from backtest import metrics as bt_metrics  # noqa: E402
from backtest import risk_overlay  # noqa: E402
from backtest.strategies import protective_vol_gate as pvg  # noqa: E402
from bot import config as bot_cfg  # noqa: E402

UNIVERSE = list(bot_cfg.SYMBOLS_CRYPTO)  # BTC, ETH, SOL, DOGE, LINK, AVAX (SPEC.md §2)
assert UNIVERSE == ["BTC", "ETH", "SOL", "DOGE", "LINK", "AVAX"], (
    "bot.config.SYMBOLS_CRYPTO a changé -- SPEC.md fige explicitement cet univers, "
    "vérification défensive pour ne jamais dériver silencieusement de la SPEC."
)

DEFAULT_DATA_DIR = REPO_ROOT / "_data" / "crypto"
OUTPUT_DIR = REPO_ROOT / "backtest" / "results" / "protective_vol_gate_6majors"
SMOKE_OUTPUT_DIR = OUTPUT_DIR / "smoke"

COST_BPS_NOMINAL = 25.0  # SPEC.md §2 -- 25 bps/côté uniforme, pessimiste
COST_BPS_STRESS_3X = 75.0
COST_BPS_STRESS_5X = 125.0

IS_MONTHS = 9
OOS_MONTHS = 3
STEP_MONTHS = 3

PERIODS_PER_YEAR_HOURLY = 8760.0  # SPEC.md §2 -- "periods_per_year=8760 partout"

# K_total = 14 (lignes RESEARCH-REGISTRY.json au 2026-09-14, vérifié avant exécution) +
# n_fenêtres x 4 (candidate) + n_fenêtres x 1 (contrôle) -- SPEC.md §6, formule figée. Le
# contrôle est compté comme essai à part entière (conservateur).
K_REGISTRY_ROWS = 14
N_GRID_COMBOS_CANDIDATE = len(pvg.PARAM_GRID)  # 4
N_GRID_COMBOS_CONTROL = 1
EXPECTED_N_WINDOWS_RANGE = (15, 16)  # SPEC.md §6 : "attendu ~15-16"

PROMOTION_RULES_THRESHOLDS = {
    "sharpe_oos_min": 0.70,
    "profit_factor_oos_min": 1.15,
    "n_trades_oos_min": 80,
    "maxdd_relative_to_benchmark_max": 1.5,
    "dsr_min": 0.50,
}

SUBPERIOD_SPLIT_DATE = pd.Timestamp("2024-01-01")

# Proxy quasi-passif (SPEC.md §7.3) : "panier long/flat SMA200 équipondéré" -- distinct du
# proxy vol-targeté de `run_vol_breakout.py` (qui répond à une question différente : la
# redondance avec le VOL TARGETING de production, pas avec une brique de trend-following déjà
# en production). Choix documenté dans `build_sma_trend_proxy_weights` ci-dessous.
PROXY_SMA_HOURS = 4800  # 200 jours * 24h (équivalent horaire du SMA200 quotidien standard)

# --smoke (SPEC.md/mission) : calendrier tronqué aux 18 premiers mois -> ~2-3 fenêtres complètes
# (9m IS + 3m OOS = 12m minimum, pas 3m -> 18m couvre 1 fenêtre garantie + une 2e partielle selon
# le calendrier réel), assez pour prouver que le pipeline tourne de bout en bout rapidement.
SMOKE_CALENDAR_MONTHS = 18


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    p.add_argument("--output-dir", default=None, help="défaut : results/protective_vol_gate_6majors[/smoke]")
    p.add_argument(
        "--smoke",
        action="store_true",
        default=False,
        help=(
            f"Calendrier tronqué aux {SMOKE_CALENDAR_MONTHS} premiers mois (~2-3 fenêtres) -- "
            "sortie dans un sous-dossier smoke/ qui n'écrase JAMAIS le results.json nominal, "
            "sauf si --output-dir est explicitement fourni."
        ),
    )
    return p


# ------------------------------------------------------------------------------------------
# Données
# ------------------------------------------------------------------------------------------


def load_all_data(data_dir: str, smoke: bool) -> dict:
    print(f"[data] chargement de {len(UNIVERSE)} majors crypto horaires depuis {data_dir} ...")
    raw = bt_data.load_universe_raw(data_dir, UNIVERSE)
    calendar_full = bt_data.build_calendar(raw)
    if smoke:
        smoke_end = calendar_full[0] + pd.DateOffset(months=SMOKE_CALENDAR_MONTHS)
        calendar = calendar_full[calendar_full <= smoke_end]
        print(
            f"[data][SMOKE] calendrier tronqué : {calendar[0]} -> {calendar[-1]} "
            f"({len(calendar)} heures, {SMOKE_CALENDAR_MONTHS} premiers mois -- calendrier "
            f"complet : {len(calendar_full)} heures)"
        )
    else:
        calendar = calendar_full
        print(f"[data] calendrier commun (union horaire) : {calendar[0]} -> {calendar[-1]}, {len(calendar)} heures")
    real_gaps = bt_data.count_real_gaps(raw, calendar)
    print(f"[data] trous réels rencontrés (NaN avant ffill, par symbole) : {real_gaps}")
    aligned = bt_data.align_universe_to_calendar(raw, calendar)
    opens = bt_data.opens_panel(aligned, UNIVERSE)
    closes = bt_data.closes_panel(aligned, UNIVERSE)
    n_nan_after_align = int(closes.isna().sum().sum())
    print(f"[data] NaN restants après alignement (closes) : {n_nan_after_align}")
    return {
        "calendar": calendar,
        "opens": opens,
        "closes": closes,
        "real_gaps": real_gaps,
        "n_nan_after_align": n_nan_after_align,
    }


# ------------------------------------------------------------------------------------------
# Cache des matrices de poids (et de l'état complet g/state, pour les analyses d'honnêteté) par
# combinaison de paramètres -- chaque combo calculé UNE SEULE FOIS sur le calendrier complet,
# réutilisé pour toutes les fenêtres IS/OOS (le signal ne dépend pas des bornes de fenêtre).
# ------------------------------------------------------------------------------------------


class CandidateCache:
    """Cache pour la candidate `protective_vol_gate` : poids ET état complet (`state`/`g`),
    ce dernier nécessaire à l'analyse d'honnêteté des épisodes de protection (SPEC.md §7.1)."""

    def __init__(self, closes: pd.DataFrame):
        self._closes = closes
        self._weights_cache: Dict[Tuple, pd.DataFrame] = {}
        self._state_cache: Dict[Tuple, pd.DataFrame] = {}

    @staticmethod
    def _key(params: dict) -> Tuple:
        return tuple(sorted(params.items()))

    def _params_obj(self, params: dict) -> pvg.ProtectiveVolGateParams:
        return pvg.ProtectiveVolGateParams(**params)

    def get_weights(self, params: dict) -> pd.DataFrame:
        key = self._key(params)
        if key not in self._weights_cache:
            self._weights_cache[key] = pvg.generate_weight_decisions(self._closes, self._params_obj(params))
        return self._weights_cache[key]

    def get_state(self, params: dict) -> pd.DataFrame:
        key = self._key(params)
        if key not in self._state_cache:
            self._state_cache[key] = pvg.generate_state_and_g(self._closes, self._params_obj(params))
        return self._state_cache[key]

    def provider(self):
        return lambda params: self.get_weights(params)


class ControlCache:
    """Contrôle apparié (SPEC.md §5) : poids CONSTANTS 1/6, g≡1, aucune gate -- une seule
    combinaison (grille `[{}]`), indépendante de `params` (toujours la même matrice)."""

    def __init__(self, calendar: pd.DatetimeIndex):
        self._weights = pd.DataFrame({sym: 1.0 / len(UNIVERSE) for sym in UNIVERSE}, index=calendar)

    def get_weights(self, params: dict) -> pd.DataFrame:
        return self._weights

    def provider(self):
        return lambda params: self._weights


CONTROL_PARAM_GRID = [{}]  # 1 seule combinaison, zéro degré de liberté (SPEC.md §5)


# ------------------------------------------------------------------------------------------
# Métriques : periods_per_year=8760 explicite PARTOUT (SPEC.md §2), jamais les défauts 252 de
# `backtest/engine.py::summarize_segment` -- moteur non modifiable, on réimplémente le même
# bloc de métriques avec le bon `periods_per_year` (même convention que `run_vol_breakout.py`).
# ------------------------------------------------------------------------------------------


def summarize_hourly(seg, periods_per_year: float = PERIODS_PER_YEAR_HOURLY) -> dict:
    returns = seg.returns
    pnls = [e["pnl"] for e in seg.realized_events]
    equity = (1.0 + returns).cumprod()
    equity_with_base = pd.concat([pd.Series([1.0]), equity])
    return {
        "sharpe": bt_metrics.sharpe_ratio(returns, periods_per_year=periods_per_year),
        "sortino": bt_metrics.sortino_ratio(returns, periods_per_year=periods_per_year),
        "profit_factor": bt_metrics.profit_factor(pnls),
        "max_drawdown": bt_metrics.max_drawdown(equity_with_base),
        "cagr": bt_metrics.cagr(equity_with_base, periods_per_year=periods_per_year),
        "average_exposure": bt_metrics.average_exposure(seg.gross_exposure),
        "n_trades_closed": len(seg.trades_closed),
        "n_periods": len(returns),
    }


# ------------------------------------------------------------------------------------------
# Walk-forward générique (candidate ET contrôle partagent EXACTEMENT ce pipeline, SPEC.md §5) :
# sélection IS (grille -- 4 combos candidate, 1 combo contrôle) + simulation OOS avec portage
# inter-fenêtres ACTIF (SPEC.md §2, "standard depuis la session #6").
# ------------------------------------------------------------------------------------------


def run_walkforward(
    windows, calendar, opens, closes, weights_provider, param_grid: List[dict], cost_bps: float,
    sim_kwargs: dict, carry_across_windows: bool = True,
):
    """Walk-forward nominal (sélection IS + simulation OOS), portage inter-fenêtres ACTIF par
    défaut pour cette candidate (SPEC.md §2 -- contrairement à `run_funding_carry.py` où il
    restait un flag opt-in informatif hérité d'avant que ce standard ne soit adopté, session
    #6). Enchaîne `carry_in(fenêtre k+1) = carry_out(fenêtre OOS k)` SI la frontière est
    calendaire-adjacente (`carry_out.last_idx + 1 == w.oos_start_idx`), sinon démarre à plat
    (`carry_in=None`) et JOURNALISE l'évènement (CARRY-EXTENSION-SPEC.md §1.3 : jamais
    silencieux) -- `simulate_segment` re-vérifie ensuite lui-même cette contiguïté
    défensivement (ne suppose jamais un invariant fourni par l'appelant)."""
    per_window = []
    segments = []
    carry_boundary_events: List[dict] = []
    t_start = time.time()
    prev_carry_out = None
    for w in windows:
        t0 = time.time()
        # `simulate_segment` exige `start_idx >= 1` -- inévitable pour la toute PREMIÈRE fenêtre
        # (son IS commence exactement à la première heure du calendrier), même déviation
        # documentée que `backtest/run_vol_breakout.py`/`backtest/run_funding_carry.py`
        # (appliquée IDENTIQUEMENT à toutes les combinaisons de la grille, sans biais).
        is_start_idx_safe = max(1, w.is_start_idx)
        sel = engine.select_params_via_is(
            weights_provider,
            calendar,
            opens,
            closes,
            cost_bps,
            is_start_idx_safe,
            w.is_end_idx,
            param_grid=param_grid,
            sim_kwargs=sim_kwargs,
        )
        chosen = sel.chosen_params
        weights_chosen = weights_provider(chosen)

        carry_in_this_window = None
        if carry_across_windows and prev_carry_out is not None:
            if int(prev_carry_out.last_idx) + 1 == w.oos_start_idx:
                carry_in_this_window = prev_carry_out
            else:
                carry_boundary_events.append(
                    {
                        "window_index": w.index,
                        "prev_carry_last_idx": int(prev_carry_out.last_idx),
                        "oos_start_idx": w.oos_start_idx,
                        "reason": (
                            "frontière non calendaire-adjacente (prev_carry.last_idx + 1 != "
                            "oos_start_idx) -- démarrage à plat pour cette fenêtre "
                            "(CARRY-EXTENSION-SPEC.md §1.3)."
                        ),
                    }
                )
                print(
                    f"[carry] fenêtre {w.index} : frontière NON contiguë -- démarrage à plat, "
                    "évènement journalisé.",
                    flush=True,
                )

        seg = engine.simulate_segment(
            calendar, weights_chosen, opens, closes, w.oos_start_idx, w.oos_end_idx, cost_bps,
            carry_in=carry_in_this_window, **sim_kwargs,
        )
        if carry_across_windows:
            prev_carry_out = seg.carry_out
        segments.append(seg)
        summary = summarize_hourly(seg)
        summary.update(
            {
                "window_index": w.index,
                "is_start": str(w.is_start),
                "is_end": str(w.is_end),
                "oos_start": str(w.oos_start),
                "oos_end": str(w.oos_end),
                "chosen_params": chosen,
                "is_sharpe_chosen": sel.is_sharpe,
                "is_candidates": sel.all_candidates,
                "carry_in_used": carry_in_this_window is not None,
            }
        )
        per_window.append(summary)
        print(
            f"[walk-forward] fenêtre {w.index}/{len(windows) - 1} ({w.oos_start.date()} -> "
            f"{w.oos_end.date()}) : chosen={chosen} is_sharpe={sel.is_sharpe:.4f} "
            f"n_trades_oos={summary['n_trades_closed']} ({time.time() - t0:.1f}s, "
            f"total {time.time() - t_start:.1f}s)",
            flush=True,
        )
    concatenated = engine.concatenate_segments(segments)
    concat_summary = summarize_hourly(concatenated)
    print(f"[walk-forward] terminé en {time.time() - t_start:.1f}s", flush=True)
    return {
        "per_window": per_window,
        "concatenated": concat_summary,
        "_segments": segments,
        "_concatenated_result": concatenated,
        "carry_boundary_events": carry_boundary_events,
    }


def rerun_oos_with_chosen_params_at_cost(
    windows, per_window_chosen: List[dict], calendar, opens, closes, weights_provider,
    cost_bps: float, sim_kwargs: dict,
):
    """Stress de coûts (SPEC.md §2/§6) : re-simule le segment OOS de CHAQUE fenêtre avec les
    paramètres DÉJÀ CHOISIS par la sélection IS nominale (jamais une nouvelle sélection), seul
    `cost_bps` change. Portage inter-fenêtres NON reproduit ici (informatif uniquement, cf.
    `cost_stress_test` -- le stress de coûts porte sur le PROFIT FACTOR à plat, comme
    `run_vol_breakout.py`/`run_funding_carry.py` ; reproduire le portage sous 3 coûts
    différents ajouterait une complexité sans valeur décisionnelle supplémentaire, le portage
    n'affecte pas le TURNOVER de la stratégie elle-même)."""
    segments = []
    for w, chosen in zip(windows, per_window_chosen):
        weights_chosen = weights_provider(chosen)
        seg = engine.simulate_segment(
            calendar, weights_chosen, opens, closes, w.oos_start_idx, w.oos_end_idx, cost_bps, **sim_kwargs
        )
        segments.append(seg)
    return engine.concatenate_segments(segments)


# ------------------------------------------------------------------------------------------
# Benchmark : buy & hold équipondéré des 6 majors, mêmes fenêtres OOS alignées, SANS coûts ni
# overlay (SPEC.md §6 -- même convention que `run_vol_breakout.py`/`run_funding_carry.py` :
# "sans overlay" désactive les DEUX composantes de la surcouche de risque).
# ------------------------------------------------------------------------------------------


def build_benchmark_weights(calendar: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame({sym: 1.0 / len(UNIVERSE) for sym in UNIVERSE}, index=calendar)


def run_flat_weights_over_windows(windows, calendar, opens, closes, weights_decided: pd.DataFrame):
    """Simule `weights_decided` (déjà construits sur tout le calendrier) sur chaque fenêtre OOS,
    SANS coûts ni overlay, capital remis à plat à chaque fenêtre (pas de portage) -- utilisé
    pour le benchmark B&H ET pour le proxy quasi-passif SMA200 (SPEC.md §6/§7.3), tous deux des
    repères STATIQUES/documentés, jamais des candidates soumises au pipeline de risque complet."""
    segments = []
    per_window = []
    for w in windows:
        seg = engine.simulate_segment(
            calendar, weights_decided, opens, closes, w.oos_start_idx, w.oos_end_idx,
            cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        )
        segments.append(seg)
        summary = summarize_hourly(seg)
        summary.update({"window_index": w.index, "oos_start": str(w.oos_start), "oos_end": str(w.oos_end)})
        per_window.append(summary)
    concatenated = engine.concatenate_segments(segments)
    concat_summary = summarize_hourly(concatenated)
    return {
        "per_window": per_window,
        "concatenated": concat_summary,
        "_segments": segments,
        "_concatenated_result": concatenated,
    }


# ------------------------------------------------------------------------------------------
# Proxy quasi-passif (SPEC.md §7.3) : "panier long/flat SMA200 équipondéré". `run_vol_breakout.py`
# construit un proxy DIFFÉRENT (vol-targeté, répond à la question de la redondance avec le vol
# targeting de production) -- CETTE SPEC demande explicitement le proxy de repli documenté :
# "panier investi quand close>SMA(200 jours=4800h) par symbole". Choix retenu ICI (documenté,
# tranché) : poids DÉCIDÉS `1/6` par symbole quand `close(t) > SMA(close, 4800h)(t)` pour CE
# symbole, `0.0` sinon -- panier long/flat PAR SYMBOLE (pas un market-timing global sur la
# moyenne du panier), même filtre de régime que `backtest/strategies/vol_breakout.py
# ._regime_filter` (réutilisé conceptuellement, pas importé -- ce module ne doit dépendre
# d'aucune autre candidate). Simulé EXACTEMENT comme le benchmark B&H (mêmes fenêtres OOS
# alignées, sans coûts ni overlay, SPEC.md §6) : c'est un repère quasi-passif documenté, pas une
# candidate évaluée sous le pipeline de risque complet.
# ------------------------------------------------------------------------------------------


def build_sma_trend_proxy_weights(closes: pd.DataFrame, sma_hours: int = PROXY_SMA_HOURS) -> pd.DataFrame:
    sma = closes.rolling(sma_hours, min_periods=sma_hours).mean()
    invested = closes > sma  # NaN (warm-up) -> comparaison False -> flat, jamais une licence à entrer
    weights = invested.astype(float) * (1.0 / len(UNIVERSE))
    return weights.fillna(0.0)


# ------------------------------------------------------------------------------------------
# Analyses d'honnêteté obligatoires (SPEC.md §7)
# ------------------------------------------------------------------------------------------


def analyze_protection_episodes(
    windows, per_window_chosen: List[dict], calendar, closes, cache: CandidateCache
) -> dict:
    """§7.1 -- épisodes de protection OOS DISTINCTS (de la coupure exécutée au redéploiement
    complet), pour chacun le rendement B&H du panier SUR L'ÉPISODE (négatif = vraie protection,
    positif = faux signal/coût d'opportunité), tableau évités-vs-ratés + somme nette + nombre
    d'épisodes.

    Un "épisode" = run contigu de `state != STATE_INVESTED` (donc `g < 1.0`) sur la matrice
    d'état complète calculée avec les paramètres CHOISIS par la sélection IS de CHAQUE fenêtre,
    restreint à la fenêtre OOS de cette même fenêtre (jamais stitché entre deux fenêtres
    walk-forward consécutives, même approximation documentée que `run_funding_carry.py
    .analyze_activation_honesty` : un épisode qui traverserait une frontière de fenêtre est
    compté comme deux épisodes distincts plutôt qu'un seul continu -- ne SOUS-compte jamais le
    nombre d'épisodes réel, ne masque jamais un épisode en le fusionnant artificiellement avec
    un autre). Impossible qu'un épisode soit scindé PAR CONSTRUCTION de la machine à états
    elle-même à l'intérieur d'une même fenêtre (la transition REDÉPLOIEMENT -> INVESTIE exige
    `percentile_t < p_in` au moment même où `g` atteint 1.0 -- aucune re-coupure ne peut avoir
    lieu au même tick, cf. `backtest/strategies/protective_vol_gate.py::run_state_machine`),
    donc deux runs de `state != INVESTED` SÉPARÉS PAR AU MOINS UNE HEURE `INVESTIE` sont
    TOUJOURS deux observations indépendantes au sens de la machine (le risque "peu d'épisodes
    indépendants" du backlog concerne surtout un éventuel enchaînement immédiat coupure ->
    redéploiement -> re-coupure, déjà géré nativement par la machine à états)."""
    r_full = pvg.basket_return(closes)  # rendement B&H du panier, causal, sans coûts ni overlay
    episodes: List[dict] = []
    for w, chosen in zip(windows, per_window_chosen):
        sg = cache.get_state(chosen)
        state_slice = sg["state"].iloc[w.oos_start_idx : w.oos_end_idx + 1]
        r_slice = r_full.iloc[w.oos_start_idx : w.oos_end_idx + 1]
        active = (state_slice.to_numpy() != pvg.STATE_INVESTED)
        idx = state_slice.index
        n = len(active)
        t = 0
        while t < n:
            if active[t]:
                start = t
                while t < n and active[t]:
                    t += 1
                end = t - 1  # dernier tick non-investi (inclus)
                span_returns = r_slice.iloc[start : end + 1]
                bh_return_over_episode = float((1.0 + span_returns.fillna(0.0)).prod() - 1.0)
                episodes.append(
                    {
                        "window_index": w.index,
                        "start": str(idx[start]),
                        "end": str(idx[end]),
                        "n_hours": end - start + 1,
                        "basket_bh_return_over_episode": bh_return_over_episode,
                        "avoided": bool(bh_return_over_episode < 0.0),
                    }
                )
            else:
                t += 1
    n_episodes = len(episodes)
    n_avoided = sum(1 for e in episodes if e["avoided"])
    n_missed = n_episodes - n_avoided
    net_sum = float(sum(e["basket_bh_return_over_episode"] for e in episodes)) if episodes else 0.0
    return {
        "episodes": episodes,
        "n_episodes_distinct": n_episodes,
        "n_episodes_avoided_negative_bh": n_avoided,
        "n_episodes_missed_positive_bh": n_missed,
        "sum_net_basket_bh_return_over_all_episodes": net_sum,
        "note": (
            "Un épisode = run contigu de state != INVESTED (g<1.0), restreint à chaque fenêtre "
            "OOS avec les params choisis par SA PROPRE sélection IS -- jamais stitché entre "
            "deux fenêtres (approximation documentée, ne sous-compte jamais). "
            "basket_bh_return_over_episode = ce que la gate a évité (négatif) ou raté "
            "(positif) EN TERMES DE RENDEMENT B&H DU PANIER (sans coûts ni overlay) sur "
            "exactement les heures où g<1.0. sum_net < 0 => la gate a net capturé plus de "
            "crashs évités que de coût d'opportunité sur l'OOS complet (SPEC.md §7.1)."
        ),
    }


def subperiod_sharpe(returns: pd.Series, split_date: pd.Timestamp) -> dict:
    before = returns[returns.index < split_date]
    after = returns[returns.index >= split_date]
    return {
        "period_before": f"< {split_date.date()}",
        "sharpe_before": bt_metrics.sharpe_ratio(before, periods_per_year=PERIODS_PER_YEAR_HOURLY),
        "n_periods_before": int(len(before.dropna())),
        "period_after": f">= {split_date.date()}",
        "sharpe_after": bt_metrics.sharpe_ratio(after, periods_per_year=PERIODS_PER_YEAR_HOURLY),
        "n_periods_after": int(len(after.dropna())),
    }


def paired_comparison(candidate_returns: pd.Series, control_returns: pd.Series) -> dict:
    """§7.4 -- comparaison appariée candidate vs contrôle sur les MÊMES fenêtres (index OOS
    identique par construction -- `.align(join='inner')` reste une sécurité défensive, pas une
    opération triviale). Critère de décision (SPEC.md §8) : DOMINATION = Sharpe ET Sortino ET
    MaxDD TOUS moins bons pour la candidate que pour le contrôle sur cette série appariée."""
    a, c = candidate_returns.align(control_returns, join="inner")
    a = a.dropna()
    c = c.reindex(a.index)
    equity_a = pd.concat([pd.Series([1.0]), (1.0 + a).cumprod()])
    equity_c = pd.concat([pd.Series([1.0]), (1.0 + c).cumprod()])
    sharpe_a = bt_metrics.sharpe_ratio(a, periods_per_year=PERIODS_PER_YEAR_HOURLY)
    sharpe_c = bt_metrics.sharpe_ratio(c, periods_per_year=PERIODS_PER_YEAR_HOURLY)
    sortino_a = bt_metrics.sortino_ratio(a, periods_per_year=PERIODS_PER_YEAR_HOURLY)
    sortino_c = bt_metrics.sortino_ratio(c, periods_per_year=PERIODS_PER_YEAR_HOURLY)
    maxdd_a = bt_metrics.max_drawdown(equity_a)
    maxdd_c = bt_metrics.max_drawdown(equity_c)
    ir = bt_metrics.information_ratio(a, c, periods_per_year=PERIODS_PER_YEAR_HOURLY)
    dominated = bool(
        not math.isnan(sharpe_a) and not math.isnan(sharpe_c) and not math.isnan(sortino_a)
        and not math.isnan(sortino_c) and not math.isnan(maxdd_a) and not math.isnan(maxdd_c)
        and sharpe_a < sharpe_c and sortino_a < sortino_c and maxdd_a > maxdd_c
    )
    return {
        "n_periods_aligned": int(len(a)),
        "candidate": {"sharpe": sharpe_a, "sortino": sortino_a, "max_drawdown": maxdd_a},
        "control": {"sharpe": sharpe_c, "sortino": sortino_c, "max_drawdown": maxdd_c},
        "information_ratio_candidate_vs_control": ir,
        "candidate_dominated_by_control": dominated,
        "note": (
            "Dominée (SPEC.md §5/§8) = Sharpe ET Sortino ET MaxDD TOUS moins bons pour la "
            "candidate que pour le contrôle sur cette série OOS appariée -- critère de "
            "décision explicite : une candidate dominée n'est PAS incubée quels que soient "
            "ses seuils Porte 1."
        ),
    }


def most_frequently_selected_is_combo(per_window_chosen: List[dict]) -> dict:
    """SPEC.md §8 : "params gelés = la combinaison la plus souvent sélectionnée en IS (égalité
    tranchée par p_in le plus bas puis V le plus court -- règle déterministe fixée ici)"."""
    counts = Counter(tuple(sorted(c.items())) for c in per_window_chosen)
    max_count = max(counts.values())
    tied = [dict(k) for k, v in counts.items() if v == max_count]
    tied.sort(key=lambda c: (c["p_in"], c["vol_window_hours"]))
    chosen = tied[0]
    return {
        "chosen": chosen,
        "n_windows_selected": max_count,
        "n_windows_total": len(per_window_chosen),
        "all_counts": [{"params": dict(k), "n_windows": v} for k, v in counts.items()],
        "tie_break_rule": "égalité tranchée par p_in le plus bas puis V (vol_window_hours) le plus court (SPEC.md §8).",
    }


# ------------------------------------------------------------------------------------------
# main
# ------------------------------------------------------------------------------------------


def main():
    args = build_parser().parse_args()
    if args.output_dir is not None:
        output_dir = Path(args.output_dir)
    elif args.smoke:
        output_dir = SMOKE_OUTPUT_DIR
    else:
        output_dir = OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    t_global = time.time()
    d = load_all_data(args.data_dir, smoke=args.smoke)
    calendar = d["calendar"]
    opens = d["opens"]
    closes = d["closes"]

    windows = engine.generate_walk_forward_windows(
        calendar, is_months=IS_MONTHS, oos_months=OOS_MONTHS, step_months=STEP_MONTHS
    )
    n_windows = len(windows)
    print(
        f"[walk-forward] {n_windows} fenêtres générées ({IS_MONTHS}m IS / {OOS_MONTHS}m OOS / "
        f"pas {STEP_MONTHS}m){' [SMOKE]' if args.smoke else ''}",
        flush=True,
    )
    if not args.smoke and not (EXPECTED_N_WINDOWS_RANGE[0] <= n_windows <= EXPECTED_N_WINDOWS_RANGE[1]):
        print(
            f"[ALERTE] nombre de fenêtres ({n_windows}) hors de la plage attendue SPEC.md "
            f"{EXPECTED_N_WINDOWS_RANGE} -- K_total recalculé dynamiquement ci-dessous, "
            "signalé dans le rapport.",
            flush=True,
        )

    sim_kwargs = dict(
        vol_ewma_halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
        vol_periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR,
    )

    candidate_cache = CandidateCache(closes)
    control_cache = ControlCache(calendar)

    print("[run] walk-forward CANDIDATE protective_vol_gate (25 bps/côté nominal, carry ON) ...", flush=True)
    candidate_result = run_walkforward(
        windows, calendar, opens, closes, candidate_cache.provider(), pvg.PARAM_GRID,
        COST_BPS_NOMINAL, sim_kwargs, carry_across_windows=True,
    )
    per_window_chosen = [pw["chosen_params"] for pw in candidate_result["per_window"]]

    print("[run] walk-forward CONTRÔLE apparié (poids constants 1/6, même pipeline, carry ON) ...", flush=True)
    control_result = run_walkforward(
        windows, calendar, opens, closes, control_cache.provider(), CONTROL_PARAM_GRID,
        COST_BPS_NOMINAL, sim_kwargs, carry_across_windows=True,
    )

    print("[run] benchmark buy & hold équipondéré (sans coûts ni overlay) ...", flush=True)
    benchmark_weights = build_benchmark_weights(calendar)
    benchmark_result = run_flat_weights_over_windows(windows, calendar, opens, closes, benchmark_weights)

    print("[run] proxy quasi-passif SMA200 long/flat équipondéré (sans coûts ni overlay) ...", flush=True)
    proxy_weights = build_sma_trend_proxy_weights(closes)
    proxy_result = run_flat_weights_over_windows(windows, calendar, opens, closes, proxy_weights)

    print("[stress] re-simulation OOS CANDIDATE à 75 et 125 bps/côté (mêmes params choisis) ...", flush=True)
    concat_75 = rerun_oos_with_chosen_params_at_cost(
        windows, per_window_chosen, calendar, opens, closes, candidate_cache.provider(),
        COST_BPS_STRESS_3X, sim_kwargs,
    )
    concat_125 = rerun_oos_with_chosen_params_at_cost(
        windows, per_window_chosen, calendar, opens, closes, candidate_cache.provider(),
        COST_BPS_STRESS_5X, sim_kwargs,
    )
    pf_75 = bt_metrics.profit_factor([e["pnl"] for e in concat_75.realized_events])
    pf_125 = bt_metrics.profit_factor([e["pnl"] for e in concat_125.realized_events])
    sharpe_75 = bt_metrics.sharpe_ratio(concat_75.returns, periods_per_year=PERIODS_PER_YEAR_HOURLY)
    sharpe_125 = bt_metrics.sharpe_ratio(concat_125.returns, periods_per_year=PERIODS_PER_YEAR_HOURLY)

    # --- DSR (SPEC.md §6) -------------------------------------------------------------------
    candidate_oos_returns = candidate_result["_concatenated_result"].returns
    control_oos_returns = control_result["_concatenated_result"].returns
    benchmark_oos_returns = benchmark_result["_concatenated_result"].returns
    proxy_oos_returns = proxy_result["_concatenated_result"].returns

    k_total = K_REGISTRY_ROWS + n_windows * N_GRID_COMBOS_CANDIDATE + n_windows * N_GRID_COMBOS_CONTROL
    dsr_result = bt_metrics.deflated_sharpe_ratio(candidate_oos_returns, trials_k=k_total)

    # --- Analyses d'honnêteté (SPEC.md §7) ---------------------------------------------------
    print("[analyses] épisodes de protection OOS distincts + B&H évité/raté (§7.1) ...", flush=True)
    protection_episodes = analyze_protection_episodes(windows, per_window_chosen, calendar, closes, candidate_cache)

    print("[analyses] sous-périodes 2022-2023 vs 2024-2026 (§7.2) ...", flush=True)
    subperiods = subperiod_sharpe(candidate_oos_returns, SUBPERIOD_SPLIT_DATE)

    print("[analyses] corrélations OOS candidate-contrôle et candidate-proxy quasi-passif (§7.3) ...", flush=True)
    aligned_cand_control, aligned_control = candidate_oos_returns.align(control_oos_returns, join="inner")
    correlation_vs_control = float(aligned_cand_control.corr(aligned_control))
    aligned_cand_proxy, aligned_proxy = candidate_oos_returns.align(proxy_oos_returns, join="inner")
    correlation_vs_quasi_passive = float(aligned_cand_proxy.corr(aligned_proxy))

    print("[analyses] comparaison appariée candidate vs contrôle (§7.4, critère de décision §8) ...", flush=True)
    paired = paired_comparison(candidate_oos_returns, control_oos_returns)

    # --- Seuils PROMOTION-RULES §1.2 / SPEC.md §6, un par un --------------------------------
    cand_concat = candidate_result["concatenated"]
    control_concat = control_result["concatenated"]
    bench_concat = benchmark_result["concatenated"]
    proxy_concat = proxy_result["concatenated"]
    maxdd_ratio = (
        cand_concat["max_drawdown"] / bench_concat["max_drawdown"]
        if bench_concat["max_drawdown"] not in (0, None) and not math.isnan(bench_concat["max_drawdown"])
        else float("nan")
    )

    verdicts = {
        "sharpe_oos": {
            "value": cand_concat["sharpe"],
            "threshold": PROMOTION_RULES_THRESHOLDS["sharpe_oos_min"],
            "rule": ">= seuil",
            "pass": bool(not math.isnan(cand_concat["sharpe"]) and cand_concat["sharpe"] >= PROMOTION_RULES_THRESHOLDS["sharpe_oos_min"]),
        },
        "profit_factor_oos": {
            "value": cand_concat["profit_factor"],
            "threshold": PROMOTION_RULES_THRESHOLDS["profit_factor_oos_min"],
            "rule": "> seuil",
            "pass": bool(not math.isnan(cand_concat["profit_factor"]) and cand_concat["profit_factor"] > PROMOTION_RULES_THRESHOLDS["profit_factor_oos_min"]),
        },
        "n_trades_oos_closed": {
            "value": cand_concat["n_trades_closed"],
            "threshold": PROMOTION_RULES_THRESHOLDS["n_trades_oos_min"],
            "rule": ">= seuil",
            "pass": bool(cand_concat["n_trades_closed"] >= PROMOTION_RULES_THRESHOLDS["n_trades_oos_min"]),
            "honesty_note": {
                "n_trades_closed_oos": cand_concat["n_trades_closed"],
                "n_protection_episodes_distincts": protection_episodes["n_episodes_distinct"],
            },
        },
        "maxdd_relative_to_benchmark": {
            "value": maxdd_ratio,
            "threshold": PROMOTION_RULES_THRESHOLDS["maxdd_relative_to_benchmark_max"],
            "rule": "<= seuil (maxdd_candidate / maxdd_benchmark_OOS_aligné)",
            "pass": bool(not math.isnan(maxdd_ratio) and maxdd_ratio <= PROMOTION_RULES_THRESHOLDS["maxdd_relative_to_benchmark_max"]),
        },
        "dsr": {
            "value": dsr_result.dsr,
            "threshold": PROMOTION_RULES_THRESHOLDS["dsr_min"],
            "rule": ">= seuil",
            "k_total": k_total,
            "pass": bool(not math.isnan(dsr_result.dsr) and dsr_result.dsr >= PROMOTION_RULES_THRESHOLDS["dsr_min"]),
        },
    }
    all_pass = all(v["pass"] for v in verdicts.values())
    dominated_by_control = paired["candidate_dominated_by_control"]

    # --- Sémantique des issues (SPEC.md §8, FIGÉE avant exécution) --------------------------
    frozen_params_info = most_frequently_selected_is_combo(per_window_chosen)
    if all_pass and not dominated_by_control:
        gate1_verdict = "incubation_proposee_sous_reserve_audit_adversarial"
    elif dominated_by_control:
        gate1_verdict = "ecartee_dominee_par_controle"
    elif cand_concat["sharpe"] is not None and not math.isnan(cand_concat["sharpe"]) and cand_concat["sharpe"] < 0:
        gate1_verdict = "rejetee_sharpe_negatif"
    elif not math.isnan(cand_concat["profit_factor"]) and cand_concat["profit_factor"] < 1.0:
        gate1_verdict = "rejetee_profit_factor_inferieur_a_1"
    else:
        gate1_verdict = "ecartee_seuil_manque"

    results = {
        "meta": {
            "candidate_id": "protective_vol_gate_6majors",
            "backlog_ref": "backtest/results/protective_vol_gate_6majors/SPEC.md (P1#6, pré-enregistrée 2026-09-14, session hebdo #7)",
            "engine": "backtest/engine.py (docs/PROMOTION-RULES.md §1.1), carry_across_windows=True (CARRY-EXTENSION-SPEC.md, standard depuis session #6)",
            "data_dir": str(args.data_dir),
            "smoke_run": bool(args.smoke),
            "univers": UNIVERSE,
            "calendar_start": str(calendar[0]),
            "calendar_end": str(calendar[-1]),
            "n_calendar_hours": len(calendar),
            "real_data_gaps_by_symbol": d["real_gaps"],
            "n_nan_after_align": d["n_nan_after_align"],
            "n_windows": n_windows,
            "walkforward": f"{IS_MONTHS}m IS / {OOS_MONTHS}m OOS / pas {STEP_MONTHS}m",
            "param_grid_candidate": pvg.PARAM_GRID,
            "param_grid_control": CONTROL_PARAM_GRID,
            "cost_bps_nominal_per_side": COST_BPS_NOMINAL,
            "sim_kwargs_hourly": sim_kwargs,
            "periods_per_year_metrics": PERIODS_PER_YEAR_HOURLY,
            "k_total": k_total,
            "k_total_detail": {
                "registry_rows": K_REGISTRY_ROWS,
                "n_windows": n_windows,
                "n_grid_combos_candidate": N_GRID_COMBOS_CANDIDATE,
                "n_grid_combos_control": N_GRID_COMBOS_CONTROL,
                "formula": "K_total = registry_rows + n_windows*n_grid_combos_candidate + n_windows*n_grid_combos_control (SPEC.md §6)",
            },
            "runtime_seconds": None,  # renseigné à la fin
        },
        "candidate_protective_vol_gate": {
            "cost_bps": COST_BPS_NOMINAL,
            "carry_across_windows": True,
            "per_window": candidate_result["per_window"],
            "concatenated": cand_concat,
            "carry_boundary_events": candidate_result["carry_boundary_events"],
        },
        "control_constant_weights_no_gate": {
            "cost_bps": COST_BPS_NOMINAL,
            "carry_across_windows": True,
            "description": "Poids constants 1/6 par symbole, g≡1, aucune gate -- même moteur/overlay/coûts/fenêtres que la candidate (SPEC.md §5).",
            "per_window": control_result["per_window"],
            "concatenated": control_concat,
            "carry_boundary_events": control_result["carry_boundary_events"],
        },
        "benchmark_equal_weight_buy_hold": {
            "cost_bps": 0.0,
            "overlay": "désactivé (apply_vol_targeting=False, no_trade_band=0.0)",
            "per_window": benchmark_result["per_window"],
            "concatenated": bench_concat,
        },
        "quasi_passive_proxy_sma200_long_flat": {
            "cost_bps": 0.0,
            "overlay": "désactivé (apply_vol_targeting=False, no_trade_band=0.0)",
            "definition": (
                f"Poids 1/6 par symbole quand close(t) > SMA(close, {PROXY_SMA_HOURS}h)(t) pour CE "
                "symbole, 0.0 sinon (panier long/flat PAR SYMBOLE, pas un market-timing global sur "
                "la moyenne du panier) -- SPEC.md §7.3, distinct du proxy vol-targeté de "
                "run_vol_breakout.py (qui répond à la question de la redondance avec le vol "
                "targeting de production, pas avec une brique de trend-following)."
            ),
            "per_window": proxy_result["per_window"],
            "concatenated": proxy_concat,
        },
        "dsr_candidate": dsr_result.to_dict(),
        "cost_stress_test": {
            "profit_factor_at_25bps_nominal": cand_concat["profit_factor"],
            "profit_factor_at_75bps_3x": pf_75,
            "profit_factor_at_125bps_5x": pf_125,
            "sharpe_at_25bps_nominal": cand_concat["sharpe"],
            "sharpe_at_75bps_3x": sharpe_75,
            "sharpe_at_125bps_5x": sharpe_125,
        },
        "honesty_analyses": {
            "protection_episodes": protection_episodes,
            "subperiods_2022_2023_vs_2024_2026": subperiods,
            "correlation_vs_control": {
                "value": correlation_vs_control,
                "note": "Corrélation des rendements horaires OOS candidate vs contrôle apparié (mêmes fenêtres, index intersecté).",
            },
            "correlation_vs_quasi_passive_proxy": {
                "value": correlation_vs_quasi_passive,
                "note": (
                    "Corrélation des rendements horaires OOS candidate vs proxy quasi-passif "
                    "SMA200 long/flat équipondéré (SPEC.md §7.3). > 0.7-0.8 = intérêt marginal "
                    "faible (redondance avec une brique de trend-following déjà en production)."
                ),
            },
            "paired_comparison_candidate_vs_control": paired,
            "maxdd_comparison": {
                "candidate": cand_concat["max_drawdown"],
                "control": control_concat["max_drawdown"],
                "benchmark_buy_hold": bench_concat["max_drawdown"],
                "note": (
                    "MaxDD candidate vs contrôle vs benchmark (SPEC.md §7.5) -- la promesse de "
                    "cette brique est LÀ, documentée même si la Porte 1 échoue par ailleurs."
                ),
            },
        },
        "promotion_rules_1_2_thresholds_verdict": verdicts,
        "promotion_rules_1_2_all_pass": bool(all_pass),
        "candidate_dominated_by_control": dominated_by_control,
        "most_frequently_selected_is_combo": frozen_params_info,
        "gate1_verdict_automatic": {
            "value": gate1_verdict,
            "note": (
                "Verdict AUTOMATIQUE (SPEC.md §8) basé UNIQUEMENT sur les 5 seuils §1.2 et la "
                "non-domination par le contrôle. NE remplace PAS l'audit adversarial "
                "indépendant obligatoire (isSound) -- 'incubation_proposee_sous_reserve_audit_"
                "adversarial' signifie explicitement 'sous réserve', jamais une incubation "
                "actée. isSound non exécuté par ce script (hors périmètre de la mission "
                "d'implémentation du backtest, cf. `run_funding_carry.py` même convention)."
            ),
        },
        "adversarial_audit_1_4": {
            "isSound": None,
            "note": (
                "SPEC.md exige un audit adversarial indépendant obligatoire avant toute "
                "décision de statut dans RESEARCH-REGISTRY.json (`isSound: false` = rejet quel "
                "que soit le chiffre). Non exécuté ici (hors périmètre de ce script)."
            ),
        },
    }

    results["meta"]["runtime_seconds"] = round(time.time() - t_global, 1)

    results_path = output_dir / "results.json"
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2, default=str, ensure_ascii=False)
    print(f"[out] {results_path}", flush=True)
    print(f"[done] durée totale : {results['meta']['runtime_seconds']}s", flush=True)

    return results, output_dir


if __name__ == "__main__":
    main()
