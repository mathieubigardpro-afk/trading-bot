#!/usr/bin/env python3
"""backtest/run_btc_seasonality.py — orchestration de la candidate `btc_seasonality_2123utc`
(`backtest/SEASONALITY-BTC-SPEC.md`, pré-enregistrée 2026-09-07, backlog P1#4). Moteur commun
`backtest/engine.py` UNIQUEMENT (`docs/PROMOTION-RULES.md` §1.1) -- ce script ne réimplémente
AUCUNE logique de simulation, seulement l'orchestration walk-forward + les analyses d'honnêteté
demandées par la SPEC.

Usage :
    python3 -m backtest.run_btc_seasonality [--data-dir _data/crypto] [--output-dir ...]

AUCUNE grille hors `backtest/strategies/seasonality.PARAM_GRID` n'est testée ici (import direct
de la constante, jamais une valeur ad hoc) : 1 seule combinaison (dict vide), zéro degré de
liberté (SPEC.md §1).

--------------------------------------------------------------------------------------------
Portage inter-fenêtres ACTIVÉ par défaut (SPEC.md §2, PAS un opt-in comme
`backtest/run_funding_carry.py --carry-across-windows`)
--------------------------------------------------------------------------------------------
SPEC.md §2 : "Mode de décision pré-enregistré : portage inter-fenêtres ACTIVÉ (fidélité
production maximale)". Contrairement à `funding_carry_6majors` (où le portage est un second run
INFORMATIF opt-in, le verdict restant sur le run à plat), ICI le run NOMINAL -- celui qui
alimente `promotion_rules_1_2_thresholds_verdict` -- simule DIRECTEMENT avec
`carry_in`/`carry_out` enchaînés entre fenêtres OOS contiguës. Attendu structurel (SPEC.md §2) :
aucune position ne traverse jamais une frontière de fenêtre (positions tenues 2h, 21h-23h UTC,
jamais à cheval sur minuit UTC -- cf. `backtest/strategies/seasonality.py` docstring) ; ce script
VÉRIFIE et JOURNALISE que `carry_out` est bien vide/plat à CHAQUE frontière (`n_carry_out_not_flat_at_boundary`
dans `results.json`, attendu 0 -- un portage non vide serait une anomalie à investiguer, jamais
un choix, cf. SPEC.md §2 dernière phrase).
"""

from __future__ import annotations

import argparse
import math
import json
import sys
import time
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtest import data_hourly as bt_data  # noqa: E402
from backtest import engine  # noqa: E402
from backtest import metrics as bt_metrics  # noqa: E402
from backtest import risk_overlay  # noqa: E402
from backtest.strategies import seasonality  # noqa: E402
from bot import config as bot_cfg  # noqa: E402

UNIVERSE = ["BTC"]  # SPEC.md §1 : "Univers : BTC seul".

DEFAULT_DATA_DIR = REPO_ROOT / "_data" / "crypto"
OUTPUT_DIR = REPO_ROOT / "backtest" / "results" / "btc_seasonality_2123utc"

# Coûts (SPEC.md §2 : "palier majors de bot/config.py = 10 + 5 = 15 bps/côté") -- IMPORTÉS depuis
# bot.config, jamais un littéral magique (mission point 3).
assert "BTC" in bot_cfg.COST_TIER_MAJORS, "bot.config.COST_TIER_MAJORS a changé -- BTC doit rester palier 'majors'."
COST_BPS_NOMINAL = float(bot_cfg.COST_TIER_FEE_TAKER_BPS["majors"] + bot_cfg.COST_TIER_SLIPPAGE_PENALTY_BPS["majors"])
assert COST_BPS_NOMINAL == 15.0, f"Coût 'majors' bot.config a changé ({COST_BPS_NOMINAL} != 15.0 attendu par SPEC.md)."
COST_BPS_STRESS_3X = COST_BPS_NOMINAL * 3.0
COST_BPS_STRESS_5X = COST_BPS_NOMINAL * 5.0

IS_MONTHS = 9
OOS_MONTHS = 3
STEP_MONTHS = 3

PERIODS_PER_YEAR_HOURLY = 8760.0  # SPEC.md §2 : "periods_per_year=8760 partout".

