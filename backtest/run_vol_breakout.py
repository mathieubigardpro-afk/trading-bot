#!/usr/bin/env python3
"""backtest/run_vol_breakout.py — orchestration de la candidate `vol_breakout_6majors`
(`backtest/results/vol_breakout_6majors/SPEC.md`, pré-enregistrée 2026-08-03, backlog P0#2).
Moteur commun `backtest/engine.py` uniquement (`docs/PROMOTION-RULES.md` §1.1) -- ce script ne
réimplémente AUCUNE logique de simulation, seulement l'orchestration walk-forward + le calcul
des analyses d'honnêteté demandées par la SPEC (jamais présentes dans `backtest/engine.py`, qui
n'a pas à connaître ce concept spécifique à une candidate).

Usage :
    python3 -m backtest.run_vol_breakout [--data-dir _data/crypto]

AUCUNE grille hors `backtest/strategies/vol_breakout.PARAM_GRID` n'est testée ici (import direct
de la constante, jamais une valeur ad hoc) : W in {55,110}, P in {0.20,0.35}, 4 combinaisons,
rien d'autre (SPEC.md §"Grille pré-enregistrée").
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
from backtest import risk_overlay  # noqa: E402
from backtest.strategies import vol_breakout as vb  # noqa: E402
from bot import config as bot_cfg  # noqa: E402

UNIVERSE = list(bot_cfg.SYMBOLS_CRYPTO)  # BTC, ETH, SOL, DOGE, LINK, AVAX (SPEC.md)
assert UNIVERSE == ["BTC", "ETH", "SOL", "DOGE", "LINK", "AVAX"], (
    "bot.config.SYMBOLS_CRYPTO a changé -- SPEC.md fige explicitement cet univers, "
    "vérification défensive pour ne jamais dériver silencieusement de la SPEC."
)

DEFAULT_DATA_DIR = REPO_ROOT / "_data" / "crypto"
OUTPUT_DIR = REPO_ROOT / "backtest" / "results" / "vol_breakout_6majors"

COST_BPS_NOMINAL = 25.0  # SPEC.md §"Univers, données, coûts" -- 25 bps/côté uniforme, pessimiste
COST_BPS_STRESS_3X = 75.0
COST_BPS_STRESS_5X = 125.0

IS_MONTHS = 9
OOS_MONTHS = 3
STEP_MONTHS = 3

PERIODS_PER_YEAR_HOURLY = 8760.0  # SPEC.md : "periods_per_year=8760 passé explicitement partout"

# K_total = 10 (lignes RESEARCH-REGISTRY.json au 2026-08-03) + n_fenêtres x 4 combinaisons
# (SPEC.md §"Benchmark et seuils"/PROMOTION-RULES.md §1.3). Vérifié : `docs/RESEARCH-
# REGISTRY.json:strategies` contient bien 10 entrées à la date de ce test.
K_REGISTRY_ROWS = 10
N_GRID_COMBOS = len(vb.PARAM_GRID)  # 4

PROMOTION_RULES_THRESHOLDS = {
    "sharpe_oos_min": 0.70,
    "profit_factor_oos_min": 1.15,
    "n_trades_oos_min": 80,
    "maxdd_relative_to_benchmark_max": 1.5,
    "dsr_min": 0.50,
}

# "Squeeze recent" = squeeze actif à t OU dans les ENTRY_SQUEEZE_LOOKBACK_HOURS heures
# précédentes -- utilisé aussi pour l'analyse d'honnêteté (épisodes de squeeze distincts).
MIN_EPISODE_GAP_HOURS = 7 * 24  # "séparées d'au moins 7 jours" (SPEC.md §"Analyses d'honnêteté")

SUBPERIOD_SPLIT_DATE = pd.Timestamp("2024-01-01")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    p.add_argument("--output-dir", default=str(OUTPUT_DIR))
    return p


# ------------------------------------------------------------------------------------------
# Données
# ------------------------------------------------------------------------------------------


def load_all_data(data_dir: str) -> dict:
    print(f"[data] chargement de {len(UNIVERSE)} majors crypto horaires depuis {data_dir} ...")
    raw = bt_data.load_universe_raw(data_dir, UNIVERSE)
    calendar = bt_data.build_calendar(raw)
    print(
        f"[data] calendrier commun (union horaire) : {calendar[0]} -> {calendar[-1]}, "
        f"{len(calendar)} heures"
    )
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
# Cache des matrices de poids par combinaison de paramètres (chaque combo est calculé UNE
# SEULE FOIS sur le calendrier complet, réutilisé pour toutes les fenêtres IS/OOS -- le signal
# ne dépend pas des bornes de fenêtre, seulement de `closes` et des paramètres).
# ------------------------------------------------------------------------------------------


class WeightsCache:
    def __init__(self, closes: pd.DataFrame):
        self._closes = closes
        self._cache: Dict[Tuple, pd.DataFrame] = {}

    def _key(self, params: dict) -> Tuple:
        return tuple(sorted(params.items()))

    def get(self, params: dict) -> pd.DataFrame:
        key = self._key(params)
        if key not in self._cache:
            p = vb.VolBreakoutParams(**params)
            self._cache[key] = vb.generate_weight_decisions(self._closes, p)
        return self._cache[key]

    def provider(self):
        return lambda params: self.get(params)


# ------------------------------------------------------------------------------------------
# Métriques : periods_per_year=8760 explicite PARTOUT (SPEC.md), jamais les défauts 252 de
# `backtest/engine.py::summarize_segment` (qui sont corrects pour le calendrier quotidien de
# `backtest/run_xsmom_invvol.py` mais faux ici -- moteur non modifiable, on n'utilise donc pas
# `engine.summarize_segment`, on réimplémente le même bloc de métriques avec le bon
# `periods_per_year`, cf. mission).
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
# Walk-forward : sélection IS (grille complète) + simulation OOS avec les MÊMES sim_kwargs
# ------------------------------------------------------------------------------------------


def run_walkforward(
    windows, calendar, opens, closes, weights_cache: WeightsCache, cost_bps: float, sim_kwargs: dict
):
    per_window = []
    segments = []
    t_start = time.time()
    for w in windows:
        t0 = time.time()
        # `simulate_segment` exige `start_idx >= 1` (une décision exécutée a besoin d'une ligne
        # de warm-up `weights_decided.iloc[start_idx-1]`, cf. docstring engine.py) -- inévitable
        # pour la toute PREMIÈRE fenêtre (`index==0`) : son IS commence exactement à la première
        # heure du calendrier (`is_start_idx==0`), pour laquelle aucune ligne antérieure n'existe
        # par construction (aucune donnée avant le tout début de l'historique). Déviation
        # minimale et documentée, appliquée IDENTIQUEMENT aux 4 combinaisons de la grille (donc
        # sans biais de sélection) : la sélection IS de cette fenêtre unique démarre à l'index 1
        # au lieu de 0 -- perte de 1 heure sur ~6600 heures IS, jamais l'OOS (toujours >> 0).
        is_start_idx_safe = max(1, w.is_start_idx)
        sel = engine.select_params_via_is(
            weights_cache.provider(),
            calendar,
            opens,
            closes,
            cost_bps,
            is_start_idx_safe,
            w.is_end_idx,
            param_grid=vb.PARAM_GRID,
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
            f"[walk-forward] fenêtre {w.index}: chosen={chosen} is_sharpe={sel.is_sharpe:.4f} "
            f"n_trades_oos={summary['n_trades_closed']} ({time.time() - t0:.1f}s)"
        )
    concatenated = engine.concatenate_segments(segments)
    concat_summary = summarize_hourly(concatenated)
    print(f"[walk-forward] terminé en {time.time() - t_start:.1f}s")
    return {
        "per_window": per_window,
        "concatenated": concat_summary,
        "_segments": segments,
        "_concatenated_result": concatenated,
    }


def rerun_oos_with_chosen_params_at_cost(
    windows, per_window_chosen: List[dict], calendar, opens, closes, weights_cache: WeightsCache,
    cost_bps: float, sim_kwargs: dict,
):
    """Stress de coûts (SPEC.md §"Analyses d'honnêteté"/§1.4) : re-simule le segment OOS de
    CHAQUE fenêtre avec les paramètres DÉJÀ CHOISIS par la sélection IS nominale (jamais une
    nouvelle sélection -- on ne re-sélectionne pas les paramètres au coût stressé, on mesure
    la sensibilité de la MÊME décision à un coût différent), seul `cost_bps` change."""
    segments = []
    for w, chosen in zip(windows, per_window_chosen):
        weights_chosen = weights_cache.get(chosen)
        seg = engine.simulate_segment(
            calendar, weights_chosen, opens, closes, w.oos_start_idx, w.oos_end_idx, cost_bps, **sim_kwargs
        )
        segments.append(seg)
    return engine.concatenate_segments(segments)


# ------------------------------------------------------------------------------------------
# Benchmark : buy & hold équipondéré des 6 majors, mêmes fenêtres OOS alignées, SANS coûts ni
# overlay (SPEC.md §"Benchmark et seuils"). "Sans overlay" = les DEUX composantes de la
# surcouche de risque désactivées (`apply_vol_targeting=False` ET `no_trade_band=0.0`) --
# interprétation tranchée : `risk_overlay.py` documente la bande de non-négociation et le vol
# targeting comme UNE SEULE "surcouche de risque" bundlée, "sans overlay" désactive le tout,
# jamais seulement une des deux moitiés.
# ------------------------------------------------------------------------------------------


def build_benchmark_weights(calendar: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame({sym: 1.0 / len(UNIVERSE) for sym in UNIVERSE}, index=calendar)


def run_benchmark(windows, calendar, opens, closes):
    weights_decided = build_benchmark_weights(calendar)
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
# Analyses d'honnêteté obligatoires (SPEC.md §"Analyses d'honnêteté obligatoires")
# ------------------------------------------------------------------------------------------


def _count_distinct_episodes(active: pd.Series, min_gap_periods: int) -> int:
    """Nombre d'épisodes DISTINCTS d'une série booléenne : deux runs de `True` séparés par un
    écart < `min_gap_periods` (lignes) comptent comme LE MÊME épisode (mission : "séparées
    d'au moins 7 jours" -- un gap plus court ne sépare pas deux épisodes indépendants)."""
    arr = active.to_numpy(dtype=bool)
    idx = np.flatnonzero(arr)
    if len(idx) == 0:
        return 0
    episodes = 1
    for i in range(1, len(idx)):
        if idx[i] - idx[i - 1] >= min_gap_periods:
            episodes += 1
    return episodes


def analyze_squeeze_honesty(windows, per_window_chosen: List[dict], calendar, closes) -> dict:
    """Pour chaque symbole, sur la concaténation des fenêtres OOS (chacune avec les
    paramètres CHOISIS par sa propre sélection IS -- jamais un paramètre unique arbitraire) :
      - nombre d'ENTRÉES distinctes au niveau du SIGNAL (transition poids 0 -> 1/6 dans
        `weights_decided`, AVANT surcouche de risque -- approximation documentée : la bande de
        non-négociation peut annuler certains de ces ordres au niveau du moteur, cf.
        `n_trades_closed` du run nominal pour le chiffre POST-overlay réellement exécuté) ;
      - nombre d'ÉPISODES de squeeze DISTINCTS (fenêtre bandwidth <= P, cf. `vb._squeeze_active`,
        toujours calculé sur les MÊMES paramètres choisis pour la fenêtre), regroupés si séparés
        de moins de 7 jours (`MIN_EPISODE_GAP_HOURS`)."""
    n_entries: Dict[str, int] = {sym: 0 for sym in UNIVERSE}
    n_episodes: Dict[str, int] = {sym: 0 for sym in UNIVERSE}
    for w, chosen in zip(windows, per_window_chosen):
        p = vb.VolBreakoutParams(**chosen)
        _middle, _upper, _lower, bandwidth = vb._bollinger_bands(closes, p.window_hours, p.k)
        squeeze_active_full = vb._squeeze_active(bandwidth, p.squeeze_lookback_hours, p.squeeze_percentile)
        weights_full = vb.generate_weight_decisions(closes, p)

        start = max(0, w.oos_start_idx - 1)
        w_slice = weights_full.iloc[start : w.oos_end_idx + 1]
        entries_bool = (w_slice.shift(1).fillna(0.0) == 0.0) & (w_slice > 0.0)
        entries_in_oos = entries_bool.iloc[1:] if w.oos_start_idx > 0 else entries_bool
        for sym in UNIVERSE:
            n_entries[sym] += int(entries_in_oos[sym].sum())

        squeeze_slice = squeeze_active_full.iloc[w.oos_start_idx : w.oos_end_idx + 1]
        for sym in UNIVERSE:
            n_episodes[sym] += _count_distinct_episodes(squeeze_slice[sym], MIN_EPISODE_GAP_HOURS)
    return {
        "n_entries_signal_level_by_symbol": n_entries,
        "n_squeeze_episodes_distinct_by_symbol": n_episodes,
        "note": (
            "n_entries_signal_level = transitions 0 -> 1/6 dans weights_decided (avant "
            "surcouche de risque no_trade_band/vol targeting) -- approximation documentée, à "
            "comparer à n_trades_closed du run nominal (post-overlay, moteur) pour le chiffre "
            "réellement exécuté. n_squeeze_episodes_distinct = épisodes de compression "
            "regroupés si séparés de moins de 7 jours -- répond au risque n°1 du backlog "
            "(80 trades sur peu d'épisodes indépendants ne sont pas 80 observations "
            "indépendantes)."
        ),
    }


def vol_targeted_passive_proxy(
    benchmark_returns_oos: pd.Series,
    halflife_periods: float = risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
    periods_per_year: float = PERIODS_PER_YEAR_HOURLY,
    target_vol_annualized: float = bot_cfg.VOL_TARGET_ANNUALIZED,
) -> pd.Series:
    """Proxy quasi-passif documenté (SPEC.md §"Analyses d'honnêteté") : rendements du panier
    équipondéré (buy & hold OOS, sans coûts) x `min(1, target_vol/vol_ewma)`, `vol_ewma` = EWMA
    causale de la vol annualisée du panier (même halflife/annualisation que la surcouche de
    risque de production, `bot.config.VOL_TARGET_ANNUALIZED`). `vol_ewma` est DÉCALÉE d'une
    période (`shift(1)`) pour rester strictement causale (le scalaire appliqué au rendement de
    l'heure t ne doit dépendre que de la vol connue à la clôture t-1), cohérent avec la
    convention `weights_decided.loc[t-1]` exécutée à `t` de `backtest/engine.py`."""
    r = benchmark_returns_oos.dropna()
    ewma_std = r.ewm(halflife=float(halflife_periods), adjust=False).std(bias=False)
    vol_ewma_annual = (ewma_std * math.sqrt(float(periods_per_year))).shift(1)
    scalar = (target_vol_annualized / vol_ewma_annual).clip(upper=1.0)
    scalar = scalar.fillna(0.5)  # cold-start prudent (même convention que risk_overlay : 0.5)
    return (r * scalar).reindex(benchmark_returns_oos.index)


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

    d = load_all_data(args.data_dir)
    calendar = d["calendar"]
    opens = d["opens"]
    closes = d["closes"]

    windows = engine.generate_walk_forward_windows(
        calendar, is_months=IS_MONTHS, oos_months=OOS_MONTHS, step_months=STEP_MONTHS
    )
    n_windows = len(windows)
    print(f"[walk-forward] {n_windows} fenêtres générées ({IS_MONTHS}m IS / {OOS_MONTHS}m OOS / pas {STEP_MONTHS}m)")

    sim_kwargs = dict(
        vol_ewma_halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
        vol_periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR,
    )

    weights_cache = WeightsCache(closes)

    print("[run] walk-forward candidate vol_breakout (25 bps/côté nominal) ...")
    candidate_result = run_walkforward(windows, calendar, opens, closes, weights_cache, COST_BPS_NOMINAL, sim_kwargs)
    per_window_chosen = [pw["chosen_params"] for pw in candidate_result["per_window"]]

    print("[run] benchmark buy & hold équipondéré (sans coûts ni overlay) ...")
    benchmark_result = run_benchmark(windows, calendar, opens, closes)

    print("[stress] re-simulation OOS à 75 et 125 bps/côté (mêmes params choisis, mêmes sim_kwargs) ...")
    concat_75 = rerun_oos_with_chosen_params_at_cost(
        windows, per_window_chosen, calendar, opens, closes, weights_cache, COST_BPS_STRESS_3X, sim_kwargs
    )
    concat_125 = rerun_oos_with_chosen_params_at_cost(
        windows, per_window_chosen, calendar, opens, closes, weights_cache, COST_BPS_STRESS_5X, sim_kwargs
    )
    pf_75 = bt_metrics.profit_factor([e["pnl"] for e in concat_75.realized_events])
    pf_125 = bt_metrics.profit_factor([e["pnl"] for e in concat_125.realized_events])

    # --- DSR (SPEC.md §"Benchmark et seuils"/PROMOTION-RULES.md §1.3) ----------------------
    candidate_oos_returns = candidate_result["_concatenated_result"].returns
    k_total = K_REGISTRY_ROWS + n_windows * N_GRID_COMBOS
    dsr_result = bt_metrics.deflated_sharpe_ratio(candidate_oos_returns, trials_k=k_total)

    # --- Analyses d'honnêteté ----------------------------------------------------------------
    print("[analyses] épisodes de squeeze distincts par symbole ...")
    squeeze_honesty = analyze_squeeze_honesty(windows, per_window_chosen, calendar, closes)

    print("[analyses] corrélation vs buy&hold vol-targeté (proxy quasi-passif) ...")
    benchmark_oos_returns = benchmark_result["_concatenated_result"].returns
    quasi_passive_returns = vol_targeted_passive_proxy(benchmark_oos_returns)
    aligned_candidate, aligned_passive = candidate_oos_returns.align(quasi_passive_returns, join="inner")
    correlation_vs_quasi_passive = float(aligned_candidate.corr(aligned_passive))

    print("[analyses] sous-périodes 2022-2023 vs 2024-2026 ...")
    subperiods = subperiod_sharpe(candidate_oos_returns, SUBPERIOD_SPLIT_DATE)

    # --- Seuils PROMOTION-RULES §1.2, un par un, verdict booléen ---------------------------
    cand_concat = candidate_result["concatenated"]
    bench_concat = benchmark_result["concatenated"]
    maxdd_ratio = (
        cand_concat["max_drawdown"] / bench_concat["max_drawdown"]
        if bench_concat["max_drawdown"] not in (0, None) and not math.isnan(bench_concat["max_drawdown"])
        else float("nan")
    )
    total_squeeze_episodes = sum(squeeze_honesty["n_squeeze_episodes_distinct_by_symbol"].values())

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
                "n_squeeze_episodes_distincts_total": total_squeeze_episodes,
                "n_squeeze_episodes_distincts_by_symbol": squeeze_honesty["n_squeeze_episodes_distinct_by_symbol"],
                "n_entries_signal_level_by_symbol": squeeze_honesty["n_entries_signal_level_by_symbol"],
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

    results = {
        "meta": {
            "candidate_id": "vol_breakout_6majors",
            "backlog_ref": "backtest/results/vol_breakout_6majors/SPEC.md (P0#2, pré-enregistrée 2026-08-03)",
            "engine": "backtest/engine.py (docs/PROMOTION-RULES.md §1.1)",
            "data_dir": str(args.data_dir),
            "univers": UNIVERSE,
            "calendar_start": str(calendar[0]),
            "calendar_end": str(calendar[-1]),
            "n_calendar_hours": len(calendar),
            "real_data_gaps_by_symbol": d["real_gaps"],
            "n_nan_after_align": d["n_nan_after_align"],
            "n_windows": n_windows,
            "walkforward": f"{IS_MONTHS}m IS / {OOS_MONTHS}m OOS / pas {STEP_MONTHS}m",
            "param_grid": vb.PARAM_GRID,
            "cost_bps_nominal_per_side": COST_BPS_NOMINAL,
            "sim_kwargs_hourly": sim_kwargs,
            "periods_per_year_metrics": PERIODS_PER_YEAR_HOURLY,
            "k_total": k_total,
            "k_total_detail": {
                "registry_rows": K_REGISTRY_ROWS,
                "n_windows": n_windows,
                "n_grid_combos": N_GRID_COMBOS,
                "formula": "K_total = registry_rows + n_windows * n_grid_combos (PROMOTION-RULES.md §1.3)",
            },
        },
        "candidate_vol_breakout": {
            "cost_bps": COST_BPS_NOMINAL,
            "per_window": candidate_result["per_window"],
            "concatenated": cand_concat,
        },
        "benchmark_equal_weight_buy_hold": {
            "cost_bps": 0.0,
            "overlay": "désactivé (apply_vol_targeting=False, no_trade_band=0.0)",
            "per_window": benchmark_result["per_window"],
            "concatenated": bench_concat,
        },
        "dsr_candidate": dsr_result.to_dict(),
        "cost_stress_test": {
            "profit_factor_at_25bps_nominal": cand_concat["profit_factor"],
            "profit_factor_at_75bps_3x": pf_75,
            "profit_factor_at_125bps_5x": pf_125,
        },
        "honesty_analyses": {
            "squeeze_episodes": squeeze_honesty,
            "correlation_vs_vol_targeted_passive_proxy": {
                "value": correlation_vs_quasi_passive,
                "proxy_definition": (
                    "rendements horaires OOS du panier équipondéré (buy & hold, sans coûts) x "
                    "min(1, VOL_TARGET_ANNUALIZED / vol_ewma_causale_du_panier) -- approximation "
                    "documentée du quasi-passif crypto déployé (bot.risk.vol_targeting), jamais "
                    "le RiskManager complet."
                ),
                "high_correlation_threshold_note": "0.7-0.8 (SPEC.md) : au-delà, intérêt marginal "
                "faible même si standalone correct.",
            },
            "subperiods_2022_2023_vs_2024_2026": subperiods,
        },
        "promotion_rules_1_2_thresholds_verdict": verdicts,
        "promotion_rules_1_2_all_pass": bool(all_pass),
    }

    results_path = output_dir / "results.json"
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2, default=str, ensure_ascii=False)
    print(f"[out] {results_path}")

    return results, output_dir


if __name__ == "__main__":
    main()
