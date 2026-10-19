#!/usr/bin/env python3
"""backtest/run_lowvol_sp100.py — orchestration de la Porte 1 de `lowvol_sp100`
(`backtest/LOWVOL-SP100-SPEC.md`, pré-enregistrée en session #11 et committée AVANT exécution,
commit 35df9b5). Moteur commun `backtest/engine.py` UNIQUEMENT (`docs/PROMOTION-RULES.md` §1.1) :
ce script ne réimplémente AUCUNE logique de simulation pour les chiffres de décision. Il
orchestre le walk-forward, les analyses d'honnêteté demandées par la SPEC et l'annexe
`band_fidelity` (`docs/BAND-FIDELITY-SPEC.md` §3.2).

Usage :
    python3 -m backtest.run_lowvol_sp100 --data-dir /home/claude/mdata/data
    python3 -m backtest.run_lowvol_sp100 --data-dir ... --smoke --output-dir <tmp>   # 3 premières fenêtres

AUCUN appel réseau, AUCUNE donnée copiée dans le dépôt (`--data-dir` externe).

Tout est FIGÉ par la SPEC (aucun paramètre de recherche n'est exposé en ligne de commande) :
  - grille `L ∈ {63, 252}` × `N ∈ {20, 30}` (import de `lowvol.PARAM_GRID`, jamais redéfinie) ;
  - walk-forward 36 mois IS / 12 mois OOS / pas 12 mois, sélection IS par Sharpe
    (`engine.select_params_via_is`), portage inter-fenêtres ACTIVÉ pour la candidate ET le
    contrôle (et pour le run S2 et le stress de coûts, même pipeline) ;
  - `apply_vol_targeting=False` (SPEC §1, `sizing_interne: false`, SIZING-FIDELITY-SPEC §3.2),
    `no_trade_band=0.05`, `periods_per_year=252` ;
  - coûts 5 bps/côté (SPY benchmark : 3 bps/côté) ; stress 15 et 25 bps ;
  - `K_total = 16 + n_fenêtres × 4 + n_fenêtres × 1` (SPEC §3).

Sorties : `backtest/results/lowvol_sp100/results.json` + `REPORT.md`.
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import sys
import time
import warnings
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtest import data as bt_data  # noqa: E402
from backtest import engine  # noqa: E402
from backtest import metrics as bt_metrics  # noqa: E402
from backtest.strategies import lowvol  # noqa: E402
from bot import config as bot_cfg  # noqa: E402
from bot.strategies.xs_momentum_sp100 import UNIVERSE_SP100  # noqa: E402

CANDIDATE_ID = "lowvol_sp100"

# --- Constantes figées par la SPEC -------------------------------------------------------------
IS_MONTHS = 36
OOS_MONTHS = 12
STEP_MONTHS = 12
COST_BPS_EQUITY = 5.0
COST_BPS_BENCHMARK = 3.0
COST_BPS_STRESS_3X = 15.0
COST_BPS_STRESS_5X = 25.0
NO_TRADE_BAND = 0.05  # sémantique S1 du moteur (BAND-FIDELITY-SPEC §2)
APPLY_VOL_TARGETING = False  # SPEC §1 : sizing_interne=false (SIZING-FIDELITY-SPEC §3.2)
PERIODS_PER_YEAR = 252
CAPITAL_ALLOC_PCT = 0.30  # SPEC §1 : a = 0,30 (BAND-FIDELITY-SPEC §3.1)
K_REGISTRY_ROWS = 16  # SPEC §3 : lignes de RESEARCH-REGISTRY.json à ce jour (revérifié à l'exécution)
N_GRID_COMBOS_CANDIDATE = len(lowvol.PARAM_GRID)  # 4
N_GRID_COMBOS_CONTROL = 1
EXPECTED_N_WINDOWS = 30  # SPEC §2 (~30 attendues, le chiffre exact constaté est documenté)

PROMOTION_RULES_THRESHOLDS = {
    "sharpe_oos_min": 0.70,
    "profit_factor_oos_min": 1.15,
    "n_trades_oos_min": 80,
    "maxdd_relative_to_benchmark_max": 1.5,
    "dsr_min": 0.50,
}

SUBPERIOD_SPLITS = [
    ("avant_2010-01-01", None, pd.Timestamp("2010-01-01")),
    ("depuis_2010-01-01", pd.Timestamp("2010-01-01"), None),
    ("depuis_2020-01-01", pd.Timestamp("2020-01-01"), None),
]

DEFAULT_OUTPUT_DIR = REPO_ROOT / "backtest" / "results" / "lowvol_sp100"

# Contexte partagé (rempli dans main() AVANT la création du Pool 'fork' — hérité par les workers).
_CTX: dict = {}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", required=True, help="répertoire contenant equities/*.csv.gz et etf/SPY.csv.gz")
    p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    p.add_argument(
        "--smoke",
        action="store_true",
        default=False,
        help="3 premières fenêtres seulement (test de plomberie) — exige un --output-dir explicite hors dépôt",
    )
    p.add_argument("--processes", type=int, default=2, help="nombre de processus (fork) pour sélection IS / chaînes OOS")
    return p


# ------------------------------------------------------------------------------------------
# Données
# ------------------------------------------------------------------------------------------


def load_all_data(data_dir: str) -> dict:
    universe = lowvol.effective_universe()
    print(
        f"[data] univers : {len(UNIVERSE_SP100)} titres UNIVERSE_SP100 - exclus {list(lowvol.EXCLUDED_TICKERS)} "
        f"présents dans l'univers : {[t for t in lowvol.EXCLUDED_TICKERS if t in UNIVERSE_SP100]} "
        f"=> univers effectif {len(universe)} titres",
        flush=True,
    )
    raw_equities = bt_data.load_universe_raw(data_dir, universe, subdir="equities")
    spy_raw = bt_data.load_symbol(data_dir, "SPY", subdir="etf")
    calendar = bt_data.build_calendar(spy_raw)
    print(f"[data] calendrier canonique (SPY) : {calendar[0].date()} -> {calendar[-1].date()}, {len(calendar)} jours de bourse", flush=True)
    aligned = bt_data.align_universe_to_calendar(raw_equities, calendar)
    opens = bt_data.opens_panel(aligned, universe)
    closes = bt_data.closes_panel(aligned, universe)
    spy_aligned = bt_data.align_to_calendar(spy_raw, calendar)
    spy_opens = spy_aligned[["open"]].rename(columns={"open": "SPY"})
    spy_closes = spy_aligned[["close"]].rename(columns={"close": "SPY"})
    raw_closes = {sym: df["close"] for sym, df in raw_equities.items()}

    # Diagnostics de données (honnêteté) : trous après première cotation, NaN restants.
    n_gap_rows = 0
    n_nan_after_listing = 0
    for sym, df in raw_equities.items():
        after = calendar[calendar >= df.index[0]]
        n_gap_rows += int((~after.isin(df.index)).sum())
        n_nan_after_listing += int(closes[sym].loc[after[0]:].isna().sum()) if len(after) else 0
    return {
        "universe": universe,
        "calendar": calendar,
        "opens": opens,
        "closes": closes,
        "spy_opens": spy_opens,
        "spy_closes": spy_closes,
        "raw_closes": raw_closes,
        "data_diagnostics": {
            "n_calendar_days": len(calendar),
            "n_missing_rows_after_first_listing_all_tickers": n_gap_rows,
            "n_nan_close_cells_after_first_listing_in_calendar": n_nan_after_listing,
        },
    }


# ------------------------------------------------------------------------------------------
# Simulation (moteur commun) — chaînes OOS avec portage inter-fenêtres
# ------------------------------------------------------------------------------------------


def _window_meta(w) -> dict:
    return {
        "window_index": w.index,
        "is_start": str(w.is_start.date()),
        "is_end": str(w.is_end.date()),
        "oos_start": str(w.oos_start.date()),
        "oos_end": str(w.oos_end.date()),
    }


def simulate_chain(
    windows,
    chosen_list: List[dict],
    provider,
    cost_bps: float,
    band: float = NO_TRADE_BAND,
    scale: float = 1.0,
    carry_across_windows: bool = True,
):
    """Simule le segment OOS de CHAQUE fenêtre avec la combinaison DÉJÀ choisie pour cette fenêtre
    (jamais une nouvelle sélection), portage inter-fenêtres ACTIF : `carry_in(k+1) = carry_out(k)`
    si la frontière est calendaire-adjacente, sinon départ à plat + évènement journalisé
    (CARRY-EXTENSION-SPEC.md §1.3). `scale`/`band` servent au run S2 (poids a·W, bande 0,05·a)."""
    cal, opens, closes = _CTX["calendar"], _CTX["opens"], _CTX["closes"]
    segments = []
    events: List[dict] = []
    prev_carry = None
    for w, chosen in zip(windows, chosen_list):
        W = provider(chosen)
        if scale != 1.0:
            W = W * scale
        carry_in = None
        if carry_across_windows and prev_carry is not None:
            if int(prev_carry.last_idx) + 1 == w.oos_start_idx:
                carry_in = prev_carry
            else:
                events.append(
                    {
                        "window_index": w.index,
                        "prev_carry_last_idx": int(prev_carry.last_idx),
                        "oos_start_idx": w.oos_start_idx,
                        "reason": "frontière non calendaire-adjacente — départ à plat (CARRY-EXTENSION-SPEC.md §1.3)",
                    }
                )
        seg = engine.simulate_segment(
            cal,
            W,
            opens,
            closes,
            w.oos_start_idx,
            w.oos_end_idx,
            cost_bps,
            no_trade_band=band,
            apply_vol_targeting=False,
            carry_in=carry_in,
        )
        if carry_across_windows:
            prev_carry = seg.carry_out
        segments.append((seg, carry_in is not None))
    return segments, events


def run_benchmark_chain(windows, cost_bps: float):
    """SPY buy & hold, mêmes fenêtres OOS, capital remis à plat à chaque fenêtre (précédent
    `run_xsmom_invvol.py` : un seul coût d'entrée par fenêtre, repère statique non soumis au
    portage). Poids 1,0 constant."""
    cal = _CTX["calendar"]
    weights = pd.DataFrame({"SPY": 1.0}, index=cal)
    segments = []
    for w in windows:
        seg = engine.simulate_segment(
            cal,
            weights,
            _CTX["spy_opens"],
            _CTX["spy_closes"],
            w.oos_start_idx,
            w.oos_end_idx,
            cost_bps,
            no_trade_band=NO_TRADE_BAND,
            apply_vol_targeting=False,
        )
        segments.append((seg, False))
    return segments, []


def _job(spec: dict):
    """Exécuté dans un worker (fork) : renvoie les segments d'une chaîne OOS."""
    kind = spec["kind"]
    windows = _CTX["windows"]
    t0 = time.time()
    if kind == "bench":
        segs, events = run_benchmark_chain(windows, COST_BPS_BENCHMARK)
    else:
        provider = _CTX["providers"][spec["provider"]]
        segs, events = simulate_chain(
            windows,
            spec["chosen"],
            provider,
            spec["cost_bps"],
            band=spec.get("band", NO_TRADE_BAND),
            scale=spec.get("scale", 1.0),
        )
    return spec["name"], segs, events, time.time() - t0


