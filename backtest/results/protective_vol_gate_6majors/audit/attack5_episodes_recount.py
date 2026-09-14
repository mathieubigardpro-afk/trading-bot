#!/usr/bin/env python3
"""ATTAQUE 5 -- Recompte independant des episodes de protection OOS (SPEC.md sect 7.1) a partir
des etats recalcules nous-memes (deja valides causalite/reproduction en attaques 1-2), avec les
chosen_params officiels de results.json (pour ne pas dependre de la selection IS, deja testee en
attaque 1). Verifie : nombre d'episodes, evites/rates, somme nette, et absence de biais de
classification (avoided = bh_return < 0 strictement -- verifie les cas limites pres de 0)."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path("/home/claude/audit-copy")
sys.path.insert(0, str(REPO))

from backtest import data_hourly as bt_data
from backtest import engine
from backtest.strategies import protective_vol_gate as pvg
from bot import config as bot_cfg

UNIVERSE = list(bot_cfg.SYMBOLS_CRYPTO)


def main():
    results = json.load(open(REPO / "backtest/results/protective_vol_gate_6majors/results.json"))
    official_pw = results["candidate_protective_vol_gate"]["per_window"]
    official_episodes = results["honesty_analyses"]["protection_episodes"]

    raw = bt_data.load_universe_raw(str(REPO / "_data" / "crypto"), UNIVERSE)
    calendar = bt_data.build_calendar(raw)
    aligned = bt_data.align_universe_to_calendar(raw, calendar)
    closes = bt_data.closes_panel(aligned, UNIVERSE)

    windows = engine.generate_walk_forward_windows(calendar, is_months=9, oos_months=3, step_months=3)

    r_full = pvg.basket_return(closes)
    state_cache = {}

    def get_state(params):
        key = tuple(sorted(params.items()))
        if key not in state_cache:
            state_cache[key] = pvg.generate_state_and_g(closes, pvg.ProtectiveVolGateParams(**params))
        return state_cache[key]

    episodes = []
    for w, official in zip(windows, official_pw):
        chosen = official["chosen_params"]
        sg = get_state(chosen)
        state_slice = sg["state"].iloc[w.oos_start_idx : w.oos_end_idx + 1]
        r_slice = r_full.iloc[w.oos_start_idx : w.oos_end_idx + 1]
        active = state_slice.to_numpy() != pvg.STATE_INVESTED
        idx = state_slice.index
        n = len(active)
        t = 0
        while t < n:
            if active[t]:
                start = t
                while t < n and active[t]:
                    t += 1
                end = t - 1
                span = r_slice.iloc[start : end + 1]
                bh = float((1.0 + span.fillna(0.0)).prod() - 1.0)
                episodes.append(
                    {
                        "window_index": w.index, "start": str(idx[start]), "end": str(idx[end]),
                        "n_hours": end - start + 1, "basket_bh_return_over_episode": bh,
                        "avoided": bool(bh < 0.0),
                    }
                )
            else:
                t += 1

    n_ep = len(episodes)
    n_avoided = sum(1 for e in episodes if e["avoided"])
    n_missed = n_ep - n_avoided
    net_sum = sum(e["basket_bh_return_over_episode"] for e in episodes)

    print(f"My n_episodes = {n_ep}  (official = {official_episodes['n_episodes_distinct']})")
    print(f"My n_avoided (bh<0) = {n_avoided}  (official = {official_episodes['n_episodes_avoided_negative_bh']})")
    print(f"My n_missed (bh>=0) = {n_missed}  (official = {official_episodes['n_episodes_missed_positive_bh']})")
    print(f"My net_sum = {net_sum:.8f}  (official = {official_episodes['sum_net_basket_bh_return_over_all_episodes']:.8f})")

    # bit-exact episode-by-episode comparison
    assert len(episodes) == len(official_episodes["episodes"]), "episode count mismatch"
    max_diff = 0.0
    for mine, off in zip(episodes, official_episodes["episodes"]):
        assert mine["window_index"] == off["window_index"]
        assert mine["start"] == off["start"] and mine["end"] == off["end"], (mine, off)
        d = abs(mine["basket_bh_return_over_episode"] - off["basket_bh_return_over_episode"])
        max_diff = max(max_diff, d)
        assert mine["avoided"] == off["avoided"]
    print(f"Max abs diff per-episode bh_return: {max_diff:.2e}")

    # Classification robustness: any episode very close to 0 (borderline avoided/missed)?
    near_zero = [e for e in episodes if abs(e["basket_bh_return_over_episode"]) < 0.005]
    print(f"\nEpisodes with |bh_return| < 0.5% (borderline avoided/missed classification): {len(near_zero)}")
    for e in near_zero:
        print(f"  window {e['window_index']} [{e['start']} -> {e['end']}] n_hours={e['n_hours']} bh={e['basket_bh_return_over_episode']:.5f} avoided={e['avoided']}")

    # Distribution of episode durations and returns
    durations = [e["n_hours"] for e in episodes]
    bh_returns = [e["basket_bh_return_over_episode"] for e in episodes]
    print(f"\nEpisode duration (hours): min={min(durations)} median={sorted(durations)[len(durations)//2]} max={max(durations)}")
    print(f"n_episodes with duration <= 3h (likely instant re-trigger noise): {sum(1 for d in durations if d <= 3)}")
    print(f"n_episodes with duration >= 72h (full ramp completed, i.e. NOT re-cut mid-ramp): {sum(1 for d in durations if d >= 72)}")

    # Cross-check n_trades_closed_oos (139) vs n_episodes (35): plausibility of ~4 trades/episode
    # (6 symbols entering/exiting somewhat independently in time due to per-symbol NaN alignment
    # -- but g is IDENTICAL across all 6 symbols by construction (single basket signal) so a
    # "cut" should close all 6 lines near-simultaneously; count trades per episode boundary.
    print(f"\nn_trades_closed official = {official_pw[0].get('n_trades_closed', None)} (per-window, see full concatenated 139)")
    total_trades_from_pw = sum(w['n_trades_closed'] for w in official_pw)
    print(f"sum(n_trades_closed per window) = {total_trades_from_pw} (should equal concatenated 139)")


if __name__ == "__main__":
    main()
