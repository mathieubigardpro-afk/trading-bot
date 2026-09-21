"""Reproduction indépendante de 3 fenêtres OOS completes (0, 1, 14) + sélection IS, en
utilisant les poids générés par reproduce_signal.py (SANS pairs_ratio.py) mais le moteur
commun engine.py (deja audité séparément, hors périmètre de cet audit sauf mésusage runner).
Compare au results.json officiel.
"""
import json
import sys
import time
import numpy as np
import pandas as pd

sys.path.insert(0, "/home/claude/audit-copy")
from backtest import data_hourly as bt_data, engine, metrics as bt_metrics, risk_overlay
from audit_scripts.reproduce_signal import independent_weights, GRID, UNIVERSE

COST_BPS = 15.0
sim_kwargs = dict(
    vol_ewma_halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
    vol_periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR,
)

raw = bt_data.load_universe_raw("_data/crypto", UNIVERSE)
cal = bt_data.build_calendar(raw)
aligned = bt_data.align_universe_to_calendar(raw, cal)
opens = bt_data.opens_panel(aligned, UNIVERSE)
closes = bt_data.closes_panel(aligned, UNIVERSE)

windows = engine.generate_walk_forward_windows(cal, is_months=9, oos_months=3, step_months=3)
assert len(windows) == 15

weights_cache = {}
def get_weights(params):
    key = tuple(sorted(params.items()))
    if key not in weights_cache:
        weights_cache[key] = independent_weights(closes, params["l_hours"], params["theta"])
    return weights_cache[key]

def summarize(seg):
    returns = seg.returns
    pnls = [e["pnl"] for e in seg.realized_events]
    equity = (1.0 + returns).cumprod()
    equity_with_base = pd.concat([pd.Series([1.0]), equity])
    return {
        "sharpe": bt_metrics.sharpe_ratio(returns, periods_per_year=8760.0),
        "profit_factor": bt_metrics.profit_factor(pnls),
        "max_drawdown": bt_metrics.max_drawdown(equity_with_base),
        "n_trades_closed": len(seg.trades_closed),
        "n_periods": len(returns),
    }

results = json.load(open("backtest/results/pairs_ethbtc_ratio_rotation/results.json"))
official_per_window = {pw["window_index"]: pw for pw in results["candidate_pairs_ratio"]["per_window"]}

test_window_indices = [0, 1, 14]
prev_carry_out = None
segments_all_for_carry = {}  # need carry chain up to window 14, so must run 0..14 sequentially for carry continuity
# To correctly test carry_across_windows, we must run ALL windows sequentially (carry-in depends on prior).
# But to save time we do a full sequential run (still much faster than 29 min since no stationarity tests / stress / control).

t0 = time.time()
per_window_repro = []
prev_carry_out = None
for w in windows:
    is_start_idx_safe = max(1, w.is_start_idx)
    # IS selection: pick best Sharpe among 4 combos on IS window (same rule as engine.select_params_via_is)
    best_params, best_sharpe = None, float("-inf")
    for params in GRID:
        wdf = get_weights(params)
        seg_is = engine.simulate_segment(cal, wdf, opens, closes, is_start_idx_safe, w.is_end_idx, COST_BPS, **sim_kwargs)
        sh = bt_metrics.sharpe_ratio(seg_is.returns)  # default periods_per_year=252 (matches engine.select_params_via_is)
        if not np.isnan(sh) and sh > best_sharpe:
            best_sharpe = sh
            best_params = params
    chosen = best_params
    wdf_chosen = get_weights(chosen)

    carry_in = None
    if prev_carry_out is not None and int(prev_carry_out.last_idx) + 1 == w.oos_start_idx:
        carry_in = prev_carry_out

    seg = engine.simulate_segment(cal, wdf_chosen, opens, closes, w.oos_start_idx, w.oos_end_idx, COST_BPS,
                                   carry_in=carry_in, **sim_kwargs)
    prev_carry_out = seg.carry_out
    summ = summarize(seg)
    summ["window_index"] = w.index
    summ["chosen_params"] = chosen
    per_window_repro.append(summ)
    if w.index in test_window_indices:
        off = official_per_window[w.index]
        print(f"--- window {w.index} ---")
        print(f"  chosen: repro={chosen} official={off['chosen_params']}")
        print(f"  sharpe: repro={summ['sharpe']:.6f} official={off['sharpe']:.6f} diff={abs(summ['sharpe']-off['sharpe']):.2e}")
        print(f"  pf:     repro={summ['profit_factor']:.6f} official={off['profit_factor']:.6f}")
        print(f"  maxdd:  repro={summ['max_drawdown']:.6f} official={off['max_drawdown']:.6f}")
        print(f"  trades: repro={summ['n_trades_closed']} official={off['n_trades_closed']}")
    print(f"[done window {w.index}] {time.time()-t0:.1f}s", flush=True)

print(f"TOTAL runtime: {time.time()-t0:.1f}s")

json.dump([{k:v for k,v in pw.items()} for pw in per_window_repro],
          open("/tmp/claude-0/-home-claude/07bb64b5-cd60-5505-ba61-1f68df5929c7/scratchpad/repro_per_window.json","w"), default=str, indent=2)