def _select_window(w):
    """Sélection IS (SPEC §1) pour une fenêtre : `engine.select_params_via_is` sur la grille."""
    # `simulate_segment` exige start_idx >= 1 : la toute première fenêtre démarre à l'index 0
    # (déviation documentée des précédents run_*.py, appliquée IDENTIQUEMENT à toutes les combos).
    is_start_idx_safe = max(1, w.is_start_idx)
    sel = engine.select_params_via_is(
        _CTX["providers"]["candidate"],
        _CTX["calendar"],
        _CTX["opens"],
        _CTX["closes"],
        COST_BPS_EQUITY,
        is_start_idx_safe,
        w.is_end_idx,
        param_grid=lowvol.PARAM_GRID,
        sim_kwargs={"no_trade_band": NO_TRADE_BAND, "apply_vol_targeting": False},
    )
    return {
        "window_index": w.index,
        "chosen_params": sel.chosen_params,
        "is_sharpe_chosen": sel.is_sharpe,
        "is_candidates": sel.all_candidates,
    }


# ------------------------------------------------------------------------------------------
# Résumés
# ------------------------------------------------------------------------------------------


def summarize_chain(windows, segments_flags, chosen_list=None, is_infos=None) -> dict:
    per_window = []
    segs = [s for s, _ in segments_flags]
    for k, (w, (seg, carried)) in enumerate(zip(windows, segments_flags)):
        summ = engine.summarize_segment(seg)
        summ.update(_window_meta(w))
        summ["carry_in_used"] = bool(carried)
        if chosen_list is not None:
            summ["chosen_params"] = chosen_list[k]
        if is_infos is not None:
            summ["is_sharpe_chosen"] = is_infos[k]["is_sharpe_chosen"]
            summ["is_candidates"] = is_infos[k]["is_candidates"]
        per_window.append(summ)
    concatenated = engine.concatenate_segments(segs)
    return {
        "per_window": per_window,
        "concatenated": engine.summarize_segment(concatenated),
        "_segments": segs,
        "_concatenated_result": concatenated,
    }


def exposure_diagnostics(concatenated) -> dict:
    ge = pd.Series(concatenated.gross_exposure, dtype=float).dropna()
    return {
        "mean_gross_exposure": float(ge.mean()),
        "max_gross_exposure": float(ge.max()),
        "min_gross_exposure": float(ge.min()),
        "share_days_gross_exposure_gt_1_05": float((ge > 1.05).mean()),
        "share_days_zero_exposure": float((ge < 1e-9).mean()),
        "note": (
            "Exposition brute = Σ|shares|·close / equity en clôture. > 1 = position longue supérieure "
            "à l'équity (cash négatif : le moteur autorise ce levier implicite sans coût de financement) ; "
            "= 0 sur toute la période = jamais investi."
        ),
    }


def nan_safe(x):
    try:
        return x is None or (isinstance(x, float) and math.isnan(x))
    except TypeError:
        return False


def subperiod_sharpes(returns: pd.Series) -> dict:
    out = {}
    for name, lo, hi in SUBPERIOD_SPLITS:
        r = returns
        if lo is not None:
            r = r[r.index >= lo]
        if hi is not None:
            r = r[r.index < hi]
        out[name] = {
            "sharpe": bt_metrics.sharpe_ratio(r, periods_per_year=PERIODS_PER_YEAR),
            "n_days": int(len(r.dropna())),
        }
    return out


def corr_safe(a: pd.Series, b: pd.Series) -> float:
    a2, b2 = a.align(b, join="inner")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if a2.std() == 0 or b2.std() == 0 or len(a2) < 3:
            return float("nan")
        return float(a2.corr(b2))


def paired_comparison(cand: pd.Series, ctl: pd.Series) -> dict:
    a, c = cand.align(ctl, join="inner")
    a = a.dropna()
    c = c.reindex(a.index)
    eq_a = pd.concat([pd.Series([1.0]), (1.0 + a).cumprod()])
    eq_c = pd.concat([pd.Series([1.0]), (1.0 + c).cumprod()])
    sh_a = bt_metrics.sharpe_ratio(a)
    sh_c = bt_metrics.sharpe_ratio(c)
    so_a = bt_metrics.sortino_ratio(a)
    so_c = bt_metrics.sortino_ratio(c)
    dd_a = bt_metrics.max_drawdown(eq_a)
    dd_c = bt_metrics.max_drawdown(eq_c)
    ir = bt_metrics.information_ratio(a, c)
    control_degenerate = bool(c.abs().max() < 1e-12) or any(math.isnan(x) for x in (sh_c, so_c, dd_c))
    if control_degenerate:
        dominated = None  # non évaluable : jamais traité silencieusement comme « non dominée »
    else:
        dominated = bool(
            not any(math.isnan(x) for x in (sh_a, so_a, dd_a)) and sh_a < sh_c and so_a < so_c and dd_a > dd_c
        )
    return {
        "n_days_aligned": int(len(a)),
        "candidate": {"sharpe": sh_a, "sortino": so_a, "max_drawdown": dd_a},
        "control": {"sharpe": sh_c, "sortino": so_c, "max_drawdown": dd_c},
        "information_ratio_candidate_vs_control": ir,
        "control_degenerate": control_degenerate,
        "candidate_dominated_by_control": dominated,
        "note": (
            "Dominée (SPEC §5) = Sharpe ET Sortino ET MaxDD TOUS moins bons pour la candidate que "
            "pour le contrôle. `null` = NON ÉVALUABLE parce que le contrôle est dégénéré (jamais "
            "investi, Sharpe/Sortino NaN) : ce n'est PAS une non-domination."
        ),
    }


# ------------------------------------------------------------------------------------------
# Annexe band_fidelity (BAND-FIDELITY-SPEC §3.2) — réplica numpy instrumenté du moteur
# ------------------------------------------------------------------------------------------


def replica_chain(windows, chosen_list, provider, band: float, scale: float, cost_bps: float):
    """Réplica numpy de la boucle spot long-only du moteur (sans vol targeting), instrumenté pour
    tracer chaque décision hold/trade, avec le MÊME portage inter-fenêtres (état normalisé par
    l'équity finale : `shares/E`, `cash/E`). Validé contre `engine.simulate_segment` (équity
    fenêtre par fenêtre) par l'appelant — il ne sert QU'À compter les décisions divergentes."""
    O = _CTX["opens"].fillna(0.0).to_numpy()
    C = _CTX["closes"].fillna(0.0).to_numpy()
    k = O.shape[1]
    cost_rate = cost_bps / 10000.0
    shares = np.zeros(k)
    cash = 1.0
    out = {"traded": [], "eq_pre": [], "cur_w": [], "tgt_w": [], "equity": [], "n_held": [], "window_equity": []}
    for w, chosen in zip(windows, chosen_list):
        Wm = provider(chosen)
        if scale != 1.0:
            Wm = Wm * scale
        W = Wm.to_numpy()
        eq_w = []
        for i in range(w.oos_start_idx, w.oos_end_idx + 1):
            o, c, what = O[i], C[i], W[i - 1]
            V = shares * o
            eq_before = cash + float(V.sum())
            safe = np.where(o == 0.0, np.nan, o)
            tgt = np.nan_to_num(what * eq_before / safe, nan=0.0)
            if eq_before > 0:
                cw = V / eq_before
            else:
                cw = np.zeros(k)
            if band and band > 0:
                hold = np.abs(what - cw) < band
                tgt = np.where(hold, shares, tgt)
            trade = tgt - shares
            ch = np.abs(trade) > 1e-9
            tv = float((np.abs(trade[ch]) * o[ch]).sum())
            cost = tv * cost_rate
            cash = cash - float((trade * o).sum()) - cost
            shares = tgt
            eq = cash + float((shares * c).sum())
            eq_w.append(eq)
            out["traded"].append(ch)
            out["eq_pre"].append(eq_before)
            out["cur_w"].append(cw)
            out["tgt_w"].append(what)
            out["equity"].append(eq)
            out["n_held"].append(int((shares > 1e-9).sum()))
        out["window_equity"].append(np.array(eq_w))
        e_final = eq_w[-1]
        if e_final > 0:  # portage : capital remis à 1, positions et cash remis à l'échelle
            shares = shares / e_final
            cash = cash / e_final
    for key in ("traded", "cur_w", "tgt_w"):
        out[key] = np.array(out[key])
    for key in ("eq_pre", "equity", "n_held"):
        out[key] = np.array(out[key])
    return out


def run_lengths(mask: np.ndarray) -> int:
    """Nombre de runs consécutifs de True dans un masque 1-D."""
    m = np.asarray(mask, dtype=bool)
    if m.size == 0:
        return 0
    prev = np.concatenate([[False], m[:-1]])
    return int((m & ~prev).sum())


