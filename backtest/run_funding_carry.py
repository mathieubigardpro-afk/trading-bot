#!/usr/bin/env python3
"""backtest/run_funding_carry.py — orchestration de la candidate `funding_carry_6majors`
(`backtest/results/funding_carry_6majors/SPEC.md`, pré-enregistrée 2026-08-31, backlog P0#1).
Moteur commun `backtest/engine.py` + extension perp `backtest/perp.py` UNIQUEMENT
(`docs/PROMOTION-RULES.md` §1.1) -- ce script ne réimplémente AUCUNE logique de simulation,
seulement l'orchestration walk-forward + le calcul des analyses d'honnêteté demandées par la
SPEC (jamais présentes dans `backtest/engine.py`, qui n'a pas à connaître ce concept spécifique
à une candidate).

Usage :
    python3 -m backtest.run_funding_carry [--data-dir _data] [--output-dir ...]

AUCUNE grille hors `backtest/strategies/funding_carry.PARAM_GRID` n'est testée ici (import
direct de la constante, jamais une valeur ad hoc) : D in {7,30} x theta_in in {0.05,0.10},
4 combinaisons, rien d'autre (SPEC.md §"Grille pré-enregistrée").

Performance : le moteur perp fait ~6.6 ms/bougie sur 12 colonnes -> walk-forward complet
(14 fenêtres x 4 combos IS + 14 OOS + stress + sensibilité levier) ~= 45 minutes. Lancer avec
`nohup python3 -m backtest.run_funding_carry > run.log 2>&1 &` et surveiller `run.log`.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtest import data_hourly as bt_data  # noqa: E402
from backtest import engine  # noqa: E402
from backtest import metrics as bt_metrics  # noqa: E402
from backtest import perp as bt_perp  # noqa: E402
from backtest import risk_overlay  # noqa: E402
from backtest.strategies import funding_carry as fcarry  # noqa: E402
from bot import config as bot_cfg  # noqa: E402

UNIVERSE = list(bot_cfg.SYMBOLS_CRYPTO)  # BTC, ETH, SOL, DOGE, LINK, AVAX (SPEC.md)
assert UNIVERSE == ["BTC", "ETH", "SOL", "DOGE", "LINK", "AVAX"], (
    "bot.config.SYMBOLS_CRYPTO a changé -- SPEC.md fige explicitement cet univers, "
    "vérification défensive pour ne jamais dériver silencieusement de la SPEC."
)
PERP_COLS = [bt_perp.perp_column_name(s) for s in UNIVERSE]

DEFAULT_DATA_DIR = REPO_ROOT / "_data"
OUTPUT_DIR = REPO_ROOT / "backtest" / "results" / "funding_carry_6majors"

# Calendrier restreint (SPEC.md §"Univers, données, calendrier, coûts") : évite les deux trous
# bruts de SOL-PERP (72h dès 2022-02-26, 48h dès 2022-04-01), aucun autre trou sur les 6 perps
# 2022-04 -> 2026-07.
CALENDAR_START = pd.Timestamp("2022-04-03 00:00:00")
CALENDAR_END = pd.Timestamp("2026-07-31 23:00:00")

COST_BPS_NOMINAL = 25.0  # SPEC.md : 25 bps/côté sur LA JAMBE SPOT ET LA JAMBE PERP
COST_BPS_STRESS_3X = 75.0
COST_BPS_STRESS_5X = 125.0

IS_MONTHS = 9
OOS_MONTHS = 3
STEP_MONTHS = 3

PERIODS_PER_YEAR_HOURLY = 8760.0  # SPEC.md : "periods_per_year=8760"

# K_total = 12 (lignes RESEARCH-REGISTRY.json au 2026-08-31, vérifié avant exécution) +
# n_fenêtres x 4 combinaisons (SPEC.md §"Walk-forward et moteur"). Si le nombre de fenêtres
# générées diffère des 14 attendues par la SPEC, le calcul reste dynamique ci-dessous et
# l'écart est signalé dans `results.json`/le rapport (mission).
K_REGISTRY_ROWS = 12
N_GRID_COMBOS = len(fcarry.PARAM_GRID)  # 4
EXPECTED_N_WINDOWS = 14  # SPEC.md, vérifié avant exécution

PROMOTION_RULES_THRESHOLDS = {
    "sharpe_oos_min": 0.70,
    "profit_factor_oos_min": 1.15,
    "n_trades_oos_perp_closed_min": 80,
    "maxdd_relative_to_benchmark_max": 1.5,
    "dsr_min": 0.50,
}

SUBPERIOD_SPLIT_DATE = pd.Timestamp("2024-01-01")

# Défauts perp du moteur (SPEC.md §"Univers..." : "défauts de l'extension, jamais assouplis").
PERP_INITIAL_MARGIN_FRAC_DEFAULT = 0.50
PERP_MAINTENANCE_MARGIN_FRAC_DEFAULT = 0.025
PERP_LIQUIDATION_FEE_BPS_DEFAULT = 100.0

# Sensibilité au levier (informatif, SPEC.md §"Analyses d'honnêteté") : marge initiale 1,0 =
# levier 1. Si infaisable au poids nominal (0.10), test de repli à poids réduit (0.08).
LEVERAGE_SENSITIVITY_MARGIN_FRAC = 1.0
LEVERAGE_SENSITIVITY_REDUCED_WEIGHT = 0.08


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    p.add_argument("--output-dir", default=str(OUTPUT_DIR))
    return p


# ------------------------------------------------------------------------------------------
# Données : spot (data_hourly) + perp/funding (backtest/perp.py), fusionnées en matrices
# opens/closes/highs/lows communes (SPEC.md §"Univers, données, calendrier, coûts").
# ------------------------------------------------------------------------------------------


def _hi_lo_panel(aligned: Dict[str, pd.DataFrame], col: str, universe: List[str]) -> pd.DataFrame:
    return pd.DataFrame({sym: aligned[sym][col] for sym in universe})


def load_all_data(data_dir: str) -> dict:
    data_dir_path = Path(data_dir)
    spot_dir = data_dir_path / "crypto"
    print(f"[data] chargement spot ({len(UNIVERSE)} majors horaires) depuis {spot_dir} ...")
    raw_spot = bt_data.load_universe_raw(spot_dir, UNIVERSE)
    calendar_full = bt_data.build_calendar(raw_spot)
    calendar = calendar_full[(calendar_full >= CALENDAR_START) & (calendar_full <= CALENDAR_END)]
    print(
        f"[data] calendrier restreint (SPEC.md) : {calendar[0]} -> {calendar[-1]}, "
        f"{len(calendar)} heures (univers spot complet : {calendar_full[0]} -> {calendar_full[-1]}, "
        f"{len(calendar_full)} heures)"
    )
    real_gaps_spot = bt_data.count_real_gaps(raw_spot, calendar)
    print(f"[data] trous réels spot (NaN avant ffill, par symbole, calendrier restreint) : {real_gaps_spot}")
    aligned_spot = bt_data.align_universe_to_calendar(raw_spot, calendar)
    spot_opens = bt_data.opens_panel(aligned_spot, UNIVERSE)
    spot_closes = bt_data.closes_panel(aligned_spot, UNIVERSE)
    spot_highs = _hi_lo_panel(aligned_spot, "high", UNIVERSE)
    spot_lows = _hi_lo_panel(aligned_spot, "low", UNIVERSE)
    n_nan_spot = int(spot_closes.isna().sum().sum())
    print(f"[data] NaN restants après alignement spot (closes) : {n_nan_spot}")

    print(f"[data] chargement perp+funding ({len(UNIVERSE)} majors) depuis {data_dir_path} ...")
    perp_opens, perp_highs, perp_lows, perp_closes, funding, funding_orphans = (
        bt_perp.build_aligned_perp_matrices(data_dir_path, UNIVERSE, calendar)
    )
    n_nan_perp = int(perp_closes.isna().sum().sum())
    print(f"[data] NaN restants après alignement perp (closes) : {n_nan_perp}")
    orphan_report = bt_perp.funding_alignment_report(funding_orphans)
    print(f"[data] orphelins de funding (calendrier restreint, cf. backtest/perp.py) : {orphan_report}")

    opens = pd.concat([spot_opens, perp_opens], axis=1)
    closes = pd.concat([spot_closes, perp_closes], axis=1)
    # Mission : "les colonnes spot n'ont pas besoin de highs/lows perp ... fournis highs/lows
    # pour TOUTES les colonnes, spot inclus, ce qui est le plus simple ; le moteur ne les
    # utilise que pour les perps" -- highs/lows spot présents mais inertes (jamais lus par
    # `simulate_segment` pour une colonne non perp).
    highs = pd.concat([spot_highs, perp_highs], axis=1)
    lows = pd.concat([spot_lows, perp_lows], axis=1)

    return {
        "calendar": calendar,
        "spot_opens": spot_opens,
        "spot_closes": spot_closes,
        "perp_closes": perp_closes,
        "opens": opens,
        "closes": closes,
        "highs": highs,
        "lows": lows,
        "funding": funding,
        "real_gaps_spot": real_gaps_spot,
        "n_nan_spot": n_nan_spot,
        "n_nan_perp": n_nan_perp,
        "funding_orphans_report": orphan_report,
    }


# ------------------------------------------------------------------------------------------
# Cache des matrices de poids par combinaison de paramètres (chaque combo calculé UNE SEULE
# FOIS sur le calendrier complet, réutilisé pour toutes les fenêtres IS/OOS et les analyses
# d'honnêteté -- le signal ne dépend pas des bornes de fenêtre).
# ------------------------------------------------------------------------------------------


class WeightsCache:
    def __init__(self, spot_closes: pd.DataFrame, perp_closes: pd.DataFrame, funding: pd.DataFrame):
        self._spot_closes = spot_closes
        self._perp_closes = perp_closes
        self._funding = funding
        self._cache: Dict[Tuple, pd.DataFrame] = {}

    def _key(self, params: dict) -> Tuple:
        return tuple(sorted(params.items()))

    def get(self, params: dict) -> pd.DataFrame:
        key = self._key(params)
        if key not in self._cache:
            p = fcarry.FundingCarryParams(**params)
            self._cache[key] = fcarry.generate_weight_decisions(
                self._spot_closes, self._perp_closes, self._funding, UNIVERSE, p
            )
        return self._cache[key]

    def provider(self):
        return lambda params: self.get(params)


# ------------------------------------------------------------------------------------------
# Métriques : periods_per_year=8760 explicite PARTOUT (SPEC.md), jamais les défauts 252 de
# `backtest/engine.py::summarize_segment` -- moteur non modifiable, on réimplémente le même
# bloc avec le bon `periods_per_year`, plus les champs spécifiques perp (trades clos par
# jambe, liquidations/faillites/ruine, cf. mission).
# ------------------------------------------------------------------------------------------


def summarize_hourly(seg, periods_per_year: float = PERIODS_PER_YEAR_HOURLY) -> dict:
    returns = seg.returns
    pnls = [e["pnl"] for e in seg.realized_events]
    equity = (1.0 + returns).cumprod()
    equity_with_base = pd.concat([pd.Series([1.0]), equity])
    trades = seg.trades_closed
    n_perp_closed = sum(1 for t in trades if t.get("leg") == "perp")
    n_spot_closed = sum(1 for t in trades if "leg" not in t)
    liquidations = list(getattr(seg, "liquidations", []))
    n_liq_perp = sum(1 for l in liquidations if l.get("symbol") != "*")
    n_ruin = sum(1 for l in liquidations if l.get("side") == "ruin")
    n_bankrupt = sum(1 for l in liquidations if l.get("bankrupt"))
    return {
        "sharpe": bt_metrics.sharpe_ratio(returns, periods_per_year=periods_per_year),
        "sortino": bt_metrics.sortino_ratio(returns, periods_per_year=periods_per_year),
        "profit_factor": bt_metrics.profit_factor(pnls),
        "max_drawdown": bt_metrics.max_drawdown(equity_with_base),
        "cagr": bt_metrics.cagr(equity_with_base, periods_per_year=periods_per_year),
        "average_exposure": bt_metrics.average_exposure(seg.gross_exposure),
        "n_trades_closed": len(trades),
        "n_trades_closed_perp": n_perp_closed,
        "n_trades_closed_spot": n_spot_closed,
        "n_periods": len(returns),
        "n_liquidations_perp": n_liq_perp,
        "n_bankrupt": n_bankrupt,
        "n_ruin": n_ruin,
        "pnl_breakdown": dict(getattr(seg, "pnl_breakdown", engine._empty_pnl_breakdown())),
    }


# ------------------------------------------------------------------------------------------
# sim_kwargs : surcouche de risque HORAIRE (défaut prod) + kwargs perp (SPEC.md).
# ------------------------------------------------------------------------------------------


def make_sim_kwargs(
    perp_cost_bps: float,
    funding: pd.DataFrame,
    highs: pd.DataFrame,
    lows: pd.DataFrame,
    perp_initial_margin_frac: float = PERP_INITIAL_MARGIN_FRAC_DEFAULT,
) -> dict:
    return dict(
        vol_ewma_halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
        vol_periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR,
        perp_symbols=set(PERP_COLS),
        funding=funding,
        highs=highs,
        lows=lows,
        perp_cost_bps=perp_cost_bps,
        perp_initial_margin_frac=perp_initial_margin_frac,
        perp_maintenance_margin_frac=PERP_MAINTENANCE_MARGIN_FRAC_DEFAULT,
        perp_liquidation_fee_bps=PERP_LIQUIDATION_FEE_BPS_DEFAULT,
    )


# ------------------------------------------------------------------------------------------
# Walk-forward : sélection IS (grille complète) + simulation OOS avec les MÊMES sim_kwargs
# ------------------------------------------------------------------------------------------


def run_walkforward(windows, calendar, opens, closes, weights_cache: WeightsCache, cost_bps: float, sim_kwargs: dict):
    per_window = []
    segments = []
    t_start = time.time()
    for w in windows:
        t0 = time.time()
        # `simulate_segment` exige `start_idx >= 1` -- inévitable pour la toute PREMIÈRE fenêtre
        # (son IS commence exactement à la première heure du calendrier restreint), cf.
        # `backtest/run_vol_breakout.py` (même déviation documentée, appliquée IDENTIQUEMENT aux
        # 4 combinaisons de la grille, donc sans biais de sélection).
        is_start_idx_safe = max(1, w.is_start_idx)
        sel = engine.select_params_via_is(
            weights_cache.provider(),
            calendar,
            opens,
            closes,
            cost_bps,
            is_start_idx_safe,
            w.is_end_idx,
            param_grid=fcarry.PARAM_GRID,
            sim_kwargs=sim_kwargs,
        )
        chosen = sel.chosen_params
        weights_chosen = weights_cache.get(chosen)
        seg = engine.simulate_segment(
            calendar, weights_chosen, opens, closes, w.oos_start_idx, w.oos_end_idx, cost_bps, **sim_kwargs
        )
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
            }
        )
        per_window.append(summary)
        print(
            f"[walk-forward] fenêtre {w.index}/{len(windows) - 1} ({w.oos_start.date()} -> "
            f"{w.oos_end.date()}) : chosen={chosen} is_sharpe={sel.is_sharpe:.4f} "
            f"n_trades_perp_oos={summary['n_trades_closed_perp']} n_liq={summary['n_liquidations_perp']} "
            f"({time.time() - t0:.1f}s, total {time.time() - t_start:.1f}s)",
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
    }


def rerun_oos_with_chosen_params_at_cost(
    windows, per_window_chosen: List[dict], calendar, opens, closes, weights_cache: WeightsCache,
    cost_bps_value: float, funding: pd.DataFrame, highs: pd.DataFrame, lows: pd.DataFrame,
):
    """Stress de coûts (SPEC.md §1.4/"Analyses d'honnêteté") : re-simule le segment OOS de
    CHAQUE fenêtre avec les paramètres DÉJÀ CHOISIS par la sélection IS nominale (jamais une
    nouvelle sélection), coût identique sur LES DEUX JAMBES (spot ET perp, SPEC.md §"Coûts")."""
    sim_kwargs = make_sim_kwargs(cost_bps_value, funding, highs, lows)
    segments = []
    for w, chosen in zip(windows, per_window_chosen):
        weights_chosen = weights_cache.get(chosen)
        seg = engine.simulate_segment(
            calendar, weights_chosen, opens, closes, w.oos_start_idx, w.oos_end_idx, cost_bps_value, **sim_kwargs
        )
        segments.append(seg)
    return engine.concatenate_segments(segments)


# ------------------------------------------------------------------------------------------
# Benchmark : buy & hold équipondéré SPOT des 6 majors, mêmes fenêtres OOS alignées, SANS
# coûts ni overlay ni perp (SPEC.md §"Benchmark et seuils", même convention que
# `run_vol_breakout.py` -- "sans overlay" désactive vol targeting ET bande de non-négociation).
# ------------------------------------------------------------------------------------------


def build_benchmark_weights(calendar: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame({sym: 1.0 / len(UNIVERSE) for sym in UNIVERSE}, index=calendar)


def run_benchmark(windows, calendar, spot_opens, spot_closes):
    weights_decided = build_benchmark_weights(calendar)
    segments = []
    per_window = []
    for w in windows:
        seg = engine.simulate_segment(
            calendar, weights_decided, spot_opens, spot_closes, w.oos_start_idx, w.oos_end_idx,
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
# Sensibilité au levier (informatif, SPEC.md §"Analyses d'honnêteté") : marge initiale 1,0.
# ------------------------------------------------------------------------------------------


def run_margin_sensitivity(
    windows, per_window_chosen, calendar, opens, closes, weights_cache: WeightsCache,
    funding: pd.DataFrame, highs: pd.DataFrame, lows: pd.DataFrame, weight_scale: float = 1.0,
) -> dict:
    sim_kwargs = make_sim_kwargs(
        COST_BPS_NOMINAL, funding, highs, lows, perp_initial_margin_frac=LEVERAGE_SENSITIVITY_MARGIN_FRAC
    )
    segments = []
    try:
        for w, chosen in zip(windows, per_window_chosen):
            weights_chosen = weights_cache.get(chosen)
            if weight_scale != 1.0:
                weights_chosen = weights_chosen * weight_scale
            seg = engine.simulate_segment(
                calendar, weights_chosen, opens, closes, w.oos_start_idx, w.oos_end_idx,
                COST_BPS_NOMINAL, **sim_kwargs,
            )
            segments.append(seg)
    except ValueError as exc:
        return {"weight_scale": weight_scale, "feasible": False, "error": str(exc)}
    concatenated = engine.concatenate_segments(segments)
    return {"weight_scale": weight_scale, "feasible": True, "concatenated": summarize_hourly(concatenated)}


# ------------------------------------------------------------------------------------------
# Analyses d'honnêteté obligatoires (SPEC.md §"Analyses d'honnêteté obligatoires")
# ------------------------------------------------------------------------------------------


def analyze_activation_honesty(windows, per_window_chosen: List[dict], weights_cache: WeightsCache) -> dict:
    """Part des heures actives et nombre d'épisodes distincts d'activation, par symbole, sur la
    concaténation des fenêtres OOS (chacune avec les params CHOISIS par sa propre sélection IS).
    Un épisode = un run continu de poids actif (`w > 0`) dans la matrice de poids DÉCIDÉS (avant
    surcouche de risque) restreinte à la fenêtre OOS -- approximation documentée aux bornes de
    fenêtre : si une position active traverse la frontière entre deux fenêtres OOS consécutives
    (potentiellement avec des params IS différents choisis de part et d'autre), elle est comptée
    comme DEUX épisodes distincts plutôt qu'un seul continu (jamais l'inverse -- ne SOUS-compte
    donc jamais le nombre de trades/épisodes réels)."""
    n_active_hours = {s: 0 for s in UNIVERSE}
    n_episodes = {s: 0 for s in UNIVERSE}
    n_oos_hours_total = 0
    for w, chosen in zip(windows, per_window_chosen):
        weights_full = weights_cache.get(chosen)
        w_slice = weights_full[UNIVERSE].iloc[w.oos_start_idx : w.oos_end_idx + 1]
        active = (w_slice > 0.0).to_numpy()
        n_oos_hours_total += active.shape[0]
        for j, sym in enumerate(UNIVERSE):
            col = active[:, j]
            n_active_hours[sym] += int(col.sum())
            if len(col) > 0:
                transitions_on = int(np.sum((~col[:-1]) & col[1:])) if len(col) > 1 else 0
                n_episodes[sym] += transitions_on + (1 if col[0] else 0)
    total_active_hours = sum(n_active_hours.values())
    total_possible = n_oos_hours_total * len(UNIVERSE)
    return {
        "n_oos_hours_total": n_oos_hours_total,
        "active_hours_by_symbol": n_active_hours,
        "active_fraction_by_symbol": {
            s: (n_active_hours[s] / n_oos_hours_total if n_oos_hours_total else float("nan")) for s in UNIVERSE
        },
        "active_fraction_aggregate": (total_active_hours / total_possible if total_possible else float("nan")),
        "n_distinct_activation_episodes_by_symbol": n_episodes,
        "n_distinct_activation_episodes_total": sum(n_episodes.values()),
        "note": (
            "Un épisode = run continu de poids DÉCIDÉ actif (avant surcouche de risque), sur la "
            "matrice de poids complète restreinte à chaque fenêtre OOS avec les params choisis "
            "par SA PROPRE sélection IS -- une position active à la frontière entre deux "
            "fenêtres OOS consécutives compte comme deux épisodes (jamais sous-compté). À "
            "comparer à n_trades_closed_perp du run nominal (post-overlay/moteur, seul chiffre "
            "utilisé pour le seuil §1.2)."
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


# ------------------------------------------------------------------------------------------
# main
# ------------------------------------------------------------------------------------------


def main():
    args = build_parser().parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    t_global = time.time()
    d = load_all_data(args.data_dir)
    calendar = d["calendar"]
    opens, closes, highs, lows, funding = d["opens"], d["closes"], d["highs"], d["lows"], d["funding"]
    spot_opens, spot_closes = d["spot_opens"], d["spot_closes"]

    windows = engine.generate_walk_forward_windows(
        calendar, is_months=IS_MONTHS, oos_months=OOS_MONTHS, step_months=STEP_MONTHS
    )
    n_windows = len(windows)
    print(
        f"[walk-forward] {n_windows} fenêtres générées ({IS_MONTHS}m IS / {OOS_MONTHS}m OOS / "
        f"pas {STEP_MONTHS}m) -- attendu par SPEC.md : {EXPECTED_N_WINDOWS}",
        flush=True,
    )
    n_windows_matches_spec = n_windows == EXPECTED_N_WINDOWS
    if not n_windows_matches_spec:
        print(
            f"[ALERTE] nombre de fenêtres ({n_windows}) != attendu SPEC.md ({EXPECTED_N_WINDOWS}) "
            "-- K_total recalculé dynamiquement ci-dessous, signalé dans le rapport.",
            flush=True,
        )

    sim_kwargs_nominal = make_sim_kwargs(COST_BPS_NOMINAL, funding, highs, lows)
    weights_cache = WeightsCache(spot_closes, d["perp_closes"], funding)

    print("[run] walk-forward candidate funding_carry (25 bps/côté nominal, spot+perp) ...", flush=True)
    candidate_result = run_walkforward(windows, calendar, opens, closes, weights_cache, COST_BPS_NOMINAL, sim_kwargs_nominal)
    per_window_chosen = [pw["chosen_params"] for pw in candidate_result["per_window"]]

    print("[run] benchmark buy & hold équipondéré SPOT (sans coûts ni overlay) ...", flush=True)
    benchmark_result = run_benchmark(windows, calendar, spot_opens, spot_closes)

    print("[stress] re-simulation OOS à 75 et 125 bps/côté (deux jambes, mêmes params choisis) ...", flush=True)
    concat_75 = rerun_oos_with_chosen_params_at_cost(
        windows, per_window_chosen, calendar, opens, closes, weights_cache, COST_BPS_STRESS_3X, funding, highs, lows
    )
    concat_125 = rerun_oos_with_chosen_params_at_cost(
        windows, per_window_chosen, calendar, opens, closes, weights_cache, COST_BPS_STRESS_5X, funding, highs, lows
    )
    summary_75 = summarize_hourly(concat_75)
    summary_125 = summarize_hourly(concat_125)

    print("[analyses] sensibilité au levier (marge initiale 1.0 = levier 1, informatif) ...", flush=True)
    leverage_full_w = run_margin_sensitivity(
        windows, per_window_chosen, calendar, opens, closes, weights_cache, funding, highs, lows, weight_scale=1.0
    )
    leverage_sensitivity = {"w_0_10_margin_1_0": leverage_full_w}
    if not leverage_full_w["feasible"]:
        # Mission : "si ValueError, note-le" -- l'infaisabilité EST le résultat informatif
        # (le sizing nominal w=0.10 n'est pas viable à levier 1) ; rien de plus à mesurer.
        print(
            f"[analyses] marge=1.0 à w=0.10 INFAISABLE (ValueError) : "
            f"{leverage_full_w['error'][:200]!r} -- résultat lui-même informatif, pas de repli.",
            flush=True,
        )
    else:
        # Mission : "sinon réduis w à 0.08 pour ce test informatif" -- w=0.10 est déjà faisable
        # à marge=1.0 (aucune contrainte n'a jamais joué), donc un test COMPLÉMENTAIRE à poids
        # réduit est fait pour donner un second point de comparaison informatif (jamais utilisé
        # pour un seuil §1.2).
        print(
            "[analyses] marge=1.0 à w=0.10 faisable (aucune violation observée sur l'historique) "
            f"-- test complémentaire informatif à w={LEVERAGE_SENSITIVITY_REDUCED_WEIGHT}.",
            flush=True,
        )
        leverage_reduced_w = run_margin_sensitivity(
            windows, per_window_chosen, calendar, opens, closes, weights_cache, funding, highs, lows,
            weight_scale=LEVERAGE_SENSITIVITY_REDUCED_WEIGHT / fcarry.WEIGHT_PER_SYMBOL,
        )
        leverage_sensitivity[f"w_{LEVERAGE_SENSITIVITY_REDUCED_WEIGHT}_margin_1_0"] = leverage_reduced_w

    # --- DSR (SPEC.md §"Walk-forward et moteur"/PROMOTION-RULES.md §1.3) --------------------
    candidate_oos_returns = candidate_result["_concatenated_result"].returns
    k_total = K_REGISTRY_ROWS + n_windows * N_GRID_COMBOS
    dsr_result = bt_metrics.deflated_sharpe_ratio(candidate_oos_returns, trials_k=k_total)

    # --- Analyses d'honnêteté ----------------------------------------------------------------
    print("[analyses] heures actives / épisodes d'activation distincts par symbole ...", flush=True)
    activation_honesty = analyze_activation_honesty(windows, per_window_chosen, weights_cache)

    print("[analyses] corrélation vs buy & hold équipondéré spot ...", flush=True)
    benchmark_oos_returns = benchmark_result["_concatenated_result"].returns
    aligned_candidate, aligned_bh = candidate_oos_returns.align(benchmark_oos_returns, join="inner")
    correlation_vs_bh = float(aligned_candidate.corr(aligned_bh))

    print("[analyses] sous-périodes 2022-2023 vs 2024-2026 ...", flush=True)
    subperiods = subperiod_sharpe(candidate_oos_returns, SUBPERIOD_SPLIT_DATE)

    # --- Seuils PROMOTION-RULES §1.2 / SPEC.md §"Benchmark et seuils", un par un -------------
    cand_concat = candidate_result["concatenated"]
    bench_concat = benchmark_result["concatenated"]
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
        "n_trades_oos_perp_closed": {
            "value": cand_concat["n_trades_closed_perp"],
            "threshold": PROMOTION_RULES_THRESHOLDS["n_trades_oos_perp_closed_min"],
            "rule": ">= seuil (lignes PERP closes uniquement, SPEC.md §'Benchmark et seuils')",
            "pass": bool(cand_concat["n_trades_closed_perp"] >= PROMOTION_RULES_THRESHOLDS["n_trades_oos_perp_closed_min"]),
            "honesty_note": {
                "n_trades_closed_perp_oos": cand_concat["n_trades_closed_perp"],
                "n_trades_closed_spot_oos": cand_concat["n_trades_closed_spot"],
                "n_trades_closed_total_oos": cand_concat["n_trades_closed"],
                "n_distinct_activation_episodes_total": activation_honesty["n_distinct_activation_episodes_total"],
                "n_distinct_activation_episodes_by_symbol": activation_honesty["n_distinct_activation_episodes_by_symbol"],
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

    pnl_bd = cand_concat["pnl_breakdown"]
    pnl_total = sum(pnl_bd.values()) if pnl_bd else float("nan")
    # "le funding net doit expliquer l'essentiel du PnL, sinon l'hypothèse n'est pas celle
    # testée" (SPEC.md §"Analyses d'honnêteté") -- funding NET des coûts qui lui sont
    # structurellement associés n'est pas isolable proprement (coûts spot/perp financent
    # aussi les entrées/sorties de la jambe DIRECTIONNELLE, pas seulement le funding) ; on
    # publie funding_received brut ET la part du PnL TOTAL (spot+perp+funding-coûts-liq) qu'il
    # représente, la lecture la plus directe et la moins ambiguë.
    funding_share_of_total_pnl = (
        pnl_bd.get("funding_received", float("nan")) / pnl_total if pnl_total and not math.isnan(pnl_total) and pnl_total != 0 else float("nan")
    )

    results = {
        "meta": {
            "candidate_id": "funding_carry_6majors",
            "backlog_ref": "backtest/results/funding_carry_6majors/SPEC.md (P0#1, pré-enregistrée 2026-08-31)",
            "engine": "backtest/engine.py + backtest/perp.py (PERP-EXTENSION-SPEC.md, docs/PROMOTION-RULES.md §1.1)",
            "data_dir": str(args.data_dir),
            "univers": UNIVERSE,
            "calendar_start": str(calendar[0]),
            "calendar_end": str(calendar[-1]),
            "n_calendar_hours": len(calendar),
            "calendar_restriction_reason": (
                "SPEC.md : évite les trous bruts de SOL-PERP (72h dès 2022-02-26, 48h dès "
                "2022-04-01) -- le moteur audité refuse tout prix perp manquant sur une "
                "position engagée (PERP-EXTENSION-SPEC.md §6)."
            ),
            "real_data_gaps_spot_by_symbol": d["real_gaps_spot"],
            "n_nan_after_align_spot": d["n_nan_spot"],
            "n_nan_after_align_perp": d["n_nan_perp"],
            "funding_orphans_report": d["funding_orphans_report"],
            "n_windows": n_windows,
            "n_windows_matches_spec": n_windows_matches_spec,
            "walkforward": f"{IS_MONTHS}m IS / {OOS_MONTHS}m OOS / pas {STEP_MONTHS}m",
            "param_grid": fcarry.PARAM_GRID,
            "weight_per_symbol": fcarry.WEIGHT_PER_SYMBOL,
            "cost_bps_nominal_per_side_both_legs": COST_BPS_NOMINAL,
            "perp_margin_defaults": {
                "perp_initial_margin_frac": PERP_INITIAL_MARGIN_FRAC_DEFAULT,
                "perp_maintenance_margin_frac": PERP_MAINTENANCE_MARGIN_FRAC_DEFAULT,
                "perp_liquidation_fee_bps": PERP_LIQUIDATION_FEE_BPS_DEFAULT,
            },
            "sim_kwargs_hourly": {k: v for k, v in sim_kwargs_nominal.items() if k not in ("funding", "highs", "lows", "perp_symbols")},
            "perp_symbols": sorted(PERP_COLS),
            "periods_per_year_metrics": PERIODS_PER_YEAR_HOURLY,
            "k_total": k_total,
            "k_total_detail": {
                "registry_rows": K_REGISTRY_ROWS,
                "n_windows": n_windows,
                "n_grid_combos": N_GRID_COMBOS,
                "formula": "K_total = registry_rows + n_windows * n_grid_combos (PROMOTION-RULES.md §1.3, SPEC.md)",
                "expected_n_windows_spec": EXPECTED_N_WINDOWS,
            },
            "runtime_seconds": None,  # renseigné à la fin
        },
        "candidate_funding_carry": {
            "cost_bps_per_side_both_legs": COST_BPS_NOMINAL,
            "per_window": candidate_result["per_window"],
            "concatenated": cand_concat,
        },
        "benchmark_equal_weight_buy_hold_spot": {
            "cost_bps": 0.0,
            "overlay": "désactivé (apply_vol_targeting=False, no_trade_band=0.0), spot uniquement, sans perp",
            "per_window": benchmark_result["per_window"],
            "concatenated": bench_concat,
        },
        "dsr_candidate": dsr_result.to_dict(),
        "cost_stress_test": {
            "profit_factor_at_25bps_nominal": cand_concat["profit_factor"],
            "profit_factor_at_75bps_3x": summary_75["profit_factor"],
            "profit_factor_at_125bps_5x": summary_125["profit_factor"],
            "sharpe_at_25bps_nominal": cand_concat["sharpe"],
            "sharpe_at_75bps_3x": summary_75["sharpe"],
            "sharpe_at_125bps_5x": summary_125["sharpe"],
            "full_summary_75bps": summary_75,
            "full_summary_125bps": summary_125,
            "note": "Re-simulation OOS avec les params DÉJÀ choisis par fenêtre (jamais une nouvelle sélection), coût identique appliqué aux DEUX jambes (spot ET perp, SPEC.md §'Coûts').",
        },
        "leverage_sensitivity_informative": {
            **leverage_sensitivity,
            "note": (
                "Informatif uniquement (SPEC.md §'Analyses d'honnêteté') -- marge initiale 1.0 "
                "= levier 1 (jamais utilisée en décision, les seuils §1.2 utilisent les défauts "
                "de l'extension, perp_initial_margin_frac=0.50). Si infaisable (ValueError) au "
                "poids nominal w=0.10, repli documenté à w=0.08 (test informatif séparé)."
            ),
        },
        "honesty_analyses": {
            "pnl_breakdown_oos": pnl_bd,
            "pnl_breakdown_note": (
                "Décomposition Spot/Perp(variation)/Funding/Coûts(spot,perp)/Liquidations du PnL "
                "OOS concaténé (PERP-EXTENSION-SPEC.md §4). Le funding net doit expliquer "
                "l'essentiel du PnL total (spot_pnl + perp_variation + funding_received - "
                "costs_spot - costs_perp - liquidation_fees), sinon l'hypothèse structurelle "
                "n'est pas celle testée (SPEC.md)."
            ),
            "funding_share_of_total_pnl": funding_share_of_total_pnl,
            "n_liquidations_perp_oos": cand_concat["n_liquidations_perp"],
            "n_bankrupt_oos": cand_concat["n_bankrupt"],
            "n_ruin_oos": cand_concat["n_ruin"],
            "activation_honesty": activation_honesty,
            "correlation_vs_equal_weight_buy_hold_spot": {
                "value": correlation_vs_bh,
                "note": (
                    "Corrélation des rendements OOS candidate vs benchmark B&H équipondéré spot "
                    "(mêmes fenêtres OOS alignées, intersection des index). Attendue ~= 0 pour "
                    "une stratégie delta-neutre -- une corrélation élevée signalerait une jambe "
                    "non couverte (SPEC.md §'Analyses d'honnêteté')."
                ),
            },
            "subperiods_2022_2023_vs_2024_2026": subperiods,
        },
        "promotion_rules_1_2_thresholds_verdict": verdicts,
        "promotion_rules_1_2_all_pass": bool(all_pass),
        "adversarial_audit_1_4": {
            "isSound": None,
            "note": (
                "SPEC.md §'Benchmark et seuils' exige un audit adversarial indépendant "
                "obligatoire (isSound: false = rejet quel que soit le chiffre). Cet audit n'est "
                "PAS exécuté par ce script (hors périmètre de la mission d'implémentation du "
                "backtest) -- à conduire séparément avant toute décision de statut dans "
                "RESEARCH-REGISTRY.json."
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
