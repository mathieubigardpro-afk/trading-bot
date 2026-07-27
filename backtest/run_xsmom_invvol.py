#!/usr/bin/env python3
"""backtest/run_xsmom_invvol.py — Partie B de la mission : teste la candidate P0#3 du backlog
(`docs/RESEARCH-BACKLOG.md` idée #3, "Momentum actions ajusté par volatilité inverse") avec le
MOTEUR COMMUN (`backtest/engine.py` + `backtest/strategies/xsmom.py`).

Usage :
    python3 -m backtest.run_xsmom_invvol --data-dir /home/claude/mdata/data

AUCUN appel réseau, AUCUNE donnée copiée dans le dépôt (`--data-dir` pointe vers les CSV gz
externes, cf. mission). AUCUNE grille de recherche : tous les paramètres sont PRÉ-FIXÉS aux
réglages de production (cf. `backtest/strategies/xsmom.py:XsMomParams`) + `weighting` fixé par
variante testée ("equal" pour le contrôle, "inv_vol" pour la candidate) — zéro degré de liberté
nouveau, l'IS walk-forward n'est donc PAS utilisé pour une sélection (documenté explicitement
dans les résultats, `is_selection_used: false`), seulement conservé pour produire une structure
de fenêtres comparable au backtest historique de référence (`docs/RESEARCH-REGISTRY.json`,
`xs_momentum_sp100` : Sharpe OOS 0,8227, PF 1,0938, MaxDD 50,29%, 1758 trades clos, 27 fenêtres).

Deux variantes exécutées sur les MÊMES fenêtres OOS concaténées :
  - `equal`   : reproduction exacte du réglage de production (contrôle -- valide le moteur par
    comparaison au backtest historique).
  - `inv_vol` : la candidate (pondération inverse-volatilité, 63 jours de bourse).
Plus un benchmark SPY buy & hold sur les mêmes fenêtres.

Sorties : `backtest/results/xs_momentum_invvol_sp100/results.json` + `REPORT.md`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtest import data as bt_data  # noqa: E402
from backtest import engine  # noqa: E402
from backtest import metrics as bt_metrics  # noqa: E402
from backtest.strategies import xsmom  # noqa: E402
from bot.strategies.xs_momentum_sp100 import UNIVERSE_SP100  # noqa: E402

HISTORICAL_REFERENCE = {
    "source": "docs/RESEARCH-REGISTRY.json:xs_momentum_sp100",
    "sharpe_oos": 0.8227,
    "profit_factor_oos": 1.0938,
    "max_drawdown_oos_pct": 50.29,
    "n_trades_oos_closed": 1758,
    "benchmark_sharpe_oos_aligned": 0.5044,
    "n_windows": 27,
    "walkforward": "36m IS / 12m OOS, 27 fenêtres",
}

PROMOTION_RULES_THRESHOLDS = {
    "sharpe_oos_min": 0.70,
    "profit_factor_oos_min": 1.15,
    "n_trades_oos_min": 80,
    "maxdd_relative_to_benchmark_max": 1.5,
    "dsr_min": 0.50,
}

# docs/RESEARCH-REGISTRY.json compte 9 lignes au moment de ce test + 1 seule combinaison
# interne (aucune grille : "equal" est le CONTRÔLE de reproduction du réglage de production
# déjà existant, pas une combinaison candidate concurrente ; seule "inv_vol" est une
# combinaison nouvelle jamais testée) -- cf. docs/PROMOTION-RULES.md §1.3.
K_TOTAL_DEFAULT = 10


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", required=True, help="répertoire contenant equities/*.csv.gz et etf/*.csv.gz")
    p.add_argument("--cost-bps-equity", type=float, default=5.0, help="coût bps/côté actions (PROMOTION-RULES §1.1)")
    p.add_argument("--cost-bps-benchmark", type=float, default=3.0, help="coût bps/côté ETF liquide (SPY)")
    p.add_argument("--is-months", type=int, default=36)
    p.add_argument("--oos-months", type=int, default=12)
    p.add_argument("--step-months", type=int, default=12)
    p.add_argument("--k-total", type=int, default=K_TOTAL_DEFAULT)
    p.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "backtest" / "results" / "xs_momentum_invvol_sp100"),
    )
    return p


def load_all_data(data_dir: str):
    print(f"[data] chargement de {len(UNIVERSE_SP100)} tickers S&P100 + SPY depuis {data_dir} ...")
    raw_equities = bt_data.load_universe_raw(data_dir, UNIVERSE_SP100, subdir="equities")
    spy_raw = bt_data.load_symbol(data_dir, "SPY", subdir="etf")
    calendar = bt_data.build_calendar(spy_raw)
    print(f"[data] calendrier canonique (SPY) : {calendar[0].date()} -> {calendar[-1].date()}, {len(calendar)} jours de bourse")

    aligned_equities = bt_data.align_universe_to_calendar(raw_equities, calendar)
    opens = bt_data.opens_panel(aligned_equities, UNIVERSE_SP100)
    closes = bt_data.closes_panel(aligned_equities, UNIVERSE_SP100)

    spy_aligned = bt_data.align_to_calendar(spy_raw, calendar)
    spy_opens = spy_aligned[["open"]].rename(columns={"open": "SPY"})
    spy_closes = spy_aligned[["close"]].rename(columns={"close": "SPY"})

    raw_closes = {sym: df["close"] for sym, df in raw_equities.items()}
    spy_raw_close = spy_raw["close"]

    return {
        "calendar": calendar,
        "opens": opens,
        "closes": closes,
        "spy_opens": spy_opens,
        "spy_closes": spy_closes,
        "raw_closes": raw_closes,
        "spy_raw_close": spy_raw_close,
    }


def build_benchmark_weights(calendar: pd.DatetimeIndex) -> pd.DataFrame:
    """Benchmark = SPY 100% en permanence (buy & hold). Constant sur tout le calendrier : une
    fois la position initiale prise à l'entrée d'une fenêtre, `simulate_segment` ne génère plus
    aucun trade supplémentaire (le poids cible recalculé chaque jour à partir de l'équity
    courante redonne exactement le même nombre d'actions, cf. démonstration dans
    `backtest/engine.py` docstring et `backtest/tests/test_engine.py`) -- un seul coût d'entrée
    par fenêtre, pas de coût de maintenance artificiel."""
    return pd.DataFrame({"SPY": 1.0}, index=calendar)


def run_variant_over_windows(
    weighting: str,
    windows,
    calendar,
    raw_closes,
    spy_raw_close,
    opens,
    closes,
    cost_bps: float,
):
    params = xsmom.XsMomParams(weighting=weighting)
    weights_decided = xsmom.generate_weight_decisions(raw_closes, spy_raw_close, calendar, params)

    per_window = []
    segments = []
    for w in windows:
        seg = engine.simulate_segment(
            calendar, weights_decided, opens, closes, w.oos_start_idx, w.oos_end_idx, cost_bps
        )
        segments.append(seg)
        summary = engine.summarize_segment(seg)
        summary.update(
            {
                "window_index": w.index,
                "is_start": str(w.is_start.date()),
                "is_end": str(w.is_end.date()),
                "oos_start": str(w.oos_start.date()),
                "oos_end": str(w.oos_end.date()),
            }
        )
        per_window.append(summary)

    concatenated = engine.concatenate_segments(segments)
    concat_summary = engine.summarize_segment(concatenated)
    return {
        "weighting": weighting,
        "cost_bps": cost_bps,
        "per_window": per_window,
        "concatenated": concat_summary,
        "_segments": segments,
        "_concatenated_result": concatenated,
    }


def run_benchmark_over_windows(windows, calendar, spy_opens, spy_closes, cost_bps: float):
    weights_decided = build_benchmark_weights(calendar)
    per_window = []
    segments = []
    for w in windows:
        seg = engine.simulate_segment(
            calendar, weights_decided, spy_opens, spy_closes, w.oos_start_idx, w.oos_end_idx, cost_bps
        )
        segments.append(seg)
        summary = engine.summarize_segment(seg)
        summary.update(
            {
                "window_index": w.index,
                "oos_start": str(w.oos_start.date()),
                "oos_end": str(w.oos_end.date()),
            }
        )
        per_window.append(summary)
    concatenated = engine.concatenate_segments(segments)
    concat_summary = engine.summarize_segment(concatenated)
    return {
        "cost_bps": cost_bps,
        "per_window": per_window,
        "concatenated": concat_summary,
        "_segments": segments,
        "_concatenated_result": concatenated,
    }


def cost_stress_profit_factor(
    weighting: str, windows, calendar, raw_closes, spy_raw_close, opens, closes, cost_bps: float
) -> float:
    params = xsmom.XsMomParams(weighting=weighting)
    weights_decided = xsmom.generate_weight_decisions(raw_closes, spy_raw_close, calendar, params)
    segments = [
        engine.simulate_segment(calendar, weights_decided, opens, closes, w.oos_start_idx, w.oos_end_idx, cost_bps)
        for w in windows
    ]
    concatenated = engine.concatenate_segments(segments)
    pnls = [e["pnl"] for e in concatenated.realized_events]
    return bt_metrics.profit_factor(pnls)


def relative_deviation(observed: float, reference: float) -> float:
    if reference == 0 or math_isnan(reference) or math_isnan(observed):
        return float("nan")
    return abs(observed - reference) / abs(reference)


def math_isnan(x) -> bool:
    try:
        return x != x  # NaN != NaN
    except TypeError:
        return False


def count_regime_off_days(spy_raw_close: pd.Series, calendar: pd.DatetimeIndex, oos_dates: pd.DatetimeIndex) -> int:
    regime = xsmom._regime_series(spy_raw_close, calendar, 200)
    regime_oos = regime.reindex(oos_dates)
    return int((~regime_oos.fillna(False)).sum())


def main():
    args = build_parser().parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    d = load_all_data(args.data_dir)
    calendar = d["calendar"]

    windows = engine.generate_walk_forward_windows(
        calendar, is_months=args.is_months, oos_months=args.oos_months, step_months=args.step_months
    )
    print(f"[walk-forward] {len(windows)} fenêtres générées ({args.is_months}m IS / {args.oos_months}m OOS / pas {args.step_months}m)")

    print("[run] variante equal-weight (contrôle/reproduction)...")
    equal_result = run_variant_over_windows(
        "equal", windows, calendar, d["raw_closes"], d["spy_raw_close"], d["opens"], d["closes"], args.cost_bps_equity
    )
    print("[run] variante inv_vol (candidate)...")
    invvol_result = run_variant_over_windows(
        "inv_vol", windows, calendar, d["raw_closes"], d["spy_raw_close"], d["opens"], d["closes"], args.cost_bps_equity
    )
    print("[run] benchmark SPY buy & hold...")
    benchmark_result = run_benchmark_over_windows(windows, calendar, d["spy_opens"], d["spy_closes"], args.cost_bps_benchmark)

    # --- Stress de coûts (candidate uniquement) --------------------------------------------
    print("[stress] profit factor à 15 et 25 bps/côté (candidate inv_vol)...")
    pf_15bps = cost_stress_profit_factor(
        "inv_vol", windows, calendar, d["raw_closes"], d["spy_raw_close"], d["opens"], d["closes"], 15.0
    )
    pf_25bps = cost_stress_profit_factor(
        "inv_vol", windows, calendar, d["raw_closes"], d["spy_raw_close"], d["opens"], d["closes"], 25.0
    )

    # --- DSR candidate (K_total, docs/PROMOTION-RULES.md §1.3) -----------------------------
    invvol_oos_returns = invvol_result["_concatenated_result"].returns
    dsr_result = bt_metrics.deflated_sharpe_ratio(invvol_oos_returns, trials_k=args.k_total)

    # --- Information Ratio inv_vol vs equal (mêmes dates OOS concaténées) -------------------
    equal_oos_returns = equal_result["_concatenated_result"].returns
    ir_invvol_vs_equal = bt_metrics.information_ratio(invvol_oos_returns, equal_oos_returns)

    # --- Baseline vs historique --------------------------------------------------------------
    eq_concat = equal_result["concatenated"]
    baseline_dev_sharpe = relative_deviation(eq_concat["sharpe"], HISTORICAL_REFERENCE["sharpe_oos"])
    baseline_dev_pf = relative_deviation(eq_concat["profit_factor"], HISTORICAL_REFERENCE["profit_factor_oos"])
    baseline_dev_maxdd = relative_deviation(eq_concat["max_drawdown"] * 100.0, HISTORICAL_REFERENCE["max_drawdown_oos_pct"])
    baseline_dev_ntrades = relative_deviation(eq_concat["n_trades_closed"], HISTORICAL_REFERENCE["n_trades_oos_closed"])

    # --- Régimes de marché rencontrés en OOS (justification §1.2 "slow strategies" si besoin) -
    all_oos_dates = pd.DatetimeIndex(invvol_result["_concatenated_result"].returns.index)
    n_regime_off_days = count_regime_off_days(d["spy_raw_close"], calendar, all_oos_dates)
    decision_mask_series = pd.Series(xsmom._decision_day_mask(calendar), index=calendar)
    n_decision_cycles_oos = int(decision_mask_series.reindex(all_oos_dates).fillna(False).sum())

    # --- Seuils PROMOTION-RULES §1.2, un par un, verdict booléen candidate inv_vol ----------
    inv_concat = invvol_result["concatenated"]
    bench_concat = benchmark_result["concatenated"]
    maxdd_ratio = (
        inv_concat["max_drawdown"] / bench_concat["max_drawdown"]
        if bench_concat["max_drawdown"] not in (0, None) and not math_isnan(bench_concat["max_drawdown"])
        else float("nan")
    )

    verdicts = {
        "sharpe_oos": {
            "value": inv_concat["sharpe"],
            "threshold": PROMOTION_RULES_THRESHOLDS["sharpe_oos_min"],
            "rule": ">= seuil",
            "pass": bool(inv_concat["sharpe"] >= PROMOTION_RULES_THRESHOLDS["sharpe_oos_min"]),
        },
        "profit_factor_oos": {
            "value": inv_concat["profit_factor"],
            "threshold": PROMOTION_RULES_THRESHOLDS["profit_factor_oos_min"],
            "rule": "> seuil",
            "pass": bool(inv_concat["profit_factor"] > PROMOTION_RULES_THRESHOLDS["profit_factor_oos_min"]),
        },
        "n_trades_oos_closed": {
            "value": inv_concat["n_trades_closed"],
            "threshold": PROMOTION_RULES_THRESHOLDS["n_trades_oos_min"],
            "rule": ">= seuil (ou justification slow-strategy)",
            "pass": bool(inv_concat["n_trades_closed"] >= PROMOTION_RULES_THRESHOLDS["n_trades_oos_min"]),
            "slow_strategy_justification": {
                "n_decision_cycles_oos": int(n_decision_cycles_oos),
                "n_decision_cycles_min_required": 24,
                "n_regime_off_days_oos": int(n_regime_off_days),
                "regimes_distincts_observes": "haussier ET baissier (jours de régime SPY<SMA200 observés en OOS)"
                if n_regime_off_days > 0
                else "haussier uniquement (aucun jour de régime off détecté en OOS)",
            },
        },
        "maxdd_relative_to_benchmark": {
            "value": maxdd_ratio,
            "threshold": PROMOTION_RULES_THRESHOLDS["maxdd_relative_to_benchmark_max"],
            "rule": "<= seuil (maxdd_candidate / maxdd_benchmark_OOS_aligné)",
            "pass": bool(not math_isnan(maxdd_ratio) and maxdd_ratio <= PROMOTION_RULES_THRESHOLDS["maxdd_relative_to_benchmark_max"]),
        },
        "dsr": {
            "value": dsr_result.dsr,
            "threshold": PROMOTION_RULES_THRESHOLDS["dsr_min"],
            "rule": ">= seuil",
            "k_total": args.k_total,
            "pass": bool(not math_isnan(dsr_result.dsr) and dsr_result.dsr >= PROMOTION_RULES_THRESHOLDS["dsr_min"]),
        },
    }
    all_pass = all(v["pass"] for v in verdicts.values())

    results = {
        "meta": {
            "candidate_id": "xs_momentum_sp100_inv_vol",
            "backlog_ref": "docs/RESEARCH-BACKLOG.md idée #3 (P0)",
            "engine": "backtest/engine.py (docs/PROMOTION-RULES.md §1.1)",
            "data_dir": str(args.data_dir),
            "univers": f"S&P100, {len(UNIVERSE_SP100)} titres, constituants ACTUELS (biais du survivant, cf. REPORT.md)",
            "calendar_start": str(calendar[0].date()),
            "calendar_end": str(calendar[-1].date()),
            "n_windows": len(windows),
            "walkforward": f"{args.is_months}m IS / {args.oos_months}m OOS / pas {args.step_months}m",
            "is_selection_used": False,
            "is_selection_note": (
                "Aucune grille de recherche : tous les paramètres sont pré-fixés aux réglages de "
                "production (top_k=10, skip=21j, lookback=126j, régime SPY>SMA200, gel mensuel) + "
                "vol_lookback_days=63 documenté par le SPEC pour weighting='inv_vol'. L'IS de chaque "
                "fenêtre walk-forward n'est donc PAS utilisé pour sélectionner un paramètre -- choix "
                "délibérément conservateur (zéro degré de liberté nouveau, K interne=1), la structure "
                "de fenêtres est conservée uniquement pour produire une équity OOS concaténée "
                "comparable au backtest historique de xs_momentum_sp100."
            ),
            "cost_bps_equity_per_side": args.cost_bps_equity,
            "cost_bps_benchmark_per_side": args.cost_bps_benchmark,
            "k_total": args.k_total,
            "k_total_note": (
                "9 entrées docs/RESEARCH-REGISTRY.json au moment de ce test + 1 combinaison interne "
                "nouvelle ('inv_vol' -- 'equal' est un CONTROLE de reproduction du réglage de "
                "production déjà existant, pas une combinaison candidate additionnelle), cf. "
                "docs/PROMOTION-RULES.md §1.3."
            ),
        },
        "historical_reference_xs_momentum_sp100_equal_weight": HISTORICAL_REFERENCE,
        "baseline_equal_weight_vs_historical": {
            "sharpe_oos": eq_concat["sharpe"],
            "profit_factor_oos": eq_concat["profit_factor"],
            "max_drawdown_oos_pct": eq_concat["max_drawdown"] * 100.0,
            "n_trades_oos_closed": eq_concat["n_trades_closed"],
            "n_windows": len(windows),
            "relative_deviation_vs_historical": {
                "sharpe": baseline_dev_sharpe,
                "profit_factor": baseline_dev_pf,
                "max_drawdown_pct": baseline_dev_maxdd,
                "n_trades_closed": baseline_dev_ntrades,
            },
            "deviation_flag_gt_25pct_sharpe": bool(not math_isnan(baseline_dev_sharpe) and baseline_dev_sharpe > 0.25),
            "investigation_note": (
                "Écart Sharpe (+25.6%) et profit factor (+66.5%) supérieurs à la tolérance "
                "anticipée (~15%, révisions de données yfinance) -- SIGNALÉ, PAS masqué (cf. "
                "REPORT.md §'Validation du moteur'). Testé et EXCLU : (1) fenêtre différente -- "
                "restreindre aux 27 premières fenêtres (même période ~1996-2023 que la référence) "
                "ne change quasiment rien (Sharpe 1.033 vs 1.034 sur 30 fenêtres) ; (2) mécanique "
                "de coûts -- la sensibilité aux coûts est maintenant vérifiée correcte (profit "
                "factor candidate décroît de 1.72 à 1.50 entre 5 et 25 bps/côté). MaxDD (48.7% vs "
                "50.3%, écart 3.2%) et exposition moyenne quasi identiques -- la MÉCANIQUE de la "
                "stratégie (timing du régime, sélection, tenue des positions) semble correctement "
                "reproduite. La cause la plus probable restante est une différence de SOURCE DE "
                "DONNÉES (ajustements dividendes/splits, révisions de prix) entre ce jeu de "
                "données et celui utilisé par le backtest historique original (`bt-final/xs-"
                "momentum-sp100/`, absent de ce dépôt -- impossible à comparer directement) et/ou "
                "une convention de calcul du profit factor différente dans ce script disparu. "
                "Conclusion : le moteur est interne cohérent (tests dédiés backtest/tests/ tous "
                "verts) mais sa fidélité BIT-A-BIT au chiffre historique n'est PAS démontrée ici -- "
                "à traiter comme une réserve ouverte, pas comme une validation complète."
            ),
        },
        "equal_weight_control": {
            "cost_bps": equal_result["cost_bps"],
            "per_window": equal_result["per_window"],
            "concatenated": equal_result["concatenated"],
        },
        "inv_vol_candidate": {
            "cost_bps": invvol_result["cost_bps"],
            "per_window": invvol_result["per_window"],
            "concatenated": invvol_result["concatenated"],
        },
        "benchmark_spy_buy_hold": {
            "cost_bps": benchmark_result["cost_bps"],
            "per_window": benchmark_result["per_window"],
            "concatenated": benchmark_result["concatenated"],
        },
        "candidate_vs_control_comparison": {
            "sharpe_invvol_minus_equal": inv_concat["sharpe"] - eq_concat["sharpe"],
            "sortino_invvol_minus_equal": inv_concat["sortino"] - eq_concat["sortino"],
            "maxdd_invvol_minus_equal_pct": (inv_concat["max_drawdown"] - eq_concat["max_drawdown"]) * 100.0,
            "information_ratio_invvol_vs_equal": ir_invvol_vs_equal,
        },
        "cost_sensitivity_stress_test_candidate_invvol": {
            "profit_factor_at_5bps_nominal": inv_concat["profit_factor"],
            "profit_factor_at_15bps_3x": pf_15bps,
            "profit_factor_at_25bps_5x": pf_25bps,
        },
        "dsr_candidate_invvol": dsr_result.to_dict(),
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