# K_total = 13 (lignes RESEARCH-REGISTRY.json au 2026-09-07, vérifié avant exécution -- cf.
# rapport) + n_fenêtres x 1 combinaison (SPEC.md §3). Calcul dynamique ci-dessous à partir du
# nombre de fenêtres RÉELLEMENT généré (jamais ajusté au chiffre attendu par la SPEC).
K_REGISTRY_ROWS = 13
N_GRID_COMBOS = len(seasonality.PARAM_GRID)  # 1 (dict vide, zéro degré de liberté)
EXPECTED_N_WINDOWS = 15  # SPEC.md §2, "le nombre exact constaté est documenté, jamais ajusté".

PROMOTION_RULES_THRESHOLDS = {
    "sharpe_oos_min": 0.70,
    "profit_factor_oos_min": 1.15,
    "n_trades_oos_min": 80,
    "maxdd_relative_to_benchmark_max": 1.5,
    "dsr_min": 0.50,
}

SUBPERIOD_SPLIT_DATE = pd.Timestamp("2024-01-01")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    p.add_argument("--output-dir", default=str(OUTPUT_DIR))
    return p


# ------------------------------------------------------------------------------------------
# Données -- BTC seul (SPEC.md §1/§2). Calendrier canonique du chargeur horaire commun
# (`backtest/data_hourly.py`), du début des données au dernier timestamp disponible, AUCUNE
# exclusion (SPEC.md §2 : "aucune exclusion"). SPEC.md donne "~2026-08-24" comme approximation
# AVANT exécution du dernier timestamp attendu ; la borne EFFECTIVEMENT constatée dans
# `_data/crypto/BTC.csv.gz` est utilisée et documentée ci-dessous SANS ajustement (cf. mission :
# "le nombre exact constaté est documenté, jamais ajusté" -- appliqué par analogie à la borne de
# calendrier, écart signalé dans le rapport).
# ------------------------------------------------------------------------------------------


def load_all_data(data_dir: str) -> dict:
    print(f"[data] chargement BTC horaire seul depuis {data_dir} (SPEC.md §1 : univers BTC seul) ...")
    raw = bt_data.load_universe_raw(data_dir, UNIVERSE)
    calendar = bt_data.build_calendar(raw)
    print(f"[data] calendrier BTC : {calendar[0]} -> {calendar[-1]}, {len(calendar)} heures")
    real_gaps = bt_data.count_real_gaps(raw, calendar)
    print(f"[data] trous réels rencontrés (NaN avant ffill) : {real_gaps}")
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
# Métriques : periods_per_year=8760 explicite PARTOUT (SPEC.md §2), même bloc que
# `backtest/run_vol_breakout.py::summarize_hourly` (moteur non modifiable, ses défauts
# `summarize_segment` sont quotidiens -- cf. sa docstring).
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
# Walk-forward NOMINAL avec portage inter-fenêtres ACTIVÉ (SPEC.md §2 -- PAS un opt-in ici,
# cf. docstring module). Même structure de boucle que
# `backtest/run_funding_carry.py::run_walkforward(carry_across_windows=True)`, adaptée à
# l'absence de grille et enrichie de la vérification "carry_out plat à chaque frontière" exigée
# par la mission.
# ------------------------------------------------------------------------------------------