def cb_exposure(equity_with_base: np.ndarray, dates: pd.DatetimeIndex, cb_cfg: dict) -> dict:
    """Exposition du wallet LABO aux circuit breakers de drawdown/perte journalière sur une courbe
    d'équity wallet (base 1 en tête, `dates` alignées sur equity_with_base[1:]). Le moteur ne
    simule AUCUN CB : ceci borne ce que la production AURAIT déclenché (reste du wallet en cash)."""
    e = np.asarray(equity_with_base, dtype=float)
    peak = np.maximum.accumulate(e)
    dd = 1.0 - e / peak
    dd1 = dd[1:]
    half, flat, freeze = cb_cfg["half"], cb_cfg["flatten"], cb_cfg["freeze"]
    r = e[1:] / e[:-1] - 1.0
    above_half = dd1 > half
    above_flat = dd1 > flat
    freeze_days = r < -freeze

    def first_date(mask):
        idx = np.flatnonzero(mask)
        return str(dates[idx[0]].date()) if len(idx) else None

    return {
        "max_wallet_drawdown": float(dd.max()),
        "cb_dd_half_size_pct": half,
        "cb_dd_flatten_pct": flat,
        "half_size_would_trigger": bool(above_half.any()),
        "flatten_would_trigger": bool(above_flat.any()),
        "n_days_dd_above_half_size": int(above_half.sum()),
        "n_days_dd_above_flatten": int(above_flat.sum()),
        "n_episodes_half_size": run_lengths(above_half),
        "n_episodes_flatten": run_lengths(above_flat),
        "first_half_size_date": first_date(above_half),
        "first_flatten_date": first_date(above_flat),
        "cb_daily_loss_freeze_pct": freeze,
        "n_days_daily_loss_above_freeze": int(freeze_days.sum()),
        "n_episodes_freeze_entries": run_lengths(freeze_days),
    }


def pocket_returns_from_wallet_segments(segments, a: float) -> pd.Series:
    """Reconversion BAND-FIDELITY-SPEC §3.2 : pour CHAQUE fenêtre (capital wallet remis à 1),
    P = 1 + (E − 1)/a puis rendements de poche = P_t/P_{t−1} − 1 (P_0 = 1) ; concaténation des
    rendements de poche comme pour S1."""
    parts = []
    for seg in segments:
        p = 1.0 + (seg.equity - 1.0) / a
        r = p.pct_change()
        r.iloc[0] = p.iloc[0] / 1.0 - 1.0
        parts.append(r)
    return pd.concat(parts)


# ------------------------------------------------------------------------------------------
# Verdict §1.2
# ------------------------------------------------------------------------------------------


def threshold_verdicts(sharpe, pf, n_trades, maxdd, bench_maxdd, dsr_value, k_total) -> dict:
    maxdd_ratio = (
        maxdd / bench_maxdd
        if bench_maxdd not in (0, None) and not nan_safe(bench_maxdd) and not nan_safe(maxdd)
        else float("nan")
    )
    T = PROMOTION_RULES_THRESHOLDS
    return {
        "sharpe_oos": {
            "value": sharpe, "threshold": T["sharpe_oos_min"], "rule": ">= seuil",
            "pass": bool(not nan_safe(sharpe) and sharpe >= T["sharpe_oos_min"]),
        },
        "profit_factor_oos": {
            "value": pf, "threshold": T["profit_factor_oos_min"], "rule": "> seuil",
            "pass": bool(not nan_safe(pf) and pf > T["profit_factor_oos_min"]),
        },
        "n_trades_oos_closed": {
            "value": n_trades, "threshold": T["n_trades_oos_min"], "rule": ">= seuil",
            "pass": bool(n_trades >= T["n_trades_oos_min"]),
        },
        "maxdd_relative_to_benchmark": {
            "value": maxdd_ratio, "threshold": T["maxdd_relative_to_benchmark_max"],
            "rule": "<= seuil (maxdd_candidate / maxdd_benchmark_OOS_aligné)",
            "pass": bool(not nan_safe(maxdd_ratio) and maxdd_ratio <= T["maxdd_relative_to_benchmark_max"]),
        },
        "dsr": {
            "value": dsr_value, "threshold": T["dsr_min"], "rule": ">= seuil", "k_total": k_total,
            "pass": bool(not nan_safe(dsr_value) and dsr_value >= T["dsr_min"]),
        },
    }


# ------------------------------------------------------------------------------------------
# main
# ------------------------------------------------------------------------------------------


def count_registry_rows() -> Optional[int]:
    try:
        with open(REPO_ROOT / "docs" / "RESEARCH-REGISTRY.json", encoding="utf-8") as f:
            return len(json.load(f)["strategies"])
    except Exception:  # pragma: no cover - diagnostic seulement
        return None


