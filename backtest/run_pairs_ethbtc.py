#!/usr/bin/env python3
"""backtest/run_pairs_ethbtc.py — orchestration de la candidate `pairs_ethbtc_ratio_rotation`
(`backtest/results/pairs_ethbtc_ratio_rotation/SPEC.md`, pré-enregistrée 2026-09-21, backlog
P1#5, session hebdomadaire #8). Moteur commun `backtest/engine.py` UNIQUEMENT
(`docs/PROMOTION-RULES.md` §1.1) -- ce script ne réimplémente AUCUNE logique de simulation,
seulement l'orchestration walk-forward + les tests de stationnarité (SPEC.md §5) + les analyses
d'honnêteté demandées par la SPEC (SPEC.md §8).

Modélisé TRÈS étroitement sur `backtest/run_protective_vol_gate.py` (même pipeline : overlay
standard horaire, `carry_across_windows=True` pour candidate ET contrôle, benchmark B&H sans
coûts/overlay, `periods_per_year=8760`, coûts importés de `bot/config.py`) -- seules les parties
SPÉCIFIQUES à cette candidate diffèrent (univers 2 actifs {BTC, ETH}, grille L x theta, tests de
stationnarité SPEC.md §5, analyse d'épisodes de TILT au lieu d'épisodes de PROTECTION).

Usage :
    python3 -m backtest.run_pairs_ethbtc [--data-dir _data/crypto] [--output-dir ...]
    python3 -m backtest.run_pairs_ethbtc --smoke   # calendrier tronqué aux 18 premiers mois,
                                                     # sortie dans results/.../smoke/

AUCUNE grille hors `backtest/strategies/pairs_ratio.PARAM_GRID` n'est testée ici (import direct
de la constante, jamais une valeur ad hoc) : L in {720,2160} x theta in {1.5,2.0}, 4
combinaisons, rien d'autre (SPEC.md §4).

Portage inter-fenêtres (SPEC.md §2, "standard depuis la session #6") : `carry_across_windows=
True` ACTIF pour la candidate ET le contrôle (SPEC.md §6 : "même moteur, même overlay, mêmes
coûts, mêmes fenêtres" -- le portage fait partie de ce pipeline commun). Le benchmark B&H et le
proxy quasi-passif SMA200 n'utilisent PAS le portage (poids constants/déterministes, repères
statiques, même convention que `run_protective_vol_gate.py`).
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
from backtest.strategies import pairs_ratio as pr  # noqa: E402
from bot import config as bot_cfg  # noqa: E402

# Univers FIXE (SPEC.md §2) : BTC, ETH -- jamais `bot.config.SYMBOLS_CRYPTO` (6 majors, univers
# d'une AUTRE candidate). Vérification défensive : `pairs_ratio.UNIVERSE` est la source de vérité
# structurelle (la stratégie elle-même n'accepte que ces 2 colonnes), ce script s'aligne dessus.
UNIVERSE = list(pr.UNIVERSE)
assert UNIVERSE == ["BTC", "ETH"], "backtest.strategies.pairs_ratio.UNIVERSE a changé -- SPEC.md §2 fige {BTC, ETH}."

DEFAULT_DATA_DIR = REPO_ROOT / "_data" / "crypto"
OUTPUT_DIR = REPO_ROOT / "backtest" / "results" / "pairs_ethbtc_ratio_rotation"
SMOKE_OUTPUT_DIR = OUTPUT_DIR / "smoke"

# Coûts (SPEC.md §2) : 15 bps/côté = 10 (taker) + 5 (slippage) palier "majors", importés de
# `bot/config.py` -- JAMAIS recopiés en dur (défensif : si ces constantes changent un jour, ce
# script le répercute automatiquement plutôt que de figer silencieusement une valeur périmée).
COST_BPS_NOMINAL = float(bot_cfg.COST_TIER_FEE_TAKER_BPS["majors"] + bot_cfg.COST_TIER_SLIPPAGE_PENALTY_BPS["majors"])
assert COST_BPS_NOMINAL == 15.0, f"palier de coûts 'majors' de bot/config.py a changé (attendu 15.0, obtenu {COST_BPS_NOMINAL})"
COST_BPS_STRESS_3X = COST_BPS_NOMINAL * 3.0
COST_BPS_STRESS_5X = COST_BPS_NOMINAL * 5.0

IS_MONTHS = 9
OOS_MONTHS = 3
STEP_MONTHS = 3

PERIODS_PER_YEAR_HOURLY = 8760.0  # SPEC.md §2

# K_total = 15 (lignes RESEARCH-REGISTRY.json au 2026-09-21, SPEC.md §7) + n_fenêtres x 4
# (candidate) + n_fenêtres x 1 (contrôle) -- formule figée, contrôle compté comme essai à part
# entière (conservateur, même convention que `run_protective_vol_gate.py`).
K_REGISTRY_ROWS = 15
N_GRID_COMBOS_CANDIDATE = len(pr.PARAM_GRID)  # 4
N_GRID_COMBOS_CONTROL = 1
EXPECTED_N_WINDOWS = 15  # SPEC.md §7 : "attendu ~15"

PROMOTION_RULES_THRESHOLDS = {
    "sharpe_oos_min": 0.70,
    "profit_factor_oos_min": 1.15,
    "n_trades_oos_min": 80,
    "maxdd_relative_to_benchmark_max": 1.5,
    "dsr_min": 0.50,
}

SUBPERIOD_SPLIT_DATE = pd.Timestamp("2024-01-01")

# Proxy quasi-passif (SPEC.md §8.3) : "panier 50/50, même construction que dans
# run_protective_vol_gate.py" -- long/flat PAR SYMBOLE quand close(t) > SMA(200j=4800h)(t),
# poids 1/2 (au lieu de 1/6, univers à 2 actifs ici), 0.0 sinon.
PROXY_SMA_HOURS = 4800  # 200 jours * 24h

SMOKE_CALENDAR_MONTHS = 18


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    p.add_argument("--output-dir", default=None, help="défaut : results/pairs_ethbtc_ratio_rotation[/smoke]")
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
    print(f"[data] chargement de {len(UNIVERSE)} symboles crypto horaires depuis {data_dir} ...")
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
# Tests de stationnarité/cointégration (SPEC.md §5) -- exécutés AVANT le walk-forward, import
# statsmodels sous garde (résultat "SKIPPED_STATSMODELS_MISSING" si absent, pour que la CI sans
# statsmodels ne casse pas).
# ------------------------------------------------------------------------------------------


def run_stationarity_tests(closes: pd.DataFrame, windows, calendar: pd.DatetimeIndex) -> dict:
    try:
        from statsmodels.tsa.stattools import adfuller, coint
        import statsmodels.api as sm
    except ImportError:
        print("[stationarity] statsmodels absent -- tests SAUTÉS (SKIPPED_STATSMODELS_MISSING).")
        return {"status": "SKIPPED_STATSMODELS_MISSING"}

    x_full = pr.log_ratio(closes)  # log-ratio ETH/BTC, calculé une seule fois sur tout le calendrier
    log_eth = np.log(closes["ETH"])
    log_btc = np.log(closes["BTC"])

    pre_oos_end_idx = windows[0].oos_start_idx  # exclusif : "tout ce qui précède le début de la
    # première fenêtre OOS" (SPEC.md §5.1a) -- indices [0, pre_oos_end_idx) du calendrier complet.
    x_pre_oos = x_full.iloc[:pre_oos_end_idx].dropna()
    log_eth_pre_oos = log_eth.iloc[:pre_oos_end_idx].dropna()
    log_btc_pre_oos = log_btc.iloc[:pre_oos_end_idx].dropna()

    print(
        f"[stationarity] ADF sur log-ratio, période pré-OOS complète "
        f"({calendar[0]} -> {calendar[pre_oos_end_idx - 1]}, {len(x_pre_oos)} obs) ..."
    )
    adf_pre_oos_stat, adf_pre_oos_pvalue, adf_pre_oos_usedlag, adf_pre_oos_nobs, adf_pre_oos_crit, _ = adfuller(
        x_pre_oos.to_numpy(), autolag="AIC", regression="c"
    )
    adf_pre_oos = {
        "period": f"{calendar[0]} -> {calendar[pre_oos_end_idx - 1]}",
        "n_obs_used": int(adf_pre_oos_nobs),
        "used_lag": int(adf_pre_oos_usedlag),
        "adf_statistic": float(adf_pre_oos_stat),
        "p_value": float(adf_pre_oos_pvalue),
        "critical_values": {k: float(v) for k, v in adf_pre_oos_crit.items()},
        "stationary_at_10pct": bool(adf_pre_oos_pvalue <= 0.10),
    }

    print("[stationarity] Engle-Granger (log-prix ETH vs BTC), période pré-OOS ...")
    eg_stat, eg_pvalue, eg_crit = coint(log_eth_pre_oos.to_numpy(), log_btc_pre_oos.to_numpy())
    engle_granger_pre_oos = {
        "period": f"{calendar[0]} -> {calendar[pre_oos_end_idx - 1]}",
        "coint_statistic": float(eg_stat),
        "p_value": float(eg_pvalue),
        "critical_values_1_5_10pct": [float(v) for v in eg_crit],
        "cointegrated_at_10pct": bool(eg_pvalue <= 0.10),
    }

    print("[stationarity] demi-vie de retour à la moyenne (AR(1) OLS sur Δx vs x retardé), pré-OOS ...")
    x_lag = x_pre_oos.shift(1)
    dx = x_pre_oos.diff()
    reg_df = pd.DataFrame({"dx": dx, "x_lag": x_lag}).dropna()
    X = sm.add_constant(reg_df["x_lag"].to_numpy())
    ols_res = sm.OLS(reg_df["dx"].to_numpy(), X).fit()
    beta = float(ols_res.params[1])
    if beta < 0.0:
        half_life_hours = float(-math.log(2.0) / beta)
    else:
        half_life_hours = float("inf")  # pas de retour à la moyenne détecté (beta >= 0)
    half_life = {
        "period": f"{calendar[0]} -> {calendar[pre_oos_end_idx - 1]}",
        "n_obs": int(len(reg_df)),
        "beta_x_lag": beta,
        "beta_pvalue": float(ols_res.pvalues[1]),
        "half_life_hours": half_life_hours,
        "half_life_days": half_life_hours / 24.0 if math.isfinite(half_life_hours) else float("inf"),
        "note": (
            "half_life = -ln(2)/beta (régression Δx_t = alpha + beta*x_{t-1} + eps, OLS, période "
            "pré-OOS). beta >= 0 => aucun retour à la moyenne détecté sur cette période (half_life "
            "= +inf, rapporté tel quel, jamais masqué). Comparé à L (grille SPEC.md §4 : 720h/30j "
            "et 2160h/90j) dans le champ 'half_life_vs_L' ci-dessous : une demi-vie >> L rend le "
            "z-score structurellement mal calibré (SPEC.md §5, dernier point)."
        ),
    }
    half_life_vs_L = {
        str(l_hours): {
            "l_hours": l_hours,
            "half_life_hours": half_life_hours,
            "ratio_half_life_over_L": (half_life_hours / l_hours) if math.isfinite(half_life_hours) else float("inf"),
        }
        for l_hours in sorted({c["l_hours"] for c in pr.PARAM_GRID})
    }

    print(f"[stationarity] ADF par fenêtre IS ({len(windows)} fenêtres) ...")
    adf_per_is_window = []
    for w in windows:
        x_is = x_full.iloc[w.is_start_idx : w.is_end_idx + 1].dropna()
        try:
            stat, pvalue, usedlag, nobs, crit, _ = adfuller(x_is.to_numpy(), autolag="AIC", regression="c")
            adf_per_is_window.append(
                {
                    "window_index": w.index,
                    "is_start": str(w.is_start),
                    "is_end": str(w.is_end),
                    "n_obs_used": int(nobs),
                    "used_lag": int(usedlag),
                    "adf_statistic": float(stat),
                    "p_value": float(pvalue),
                    "stationary_at_10pct": bool(pvalue <= 0.10),
                }
            )
        except Exception as exc:  # défensif : ne jamais faire échouer tout le run sur UNE fenêtre
            adf_per_is_window.append(
                {
                    "window_index": w.index,
                    "is_start": str(w.is_start),
                    "is_end": str(w.is_end),
                    "error": str(exc),
                }
            )

    n_is_windows_stationary = sum(1 for r in adf_per_is_window if r.get("stationary_at_10pct") is True)

    return {
        "status": "OK",
        "adf_log_ratio_pre_oos": adf_pre_oos,
        "engle_granger_log_prices_pre_oos": engle_granger_pre_oos,
        "half_life_mean_reversion_pre_oos": half_life,
        "half_life_vs_L_grid": half_life_vs_L,
        "adf_log_ratio_per_is_window": adf_per_is_window,
        "n_is_windows_stationary_at_10pct": n_is_windows_stationary,
        "n_is_windows_total": len(adf_per_is_window),
        "note": (
            "Diagnostic d'honnêteté (SPEC.md §5), rôle décisionnel FIGÉ : ne remplace ni "
            "n'assouplit aucun seuil Porte 1 §1.2 -- exécuté et consigné AVANT le walk-forward, "
            "aucun re-choix de fenêtre/univers/grille autorisé après lecture de ces résultats."
        ),
    }


# ------------------------------------------------------------------------------------------
# Cache des matrices de poids (et de l'état complet), par combinaison de paramètres -- chaque
# combo calculé UNE SEULE FOIS sur le calendrier complet, réutilisé pour toutes les fenêtres.
# ------------------------------------------------------------------------------------------


class CandidateCache:
    def __init__(self, closes: pd.DataFrame):
        self._closes = closes
        self._weights_cache: Dict[Tuple, pd.DataFrame] = {}
        self._state_cache: Dict[Tuple, pd.DataFrame] = {}

    @staticmethod
    def _key(params: dict) -> Tuple:
        return tuple(sorted(params.items()))

    def _params_obj(self, params: dict) -> pr.PairsRatioParams:
        return pr.PairsRatioParams(**params)

    def get_weights(self, params: dict) -> pd.DataFrame:
        key = self._key(params)
        if key not in self._weights_cache:
            self._weights_cache[key] = pr.generate_weight_decisions(self._closes, self._params_obj(params))
        return self._weights_cache[key]

    def get_state(self, params: dict) -> pd.DataFrame:
        key = self._key(params)
        if key not in self._state_cache:
            self._state_cache[key] = pr.generate_state(self._closes, self._params_obj(params))
        return self._state_cache[key]

    def provider(self):
        return lambda params: self.get_weights(params)


class ControlCache:
    """Contrôle apparié (SPEC.md §6) : poids CONSTANTS {BTC:0.5, ETH:0.5} (aucun signal), une
    seule combinaison (grille `[{}]`), indépendante de `params`."""

    def __init__(self, calendar: pd.DatetimeIndex):
        self._weights = pd.DataFrame({sym: 0.5 for sym in UNIVERSE}, index=calendar)

    def get_weights(self, params: dict) -> pd.DataFrame:
        return self._weights

    def provider(self):
        return lambda params: self._weights


CONTROL_PARAM_GRID = [{}]  # 1 seule combinaison, zéro degré de liberté (SPEC.md §6)


# ------------------------------------------------------------------------------------------
# Métriques : periods_per_year=8760 explicite PARTOUT (SPEC.md §2).
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
# Walk-forward générique (candidate ET contrôle partagent EXACTEMENT ce pipeline, SPEC.md §6).
# ------------------------------------------------------------------------------------------


def run_walkforward(
    windows, calendar, opens, closes, weights_provider, param_grid: List[dict], cost_bps: float,
    sim_kwargs: dict, carry_across_windows: bool = True,
):
    per_window = []
    segments = []
    carry_boundary_events: List[dict] = []
    t_start = time.time()
    prev_carry_out = None
    for w in windows:
        t0 = time.time()
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
    segments = []
    for w, chosen in zip(windows, per_window_chosen):
        weights_chosen = weights_provider(chosen)
        seg = engine.simulate_segment(
            calendar, weights_chosen, opens, closes, w.oos_start_idx, w.oos_end_idx, cost_bps, **sim_kwargs
        )
        segments.append(seg)
    return engine.concatenate_segments(segments)


# ------------------------------------------------------------------------------------------
# Benchmark : buy & hold équipondéré BTC+ETH, mêmes fenêtres OOS alignées, SANS coûts ni overlay
# (SPEC.md §7).
# ------------------------------------------------------------------------------------------


def build_benchmark_weights(calendar: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame({sym: 0.5 for sym in UNIVERSE}, index=calendar)


def run_flat_weights_over_windows(windows, calendar, opens, closes, weights_decided: pd.DataFrame):
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
# Proxy quasi-passif (SPEC.md §8.3) : "panier 50/50 SMA200, même construction que dans
# run_protective_vol_gate.py" -- poids DÉCIDÉS 1/2 par symbole quand close(t) > SMA(close,4800h)(t)
# pour CE symbole, 0.0 sinon (panier long/flat PAR SYMBOLE).
# ------------------------------------------------------------------------------------------


def build_sma_trend_proxy_weights(closes: pd.DataFrame, sma_hours: int = PROXY_SMA_HOURS) -> pd.DataFrame:
    sma = closes.rolling(sma_hours, min_periods=sma_hours).mean()
    invested = closes > sma  # NaN (warm-up) -> comparaison False -> flat, jamais une licence à entrer
    weights = invested.astype(float) * (1.0 / len(UNIVERSE))
    return weights.fillna(0.0)


# ------------------------------------------------------------------------------------------
# Analyses d'honnêteté obligatoires (SPEC.md §8)
# ------------------------------------------------------------------------------------------


def analyze_tilt_episodes(
    windows, per_window_chosen: List[dict], cache: CandidateCache,
    candidate_oos_returns: pd.Series, control_oos_returns: pd.Series,
) -> dict:
    """§8.1 -- épisodes de TILT OOS DISTINCTS (entrée -> retour neutre), pour chacun sa durée et
    son PnL RELATIF (rendement candidate - rendement contrôle sur l'épisode, mêmes heures). Un
    "épisode" = run contigu de `state != STATE_NEUTRAL` sur la matrice d'état complète calculée
    avec les paramètres CHOISIS par la sélection IS de CHAQUE fenêtre, restreint à la fenêtre OOS
    de cette même fenêtre (jamais stitché entre deux fenêtres walk-forward consécutives, même
    approximation documentée que `run_protective_vol_gate.py::analyze_protection_episodes` : un
    épisode qui traverserait une frontière de fenêtre est compté comme deux épisodes distincts
    plutôt qu'un seul continu -- ne SOUS-compte jamais le nombre d'épisodes réel)."""
    episodes: List[dict] = []
    for w, chosen in zip(windows, per_window_chosen):
        sg = cache.get_state(chosen)
        state_slice = sg["state"].iloc[w.oos_start_idx : w.oos_end_idx + 1]
        active = (state_slice.to_numpy() != pr.STATE_NEUTRAL)
        idx = state_slice.index
        n = len(active)
        t = 0
        while t < n:
            if active[t]:
                start = t
                while t < n and active[t]:
                    t += 1
                end = t - 1
                ep_start_ts, ep_end_ts = idx[start], idx[end]
                cand_slice = candidate_oos_returns.loc[ep_start_ts:ep_end_ts]
                ctrl_slice = control_oos_returns.loc[ep_start_ts:ep_end_ts]
                cand_ret = float((1.0 + cand_slice.fillna(0.0)).prod() - 1.0)
                ctrl_ret = float((1.0 + ctrl_slice.fillna(0.0)).prod() - 1.0)
                episodes.append(
                    {
                        "window_index": w.index,
                        "start": str(ep_start_ts),
                        "end": str(ep_end_ts),
                        "n_hours": end - start + 1,
                        "candidate_return_over_episode": cand_ret,
                        "control_return_over_episode": ctrl_ret,
                        "relative_pnl_candidate_minus_control": cand_ret - ctrl_ret,
                    }
                )
            else:
                t += 1
    n_episodes = len(episodes)
    durations = [e["n_hours"] for e in episodes]
    median_duration_hours = float(np.median(durations)) if durations else float("nan")
    relative_pnls = [e["relative_pnl_candidate_minus_control"] for e in episodes]
    n_episodes_positive_relative = sum(1 for p in relative_pnls if p > 0.0)
    return {
        "episodes": episodes,
        "n_episodes_distinct": n_episodes,
        "median_duration_hours": median_duration_hours,
        "median_duration_days": median_duration_hours / 24.0 if not math.isnan(median_duration_hours) else float("nan"),
        "n_episodes_relative_pnl_positive": n_episodes_positive_relative,
        "n_episodes_relative_pnl_negative_or_zero": n_episodes - n_episodes_positive_relative,
        "sum_relative_pnl_over_all_episodes": float(sum(relative_pnls)) if relative_pnls else 0.0,
        "note": (
            "Un épisode = run contigu de state != NEUTRE, restreint à chaque fenêtre OOS avec les "
            "params choisis par SA PROPRE sélection IS -- jamais stitché entre deux fenêtres "
            "(approximation documentée, ne sous-compte jamais le nombre d'épisodes réel, SPEC.md "
            "§8.1). relative_pnl_candidate_minus_control = rendement composé de la candidate moins "
            "celui du contrôle apparié, calculés sur EXACTEMENT les mêmes heures (celles où "
            "state != NEUTRAL). 'Beaucoup de trades issus de peu d'épisodes != observations "
            "indépendantes' (risque n°2 de la fiche backlog) -- n_episodes_distinct est le compte "
            "pertinent pour juger l'indépendance statistique, pas n_trades_closed."
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
    """§8.4 -- comparaison appariée candidate vs contrôle sur les MÊMES fenêtres. Critère de
    décision (SPEC.md §6/§9) : DOMINATION = Sharpe ET Sortino ET MaxDD TOUS moins bons pour la
    candidate que pour le contrôle sur cette série appariée."""
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
            "Dominée (SPEC.md §6/§9) = Sharpe ET Sortino ET MaxDD TOUS moins bons pour la "
            "candidate que pour le contrôle sur cette série OOS appariée -- critère de décision "
            "explicite : une candidate dominée n'est PAS incubée quels que soient ses seuils "
            "Porte 1."
        ),
    }


def most_frequently_selected_is_combo(per_window_chosen: List[dict]) -> dict:
    """SPEC.md §9 : "params gelés = la combinaison la plus souvent sélectionnée en IS (égalité
    tranchée par theta le plus HAUT puis L le plus LONG -- le plus conservateur : moins de trades,
    signal plus lent)"."""
    counts = Counter(tuple(sorted(c.items())) for c in per_window_chosen)
    max_count = max(counts.values())
    tied = [dict(k) for k, v in counts.items() if v == max_count]
    tied.sort(key=lambda c: (-c["theta"], -c["l_hours"]))
    chosen = tied[0]
    return {
        "chosen": chosen,
        "n_windows_selected": max_count,
        "n_windows_total": len(per_window_chosen),
        "all_counts": [{"params": dict(k), "n_windows": v} for k, v in counts.items()],
        "tie_break_rule": "égalité tranchée par theta le plus HAUT puis L (l_hours) le plus LONG (SPEC.md §9).",
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
    if not args.smoke and n_windows != EXPECTED_N_WINDOWS:
        print(
            f"[ALERTE] nombre de fenêtres ({n_windows}) != attendu SPEC.md ({EXPECTED_N_WINDOWS}) "
            "-- K_total recalculé dynamiquement ci-dessous, signalé dans le rapport.",
            flush=True,
        )

    # --- Tests de stationnarité (SPEC.md §5), AVANT tout walk-forward --------------------------
    if not args.smoke:
        print("[stationarity] tests ADF/Engle-Granger/demi-vie (SPEC.md §5) ...", flush=True)
        stationarity = run_stationarity_tests(closes, windows, calendar)
    else:
        stationarity = {"status": "SKIPPED_SMOKE_RUN"}

    sim_kwargs = dict(
        vol_ewma_halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
        vol_periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR,
    )

    candidate_cache = CandidateCache(closes)
    control_cache = ControlCache(calendar)

    print(f"[run] walk-forward CANDIDATE pairs_ratio ({COST_BPS_NOMINAL:.0f} bps/côté nominal, carry ON) ...", flush=True)
    candidate_result = run_walkforward(
        windows, calendar, opens, closes, candidate_cache.provider(), pr.PARAM_GRID,
        COST_BPS_NOMINAL, sim_kwargs, carry_across_windows=True,
    )
    per_window_chosen = [pw["chosen_params"] for pw in candidate_result["per_window"]]

    print("[run] walk-forward CONTRÔLE apparié (poids constants 50/50, même pipeline, carry ON) ...", flush=True)
    control_result = run_walkforward(
        windows, calendar, opens, closes, control_cache.provider(), CONTROL_PARAM_GRID,
        COST_BPS_NOMINAL, sim_kwargs, carry_across_windows=True,
    )

    print("[run] benchmark buy & hold équipondéré BTC+ETH (sans coûts ni overlay) ...", flush=True)
    benchmark_weights = build_benchmark_weights(calendar)
    benchmark_result = run_flat_weights_over_windows(windows, calendar, opens, closes, benchmark_weights)

    print("[run] proxy quasi-passif SMA200 long/flat 50/50 (sans coûts ni overlay) ...", flush=True)
    proxy_weights = build_sma_trend_proxy_weights(closes)
    proxy_result = run_flat_weights_over_windows(windows, calendar, opens, closes, proxy_weights)

    print(
        f"[stress] re-simulation OOS CANDIDATE à {COST_BPS_STRESS_3X:.0f} et "
        f"{COST_BPS_STRESS_5X:.0f} bps/côté (mêmes params choisis) ...", flush=True,
    )
    concat_3x = rerun_oos_with_chosen_params_at_cost(
        windows, per_window_chosen, calendar, opens, closes, candidate_cache.provider(),
        COST_BPS_STRESS_3X, sim_kwargs,
    )
    concat_5x = rerun_oos_with_chosen_params_at_cost(
        windows, per_window_chosen, calendar, opens, closes, candidate_cache.provider(),
        COST_BPS_STRESS_5X, sim_kwargs,
    )
    pf_3x = bt_metrics.profit_factor([e["pnl"] for e in concat_3x.realized_events])
    pf_5x = bt_metrics.profit_factor([e["pnl"] for e in concat_5x.realized_events])
    sharpe_3x = bt_metrics.sharpe_ratio(concat_3x.returns, periods_per_year=PERIODS_PER_YEAR_HOURLY)
    sharpe_5x = bt_metrics.sharpe_ratio(concat_5x.returns, periods_per_year=PERIODS_PER_YEAR_HOURLY)

    # --- DSR (SPEC.md §7) -------------------------------------------------------------------
    candidate_oos_returns = candidate_result["_concatenated_result"].returns
    control_oos_returns = control_result["_concatenated_result"].returns
    benchmark_oos_returns = benchmark_result["_concatenated_result"].returns
    proxy_oos_returns = proxy_result["_concatenated_result"].returns

    k_total = K_REGISTRY_ROWS + n_windows * N_GRID_COMBOS_CANDIDATE + n_windows * N_GRID_COMBOS_CONTROL
    dsr_result = bt_metrics.deflated_sharpe_ratio(candidate_oos_returns, trials_k=k_total)

    # --- Analyses d'honnêteté (SPEC.md §8) ---------------------------------------------------
    print("[analyses] épisodes de tilt OOS distincts + PnL relatif candidate-contrôle (§8.1) ...", flush=True)
    tilt_episodes = analyze_tilt_episodes(
        windows, per_window_chosen, candidate_cache, candidate_oos_returns, control_oos_returns
    )

    print("[analyses] sous-périodes avant/depuis 2024-01-01 (§8.2) ...", flush=True)
    subperiods = subperiod_sharpe(candidate_oos_returns, SUBPERIOD_SPLIT_DATE)

    print("[analyses] corrélations OOS candidate-contrôle et candidate-proxy quasi-passif (§8.3) ...", flush=True)
    aligned_cand_control, aligned_control = candidate_oos_returns.align(control_oos_returns, join="inner")
    correlation_vs_control = float(aligned_cand_control.corr(aligned_control))
    aligned_cand_proxy, aligned_proxy = candidate_oos_returns.align(proxy_oos_returns, join="inner")
    correlation_vs_quasi_passive = float(aligned_cand_proxy.corr(aligned_proxy))

    print("[analyses] comparaison appariée candidate vs contrôle (§8.4, critère de décision §9) ...", flush=True)
    paired = paired_comparison(candidate_oos_returns, control_oos_returns)

    # --- Seuils PROMOTION-RULES §1.2 / SPEC.md §7, un par un --------------------------------
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
                "n_tilt_episodes_distincts": tilt_episodes["n_episodes_distinct"],
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

    # --- Sémantique des issues (SPEC.md §9, FIGÉE avant exécution) --------------------------
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
            "candidate_id": "pairs_ethbtc_ratio_rotation",
            "backlog_ref": "backtest/results/pairs_ethbtc_ratio_rotation/SPEC.md (P1#5, pré-enregistrée 2026-09-21, session hebdo #8)",
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
            "param_grid_candidate": pr.PARAM_GRID,
            "param_grid_control": CONTROL_PARAM_GRID,
            "cost_bps_nominal_per_side": COST_BPS_NOMINAL,
            "cost_bps_source": "bot.config.COST_TIER_FEE_TAKER_BPS['majors'] + bot.config.COST_TIER_SLIPPAGE_PENALTY_BPS['majors'] (SPEC.md §2)",
            "sim_kwargs_hourly": sim_kwargs,
            "periods_per_year_metrics": PERIODS_PER_YEAR_HOURLY,
            "k_total": k_total,
            "k_total_detail": {
                "registry_rows": K_REGISTRY_ROWS,
                "n_windows": n_windows,
                "n_grid_combos_candidate": N_GRID_COMBOS_CANDIDATE,
                "n_grid_combos_control": N_GRID_COMBOS_CONTROL,
                "formula": "K_total = registry_rows + n_windows*n_grid_combos_candidate + n_windows*n_grid_combos_control (SPEC.md §7)",
            },
            "runtime_seconds": None,  # renseigné à la fin
        },
        "stationarity": stationarity,
        "candidate_pairs_ratio": {
            "cost_bps": COST_BPS_NOMINAL,
            "carry_across_windows": True,
            "per_window": candidate_result["per_window"],
            "concatenated": cand_concat,
            "carry_boundary_events": candidate_result["carry_boundary_events"],
        },
        "control_constant_weights_50_50": {
            "cost_bps": COST_BPS_NOMINAL,
            "carry_across_windows": True,
            "description": "Poids constants {BTC:0.5, ETH:0.5}, aucun signal -- même moteur/overlay/coûts/fenêtres que la candidate (SPEC.md §6).",
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
                f"Poids 1/2 par symbole quand close(t) > SMA(close, {PROXY_SMA_HOURS}h)(t) pour CE "
                "symbole, 0.0 sinon (panier long/flat PAR SYMBOLE) -- SPEC.md §8.3, même "
                "construction que run_protective_vol_gate.py, univers à 2 actifs ici."
            ),
            "per_window": proxy_result["per_window"],
            "concatenated": proxy_concat,
        },
        "dsr_candidate": dsr_result.to_dict(),
        "cost_stress_test": {
            "profit_factor_at_15bps_nominal": cand_concat["profit_factor"],
            "profit_factor_at_45bps_3x": pf_3x,
            "profit_factor_at_75bps_5x": pf_5x,
            "sharpe_at_15bps_nominal": cand_concat["sharpe"],
            "sharpe_at_45bps_3x": sharpe_3x,
            "sharpe_at_75bps_5x": sharpe_5x,
        },
        "honesty_analyses": {
            "tilt_episodes": tilt_episodes,
            "subperiods_before_after_2024_01_01": subperiods,
            "correlation_vs_control": {
                "value": correlation_vs_control,
                "note": "Corrélation des rendements horaires OOS candidate vs contrôle apparié (mêmes fenêtres, index intersecté).",
            },
            "correlation_vs_quasi_passive_proxy": {
                "value": correlation_vs_quasi_passive,
                "note": (
                    "Corrélation des rendements horaires OOS candidate vs proxy quasi-passif "
                    "SMA200 long/flat 50/50 (SPEC.md §8.3). > 0.7-0.8 = intérêt marginal faible "
                    "(redondance avec une brique de trend-following déjà en production)."
                ),
            },
            "paired_comparison_candidate_vs_control": paired,
            "maxdd_comparison": {
                "candidate": cand_concat["max_drawdown"],
                "control": control_concat["max_drawdown"],
                "benchmark_buy_hold": bench_concat["max_drawdown"],
            },
            "stationarity_summary": stationarity,
        },
        "promotion_rules_1_2_thresholds_verdict": verdicts,
        "promotion_rules_1_2_all_pass": bool(all_pass),
        "candidate_dominated_by_control": dominated_by_control,
        "most_frequently_selected_is_combo": frozen_params_info,
        "gate1_verdict_automatic": {
            "value": gate1_verdict,
            "note": (
                "Verdict AUTOMATIQUE (SPEC.md §9) basé UNIQUEMENT sur les 5 seuils §1.2 et la "
                "non-domination par le contrôle. NE remplace PAS l'audit adversarial indépendant "
                "obligatoire (isSound) -- 'incubation_proposee_sous_reserve_audit_adversarial' "
                "signifie explicitement 'sous réserve', jamais une incubation actée. isSound non "
                "exécuté par ce script (hors périmètre de la mission d'implémentation du "
                "backtest, cf. `run_protective_vol_gate.py` même convention)."
            ),
        },
        "adversarial_audit_1_4": {
            "isSound": None,
            "note": (
                "SPEC.md exige un audit adversarial indépendant obligatoire avant toute décision "
                "de statut dans RESEARCH-REGISTRY.json (`isSound: false` = rejet quel que soit le "
                "chiffre). Non exécuté ici (hors périmètre de ce script)."
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