def run_walkforward_with_carry(windows, calendar, opens, closes, weights_full: pd.DataFrame, cost_bps: float, sim_kwargs: dict):
    per_window = []
    segments = []
    carry_boundary_events: List[dict] = []  # frontières NON calendaires-adjacentes (attendu : 0)
    carry_out_not_flat_events: List[dict] = []  # carry_out non vide en fin de fenêtre (attendu : 0)
    t_start = time.time()
    prev_carry_out = None
    weights_provider = lambda _params: weights_full  # noqa: E731 -- grille à 1 combo (SPEC.md §1)

    for w in windows:
        t0 = time.time()
        # `simulate_segment` exige `start_idx >= 1` -- inévitable pour la toute première fenêtre
        # (son IS commence exactement à la première heure du calendrier), même déviation
        # documentée que `backtest/run_vol_breakout.py`/`backtest/run_funding_carry.py`.
        is_start_idx_safe = max(1, w.is_start_idx)
        sel = engine.select_params_via_is(
            weights_provider,
            calendar,
            opens,
            closes,
            cost_bps,
            is_start_idx_safe,
            w.is_end_idx,
            param_grid=seasonality.PARAM_GRID,
            sim_kwargs=sim_kwargs,
        )
        chosen = sel.chosen_params  # toujours {} (SPEC.md §1, zéro degré de liberté)

        carry_in_this_window = None
        if prev_carry_out is not None:
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
                    f"[carry] fenêtre {w.index} : frontière NON contiguë avec la fenêtre "
                    f"précédente (prev_last_idx={prev_carry_out.last_idx}, "
                    f"oos_start_idx={w.oos_start_idx}) -- démarrage à plat, évènement journalisé.",
                    flush=True,
                )

        seg = engine.simulate_segment(
            calendar, weights_full, opens, closes, w.oos_start_idx, w.oos_end_idx, cost_bps,
            carry_in=carry_in_this_window, **sim_kwargs,
        )

        # Vérification/journalisation exigée par la mission : `carry_out` doit être plat (aucune
        # position ouverte) à CHAQUE frontière -- attendu structurel SPEC.md §2 (positions tenues
        # 2h, 21h-23h UTC, jamais à cheval sur minuit UTC -- les frontières de fenêtre tombent à
        # 00:00 UTC du 1er jour du mois civil de la fenêtre suivante, cf. rapport). Un portage non
        # vide est une ANOMALIE à investiguer, jamais un choix (SPEC.md §2 dernière phrase).
        carry_out_flat = seg.carry_out is None
        if not carry_out_flat:
            _detail = {
                "window_index": w.index,
                "oos_end": str(w.oos_end),
                "oos_end_idx": w.oos_end_idx,
                "weights_at_last_close": {k: float(v) for k, v in seg.carry_out.weights_at_last_close.items()},
                "n_open_lines": len(seg.carry_out.open_lines),
            }
            carry_out_not_flat_events.append(_detail)
            print(f"[carry][ANOMALIE] carry_out NON plat à la fin de la fenêtre {w.index} : {_detail}", flush=True)

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
                "carry_out_flat": carry_out_flat,
            }
        )
        per_window.append(summary)
        print(
            f"[walk-forward] fenêtre {w.index}/{len(windows) - 1} ({w.oos_start.date()} -> "
            f"{w.oos_end.date()}) : n_trades_oos={summary['n_trades_closed']} "
            f"carry_in_used={summary['carry_in_used']} carry_out_flat={carry_out_flat} "
            f"({time.time() - t0:.1f}s)",
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
        "carry_out_not_flat_events": carry_out_not_flat_events,
    }


def rerun_oos_with_carry_at_cost(windows, calendar, opens, closes, weights_full: pd.DataFrame, cost_bps: float, sim_kwargs: dict):
    """Stress de coûts (SPEC.md §2 : "PF à 3x et 5x") : re-simule le walk-forward COMPLET avec
    portage inter-fenêtres (même mode que le run nominal, seul `cost_bps` change) -- il n'y a
    PAS de "paramètres déjà choisis" à figer ici (grille à 1 combo, SPEC.md §1), contrairement à
    `run_vol_breakout.py`/`run_funding_carry.py` où le stress réutilise les `chosen_params` d'une
    sélection IS non triviale."""
    segments = []
    prev_carry_out = None
    for w in windows:
        carry_in_this_window = None
        if prev_carry_out is not None and int(prev_carry_out.last_idx) + 1 == w.oos_start_idx:
            carry_in_this_window = prev_carry_out
        seg = engine.simulate_segment(
            calendar, weights_full, opens, closes, w.oos_start_idx, w.oos_end_idx, cost_bps,
            carry_in=carry_in_this_window, **sim_kwargs,
        )
        prev_carry_out = seg.carry_out
        segments.append(seg)
    return engine.concatenate_segments(segments)


# ------------------------------------------------------------------------------------------
# Benchmark : buy & hold BTC (univers de 1, SPEC.md §2), mêmes fenêtres OOS alignées, SANS coûts
# ni overlay (même convention que `run_vol_breakout.py`/`run_funding_carry.py` : "sans overlay"
# désactive les DEUX composantes de la surcouche de risque -- vol targeting ET bande).
# ------------------------------------------------------------------------------------------