def main():
    args = build_parser().parse_args()
    output_dir = Path(args.output_dir)
    if args.smoke and str(output_dir.resolve()) == str(DEFAULT_OUTPUT_DIR.resolve()):
        raise SystemExit("--smoke exige un --output-dir explicite (ne jamais écraser les résultats nominaux)")
    output_dir.mkdir(parents=True, exist_ok=True)
    t_global = time.time()

    d = load_all_data(args.data_dir)
    calendar, universe = d["calendar"], d["universe"]
    windows = engine.generate_walk_forward_windows(calendar, IS_MONTHS, OOS_MONTHS, STEP_MONTHS)
    if args.smoke:
        windows = windows[:3]
    n_windows = len(windows)
    print(f"[walk-forward] {n_windows} fenêtres ({IS_MONTHS}m IS / {OOS_MONTHS}m OOS / pas {STEP_MONTHS}m){' [SMOKE]' if args.smoke else ''}", flush=True)
    if not args.smoke and n_windows != EXPECTED_N_WINDOWS:
        print(f"[ALERTE] {n_windows} fenêtres != {EXPECTED_N_WINDOWS} attendues (SPEC §2) — K_total suit le nombre constaté", flush=True)

    registry_rows_actual = count_registry_rows()
    if registry_rows_actual is not None and registry_rows_actual != K_REGISTRY_ROWS:
        print(f"[ALERTE] RESEARCH-REGISTRY.json compte {registry_rows_actual} lignes != {K_REGISTRY_ROWS} (SPEC §3)", flush=True)

    # --- Signaux (calculés UNE fois, réutilisés par toutes les combinaisons) ----------------
    panels = lowvol.compute_signal_panels(d["raw_closes"], calendar, universe)
    weights_by_combo: Dict[tuple, pd.DataFrame] = {}
    for p in lowvol.PARAM_GRID:
        weights_by_combo[tuple(sorted(p.items()))] = lowvol.generate_weight_decisions(
            d["raw_closes"], calendar, lowvol.LowVolParams(**p), universe, panels
        )
    control_weights = lowvol.generate_control_weights(d["raw_closes"], calendar, universe, panels)

    providers = {
        "candidate": lambda params: weights_by_combo[tuple(sorted(params.items()))],
        "control": lambda params: control_weights,
    }
    _CTX.update(
        calendar=calendar, opens=d["opens"], closes=d["closes"], spy_opens=d["spy_opens"],
        spy_closes=d["spy_closes"], windows=windows, providers=providers,
    )

    ctx = mp.get_context("fork")
    nproc = max(1, args.processes)

    # --- Sélection IS (indépendante par fenêtre) ----------------------------------------------
    ckpt = output_dir / "_is_selection_checkpoint.json"
    is_infos = None
    if ckpt.exists():
        try:
            saved = json.loads(ckpt.read_text())
            if saved.get("n_windows") == n_windows and saved.get("calendar_end") == str(calendar[-1].date()):
                is_infos = saved["infos"]
                print(f"[IS] sélection reprise depuis le checkpoint {ckpt.name}", flush=True)
        except Exception:
            is_infos = None
    if is_infos is None:
        t0 = time.time()
        print(f"[IS] sélection par fenêtre (grille {N_GRID_COMBOS_CANDIDATE} combos) ...", flush=True)
        with ctx.Pool(nproc) as pool:
            is_infos = pool.map(_select_window, windows, chunksize=1)
        print(f"[IS] terminé en {time.time() - t0:.0f}s", flush=True)
        ckpt.write_text(json.dumps({"n_windows": n_windows, "calendar_end": str(calendar[-1].date()), "infos": is_infos}, default=str))
    chosen_list = [info["chosen_params"] for info in is_infos]
    for w, info in zip(windows, is_infos):
        print(f"[IS] fenêtre {w.index:2d} chosen={info['chosen_params']} is_sharpe={info['is_sharpe_chosen']:.4f}", flush=True)

    # --- Chaînes OOS (candidate nominale, contrôle, S2, stress, benchmark) --------------------
    a = CAPITAL_ALLOC_PCT
    jobs = [
        {"name": "candidate", "kind": "chain", "provider": "candidate", "chosen": chosen_list, "cost_bps": COST_BPS_EQUITY},
        {"name": "control", "kind": "chain", "provider": "control", "chosen": [{}] * n_windows, "cost_bps": COST_BPS_EQUITY},
        {"name": "s2", "kind": "chain", "provider": "candidate", "chosen": chosen_list, "cost_bps": COST_BPS_EQUITY,
         "band": NO_TRADE_BAND * a, "scale": a},
        {"name": "stress_3x", "kind": "chain", "provider": "candidate", "chosen": chosen_list, "cost_bps": COST_BPS_STRESS_3X},
        {"name": "stress_5x", "kind": "chain", "provider": "candidate", "chosen": chosen_list, "cost_bps": COST_BPS_STRESS_5X},
        {"name": "benchmark", "kind": "bench"},
    ]
    print(f"[run] {len(jobs)} chaînes OOS (candidate, contrôle, S2, stress 3x/5x, benchmark SPY) ...", flush=True)
    t0 = time.time()
    results_raw = {}
    with ctx.Pool(nproc) as pool:
        for name, segs, events, secs in pool.imap_unordered(_job, jobs):
            results_raw[name] = (segs, events)
            print(f"[run] chaîne '{name}' terminée en {secs:.0f}s", flush=True)
    print(f"[run] toutes les chaînes OOS en {time.time() - t0:.0f}s", flush=True)

    cand = summarize_chain(windows, results_raw["candidate"][0], chosen_list, is_infos)
    ctl = summarize_chain(windows, results_raw["control"][0])
    bench = summarize_chain(windows, results_raw["benchmark"][0])
    st3 = summarize_chain(windows, results_raw["stress_3x"][0])
    st5 = summarize_chain(windows, results_raw["stress_5x"][0])
    s2_segments = [s for s, _ in results_raw["s2"][0]]

    cand_conc = cand["_concatenated_result"]
    ctl_conc = ctl["_concatenated_result"]
    bench_conc = bench["_concatenated_result"]
    cand_sum, ctl_sum, bench_sum = cand["concatenated"], ctl["concatenated"], bench["concatenated"]
    r_cand, r_ctl, r_bench = cand_conc.returns, ctl_conc.returns, bench_conc.returns

    # --- DSR (SPEC §3) --------------------------------------------------------------------------
    k_total = K_REGISTRY_ROWS + n_windows * N_GRID_COMBOS_CANDIDATE + n_windows * N_GRID_COMBOS_CONTROL
    dsr_result = bt_metrics.deflated_sharpe_ratio(r_cand, trials_k=k_total)

    # --- Stress de coûts ---------------------------------------------------------------------
    def pf_sharpe(res):
        c = res["_concatenated_result"]
        return (
            bt_metrics.profit_factor([e["pnl"] for e in c.realized_events]),
            bt_metrics.sharpe_ratio(c.returns, periods_per_year=PERIODS_PER_YEAR),
        )

    pf3, sh3 = pf_sharpe(st3)
    pf5, sh5 = pf_sharpe(st5)
    cost_stress = {
        "profit_factor_at_5bps_nominal": cand_sum["profit_factor"],
        "profit_factor_at_15bps_3x": pf3,
        "profit_factor_at_25bps_5x": pf5,
        "sharpe_at_5bps_nominal": cand_sum["sharpe"],
        "sharpe_at_15bps_3x": sh3,
        "sharpe_at_25bps_5x": sh5,
        "note": "OOS re-simulé avec les combinaisons DÉJÀ choisies par la sélection IS nominale (jamais une nouvelle sélection), portage inter-fenêtres actif, seul cost_bps change.",
    }

    # --- Analyses d'honnêteté ---------------------------------------------------------------------
    sub = {
        "candidate": subperiod_sharpes(r_cand),
        "control": subperiod_sharpes(r_ctl),
        "benchmark_spy": subperiod_sharpes(r_bench),
    }
    correlations = {
        "candidate_vs_control": corr_safe(r_cand, r_ctl),
        "candidate_vs_spy": corr_safe(r_cand, r_bench),
        "control_vs_spy": corr_safe(r_ctl, r_bench),
        "note": "Corrélation des rendements quotidiens OOS concaténés (mêmes dates). NaN = série constante (contrôle jamais investi).",
    }
    paired = paired_comparison(r_cand, r_ctl)

    # turnover : signal (poids cibles) et réalisé (coûts / taux de coût)
    sig_turn_cand: List[float] = []
    sig_turn_ctl: List[float] = []
    for w, chosen in zip(windows, chosen_list):
        sig_turn_cand += lowvol.signal_turnover_one_way(providers["candidate"](chosen), calendar, w.oos_start_idx, w.oos_end_idx)
        sig_turn_ctl += lowvol.signal_turnover_one_way(control_weights, calendar, w.oos_start_idx, w.oos_end_idx)
    n_months_oos = len(r_cand) / (PERIODS_PER_YEAR / 12.0)
    realized_cost_cand = cand_conc.pnl_breakdown["costs_spot"]
    realized_cost_ctl = ctl_conc.pnl_breakdown["costs_spot"]
    turnover = {
        "signal_monthly_one_way_mean_candidate": float(np.mean(sig_turn_cand)) if sig_turn_cand else float("nan"),
        "signal_monthly_one_way_mean_control": float(np.mean(sig_turn_ctl)) if sig_turn_ctl else float("nan"),
        "n_decision_dates_oos": len(sig_turn_cand),
        "realized_monthly_one_way_candidate": float(realized_cost_cand / (COST_BPS_EQUITY / 1e4) / 2.0 / n_months_oos),
        "realized_monthly_one_way_control": float(realized_cost_ctl / (COST_BPS_EQUITY / 1e4) / 2.0 / n_months_oos),
        "annual_cost_drag_candidate_pct_of_initial_capital_per_window": float(realized_cost_cand / (len(r_cand) / PERIODS_PER_YEAR)),
        "note": (
            "signal = fraction du portefeuille CIBLE remplacée à chaque rebalance mensuel (0,5·Σ|Δw|), "
            "avant bande de non-négociation. realized = Σ coûts spot / taux de coût / 2 / nb de mois OOS "
            "(turnover dollar réel exécuté par le moteur, en fraction du capital de base de chaque fenêtre)."
        ),
    }

    # --- Seuils §1.2 (S1) -----------------------------------------------------------------------
    verdicts = threshold_verdicts(
        cand_sum["sharpe"], cand_sum["profit_factor"], cand_sum["n_trades_closed"],
        cand_sum["max_drawdown"], bench_sum["max_drawdown"], dsr_result.dsr, k_total,
    )
    all_pass = all(v["pass"] for v in verdicts.values())
    dominated = paired["candidate_dominated_by_control"]
    sh, pf = cand_sum["sharpe"], cand_sum["profit_factor"]
    if all_pass:
        if dominated is True:
            gate1 = "ecartee_dominee_par_controle"
        elif dominated is False:
            gate1 = "incubation_proposee_sous_reserve_audit_adversarial"
        else:
            gate1 = "indetermine_controle_degenere_domination_non_evaluable"
    elif nan_safe(sh) or nan_safe(pf) or sh <= 0 or pf <= 1.0:
        gate1 = "rejetee"
    else:
        gate1 = "ecartee_seuil_manque"

    # --- Annexe band_fidelity S2 (BAND-FIDELITY-SPEC §3.2) ------------------------------------------
    print("[band_fidelity] réplica instrumenté S1/S2 + validation contre le moteur ...", flush=True)
    rep1 = replica_chain(windows, chosen_list, providers["candidate"], NO_TRADE_BAND, 1.0, COST_BPS_EQUITY)
    rep2 = replica_chain(windows, chosen_list, providers["candidate"], NO_TRADE_BAND * a, a, COST_BPS_EQUITY)
    max_diff_s1 = max(float(np.max(np.abs(rw - s.equity.to_numpy()))) for rw, s in zip(rep1["window_equity"], cand["_segments"]))
    max_diff_s2 = max(float(np.max(np.abs(rw - s.equity.to_numpy()))) for rw, s in zip(rep2["window_equity"], s2_segments))
    replica_ok = bool(max_diff_s1 < 1e-7 and max_diff_s2 < 1e-7)
    if not replica_ok:
        print(f"[ALERTE] réplica != moteur (S1 {max_diff_s1:.2e}, S2 {max_diff_s2:.2e}) — divergences de décisions NON FIABLES", flush=True)

    s2_conc = engine.concatenate_segments(s2_segments)
    r_s2_pocket = pocket_returns_from_wallet_segments(s2_segments, a)
    eq_s2_pocket = pd.concat([pd.Series([1.0]), (1.0 + r_s2_pocket).cumprod()])
    s2_metrics = {
        "sharpe": bt_metrics.sharpe_ratio(r_s2_pocket, periods_per_year=PERIODS_PER_YEAR),
        "sortino": bt_metrics.sortino_ratio(r_s2_pocket, periods_per_year=PERIODS_PER_YEAR),
        "profit_factor": bt_metrics.profit_factor([e["pnl"] for e in s2_conc.realized_events]),
        "max_drawdown": bt_metrics.max_drawdown(eq_s2_pocket),
        "cagr": bt_metrics.cagr(eq_s2_pocket, periods_per_year=PERIODS_PER_YEAR),
        "n_trades_closed": len(s2_conc.trades_closed),
        "average_exposure_wallet": bt_metrics.average_exposure(s2_conc.gross_exposure),
        "n_days": int(len(r_s2_pocket)),
    }
    dsr_s2 = bt_metrics.deflated_sharpe_ratio(r_s2_pocket, trials_k=k_total)
    verdicts_s2 = threshold_verdicts(
        s2_metrics["sharpe"], s2_metrics["profit_factor"], s2_metrics["n_trades_closed"],
        s2_metrics["max_drawdown"], bench_sum["max_drawdown"], dsr_s2.dsr, k_total,
    )
    flips = {k: bool(verdicts[k]["pass"] != verdicts_s2[k]["pass"]) for k in verdicts}
    band_fragile = bool(any(flips.values()))

    # divergences de décisions
    k_pa = lambda P: P / (1.0 + a * (P - 1.0))  # noqa: E731
    P1 = rep1["eq_pre"][:, None]
    what1, wp1 = rep1["tgt_w"], rep1["cur_w"]
    active1 = (wp1 > 1e-12) | (what1 > 1e-12)
    h1 = np.abs(what1 - wp1) < NO_TRADE_BAND
    h2cf = np.abs(what1 - wp1 * k_pa(P1)) < NO_TRADE_BAND
    d1 = active1 & (h1 != h2cf)
    xor = rep1["traded"] ^ rep2["traded"]
    divergences = {
        "n_decision_bars_divergent_same_state_D1": int(d1.any(axis=1).sum()),
        "n_decision_cells_divergent_same_state_D1": int(d1.sum()),
        "n_active_cells_S1_path": int(active1.sum()),
        "n_decision_bars_divergent_independent_paths_D2": int(xor.any(axis=1).sum()),
        "n_decision_cells_divergent_independent_paths_D2": int(xor.sum()),
        "n_orders_S1": int(rep1["traded"].sum()),
        "n_orders_S2": int(rep2["traded"].sum()),
        "n_bars_with_order_S1": int(rep1["traded"].any(axis=1).sum()),
        "n_bars_with_order_S2": int(rep2["traded"].any(axis=1).sum()),
        "n_bars_total_oos": int(len(rep1["eq_pre"])),
        "definition": (
            "D1 = barres où, SUR LA TRAJECTOIRE S1, la règle de bande S1 et la règle S2 (évaluée sur le MÊME état de "
            "détention via k(P,a) = P/(1+a(P−1))) donnent des décisions hold/trade différentes pour au moins un titre actif "
            "(mesure indépendante du chemin, retenue comme mesure principale) ; D2 = barres où le XOR des drapeaux « ordre "
            "émis » entre les runs S1 et S2 indépendants est non nul (dépendant du chemin)."
        ),
        "replica_validation": {
            "max_abs_equity_diff_vs_engine_S1": max_diff_s1,
            "max_abs_equity_diff_vs_engine_S2": max_diff_s2,
            "replica_matches_engine": replica_ok,
        },
    }

    # bloc cb : wallet LABO
    labo = [w for w in bot_cfg.WALLETS if w["id"] == "labo"]
    risque = labo[0]["risque"] if labo else {}
    cb_cfg = {
        "half": float(risque.get("cb_dd_half_size_pct", 0.15)),
        "flatten": float(risque.get("cb_dd_flatten_pct", 0.25)),
        "freeze": float(risque.get("cb_daily_loss_freeze_pct", 0.03)),
    }
    s2_dates = pd.DatetimeIndex(r_s2_pocket.index)
    p_cat = eq_s2_pocket.to_numpy()
    e_wallet_formula = 1.0 + a * (p_cat - 1.0)  # SPEC : équity wallet = 1 + a·(P−1), P = équity de poche concaténée
    e_wallet_rebal = np.concatenate([[1.0], np.cumprod(1.0 + s2_conc.returns.to_numpy())])  # variante : a ré-équilibré à chaque fenêtre
    cb_block = {
        "wallet_target": "labo",
        "thresholds_source": "bot/config.py WALLETS[labo]['risque'] (lecture seule)",
        "a": a,
        "wallet_equity_formula_1_plus_a_P_minus_1": cb_exposure(e_wallet_formula, s2_dates, cb_cfg),
        "alt_wallet_equity_rebalanced_each_window": cb_exposure(e_wallet_rebal, s2_dates, cb_cfg),
        "max_wallet_drawdown": float(np.max(1.0 - e_wallet_formula / np.maximum.accumulate(e_wallet_formula))),
        "pocket_maxdd_S1_for_reference": cand_sum["max_drawdown"],
        "pocket_maxdd_S2_for_reference": s2_metrics["max_drawdown"],
        "a_times_pocket_maxdd_S1": a * cand_sum["max_drawdown"],
        "a_times_pocket_maxdd_S2": a * s2_metrics["max_drawdown"],
        "spec_expectation_refuted_a_times_pocket_maxdd_above_half_size": bool(a * cand_sum["max_drawdown"] > cb_cfg["half"]),
        "spec_expectation": "MaxDD wallet ≈ a × MaxDD poche ≈ 11-14 %, sous le seuil half-size labo (15 %) — À VÉRIFIER (SPEC §1).",
        "not_modelled": "Circuit breaker « pertes consécutives » (cooldown) non modélisé ; flatten production STICKY (une fois déclenché, revue manuelle) — n_episodes_flatten est un nombre de franchissements simulés, pas de flattens réels.",
        "note": (
            "Le moteur ne simule AUCUN circuit breaker : ces chiffres bornent ce que la production aurait déclenché "
            "(reste du wallet en cash). Deux lectures de l'équity wallet sont données : la formule SPEC E = 1 + a·(P−1) "
            "appliquée à l'équity de poche concaténée sur 30 ans (a fixé en part du capital INITIAL, le reste en cash), "
            "et la variante où a est ré-équilibré à 0,30 à chaque début de fenêtre OOS."
        ),
    }
    band_fidelity = {
        "a": a,
        "sharpe_s1": cand_sum["sharpe"],
        "sharpe_s2": s2_metrics["sharpe"],
        "delta_sharpe_s2_minus_s1": s2_metrics["sharpe"] - cand_sum["sharpe"],
        "band_fragile": band_fragile,
        "band_fragile_definition": "true si au moins un des 5 seuils §1.2 bascule (passe <-> échoue) entre S1 et S2 (BAND-FIDELITY-SPEC §3.3) — FLAG, pas une règle de rejet.",
        "s1": {
            "sharpe": cand_sum["sharpe"], "profit_factor": cand_sum["profit_factor"],
            "n_trades_closed": cand_sum["n_trades_closed"], "max_drawdown": cand_sum["max_drawdown"],
            "dsr": dsr_result.dsr, "maxdd_relative_to_benchmark": verdicts["maxdd_relative_to_benchmark"]["value"],
            "thresholds_pass": {k: v["pass"] for k, v in verdicts.items()},
        },
        "s2_pocket_units": {
            **s2_metrics, "dsr": dsr_s2.dsr,
            "maxdd_relative_to_benchmark": verdicts_s2["maxdd_relative_to_benchmark"]["value"],
            "thresholds_pass": {k: v["pass"] for k, v in verdicts_s2.items()},
        },
        "threshold_flips_s1_vs_s2": flips,
        "decision_divergences": divergences,
        "cb": cb_block,
        "s2_definition": (
            "Run S2 apparié : même moteur, mêmes fenêtres, MÊME combinaison retenue par fenêtre que S1 (sélection IS non rejouée), "
            f"poids décidés a·W (a={a}), no_trade_band = 0,05·a = {NO_TRADE_BAND * a:.4f}, mêmes coûts, portage inter-fenêtres actif ; "
            "métriques reconverties en unités de poche par fenêtre via P = 1 + (E−1)/a puis concaténées. "
            "Hypothèse : reste du wallet en cash (borne documentée, BAND-FIDELITY-SPEC §4)."
        ),
        "knife_edge_note": (
            "Poids cible 1/N : N=20 donne exactement 0,05 = la bande (condition de hold stricte `|Δ|<bande` => ordre émis à la limite, "
            "en S1 comme en S2 puisque a·(1/20) et 0,05·a sont le même flottant) ; N=30 (0,033) et le contrôle (~0,01) sont SOUS la bande : "
            "aucun ordre d'entrée n'est jamais émis depuis flat (cf. structural_diagnostics)."
        ),
    }

    # --- Diagnostics structurels (honnêteté) ----------------------------------------------------------
    decision_mask = lowvol.decision_day_mask(calendar)
    oos_dec_idx = [i for w in windows for i in range(w.oos_start_idx, w.oos_end_idx + 1) if decision_mask[i]]
    elig_counts = {}
    for L in lowvol.VOL_WINDOWS:
        e = ((panels["n_returns"] >= lowvol.MIN_HISTORY_DAYS) & panels[f"vol_{L}"].notna()).sum(axis=1)
        elig_counts[L] = e
    same_elig = bool((elig_counts[63] == elig_counts[252]).all())
    elig_oos = elig_counts[63].iloc[oos_dec_idx]
    n_degenerate_months = int((elig_oos < min(lowvol.N_HOLDINGS)).sum())
    pnls_cand = np.array([e["pnl"] for e in cand_conc.realized_events], dtype=float)
    structural = {
        "candidate_realized_events": {
            "n_events": int(len(pnls_cand)),
            "n_gains": int((pnls_cand > 0).sum()),
            "n_losses": int((pnls_cand < 0).sum()),
            "sum_gains": float(pnls_cand[pnls_cand > 0].sum()),
            "sum_losses": float(pnls_cand[pnls_cand < 0].sum()),
            "note": "Évènements de réalisation de PnL (fermetures complètes ET partielles) alimentant le profit factor. Peu de pertes réalisées = les positions perdantes sous la bande ne sont jamais vendues.",
        },
        "candidate": exposure_diagnostics(cand_conc),
        "control": exposure_diagnostics(ctl_conc),
        "candidate_mean_number_of_held_names_S1_replica": float(np.mean(rep1["n_held"])),
        "candidate_max_number_of_held_names_S1_replica": int(np.max(rep1["n_held"])),
        "eligible_universe_size_at_oos_decision_dates": {
            "min": int(elig_oos.min()), "median": float(elig_oos.median()), "max": int(elig_oos.max()),
            "n_decision_dates": int(len(elig_oos)), "n_months_fewer_than_20_eligible": n_degenerate_months,
        },
        "eligibility_independent_of_L": same_elig,
        "n_combos_with_nan_is_sharpe_windows": int(sum(
            1 for info in is_infos for c in info["is_candidates"] if nan_safe(c["is_sharpe"])
        )),
    }

    # sélection la plus fréquente
    counts = Counter(tuple(sorted(c.items())) for c in chosen_list)

    # --- Assemblage ---------------------------------------------------------------------------------
    anomalies = [
        {
            "id": "A1_BK_absent_de_UNIVERSE_SP100",
            "texte": (
                "La SPEC (§1) et la mission supposent BK (indisponible) à retirer de UNIVERSE_SP100 pour un univers de 102 titres. "
                "UNIVERSE_SP100 NE CONTIENT PAS de ticker 'BK' (103 titres, dont BKNG, tous 103 chargés avec succès) : l'exclusion est "
                "un no-op et l'univers effectif de ce run est de 103 titres, pas 102. Aucune substitution/ajout/retrait n'a été fait."
            ),
        },
        {
            "id": "A2_interpretation_eligibilite_252_jours",
            "texte": (
                "« ≥ 252 jours de bourse d'historique réel » est implémenté comme ≥ 252 RENDEMENTS quotidiens réels (253 clôtures) : seule "
                "lecture rendant l'éligibilité identique pour L=63 et L=252 (vérifié : eligibility_independent_of_L). Écart d'un jour "
                "d'historique pour un titre à son entrée dans l'univers, sans effet matériel attendu."
            ),
        },
        {
            "id": "A3_bande_de_non_negociation_vs_poids_1_sur_N",
            "texte": (
                "Avec no_trade_band=0,05 (SPEC §2, sémantique S1 du moteur) comparée PAR TITRE aux poids cibles : N=30 (1/30=0,033) et le "
                "contrôle (~1/100 = 0,01) sont SOUS la bande — le moteur ne passe JAMAIS d'ordre d'entrée depuis flat (hold = |cible-courant| < 0,05). "
                "Conséquences constatées : N=30 jamais investi (Sharpe IS NaN, jamais sélectionnable), CONTRÔLE APPARIÉ JAMAIS INVESTI "
                "(Sharpe/Sortino NaN, comparaison et domination NON évaluables). N=20 est exactement à la limite (1/20 = 0,05, ordre émis car "
                "inégalité stricte) ; mais les titres sortis du top-N dont le poids dérivé est < 0,05 ne sont jamais vendus (cible 0, |Δ| < 0,05 => hold) : "
                "des positions résiduelles s'accumulent et l'exposition brute dépasse 1 (levier implicite, cash négatif non financé). Corollaire : ces positions perdantes "
                "ne sont presque jamais RÉALISÉES (seules les lignes dont le poids dérivé est ≥ 0,05 sont vendues), ce qui gonfle mécaniquement le profit factor (gains réalisés / pertes rarement réalisées) "
                "et fausse le décompte de trades clos. Voir structural_diagnostics. "
                "AUCUN paramètre n'a été modifié : les chiffres officiels sont ceux de la SPEC à la lettre ; la SPEC n'avait pas anticipé ce mécanisme "
                "(cf. piège « poids sous 0,05 n'entre jamais », BAND-FIDELITY-SPEC §1.3)."
            ),
        },
    ]

    results = {
        "meta": {
            "candidate_id": CANDIDATE_ID,
            "spec": "backtest/LOWVOL-SP100-SPEC.md (pré-enregistrée session #11, commit 35df9b5)",
            "engine": "backtest/engine.py (docs/PROMOTION-RULES.md §1.1), portage inter-fenêtres ACTIF",
            "data_dir": str(args.data_dir),
            "smoke_run": bool(args.smoke),
            "universe_declared": f"UNIVERSE_SP100 ({len(UNIVERSE_SP100)} titres, constituants ACTUELS — biais du survivant)",
            "excluded_tickers": list(lowvol.EXCLUDED_TICKERS),
            "excluded_tickers_actually_present_in_universe": [t for t in lowvol.EXCLUDED_TICKERS if t in UNIVERSE_SP100],
            "universe_effective_n": len(universe),
            "calendar_start": str(calendar[0].date()),
            "calendar_end": str(calendar[-1].date()),
            "n_calendar_days": len(calendar),
            "data_diagnostics": d["data_diagnostics"],
            "n_windows": n_windows,
            "n_windows_expected_spec": EXPECTED_N_WINDOWS,
            "walkforward": f"{IS_MONTHS}m IS / {OOS_MONTHS}m OOS / pas {STEP_MONTHS}m",
            "oos_start": str(windows[0].oos_start.date()),
            "oos_end": str(windows[-1].oos_end.date()),
            "param_grid": lowvol.PARAM_GRID,
            "is_selection_used": True,
            "is_selection_criterion": "Sharpe IS (engine.select_params_via_is), IS simulé à plat, mêmes réglages overlay que l'OOS",
            "carry_across_windows": True,
            "overlay": {
                "apply_vol_targeting": APPLY_VOL_TARGETING,
                "no_trade_band": NO_TRADE_BAND,
                "periods_per_year": PERIODS_PER_YEAR,
            },
            "cost_bps_equity_per_side": COST_BPS_EQUITY,
            "cost_bps_benchmark_per_side": COST_BPS_BENCHMARK,
            "capital_alloc_pct_planned": CAPITAL_ALLOC_PCT,
            "sizing_interne": False,
            "k_total": k_total,
            "k_total_detail": {
                "registry_rows_spec": K_REGISTRY_ROWS,
                "registry_rows_actual_at_run": registry_rows_actual,
                "n_windows": n_windows,
                "n_grid_combos_candidate": N_GRID_COMBOS_CANDIDATE,
                "n_grid_combos_control": N_GRID_COMBOS_CONTROL,
                "formula": "K_total = 16 + n_windows×4 + n_windows×1 (SPEC §3, contrôle compté — option conservatrice)",
            },
            "runtime_seconds": None,
        },
        "anomalies": anomalies,
        "candidate_lowvol": {
            "cost_bps": COST_BPS_EQUITY,
            "carry_across_windows": True,
            "per_window": cand["per_window"],
            "concatenated": cand_sum,
            "carry_boundary_events": results_raw["candidate"][1],
        },
        "control_equal_weight_all_eligible": {
            "cost_bps": COST_BPS_EQUITY,
            "carry_across_windows": True,
            "description": "Univers éligible ENTIER équipondéré, mêmes dates de rebalance, même moteur/overlay/coûts/fenêtres/portage (SPEC §2).",
            "per_window": ctl["per_window"],
            "concatenated": ctl_sum,
            "carry_boundary_events": results_raw["control"][1],
        },
        "benchmark_spy_buy_hold": {
            "cost_bps": COST_BPS_BENCHMARK,
            "per_window": bench["per_window"],
            "concatenated": bench_sum,
        },
        "dsr_candidate": dsr_result.to_dict(),
        "cost_stress_test": cost_stress,
        "honesty_analyses": {
            "subperiods_sharpe": sub,
            "correlations": correlations,
            "turnover": turnover,
            "paired_comparison_candidate_vs_control": paired,
            "structural_diagnostics": structural,
        },
        "band_fidelity": band_fidelity,
        "promotion_rules_1_2_thresholds_verdict": verdicts,
        "promotion_rules_1_2_all_pass": bool(all_pass),
        "candidate_dominated_by_control": dominated,
        "most_frequently_selected_is_combo": {
            "counts": [{"params": dict(k), "n_windows": v} for k, v in counts.most_common()],
            "n_windows_total": n_windows,
        },
        "gate1_verdict_automatic": {
            "value": gate1,
            "note": (
                "Verdict AUTOMATIQUE (SPEC §5) fondé UNIQUEMENT sur les 5 seuils §1.2 et la domination par le contrôle. NE remplace PAS "
                "l'audit adversarial indépendant §1.4 (isSound), ni l'arbitrage de band_fragile (BAND-FIDELITY-SPEC §3.3)."
            ),
        },
        "adversarial_audit_1_4": {"isSound": None, "note": "Non exécuté ici (hors périmètre du script d'implémentation) ; obligatoire avant toute décision de statut."},
    }
    results["meta"]["runtime_seconds"] = round(time.time() - t_global, 1)

    results_path = output_dir / "results.json"
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str, ensure_ascii=False)
    print(f"[out] {results_path}", flush=True)

    write_report(results, output_dir / "REPORT.md")
    print(f"[out] {output_dir / 'REPORT.md'}", flush=True)
    if ckpt.exists():
        ckpt.unlink()
    print(f"[done] durée totale : {results['meta']['runtime_seconds']}s", flush=True)
    return results, output_dir


