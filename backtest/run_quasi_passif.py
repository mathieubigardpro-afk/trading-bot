#!/usr/bin/env python3
"""backtest/run_quasi_passif.py — orchestration de `quasi_passif_crypto_wf_retest`
(`backtest/results/quasi_passif_crypto_wf_retest/SPEC.md`, pré-enregistrée 2026-08-10,
backlog P2#13, session hebdomadaire #3). Moteur commun `backtest/engine.py` uniquement
(`docs/PROMOTION-RULES.md` §1.1) -- ce script assemble les 3 variantes réelles (prudent,
équilibré, agressif = les 3 wallets), calcule les métriques walk-forward OOS et les analyses
d'honnêteté demandées par la SPEC. AUCUN paramètre de stratégie n'est optimisé : la "grille"
de chaque variante contient EXACTEMENT 1 combinaison (les paramètres de production, lus
depuis `bot.config.WALLETS`, jamais recopiés en dur) -- `select_params_via_is` tourne quand
même (§1.3, chaque fenêtre compte 1 essai par variante).

Usage :
    python3 -m backtest.run_quasi_passif [--data-dir /tmp/md/data/crypto]
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
from backtest.strategies import quasi_passif as qp  # noqa: E402
from bot import config as bot_cfg  # noqa: E402
from bot.strategies.quasi_passif_crypto import (  # noqa: E402
    REGIME_SMA_DAYS,
    SPEC_UNIVERSE_BY_WALLET,
)

# --------------------------------------------------------------------------------------------
# Constantes SPEC (backtest/results/quasi_passif_crypto_wf_retest/SPEC.md) -- lues ou vérifiées
# contre bot.config.WALLETS, JAMAIS recopiées en dur pour les paramètres de risque.
# --------------------------------------------------------------------------------------------

VARIANTS = ["prudent", "equilibre", "agressif"]

# Coûts "au palier le plus défavorable de chaque univers" (SPEC.md §"Données, fenêtres, coûts").
COST_BPS_NOMINAL = {"prudent": 15.0, "equilibre": 25.0, "agressif": 45.0}

# Chiffres non audités d'origine (docs/RESEARCH-LOG.md, 2026-07-23 "Backtest quasi-passif
# crypto (non audité, complément vague 1)") -- pour comparaison honnête, JAMAIS pour recaler
# un paramètre.
UNAUDITED_SHARPE_ORIGIN = {"prudent": 1.24, "equilibre": 1.47, "agressif": 1.49}

# --------------------------------------------------------------------------------------------
# CORRECTIF FIDÉLITÉ PRODUCTION (finding CRITIQUE de l'audit adversarial indépendant, session
# hebdomadaire #3, 2026-08-10) : la production NE double-applique PAS le vol-targeting.
# `bot/runner.py:_risk_manager_for_wallet` (lignes ~545-622) construit le `RiskManager`
# PORTEFEUILLE avec `vol_target_annualized=50.0` (borne haute du constructeur,
# `0 < x <= 50`) et `vol_coldstart_scalar=1.0` -- ce qui neutralise à ~1.0, dans toutes les
# conditions réalistes, le scalaire de vol-targeting PORTEFEUILLE appliqué par
# `bot/risk/manager.py.RiskManager.apply()`. Le SEUL vol-targeting réellement actif en
# production sur la poche crypto est celui INTERNE à `bot.strategies.quasi_passif_crypto`
# (poids_brut = min(gross_exposure_max, vol_target/vol_réalisée), déjà reproduit par
# `backtest/strategies/quasi_passif.py`). La première exécution de ce backtest (avant ce
# correctif) passait `vol_target_annualized=risk_profile["vol_target_annualized"]` (et le
# `vol_coldstart_scalar` par défaut du moteur, 0.5) à `backtest/risk_overlay.py` -- une
# SECONDE couche de vol-targeting réellement active dans le backtest mais INEXISTANTE en
# production. Ces deux constantes reproduisent EXACTEMENT la neutralisation de
# `_risk_manager_for_wallet` (mêmes valeurs, même constructeur `RiskManager`) : la bande de
# non-négociation (0.05) et tout le reste (coûts, sizing de la stratégie) restent inchangés.
PRODUCTION_OVERLAY_VOL_TARGET_NEUTRALIZED = 50.0
PRODUCTION_OVERLAY_COLDSTART_SCALAR_NEUTRALIZED = 1.0

DEFAULT_DATA_DIR = "/tmp/md/data/crypto"
OUTPUT_DIR = REPO_ROOT / "backtest" / "results" / "quasi_passif_crypto_wf_retest"

IS_MONTHS = 9
OOS_MONTHS = 3
STEP_MONTHS = 3

PERIODS_PER_YEAR_HOURLY = 8760.0

# K_total = lignes RESEARCH-REGISTRY.json (11 au 2026-08-10) + 3 variantes x n_fenêtres x
# 1 combo (SPEC.md §"Benchmark et seuils"/PROMOTION-RULES.md §1.3) -- COMMUN aux 3 variantes
# (une seule session de recherche, pas 3 registres séparés).
K_REGISTRY_ROWS = 11

PROMOTION_RULES_THRESHOLDS = {
    "sharpe_oos_min": 0.70,
    "profit_factor_oos_min": 1.15,
    "n_trades_oos_min": 80,
    "maxdd_relative_to_benchmark_max": 1.5,
    "dsr_min": 0.50,
}

# Règle de substitution pré-enregistrée pour < 80 trades OOS clos (SPEC.md §"Benchmark et
# seuils", justification écrite §1.2 pour stratégie structurellement lente).
SUBSTITUTE_MIN_OOS_MONTHS = 24
SUBSTITUTE_MIN_REGIMES = 2

SUBPERIOD_SPLIT_DATE = pd.Timestamp("2024-01-01")

ALL_SYMBOLS = sorted(set(s for u in SPEC_UNIVERSE_BY_WALLET.values() for s in u))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    p.add_argument("--output-dir", default=str(OUTPUT_DIR))
    return p


def _wallet_profile(wallet_id: str) -> dict:
    matches = [w for w in bot_cfg.WALLETS if w.get("id") == wallet_id]
    if not matches:
        raise KeyError(f"wallet {wallet_id!r} introuvable dans bot.config.WALLETS")
    return matches[0]


# ------------------------------------------------------------------------------------------
# Données -- UN SEUL calendrier canonique (union des 14 symboles utilisés par au moins une
# variante) partagé par les 3 variantes, pour garantir des fenêtres OOS "mêmes fenêtres
# alignées" par construction (jamais 3 calendriers légèrement différents).
# ------------------------------------------------------------------------------------------


def load_all_data(data_dir: str) -> dict:
    print(f"[data] chargement de {len(ALL_SYMBOLS)} cryptos horaires (union des 3 univers) depuis {data_dir} ...")
    raw = bt_data.load_universe_raw(data_dir, ALL_SYMBOLS)
    calendar = bt_data.build_calendar(raw)
    print(f"[data] calendrier commun (union horaire) : {calendar[0]} -> {calendar[-1]}, {len(calendar)} heures")
    real_gaps = bt_data.count_real_gaps(raw, calendar)
    print(f"[data] trous réels rencontrés (NaN avant ffill, par symbole) : {real_gaps}")
    aligned = bt_data.align_universe_to_calendar(raw, calendar)
    n_nan_after_align = {sym: int(aligned[sym]["close"].isna().sum()) for sym in ALL_SYMBOLS}
    print(f"[data] NaN restants après alignement (closes, par symbole) : {n_nan_after_align}")
    return {
        "raw": raw,
        "calendar": calendar,
        "aligned": aligned,
        "real_gaps": real_gaps,
        "n_nan_after_align": n_nan_after_align,
    }


def summarize_hourly(seg, periods_per_year: float = PERIODS_PER_YEAR_HOURLY) -> dict:
    """Même bloc de métriques que `backtest/run_vol_breakout.py::summarize_hourly` --
    `periods_per_year=8760` explicite partout (SPEC.md), jamais les défauts 252 de
    `backtest/engine.py::summarize_segment`."""
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
# Walk-forward par variante : sélection IS (grille = 1 combo -> select_params_via_is
# court-circuite, aucune simulation IS nécessaire) + simulation OOS avec les MÊMES sim_kwargs.
# ------------------------------------------------------------------------------------------


def run_walkforward_variant(
    windows,
    calendar: pd.DatetimeIndex,
    opens: pd.DataFrame,
    closes: pd.DataFrame,
    weights_decided: pd.DataFrame,
    cost_bps: float,
    sim_kwargs: dict,
) -> dict:
    per_window = []
    segments = []
    provider = lambda params: weights_decided  # noqa: E731 -- 1 seule combinaison, ignore params
    t_start = time.time()
    for w in windows:
        is_start_idx_safe = max(1, w.is_start_idx)
        sel = engine.select_params_via_is(
            provider,
            calendar,
            opens,
            closes,
            cost_bps,
            is_start_idx_safe,
            w.is_end_idx,
            param_grid=[{}],
            sim_kwargs=sim_kwargs,
        )
        seg = engine.simulate_segment(
            calendar, weights_decided, opens, closes, w.oos_start_idx, w.oos_end_idx, cost_bps, **sim_kwargs
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
                "chosen_params": sel.chosen_params,
                "is_sharpe_chosen": sel.is_sharpe,
            }
        )
        per_window.append(summary)
    concatenated = engine.concatenate_segments(segments)
    concat_summary = summarize_hourly(concatenated)
    print(f"[walk-forward] terminé en {time.time() - t_start:.1f}s ({len(windows)} fenêtres)")
    return {
        "per_window": per_window,
        "concatenated": concat_summary,
        "_segments": segments,
        "_concatenated_result": concatenated,
    }


def rerun_oos_at_cost(
    windows, calendar, opens, closes, weights_decided: pd.DataFrame, cost_bps: float, sim_kwargs: dict
):
    """Stress de coûts (SPEC.md §"Données, fenêtres, coûts"/§1.4) : re-simule le segment OOS de
    CHAQUE fenêtre avec la MÊME matrice de poids (aucun paramètre à re-choisir, une seule
    combinaison), seul `cost_bps` change."""
    segments = []
    for w in windows:
        seg = engine.simulate_segment(
            calendar, weights_decided, opens, closes, w.oos_start_idx, w.oos_end_idx, cost_bps, **sim_kwargs
        )
        segments.append(seg)
    return engine.concatenate_segments(segments)


# ------------------------------------------------------------------------------------------
# Benchmark : buy & hold équipondéré du MÊME univers, mêmes fenêtres OOS alignées, SANS coûts
# ni overlay (SPEC.md §"Benchmark et seuils" -- même convention que run_vol_breakout.py).
# ------------------------------------------------------------------------------------------


def build_benchmark_weights(calendar: pd.DatetimeIndex, universe: List[str]) -> pd.DataFrame:
    return pd.DataFrame({sym: 1.0 / len(universe) for sym in universe}, index=calendar)


def run_benchmark_variant(windows, calendar, opens, closes, universe: List[str]) -> dict:
    weights_decided = build_benchmark_weights(calendar, universe)
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


def count_sma200_crossings(raw: Dict[str, pd.DataFrame], symbols: List[str]) -> Dict[str, dict]:
    """Nombre de croisements SMA200 DISTINCTS par actif sur tout l'historique disponible
    (2022-01 -> 2026-06), calculé sur la série `trend_on` journalière (SMA200 des clôtures
    journalières complètes, `_daily_closes`/rolling(200) -- même filtre que la stratégie,
    jamais un recalcul divergent). Un "croisement" = une transition de `trend_on` (True<->False)
    d'un jour au suivant, une fois les 200 premiers jours (warm-up, `trend_on` indéterminé)
    exclus. `n_entries` (off->on) est la grandeur comparable à un nombre d'ÉPISODES indépendants
    (SPEC.md : "épisodes indépendants vs nombre de trades")."""
    from bot.strategies.quasi_passif_crypto import _daily_closes

    out = {}
    for sym in symbols:
        daily = _daily_closes(raw.get(sym))
        sma = daily.rolling(REGIME_SMA_DAYS).mean()
        trend_on = (daily > sma).where(sma.notna())  # NaN tant que la SMA200 n'est pas calculable
        trend_on_valid = trend_on.dropna().astype(bool)
        if len(trend_on_valid) < 2:
            out[sym] = {"n_transitions": 0, "n_entries_off_to_on": 0, "n_days_trend_computable": len(trend_on_valid)}
            continue
        transitions = trend_on_valid.astype(int).diff().dropna()
        n_transitions = int((transitions != 0).sum())
        n_entries = int((transitions == 1).sum())
        out[sym] = {
            "n_transitions": n_transitions,
            "n_entries_off_to_on": n_entries,
            "n_days_trend_computable": int(len(trend_on_valid)),
        }
    return out


def cross_variant_correlation(oos_returns_by_variant: Dict[str, pd.Series]) -> dict:
    """Corrélation OOS pairwise entre les 3 variantes (SPEC.md : "elles partagent BTC/ETH...
    à chiffrer"), sur l'intersection des timestamps (les 3 variantes partagent le même
    calendrier/fenêtres par construction, cf. `load_all_data`, donc l'intersection == l'union
    normalement -- l'alignement explicite reste une garde défensive, pas une hypothèse non
    vérifiée)."""
    out = {}
    variants = list(oos_returns_by_variant.keys())
    for i in range(len(variants)):
        for j in range(i + 1, len(variants)):
            a, b = variants[i], variants[j]
            ra, rb = oos_returns_by_variant[a].align(oos_returns_by_variant[b], join="inner")
            corr = float(ra.corr(rb)) if len(ra) > 1 else float("nan")
            out[f"{a}_vs_{b}"] = {"correlation": corr, "n_periods_common": int(len(ra))}
    return out


def compute_verdicts(
    cand_concat: dict,
    bench_concat: dict,
    dsr_value: float,
    k_total: int,
    n_windows: int,
    subperiods: dict,
) -> Tuple[dict, bool]:
    """Les 5 verdicts §1.2 (PROMOTION-RULES.md) à partir d'un bloc de métriques OOS concaténées
    -- factorisé pour être appliqué IDENTIQUEMENT aux chiffres corrigés (décision) et aux
    chiffres pré-correctif (traçabilité), afin que la comparaison des deux jeux de verdicts
    soit une comparaison à méthodologie strictement égale (seul l'overlay de vol-targeting
    diffère)."""
    maxdd_ratio = (
        cand_concat["max_drawdown"] / bench_concat["max_drawdown"]
        if bench_concat["max_drawdown"] not in (0, None) and not math.isnan(bench_concat["max_drawdown"])
        else float("nan")
    )
    n_trades = cand_concat["n_trades_closed"]
    n_oos_months_total = n_windows * OOS_MONTHS
    substitution_ok = n_oos_months_total >= SUBSTITUTE_MIN_OOS_MONTHS
    two_regimes_present = subperiods["n_periods_before"] > 0 and subperiods["n_periods_after"] > 0
    trades_pass_direct = n_trades >= PROMOTION_RULES_THRESHOLDS["n_trades_oos_min"]
    trades_pass_substitute = (not trades_pass_direct) and substitution_ok and two_regimes_present
    trades_pass = bool(trades_pass_direct or trades_pass_substitute)

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
            "value": n_trades,
            "threshold": PROMOTION_RULES_THRESHOLDS["n_trades_oos_min"],
            "rule": ">= seuil OU justification écrite substitution SPEC (>=24 mois OOS ET >=2 régimes)",
            "pass": trades_pass,
            "substitution_applied": bool(trades_pass_substitute),
            "substitution_detail": {
                "n_oos_months_total": n_oos_months_total,
                "min_required_months": SUBSTITUTE_MIN_OOS_MONTHS,
                "months_ok": bool(substitution_ok),
                "two_regimes_present_bear_2022_2023_and_2024_2026": bool(two_regimes_present),
            },
        },
        "maxdd_relative_to_benchmark": {
            "value": maxdd_ratio,
            "threshold": PROMOTION_RULES_THRESHOLDS["maxdd_relative_to_benchmark_max"],
            "rule": "<= seuil (maxdd_candidate / maxdd_benchmark_OOS_aligné)",
            "pass": bool(not math.isnan(maxdd_ratio) and maxdd_ratio <= PROMOTION_RULES_THRESHOLDS["maxdd_relative_to_benchmark_max"]),
        },
        "dsr": {
            "value": dsr_value,
            "threshold": PROMOTION_RULES_THRESHOLDS["dsr_min"],
            "rule": ">= seuil",
            "k_total": k_total,
            "pass": bool(not math.isnan(dsr_value) and dsr_value >= PROMOTION_RULES_THRESHOLDS["dsr_min"]),
        },
    }
    all_pass = all(v["pass"] for v in verdicts.values())
    return verdicts, bool(all_pass)


# ------------------------------------------------------------------------------------------
# main
# ------------------------------------------------------------------------------------------


def main():
    args = build_parser().parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    d = load_all_data(args.data_dir)
    calendar = d["calendar"]
    aligned = d["aligned"]
    raw = d["raw"]

    windows = engine.generate_walk_forward_windows(calendar, is_months=IS_MONTHS, oos_months=OOS_MONTHS, step_months=STEP_MONTHS)
    n_windows = len(windows)
    print(f"[walk-forward] {n_windows} fenêtres générées ({IS_MONTHS}m IS / {OOS_MONTHS}m OOS / pas {STEP_MONTHS}m), COMMUNES aux 3 variantes")

    n_grid_combos = 1  # zéro degré de liberté nouveau (SPEC.md) : 1 combinaison par variante
    k_total = K_REGISTRY_ROWS + len(VARIANTS) * n_windows * n_grid_combos

    sma200_crossings_all = count_sma200_crossings(raw, ALL_SYMBOLS)

    variant_results: Dict[str, dict] = {}
    oos_returns_by_variant: Dict[str, pd.Series] = {}

    for variant_id in VARIANTS:
        print(f"\n=== Variante {variant_id!r} ===")
        wallet_profile = _wallet_profile(variant_id)
        risk_profile = wallet_profile["risque"]
        universe = SPEC_UNIVERSE_BY_WALLET[variant_id]
        cost_bps = COST_BPS_NOMINAL[variant_id]

        opens = bt_data.opens_panel(aligned, universe)
        closes = bt_data.closes_panel(aligned, universe)

        print(f"[signal] génération weights_decided ({len(universe)} actifs, décision quotidienne) ...")
        t0 = time.time()
        weights_decided = qp.generate_weight_decisions(raw, calendar, universe, risk_profile)
        print(f"[signal] {time.time() - t0:.1f}s")

        # --- sim_kwargs CORRIGÉS (décision, fidèles à `bot/runner.py:_risk_manager_for_wallet`)
        # : overlay neutralisé (vol_target_annualized=50.0, vol_coldstart_scalar=1.0), cf.
        # constantes de tête PRODUCTION_OVERLAY_*_NEUTRALIZED. Le SEUL vol-targeting réellement
        # actif reste celui, interne, de `backtest/strategies/quasi_passif.py` (fidèle à la
        # stratégie de production).
        sim_kwargs = dict(
            vol_ewma_halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
            vol_periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR,
            vol_target_annualized=PRODUCTION_OVERLAY_VOL_TARGET_NEUTRALIZED,
            vol_coldstart_scalar=PRODUCTION_OVERLAY_COLDSTART_SCALAR_NEUTRALIZED,
            no_trade_band=0.05,
        )
        # --- sim_kwargs PRÉ-CORRECTIF (traçabilité UNIQUEMENT, cf. mission de suivi de
        # l'audit) : reproduit EXACTEMENT la première exécution de ce script (avant le finding
        # d'audit), avec un second vol-targeting PORTEFEUILLE réellement actif
        # (`vol_target_annualized` du profil de la variante, `vol_coldstart_scalar` par défaut
        # du moteur = 0.5) -- comportement qui N'EXISTE PAS en production.
        sim_kwargs_pre_correctif = dict(
            vol_ewma_halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
            vol_periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR,
            vol_target_annualized=float(risk_profile["vol_target_annualized"]),
            no_trade_band=0.05,
        )

        print(f"[run] walk-forward CORRIGÉ ({cost_bps} bps/côté nominal, overlay neutralisé fidèle production) ...")
        candidate_result = run_walkforward_variant(windows, calendar, opens, closes, weights_decided, cost_bps, sim_kwargs)

        print(f"[run] walk-forward PRÉ-CORRECTIF ({cost_bps} bps/côté nominal, traçabilité uniquement) ...")
        candidate_result_pre = run_walkforward_variant(
            windows, calendar, opens, closes, weights_decided, cost_bps, sim_kwargs_pre_correctif
        )

        print("[run] benchmark buy & hold équipondéré (sans coûts ni overlay) -- inchangé par le correctif ...")
        benchmark_result = run_benchmark_variant(windows, calendar, opens, closes, universe)

        cost_3x = cost_bps * 3.0
        cost_5x = cost_bps * 5.0
        print(f"[stress] re-simulation OOS (chiffres CORRIGÉS) à {cost_3x} et {cost_5x} bps/côté ...")
        concat_3x = rerun_oos_at_cost(windows, calendar, opens, closes, weights_decided, cost_3x, sim_kwargs)
        concat_5x = rerun_oos_at_cost(windows, calendar, opens, closes, weights_decided, cost_5x, sim_kwargs)
        pf_3x = bt_metrics.profit_factor([e["pnl"] for e in concat_3x.realized_events])
        pf_5x = bt_metrics.profit_factor([e["pnl"] for e in concat_5x.realized_events])

        # turnover OOS annualisé x coût, rapporté au rendement (SPEC.md, stress "coût de la
        # dérive") : calculé sur `weights_decided` (signal BRUT de la stratégie, avant overlay)
        # -- inchangé par le correctif, un seul calcul suffit pour les 2 jeux de chiffres.
        cand_oos_returns = candidate_result["_concatenated_result"].returns
        cand_oos_returns_pre = candidate_result_pre["_concatenated_result"].returns
        cand_weights_oos_slices = [weights_decided.iloc[w.oos_start_idx - 1 : w.oos_end_idx] for w in windows]
        turnover_hourly_mean = float(
            pd.concat([w.diff().abs().sum(axis=1) for w in cand_weights_oos_slices]).mean()
        )
        turnover_annualized = turnover_hourly_mean * PERIODS_PER_YEAR_HOURLY
        cost_of_drift_annualized = turnover_annualized * (cost_bps / 10000.0)

        dsr_result = bt_metrics.deflated_sharpe_ratio(cand_oos_returns, trials_k=k_total)
        dsr_result_pre = bt_metrics.deflated_sharpe_ratio(cand_oos_returns_pre, trials_k=k_total)

        cand_concat = candidate_result["concatenated"]
        cand_concat_pre = candidate_result_pre["concatenated"]
        bench_concat = benchmark_result["concatenated"]

        subperiods = subperiod_sharpe(cand_oos_returns, SUBPERIOD_SPLIT_DATE)
        subperiods_pre = subperiod_sharpe(cand_oos_returns_pre, SUBPERIOD_SPLIT_DATE)

        verdicts, all_pass = compute_verdicts(cand_concat, bench_concat, dsr_result.dsr, k_total, n_windows, subperiods)
        verdicts_pre, all_pass_pre = compute_verdicts(
            cand_concat_pre, bench_concat, dsr_result_pre.dsr, k_total, n_windows, subperiods_pre
        )
        n_trades = cand_concat["n_trades_closed"]

        # --- comparaison des verdicts corrigé vs pré-correctif, critère par critère (mission de
        # suivi de l'audit : "vérifie et confirme qu'aucun des 15 verdicts ne bascule").
        verdict_flip_by_criterion = {
            crit: bool(verdicts[crit]["pass"] != verdicts_pre[crit]["pass"]) for crit in verdicts
        }
        any_flip = any(verdict_flip_by_criterion.values())

        variant_results[variant_id] = {
            "universe": universe,
            "risk_profile": risk_profile,
            "cost_bps_nominal": cost_bps,
            "sim_kwargs": sim_kwargs,
            "overlay_fidelity_note": (
                "CORRECTIF audit session #3 (isSound=true, finding critique) : overlay "
                "vol-targeting PORTEFEUILLE neutralisé (vol_target_annualized=50.0, "
                "vol_coldstart_scalar=1.0), fidèle à `bot/runner.py:_risk_manager_for_wallet`. "
                "Ces chiffres 'candidate'/'dsr'/'cost_stress_test'/'honesty_analyses' sont "
                "désormais les chiffres DE DÉCISION. Les chiffres pré-correctif (double "
                "vol-targeting, jamais actif en production) sont conservés séparément dans "
                "'pre_correctif_audit_session3' pour traçabilité uniquement."
            ),
            "candidate": {
                "per_window": candidate_result["per_window"],
                "concatenated": cand_concat,
            },
            "benchmark_equal_weight_buy_hold": {
                "cost_bps": 0.0,
                "overlay": "désactivé (apply_vol_targeting=False, no_trade_band=0.0) -- inchangé par le correctif",
                "per_window": benchmark_result["per_window"],
                "concatenated": bench_concat,
            },
            "dsr": dsr_result.to_dict(),
            "cost_stress_test": {
                "profit_factor_at_nominal": cand_concat["profit_factor"],
                f"profit_factor_at_{cost_3x:.0f}bps_3x": pf_3x,
                f"profit_factor_at_{cost_5x:.0f}bps_5x": pf_5x,
                "turnover_hourly_mean_abs_weight_delta": turnover_hourly_mean,
                "turnover_annualized_8760h": turnover_annualized,
                "cost_of_drift_annualized_fraction": cost_of_drift_annualized,
            },
            "honesty_analyses": {
                "subperiods_2022_2023_vs_2024_2026": subperiods,
                "average_gross_exposure_realized_oos": cand_concat["average_exposure"],
                "unaudited_origin_comparison": {
                    "unaudited_sharpe_2026_07_23": UNAUDITED_SHARPE_ORIGIN[variant_id],
                    "audited_sharpe_oos_walkforward": cand_concat["sharpe"],
                    "delta": cand_concat["sharpe"] - UNAUDITED_SHARPE_ORIGIN[variant_id],
                    "note": (
                        "Comparaison SANS retouche de paramètre : chiffre non audité = exécution "
                        "unique sans walk-forward/DSR/audit (docs/RESEARCH-LOG.md 2026-07-23). "
                        "Causes plausibles d'écart : walk-forward (Sharpe mesuré hors-échantillon "
                        "uniquement, jamais sur la même fenêtre que le sizing), coûts pessimistes "
                        "au palier le plus défavorable de l'univers, période de données "
                        "potentiellement différente. Le double vol-targeting N'EST PLUS invoqué "
                        "ici comme cause d'écart depuis le correctif de fidélité (overlay "
                        "neutralisé, fidèle production, cf. 'overlay_fidelity_note')."
                    ),
                },
                "sma200_crossings_by_asset": {s: sma200_crossings_all[s] for s in universe},
            },
            "promotion_rules_1_2_thresholds_verdict": verdicts,
            "promotion_rules_1_2_all_pass": bool(all_pass),
            "pre_correctif_audit_session3": {
                "description": (
                    "Chiffres de la PREMIÈRE exécution de ce script (double vol-targeting "
                    "stratégie PUIS overlay portefeuille actif, sim_kwargs_pre_correctif) -- "
                    "conservés UNIQUEMENT pour traçabilité de l'audit, PLUS les chiffres de "
                    "décision depuis le correctif de fidélité production ci-dessus."
                ),
                "sim_kwargs": sim_kwargs_pre_correctif,
                "candidate_concatenated": cand_concat_pre,
                "dsr": dsr_result_pre.to_dict(),
                "subperiods_2022_2023_vs_2024_2026": subperiods_pre,
                "promotion_rules_1_2_thresholds_verdict": verdicts_pre,
                "promotion_rules_1_2_all_pass": bool(all_pass_pre),
                "verdict_flip_vs_corrected_by_criterion": verdict_flip_by_criterion,
                "any_verdict_flip": bool(any_flip),
            },
        }
        oos_returns_by_variant[variant_id] = cand_oos_returns

        print(
            f"[{variant_id}] CORRIGÉ Sharpe={cand_concat['sharpe']:.3f} PF={cand_concat['profit_factor']:.3f} "
            f"MaxDD={cand_concat['max_drawdown']:.3f} trades={n_trades} DSR={dsr_result.dsr:.3f} "
            f"all_pass={all_pass} | PRÉ-CORRECTIF Sharpe={cand_concat_pre['sharpe']:.3f} "
            f"PF={cand_concat_pre['profit_factor']:.3f} MaxDD={cand_concat_pre['max_drawdown']:.3f} "
            f"DSR={dsr_result_pre.dsr:.3f} all_pass={all_pass_pre} | any_verdict_flip={any_flip}"
        )

    correlation = cross_variant_correlation(oos_returns_by_variant)

    results = {
        "meta": {
            "candidate_id": "quasi_passif_crypto_wf_retest",
            "backlog_ref": "backtest/results/quasi_passif_crypto_wf_retest/SPEC.md (P2#13, pré-enregistrée 2026-08-10)",
            "engine": "backtest/engine.py (docs/PROMOTION-RULES.md §1.1)",
            "data_dir": str(args.data_dir),
            "all_symbols_union": ALL_SYMBOLS,
            "calendar_start": str(calendar[0]),
            "calendar_end": str(calendar[-1]),
            "n_calendar_hours": len(calendar),
            "real_data_gaps_by_symbol": d["real_gaps"],
            "n_nan_after_align_by_symbol": d["n_nan_after_align"],
            "n_windows": n_windows,
            "walkforward": f"{IS_MONTHS}m IS / {OOS_MONTHS}m OOS / pas {STEP_MONTHS}m",
            "variants": VARIANTS,
            "cost_bps_nominal_per_variant": COST_BPS_NOMINAL,
            "periods_per_year_metrics": PERIODS_PER_YEAR_HOURLY,
            "k_total": k_total,
            "k_total_detail": {
                "registry_rows": K_REGISTRY_ROWS,
                "n_windows": n_windows,
                "n_variants": len(VARIANTS),
                "n_grid_combos_per_variant": n_grid_combos,
                "formula": "K_total = registry_rows + n_variants * n_windows * n_grid_combos_per_variant (SPEC.md, commun aux 3 variantes)",
            },
            "audit_fidelity_correctif": {
                "description": (
                    "Correctif appliqué suite au finding CRITIQUE de l'audit adversarial "
                    "indépendant (session #3, isSound=true par ailleurs) : la production NE "
                    "double-applique PAS le vol-targeting. `bot/runner.py:"
                    "_risk_manager_for_wallet` neutralise le vol-targeting PORTEFEUILLE "
                    "(vol_target_annualized=50.0, vol_coldstart_scalar=1.0) -- seul le "
                    "vol-targeting INTERNE à la stratégie est réellement actif en production. "
                    "Les résultats 'variants.*.candidate'/'dsr'/'cost_stress_test'/"
                    "'honesty_analyses' ci-dessous reflètent CE correctif (chiffres de "
                    "DÉCISION) ; les chiffres pré-correctif sont conservés sous "
                    "'variants.*.pre_correctif_audit_session3' pour traçabilité."
                ),
                "production_reference": "bot/runner.py:_risk_manager_for_wallet (lignes ~545-622)",
                "overlay_vol_target_annualized_neutralized": PRODUCTION_OVERLAY_VOL_TARGET_NEUTRALIZED,
                "overlay_vol_coldstart_scalar_neutralized": PRODUCTION_OVERLAY_COLDSTART_SCALAR_NEUTRALIZED,
                "no_trade_band_unchanged": 0.05,
                "any_verdict_flip_any_variant": any(
                    variant_results[v]["pre_correctif_audit_session3"]["any_verdict_flip"] for v in VARIANTS
                ),
                "verdict_flip_detail_by_variant": {
                    v: variant_results[v]["pre_correctif_audit_session3"]["verdict_flip_vs_corrected_by_criterion"]
                    for v in VARIANTS
                },
            },
        },
        "variants": variant_results,
        "cross_variant_honesty_analyses": {
            "oos_return_correlation": correlation,
            "note": (
                "Les 3 variantes partagent BTC/ETH (prudent en est même exclusivement composé) "
                "-- une corrélation élevée signifie ~1 pari corrélé plutôt que 3 validations "
                "indépendantes, cf. SPEC.md §'Analyses d'honnêteté obligatoires'."
            ),
        },
    }

    results_path = output_dir / "results.json"
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2, default=str, ensure_ascii=False)
    print(f"\n[out] {results_path}")

    any_flip_global = results["meta"]["audit_fidelity_correctif"]["any_verdict_flip_any_variant"]
    print(f"\n[audit-correctif] any_verdict_flip_any_variant = {any_flip_global}")
    for v in VARIANTS:
        pre = variant_results[v]["pre_correctif_audit_session3"]
        print(f"  {v}: all_pass corrigé={variant_results[v]['promotion_rules_1_2_all_pass']} "
              f"pré-correctif={pre['promotion_rules_1_2_all_pass']} flips={pre['verdict_flip_vs_corrected_by_criterion']}")

    return results, output_dir


if __name__ == "__main__":
    main()