def build_benchmark_weights(calendar: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame({sym: 1.0 for sym in UNIVERSE}, index=calendar)


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
    opens = d["opens"]
    closes = d["closes"]

    # Défauts de la surcouche de risque -- vérification défensive qu'ils correspondent bien à
    # SPEC.md §2 avant de les utiliser implicitement (jamais de dérive silencieuse si
    # `backtest/risk_overlay.py`/`bot/config.py` changent).
    assert risk_overlay.DEFAULT_NO_TRADE_BAND == 0.05, "risk_overlay.DEFAULT_NO_TRADE_BAND a dérivé de SPEC.md (0.05)."
    assert risk_overlay.DEFAULT_VOL_TARGET_ANNUALIZED == 0.275, "risk_overlay.DEFAULT_VOL_TARGET_ANNUALIZED a dérivé de SPEC.md (0.275)."

    windows = engine.generate_walk_forward_windows(
        calendar, is_months=IS_MONTHS, oos_months=OOS_MONTHS, step_months=STEP_MONTHS
    )
    n_windows = len(windows)
    print(
        f"[walk-forward] {n_windows} fenêtres générées ({IS_MONTHS}m IS / {OOS_MONTHS}m OOS / "
        f"pas {STEP_MONTHS}m) -- attendu par SPEC.md §2 : ~{EXPECTED_N_WINDOWS}",
        flush=True,
    )
    n_windows_matches_spec = n_windows == EXPECTED_N_WINDOWS
    if not n_windows_matches_spec:
        print(
            f"[ALERTE] nombre de fenêtres ({n_windows}) != attendu SPEC.md (~{EXPECTED_N_WINDOWS}) "
            "-- K_total recalculé dynamiquement ci-dessous à partir du chiffre RÉEL, signalé dans le rapport.",
            flush=True,
        )

    # Contiguïté OOS structurelle du walk-forward (step_months == oos_months) -- vérifiée
    # explicitement AVANT le run (condition nécessaire à ce que le portage inter-fenêtres soit
    # actif sur toutes les frontières, cf. `run_walkforward_with_carry`).
    for k in range(1, n_windows):
        assert windows[k].oos_start_idx == windows[k - 1].oos_end_idx + 1, (
            f"Fenêtres OOS non contiguës entre {k-1} et {k} -- attendu par construction "
            "(step_months == oos_months, SPEC.md §2)."
        )

    sim_kwargs = dict(
        vol_ewma_halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
        vol_periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR,
    )

    weights_full = seasonality.generate_weight_decisions(calendar, symbols=UNIVERSE)

    print(
        f"[run] walk-forward candidate btc_seasonality_2123utc ({COST_BPS_NOMINAL:.0f} bps/côté "
        "nominal, portage inter-fenêtres ACTIVÉ -- SPEC.md §2) ...",
        flush=True,
    )
    candidate_result = run_walkforward_with_carry(windows, calendar, opens, closes, weights_full, COST_BPS_NOMINAL, sim_kwargs)

    print("[run] benchmark buy & hold BTC (sans coûts ni overlay) ...", flush=True)
    benchmark_result = run_benchmark(windows, calendar, opens, closes)

    print(f"[stress] re-simulation walk-forward (avec portage) à {COST_BPS_STRESS_3X:.0f} et {COST_BPS_STRESS_5X:.0f} bps/côté ...", flush=True)
    concat_3x = rerun_oos_with_carry_at_cost(windows, calendar, opens, closes, weights_full, COST_BPS_STRESS_3X, sim_kwargs)
    concat_5x = rerun_oos_with_carry_at_cost(windows, calendar, opens, closes, weights_full, COST_BPS_STRESS_5X, sim_kwargs)
    summary_3x = summarize_hourly(concat_3x)
    summary_5x = summarize_hourly(concat_5x)

    # --- DSR (SPEC.md §3/PROMOTION-RULES.md §1.3) -------------------------------------------
    candidate_oos_returns = candidate_result["_concatenated_result"].returns
    k_total = K_REGISTRY_ROWS + n_windows * N_GRID_COMBOS
    dsr_result = bt_metrics.deflated_sharpe_ratio(candidate_oos_returns, trials_k=k_total)

    # --- Analyses d'honnêteté ------------------------------------------------------------------
    print("[analyses] sous-périodes avant/depuis 2024-01-01 ...", flush=True)
    subperiods = subperiod_sharpe(candidate_oos_returns, SUBPERIOD_SPLIT_DATE)

    print("[analyses] corrélation vs buy & hold BTC OOS aligné ...", flush=True)
    benchmark_oos_returns = benchmark_result["_concatenated_result"].returns
    aligned_candidate, aligned_bh = candidate_oos_returns.align(benchmark_oos_returns, join="inner")
    correlation_vs_bh = float(aligned_candidate.corr(aligned_bh))

    # --- Vérification carry (mission) --------------------------------------------------------
    n_carry_boundary_events = len(candidate_result["carry_boundary_events"])
    n_carry_out_not_flat = len(candidate_result["carry_out_not_flat_events"])
    n_carry_in_used = sum(1 for pw in candidate_result["per_window"] if pw["carry_in_used"])
    print(
        f"[carry] vérification : {n_carry_boundary_events} frontière(s) non contiguë(s) "
        f"(attendu 0), {n_carry_out_not_flat} carry_out NON plat (attendu 0), "
        f"{n_carry_in_used}/{n_windows} fenêtres ayant reçu un carry_in.",
        flush=True,
    )

    # --- Seuils PROMOTION-RULES §1.2 (SPEC.md §4), un par un, verdict booléen ----------------
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
        "n_trades_oos_closed": {
            "value": cand_concat["n_trades_closed"],
            "threshold": PROMOTION_RULES_THRESHOLDS["n_trades_oos_min"],
            "rule": ">= seuil",
            "pass": bool(cand_concat["n_trades_closed"] >= PROMOTION_RULES_THRESHOLDS["n_trades_oos_min"]),
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
    beats_benchmark_sharpe = bool(
        not math.isnan(cand_concat["sharpe"]) and not math.isnan(bench_concat["sharpe"])
        and cand_concat["sharpe"] > bench_concat["sharpe"]
    )

    results = {
        "meta": {
            "candidate_id": "btc_seasonality_2123utc",
            "backlog_ref": "backtest/SEASONALITY-BTC-SPEC.md (P1#4, pré-enregistrée 2026-09-07)",
            "engine": "backtest/engine.py (docs/PROMOTION-RULES.md §1.1), portage inter-fenêtres ACTIVÉ (CARRY-EXTENSION-SPEC.md)",
            "data_dir": str(args.data_dir),
            "univers": UNIVERSE,
            "calendar_start": str(calendar[0]),
            "calendar_end": str(calendar[-1]),
            "calendar_end_vs_spec_note": (
                "SPEC.md §2 anticipait '~2026-08-24' AVANT exécution ; le dernier timestamp "
                f"RÉELLEMENT présent dans _data/crypto/BTC.csv.gz est {calendar[-1]} -- écart "
                "documenté, non ajusté (mission : le chiffre constaté prime, jamais retouché "
                "après coup)."
            ),
            "n_calendar_hours": len(calendar),
            "real_data_gaps_by_symbol": d["real_gaps"],
            "n_nan_after_align": d["n_nan_after_align"],
            "n_windows": n_windows,
            "n_windows_matches_spec_expectation": n_windows_matches_spec,
            "expected_n_windows_spec": EXPECTED_N_WINDOWS,
            "walkforward": f"{IS_MONTHS}m IS / {OOS_MONTHS}m OOS / pas {STEP_MONTHS}m",
            "param_grid": seasonality.PARAM_GRID,
            "target_open_hours_utc": sorted(seasonality.TARGET_OPEN_HOURS_UTC),
            "cost_bps_nominal_per_side": COST_BPS_NOMINAL,
            "cost_bps_source": "bot.config.COST_TIER_FEE_TAKER_BPS['majors'] + bot.config.COST_TIER_SLIPPAGE_PENALTY_BPS['majors'] (10 + 5)",
            "sim_kwargs_hourly": sim_kwargs,
            "no_trade_band": risk_overlay.DEFAULT_NO_TRADE_BAND,
            "vol_target_annualized": risk_overlay.DEFAULT_VOL_TARGET_ANNUALIZED,
            "periods_per_year_metrics": PERIODS_PER_YEAR_HOURLY,
            "k_total": k_total,
            "k_total_detail": {
                "registry_rows": K_REGISTRY_ROWS,
                "n_windows": n_windows,
                "n_grid_combos": N_GRID_COMBOS,
                "formula": "K_total = registry_rows + n_windows * n_grid_combos (PROMOTION-RULES.md §1.3, SPEC.md §3)",
            },
            "carry_verification": {
                "carry_across_windows_enabled": True,
                "note": "SPEC.md §2 : mode NOMINAL (pas un opt-in informatif comme funding_carry_6majors) -- alimente directement promotion_rules_1_2_thresholds_verdict.",
                "n_windows_with_carry_in_used": n_carry_in_used,
                "n_carry_boundary_events_non_contiguous": n_carry_boundary_events,
                "carry_boundary_events": candidate_result["carry_boundary_events"],
                "n_carry_out_not_flat_at_boundary": n_carry_out_not_flat,
                "carry_out_not_flat_events": candidate_result["carry_out_not_flat_events"],
                "expected": "n_carry_boundary_events_non_contiguous == 0 ET n_carry_out_not_flat_at_boundary == 0 (SPEC.md §2 : positions tenues 2h, jamais à cheval sur une frontière de fenêtre).",
            },
            "runtime_seconds": None,
        },
        "candidate_btc_seasonality": {
            "cost_bps": COST_BPS_NOMINAL,
            "per_window": candidate_result["per_window"],
            "concatenated": cand_concat,
        },
        "benchmark_buy_and_hold_btc": {
            "cost_bps": 0.0,
            "overlay": "désactivé (apply_vol_targeting=False, no_trade_band=0.0)",
            "per_window": benchmark_result["per_window"],
            "concatenated": bench_concat,
        },
        "dsr_candidate": dsr_result.to_dict(),
        "cost_stress_test": {
            "profit_factor_at_15bps_nominal": cand_concat["profit_factor"],
            "profit_factor_at_45bps_3x": summary_3x["profit_factor"],
            "profit_factor_at_75bps_5x": summary_5x["profit_factor"],
            "sharpe_at_15bps_nominal": cand_concat["sharpe"],
            "sharpe_at_45bps_3x": summary_3x["sharpe"],
            "sharpe_at_75bps_5x": summary_5x["sharpe"],
            "full_summary_45bps_3x": summary_3x,
            "full_summary_75bps_5x": summary_5x,
            "note": "Re-simulation walk-forward COMPLÈTE (avec portage inter-fenêtres, mode nominal), seul cost_bps change -- pas de 'params déjà choisis' à figer (grille à 1 combo, SPEC.md §1).",
        },
        "honesty_analyses": {
            "correlation_vs_buy_and_hold_btc": {
                "value": correlation_vs_bh,
                "note": "Corrélation des rendements horaires OOS candidate vs B&H BTC (mêmes fenêtres OOS alignées, intersection des index).",
            },
            "subperiods_2022_2023_vs_2024_2026": subperiods,
            "beats_benchmark_sharpe_oos_aligned": beats_benchmark_sharpe,
            "beats_benchmark_note": (
                "SPEC.md §5 : une candidate long-BTC-2h/jour est corrélée au B&H BTC -- si elle "
                "passe les 5 seuils SANS battre le Sharpe B&H BTC OOS aligné, précédent "
                "`ecartee` (valeur marginale nulle vs incumbent, session #1) applicable. Ce "
                "champ ne constitue PAS un verdict (réservé à l'orchestrateur/l'auditeur, cf. "
                "mission), seulement le fait mesuré."
            ),
        },
        "promotion_rules_1_2_thresholds_verdict": verdicts,
        "promotion_rules_1_2_all_pass": bool(all_pass),
        "adversarial_audit_1_4": {
            "isSound": None,
            "note": (
                "SPEC.md §4 exige un audit adversarial indépendant obligatoire (isSound: false "
                "= rejet automatique quels que soient les chiffres). Cet audit n'est PAS exécuté "
                "par ce script (hors périmètre de la mission d'implémentation du backtest) -- à "
                "conduire séparément avant toute décision de statut dans RESEARCH-REGISTRY.json."
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