# ------------------------------------------------------------------------------------------
# REPORT.md (français) — rendu déterministe depuis results.json
# ------------------------------------------------------------------------------------------


def _f(x, nd=3, pct=False):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    if isinstance(x, float) and math.isinf(x):
        return "inf"
    if pct:
        return f"{x * 100 + 0.0:.{max(nd - 1, 1)}f} %".replace(".", ",").replace("-0,00", "0,00")
    return f"{x + 0.0:.{nd}f}".replace(".", ",")


def _ok(b):
    return "PASS" if b else "ÉCHEC"


def write_report(res: dict, path: Path) -> None:
    m = res["meta"]
    cand = res["candidate_lowvol"]["concatenated"]
    ctl = res["control_equal_weight_all_eligible"]["concatenated"]
    bench = res["benchmark_spy_buy_hold"]["concatenated"]
    v = res["promotion_rules_1_2_thresholds_verdict"]
    dsr = res["dsr_candidate"]
    cs = res["cost_stress_test"]
    ha = res["honesty_analyses"]
    bf = res["band_fidelity"]
    st = ha["structural_diagnostics"]
    pc = ha["paired_comparison_candidate_vs_control"]
    cb = bf["cb"]
    cba = cb["wallet_equity_formula_1_plus_a_P_minus_1"]
    cbb = cb["alt_wallet_equity_rebalanced_each_window"]
    L = []
    w = L.append
    w("# Rapport de recherche — `lowvol_sp100` (Porte 1)\n")
    w(
        "*Candidate : « low volatility » long-only sur le S&P100 (bottom-N par volatilité réalisée, équipondéré, rebalance mensuel). "
        "SPEC pré-enregistrée : `backtest/LOWVOL-SP100-SPEC.md` (session #11, commit 35df9b5, committée AVANT exécution). Moteur : "
        "`backtest/engine.py`. Signal : `backtest/strategies/lowvol.py`. Orchestration : `backtest/run_lowvol_sp100.py`. Résultats bruts : "
        "`backtest/results/lowvol_sp100/results.json`.*\n"
    )
    w(
        "**Ce document n'est PAS l'audit adversarial indépendant (`docs/PROMOTION-RULES.md` §1.4, verdict `isSound`) : c'est le backtest "
        "walk-forward lui-même (Porte 1, §1.1 à §1.3). Aucun paramètre n'a été ajusté après observation de l'OOS ; aucun re-run avec d'autres valeurs.**\n"
    )
    w("---\n")
    w("## 0. Anomalies et ambiguïtés constatées à l'exécution (lire en premier)\n")
    for an in res["anomalies"]:
        w(f"- **{an['id']}** — {an['texte']}")
    w("")
    w(
        f"Exposition brute moyenne mesurée : candidate **{_f(st['candidate']['mean_gross_exposure'])}** (max {_f(st['candidate']['max_gross_exposure'])}, "
        f"{_f(st['candidate']['share_days_gross_exposure_gt_1_05'], 3, True)} des jours > 1,05) ; contrôle **{_f(st['control']['mean_gross_exposure'])}** "
        f"({_f(st['control']['share_days_zero_exposure'], 3, True)} des jours à exposition nulle). "
        f"Nombre moyen de titres détenus (réplica S1) : {_f(st['candidate_mean_number_of_held_names_S1_replica'], 1)} "
        f"(max {st['candidate_max_number_of_held_names_S1_replica']}) pour une cible de 20. "
        f"Fenêtres IS dont la combinaison avait un Sharpe NaN (jamais investie) : {st['n_combos_with_nan_is_sharpe_windows']} couples (fenêtre, combo) sur "
        f"{m['n_windows'] * len(m['param_grid'])}.\n"
    )
    w("---\n")
    w("## 1. Résumé exécutif (OOS concaténé)\n")
    w("| | Sharpe | Sortino | Profit factor | MaxDD | CAGR | Trades clos | Expo. moy. |")
    w("|---|---|---|---|---|---|---|---|")
    for name, s in (("**`lowvol_sp100` (combo walk-forward)**", cand), ("Contrôle (éligibles équipondérés)", ctl), ("Benchmark SPY B&H", bench)):
        w(
            f"| {name} | {_f(s['sharpe'])} | {_f(s['sortino'])} | {_f(s['profit_factor'])} | {_f(s['max_drawdown'], 3, True)} | "
            f"{_f(s['cagr'], 3, True)} | {s['n_trades_closed']} | {_f(s['average_exposure'])} |"
        )
    w(
        f"\n*{m['n_windows']} fenêtres walk-forward ({m['walkforward']}), OOS {m['oos_start']} → {m['oos_end']}, univers effectif {m['universe_effective_n']} titres, "
        f"coûts {m['cost_bps_equity_per_side']:.0f} bps/côté (SPY {m['cost_bps_benchmark_per_side']:.0f}), `apply_vol_targeting=False`, bande 0,05, portage inter-fenêtres actif.*\n"
    )
    w(f"**Verdict automatique (SPEC §5) : `{res['gate1_verdict_automatic']['value']}`** — sous réserve de l'audit adversarial §1.4 (non exécuté) et de l'arbitrage `band_fragile`.\n")
    w("---\n")
    w("## 2. Seuils `docs/PROMOTION-RULES.md` §1.2 — S1 (sémantique du moteur)\n")
    w("| Critère | Seuil | Valeur | Verdict |")
    w("|---|---|---|---|")
    w(f"| Sharpe OOS net de coûts | ≥ {_f(v['sharpe_oos']['threshold'], 2)} | **{_f(v['sharpe_oos']['value'])}** | {_ok(v['sharpe_oos']['pass'])} |")
    w(f"| Profit factor OOS | > {_f(v['profit_factor_oos']['threshold'], 2)} | **{_f(v['profit_factor_oos']['value'])}** | {_ok(v['profit_factor_oos']['pass'])} |")
    w(f"| Trades OOS clos | ≥ {v['n_trades_oos_closed']['threshold']} | **{v['n_trades_oos_closed']['value']}** | {_ok(v['n_trades_oos_closed']['pass'])} |")
    w(
        f"| MaxDD ≤ 1,5× MaxDD SPY aligné | ratio ≤ {_f(v['maxdd_relative_to_benchmark']['threshold'], 2)} | **{_f(v['maxdd_relative_to_benchmark']['value'])}** "
        f"({_f(cand['max_drawdown'], 3, True)} / {_f(bench['max_drawdown'], 3, True)}) | {_ok(v['maxdd_relative_to_benchmark']['pass'])} |"
    )
    w(f"| DSR (K_total = {m['k_total']}) | ≥ {_f(v['dsr']['threshold'], 2)} | **{_f(v['dsr']['value'], 4)}** | {_ok(v['dsr']['pass'])} |")
    w(f"\n`promotion_rules_1_2_all_pass : {str(res['promotion_rules_1_2_all_pass']).lower()}`.\n")
    kd = m["k_total_detail"]
    w(
        f"**DSR** : `K_total = {kd['registry_rows_spec']} + {kd['n_windows']}×{kd['n_grid_combos_candidate']} + {kd['n_windows']}×{kd['n_grid_combos_control']} = {m['k_total']}` "
        f"(lignes du registre à l'exécution : {kd['registry_rows_actual_at_run']}). Bailey & López de Prado 2014 sur {dsr['n_obs']} rendements quotidiens OOS concaténés : "
        f"Sharpe quotidien {_f(dsr['sharpe_hat_period'], 5)}, skew {_f(dsr['skew'])}, kurtosis excess {_f(dsr['kurtosis_excess'])}, SR0 = {_f(dsr['sr0_benchmark_expected_max_sharpe'], 5)}, "
        f"DSR = {_f(dsr['dsr'], 4)}. Rappel d'honnêteté (#12b) : à n ≈ 7 500 observations quotidiennes le seuil DSR ≥ 0,50 est peu discriminant — aucun argument "
        "« DSR élevé » ne compense un autre seuil manqué.\n"
    )
    w("### Stress de coûts (combinaisons IS déjà choisies, portage actif)\n")
    w("| Coût/côté | Profit factor | Sharpe |")
    w("|---|---|---|")
    w(f"| 5 bps (nominal) | {_f(cs['profit_factor_at_5bps_nominal'])} | {_f(cs['sharpe_at_5bps_nominal'])} |")
    w(f"| 15 bps (3×) | {_f(cs['profit_factor_at_15bps_3x'])} | {_f(cs['sharpe_at_15bps_3x'])} |")
    w(f"| 25 bps (5×) | {_f(cs['profit_factor_at_25bps_5x'])} | {_f(cs['sharpe_at_25bps_5x'])} |")
    w("\n---\n")
    w("## 3. Contrôle apparié et comparaison\n")
    w(
        f"Contrôle = univers éligible entier équipondéré, mêmes dates de rebalance. Sharpe {_f(pc['control']['sharpe'])}, Sortino {_f(pc['control']['sortino'])}, "
        f"MaxDD {_f(pc['control']['max_drawdown'], 3, True)} ; candidate : Sharpe {_f(pc['candidate']['sharpe'])}, Sortino {_f(pc['candidate']['sortino'])}, "
        f"MaxDD {_f(pc['candidate']['max_drawdown'], 3, True)} ; Information Ratio candidate vs contrôle {_f(pc['information_ratio_candidate_vs_control'])}.\n"
    )
    if pc["control_degenerate"]:
        w(
            "**Le contrôle est DÉGÉNÉRÉ (jamais investi : poids ~0,01 sous la bande de 0,05, cf. anomalie A3).** La comparaison candidate/contrôle et la domination "
            "sont donc **NON ÉVALUABLES** (`candidate_dominated_by_control = null`) : cela ne vaut PAS non-domination. La sémantique SPEC §5 (« non dominée par le contrôle ») "
            "ne peut pas être appliquée mécaniquement ; la décision est renvoyée à l'orchestrateur.\n"
        )
    else:
        w(f"Candidate dominée par le contrôle (Sharpe ET Sortino ET MaxDD) : **{str(pc['candidate_dominated_by_control']).lower()}**.\n")
    w("---\n")
    w("## 4. Analyses d'honnêteté obligatoires (consignées, jamais critères de sauvetage)\n")
    w("### Sous-périodes (Sharpe OOS)\n")
    w("| Période | Candidate | Contrôle | SPY B&H | n jours |")
    w("|---|---|---|---|---|")
    for name in ("avant_2010-01-01", "depuis_2010-01-01", "depuis_2020-01-01"):
        sc, sk, sb = ha["subperiods_sharpe"]["candidate"][name], ha["subperiods_sharpe"]["control"][name], ha["subperiods_sharpe"]["benchmark_spy"][name]
        w(f"| {name} | {_f(sc['sharpe'])} | {_f(sk['sharpe'])} | {_f(sb['sharpe'])} | {sc['n_days']} |")
    w("")
    co = ha["correlations"]
    w(f"### Corrélations (rendements quotidiens OOS)\n\nCandidate/contrôle {_f(co['candidate_vs_control'])} ; candidate/SPY {_f(co['candidate_vs_spy'])} ; contrôle/SPY {_f(co['control_vs_spy'])}.\n")
    tu = ha["turnover"]
    w(
        f"### Turnover mensuel moyen\n\nSignal (poids cibles, fraction remplacée) : candidate **{_f(tu['signal_monthly_one_way_mean_candidate'], 3, True)}** par mois "
        f"({tu['n_decision_dates_oos']} dates de décision OOS), contrôle {_f(tu['signal_monthly_one_way_mean_control'], 3, True)}. Réalisé (moteur, après bande, one-way) : "
        f"candidate {_f(tu['realized_monthly_one_way_candidate'], 3, True)} par mois, contrôle {_f(tu['realized_monthly_one_way_control'], 3, True)}.\n"
    )
    w("### Combinaisons sélectionnées en IS (nombre de fenêtres)\n")
    for c in res["most_frequently_selected_is_combo"]["counts"]:
        w(f"- `L={c['params']['vol_window']}, N={c['params']['n_holdings']}` : {c['n_windows']} / {res['most_frequently_selected_is_combo']['n_windows_total']}")
    w("")
    w("---\n")
    w("## 5. Annexe `band_fidelity` (BAND-FIDELITY-SPEC §3.2)\n")
    w(bf["s2_definition"] + "\n")
    s1, s2 = bf["s1"], bf["s2_pocket_units"]
    w("| Seuil §1.2 | S1 | S2 (unités de poche) | Bascule |")
    w("|---|---|---|---|")
    w(f"| Sharpe ≥ 0,70 | {_f(s1['sharpe'])} ({_ok(s1['thresholds_pass']['sharpe_oos'])}) | {_f(s2['sharpe'])} ({_ok(s2['thresholds_pass']['sharpe_oos'])}) | {bf['threshold_flips_s1_vs_s2']['sharpe_oos']} |")
    w(f"| PF > 1,15 | {_f(s1['profit_factor'])} ({_ok(s1['thresholds_pass']['profit_factor_oos'])}) | {_f(s2['profit_factor'])} ({_ok(s2['thresholds_pass']['profit_factor_oos'])}) | {bf['threshold_flips_s1_vs_s2']['profit_factor_oos']} |")
    w(f"| Trades clos ≥ 80 | {s1['n_trades_closed']} ({_ok(s1['thresholds_pass']['n_trades_oos_closed'])}) | {s2['n_trades_closed']} ({_ok(s2['thresholds_pass']['n_trades_oos_closed'])}) | {bf['threshold_flips_s1_vs_s2']['n_trades_oos_closed']} |")
    w(f"| MaxDD/SPY ≤ 1,5 | {_f(s1['maxdd_relative_to_benchmark'])} ({_ok(s1['thresholds_pass']['maxdd_relative_to_benchmark'])}) | {_f(s2['maxdd_relative_to_benchmark'])} ({_ok(s2['thresholds_pass']['maxdd_relative_to_benchmark'])}) | {bf['threshold_flips_s1_vs_s2']['maxdd_relative_to_benchmark']} |")
    w(f"| DSR ≥ 0,50 | {_f(s1['dsr'], 4)} ({_ok(s1['thresholds_pass']['dsr'])}) | {_f(s2['dsr'], 4)} ({_ok(s2['thresholds_pass']['dsr'])}) | {bf['threshold_flips_s1_vs_s2']['dsr']} |")
    dv = bf["decision_divergences"]
    w(
        f"\n`a = {bf['a']}` ; `sharpe_s1 = {_f(bf['sharpe_s1'])}` ; `sharpe_s2 = {_f(bf['sharpe_s2'])}` (Δ = {_f(bf['delta_sharpe_s2_minus_s1'])}) ; **`band_fragile = {str(bf['band_fragile']).lower()}`**. "
        f"Barres de décisions divergentes (sur {dv['n_bars_total_oos']} barres OOS) : D1 mêmes états = **{dv['n_decision_bars_divergent_same_state_D1']}** "
        f"({dv['n_decision_cells_divergent_same_state_D1']} cellules titre-barre sur {dv['n_active_cells_S1_path']} actives), D2 chemins indépendants = {dv['n_decision_bars_divergent_independent_paths_D2']} "
        f"({dv['n_decision_cells_divergent_independent_paths_D2']} cellules) ; ordres S1 {dv['n_orders_S1']} / S2 {dv['n_orders_S2']}. Réplica vs moteur : écart d'équity max "
        f"S1 {dv['replica_validation']['max_abs_equity_diff_vs_engine_S1']:.1e}, S2 {dv['replica_validation']['max_abs_equity_diff_vs_engine_S2']:.1e} "
        f"(`replica_matches_engine = {str(dv['replica_validation']['replica_matches_engine']).lower()}`).\n"
    )
    w(bf["knife_edge_note"] + "\n")
    w("### Bloc `cb` — exposition aux circuit breakers du wallet LABO\n")
    w(
        f"Seuils labo (`bot/config.py`, lecture seule) : half-size {_f(cba['cb_dd_half_size_pct'], 2, True)}, flatten {_f(cba['cb_dd_flatten_pct'], 2, True)}, gel d'entrées sur perte journalière > {_f(cba['cb_daily_loss_freeze_pct'], 2, True)}. "
        f"MaxDD de poche S2 (référence) : {_f(cb['pocket_maxdd_S2_for_reference'], 3, True)}.\n"
    )
    w("| Lecture de l'équity wallet | MaxDD wallet | Jours > half-size | Épisodes half-size | Jours > flatten | Épisodes flatten | 1er flatten | Épisodes gel perte jour. |")
    w("|---|---|---|---|---|---|---|---|")
    for name, c in (("E = 1 + a·(P−1) (SPEC, P concaténée)", cba), ("a ré-équilibré à chaque fenêtre", cbb)):
        w(
            f"| {name} | {_f(c['max_wallet_drawdown'], 3, True)} | {c['n_days_dd_above_half_size']} | {c['n_episodes_half_size']} | {c['n_days_dd_above_flatten']} | "
            f"{c['n_episodes_flatten']} | {c['first_flatten_date'] or '—'} | {c['n_episodes_freeze_entries']} |"
        )
    w(
        f"\n{cb['spec_expectation']} Constat : a × MaxDD poche = {_f(cb['a_times_pocket_maxdd_S1'], 3, True)} (S1) / {_f(cb['a_times_pocket_maxdd_S2'], 3, True)} (S2), "
        f"soit {'AU-DESSUS' if cb['spec_expectation_refuted_a_times_pocket_maxdd_above_half_size'] else 'sous'} du seuil half-size labo ({_f(cba['cb_dd_half_size_pct'], 2, True)}) : "
        f"l'attente a priori de la SPEC (MaxDD de poche 35-45 %) est {'RÉFUTÉE' if cb['spec_expectation_refuted_a_times_pocket_maxdd_above_half_size'] else 'confirmée'} "
        f"(MaxDD de poche mesuré {_f(cb['pocket_maxdd_S1_for_reference'], 3, True)} en S1). La première lecture (formule SPEC sur l'équity de poche concaténée) laisse la part de la poche dériver bien "
        f"au-delà de a (le reste du wallet est figé en cash alors que la poche compose sur 30 ans) : elle SURESTIME l'exposition d'un wallet réel ; la seconde (a ré-équilibré chaque année) est plus proche "
        f"d'un wallet vivant. {cb['not_modelled']}\n"
    )
    w("---\n")
    w("## 6. Lecture honnête des chiffres (ne remplace ni l'audit §1.4 ni l'arbitrage de l'orchestrateur)\n")
    re_ = st["candidate_realized_events"]
    neg_windows = ", ".join(
        f"#{pw['window_index']} ({pw['oos_start'][:4]}, {_f(pw['sharpe'], 2)})"
        for pw in res["candidate_lowvol"]["per_window"] if not nan_safe(pw["sharpe"]) and pw["sharpe"] < 0
    ) or "aucune"
    w(
        f"- Les 5 seuils §1.2 sont {'tous passés' if res['promotion_rules_1_2_all_pass'] else 'PAS tous passés'} en S1 {'et en S2' if not bf['band_fragile'] and all(bf['s2_pocket_units']['thresholds_pass'].values()) else '(voir §5 pour S2)'}, "
        f"**mais le chemin d'exécution du moteur n'est pas celui d'un portefeuille « 20 titres équipondérés, long-only, non levier »** : exposition brute moyenne {_f(st['candidate']['mean_gross_exposure'])} "
        f"(max {_f(st['candidate']['max_gross_exposure'])}), {_f(st['candidate_mean_number_of_held_names_S1_replica'], 1)} titres détenus en moyenne (max {st['candidate_max_number_of_held_names_S1_replica']}) au lieu de 20.\n"
        f"- Le profit factor ({_f(cand['profit_factor'], 2)}) est mécaniquement gonflé : {re_['n_gains']} évènements de réalisation gagnants contre {re_['n_losses']} perdants "
        f"(Σ gains {_f(re_['sum_gains'])} vs Σ pertes {_f(re_['sum_losses'])}) — une position dont le poids dérivé est tombé sous 0,05 n'est jamais vendue, donc ses pertes ne sont pas réalisées. "
        "Le seuil PF > 1,15 est franchi de très loin pour cette raison, pas par la robustesse de l'edge. Le décompte de trades clos est faussé dans le même sens.\n"
        f"- Le Sharpe est {_f(cand['sharpe'])} contre {_f(bench['sharpe'])} pour SPY, mais corrélation {_f(ha['correlations']['candidate_vs_spy'])} avec SPY et MaxDD de poche {_f(cand['max_drawdown'], 3, True)} "
        f"(SPY {_f(bench['max_drawdown'], 3, True)}) : fenêtres OOS à Sharpe négatif : {neg_windows} (cf. §8).\n"
        f"- Sous-périodes : Sharpe {_f(ha['subperiods_sharpe']['candidate']['avant_2010-01-01']['sharpe'])} avant 2010, {_f(ha['subperiods_sharpe']['candidate']['depuis_2010-01-01']['sharpe'])} depuis 2010, "
        f"{_f(ha['subperiods_sharpe']['candidate']['depuis_2020-01-01']['sharpe'])} depuis 2020 (SPY : {_f(ha['subperiods_sharpe']['benchmark_spy']['depuis_2020-01-01']['sharpe'])} depuis 2020).\n"
        "- Le contrôle apparié, adversaire obligatoire de la SPEC, n'est pas investi par le moteur : la question « la sélection low-vol bat-elle l'équipondéré large ? » reste **sans réponse** dans ce run. "
        "Aucune variante (bande 0, poids rééchelonnés, etc.) n'a été exécutée : la SPEC §5 impose qu'une variante soit un nouvel id avec un nouveau K_total.\n"
    )
    w("---\n")
    w("## 7. Réserves méthodologiques (inchangées par ce run)\n")
    w(
        "- **Biais du survivant** : constituants ACTUELS du S&P100 (réserve documentée par la SPEC).\n"
        "- **Pas de taux sans risque** soustrait des ratios ; pas de financement du levier implicite.\n"
        "- **Moteur** : pas de circuit breakers, caps ni vol targeting ; bande par TITRE (cf. A3).\n"
        "- **Contrôle** : voir §3 — son rôle d'adversaire (« équipondéré large ») n'est pas rempli si le moteur ne l'investit jamais.\n"
        "- **Audit adversarial §1.4** (points SIZING-FIDELITY §3.4 et BAND-FIDELITY §3.7) : à mener par une session distincte ; `isSound` non renseigné.\n"
    )
    w("---\n")
    w("## 8. Walk-forward fenêtre par fenêtre (candidate)\n")
    w("| # | OOS | combo retenue (L, N) | Sharpe IS | Sharpe OOS | Trades clos |")
    w("|---|---|---|---|---|---|")
    for pw in res["candidate_lowvol"]["per_window"]:
        cp = pw["chosen_params"]
        w(f"| {pw['window_index']} | {pw['oos_start']} → {pw['oos_end']} | ({cp['vol_window']}, {cp['n_holdings']}) | {_f(pw['is_sharpe_chosen'])} | {_f(pw['sharpe'])} | {pw['n_trades_closed']} |")
    w("")
    path.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
