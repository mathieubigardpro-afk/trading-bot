#!/usr/bin/env python3
"""ATTAQUE 1 -- Reproduction independante du signal (sans importer
backtest/strategies/protective_vol_gate.py), verification des 4 combos IS pour 3 fenetres
(0, une mediane, une 2024+), et comparaison au results.json officiel.

N'importe QUE backtest.data_hourly et backtest.engine (moteur commun) + bot.config -- jamais
backtest.strategies.protective_vol_gate.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path("/home/claude/audit-copy")
sys.path.insert(0, str(REPO))

from backtest import data_hourly as bt_data
from backtest import engine
from backtest import metrics as bt_metrics
from backtest import risk_overlay
from bot import config as bot_cfg

UNIVERSE = list(bot_cfg.SYMBOLS_CRYPTO)
assert UNIVERSE == ["BTC", "ETH", "SOL", "DOGE", "LINK", "AVAX"]

# ---- reimplementation from scratch, independent of pvg module -----------------------------
P_OUT = 0.80
R_HOURS = 72
PCT_WINDOW = 4320
MIN_VALID = 2160


def my_basket_return(closes: pd.DataFrame) -> pd.Series:
    rets = closes.diff() / closes.shift(1)
    return rets.mean(axis=1, skipna=True)


def my_realized_vol(r: pd.Series, V: int) -> pd.Series:
    return r.rolling(V, min_periods=V).std(ddof=1)


def my_percentile_rank(vol: pd.Series) -> pd.Series:
    return vol.rolling(PCT_WINDOW, min_periods=MIN_VALID).rank(pct=True)


def my_state_machine(pct: np.ndarray, p_in: float):
    n = len(pct)
    g = np.empty(n)
    state = np.empty(n, dtype=np.int8)
    cur_state = 0  # 0=invested,1=protected,2=redeploy
    cur_g = 1.0
    ramp = 0
    for t in range(n):
        p = pct[t]
        if p != p:  # NaN
            cur_state = 0
            cur_g = 1.0
        elif cur_state == 0:
            if p >= p_in:
                cur_state = 1
                cur_g = 0.0
            else:
                cur_g = 1.0
        elif cur_state == 1:
            if p <= P_OUT:
                cur_state = 2
                ramp = 0
                cur_g = 0.0
            else:
                cur_g = 0.0
        else:
            if p >= p_in:
                cur_state = 1
                cur_g = 0.0
            else:
                ramp += 1
                if ramp >= R_HOURS:
                    cur_state = 0
                    cur_g = 1.0
                else:
                    cur_g = ramp / R_HOURS
        state[t] = cur_state
        g[t] = cur_g
    return state, g


def my_weights(closes: pd.DataFrame, V: int, p_in: float) -> pd.DataFrame:
    r = my_basket_return(closes)
    vol = my_realized_vol(r, V)
    pct = my_percentile_rank(vol)
    _, g = my_state_machine(pct.to_numpy(), p_in)
    w = pd.DataFrame({sym: g / 6.0 for sym in closes.columns}, index=closes.index)
    return w.fillna(0.0)


MY_GRID = [
    {"vol_window_hours": 24, "p_in": 0.95},
    {"vol_window_hours": 24, "p_in": 0.98},
    {"vol_window_hours": 72, "p_in": 0.95},
    {"vol_window_hours": 72, "p_in": 0.98},
]


def main():
    print("[data] loading ...")
    raw = bt_data.load_universe_raw(str(REPO / "_data" / "crypto"), UNIVERSE)
    calendar = bt_data.build_calendar(raw)
    aligned = bt_data.align_universe_to_calendar(raw, calendar)
    opens = bt_data.opens_panel(aligned, UNIVERSE)
    closes = bt_data.closes_panel(aligned, UNIVERSE)
    print(f"[data] calendar {calendar[0]} -> {calendar[-1]}, {len(calendar)} hours")

    windows = engine.generate_walk_forward_windows(calendar, is_months=9, oos_months=3, step_months=3)
    print(f"[wf] {len(windows)} windows")

    results = json.load(open(REPO / "backtest/results/protective_vol_gate_6majors/results.json"))
    official_pw = results["candidate_protective_vol_gate"]["per_window"]

    sim_kwargs = dict(
        vol_ewma_halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
        vol_periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR,
    )

    # precompute weights matrices for each combo ONCE
    weight_cache = {}
    for combo in MY_GRID:
        key = tuple(sorted(combo.items()))
        weight_cache[key] = my_weights(closes, combo["vol_window_hours"], combo["p_in"])

    def provider(params):
        key = tuple(sorted(params.items()))
        return weight_cache[key]

    # Sequential walk-forward reproduction WITH carry_across_windows=True (must match the
    # official run windows 0..14, since carry state depends on the full chain up to that point).
    prev_carry_out = None
    all_ok = True
    my_segments = []
    for w in windows:
        official = official_pw[w.index]
        is_start_idx_safe = max(1, w.is_start_idx)
        sel = engine.select_params_via_is(
            provider, calendar, opens, closes, 25.0, is_start_idx_safe, w.is_end_idx,
            param_grid=MY_GRID, sim_kwargs=sim_kwargs,
        )
        chosen_ok = sel.chosen_params == official["chosen_params"]

        carry_in_this = None
        if prev_carry_out is not None and int(prev_carry_out.last_idx) + 1 == w.oos_start_idx:
            carry_in_this = prev_carry_out

        weights_chosen = provider(sel.chosen_params)
        seg = engine.simulate_segment(
            calendar, weights_chosen, opens, closes, w.oos_start_idx, w.oos_end_idx, 25.0,
            carry_in=carry_in_this, **sim_kwargs,
        )
        prev_carry_out = seg.carry_out
        my_segments.append(seg)

        my_sharpe = bt_metrics.sharpe_ratio(seg.returns, periods_per_year=8760.0)
        diff_sharpe = abs(my_sharpe - official["sharpe"])
        diff_trades = abs(len(seg.trades_closed) - official["n_trades_closed"])
        carry_match = (carry_in_this is not None) == official["carry_in_used"]
        ok = chosen_ok and diff_sharpe < 1e-9 and diff_trades == 0 and carry_match
        all_ok = all_ok and ok
        flag = "OK" if ok else "MISMATCH"
        print(
            f"window {w.index:2d} [{flag}] chosen_match={chosen_ok} carry_in(my={carry_in_this is not None},"
            f"off={official['carry_in_used']}) sharpe(my={my_sharpe:.6f},off={official['sharpe']:.6f},"
            f"diff={diff_sharpe:.2e}) trades(my={len(seg.trades_closed)},off={official['n_trades_closed']})"
        )

    print("\n=== Concatenated comparison ===")
    concat = engine.concatenate_segments(my_segments)
    my_sharpe_c = bt_metrics.sharpe_ratio(concat.returns, periods_per_year=8760.0)
    my_pf_c = bt_metrics.profit_factor([e["pnl"] for e in concat.realized_events])
    off_c = results["candidate_protective_vol_gate"]["concatenated"]
    print(f"my concat sharpe={my_sharpe_c:.6f} official={off_c['sharpe']:.6f}")
    print(f"my concat PF={my_pf_c:.6f} official={off_c['profit_factor']:.6f}")
    print(f"my concat n_trades={len(concat.trades_closed)} official={off_c['n_trades_closed']}")

    print("\nALL WINDOWS MATCH:" , all_ok)


if __name__ == "__main__":
    main()
