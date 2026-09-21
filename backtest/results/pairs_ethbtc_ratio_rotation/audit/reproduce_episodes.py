"""AXE 7 -- Recompte independant des episodes de tilt OOS distincts (sans pairs_ratio.py pour
le signal ; utilise les chosen_params officiels, deja valides bit-exact par reproduce_windows.py
pour les 15 fenetres). Rapide (pas de simulation moteur, seulement etat)."""
import json
import sys
import numpy as np

sys.path.insert(0, "/home/claude/audit-copy")
from backtest import data_hourly as bt_data, engine
from audit_scripts.reproduce_signal import independent_log_ratio, independent_zscore, independent_state_machine, UNIVERSE

raw = bt_data.load_universe_raw("_data/crypto", UNIVERSE)
cal = bt_data.build_calendar(raw)
aligned = bt_data.align_universe_to_calendar(raw, cal)
closes = bt_data.closes_panel(aligned, UNIVERSE)
windows = engine.generate_walk_forward_windows(cal, is_months=9, oos_months=3, step_months=3)

official = json.load(open("/home/claude/audit-copy/backtest/results/pairs_ethbtc_ratio_rotation/results.json"))
off_pw = {pw["window_index"]: pw for pw in official["candidate_pairs_ratio"]["per_window"]}

state_cache = {}
def get_state(l_hours, theta):
    key = (l_hours, theta)
    if key not in state_cache:
        x = independent_log_ratio(closes)
        z = independent_zscore(x, l_hours)
        state_cache[key] = independent_state_machine(z, theta)
    return state_cache[key]

NEUTRAL = 0
episodes = []
for w in windows:
    chosen = off_pw[w.index]["chosen_params"]
    state_full = get_state(chosen["l_hours"], chosen["theta"])
    state_slice = state_full[w.oos_start_idx : w.oos_end_idx + 1]
    active = state_slice != NEUTRAL
    n = len(active)
    t = 0
    while t < n:
        if active[t]:
            start = t
            while t < n and active[t]:
                t += 1
            end = t - 1
            episodes.append({"window_index": w.index, "n_hours": end - start + 1})
        else:
            t += 1

n_episodes = len(episodes)
durations = [e["n_hours"] for e in episodes]
median_dur = float(np.median(durations))

print(f"n_episodes_distinct (independent) = {n_episodes}")
print(f"median_duration_hours (independent) = {median_dur}")
official_te = official["honesty_analyses"]["tilt_episodes"]
print(f"official n_episodes_distinct = {official_te['n_episodes_distinct']}")
print(f"official median_duration_hours = {official_te['median_duration_hours']}")
print(f"MATCH n_episodes: {n_episodes == official_te['n_episodes_distinct']}")
print(f"MATCH median_duration: {abs(median_dur - official_te['median_duration_hours']) < 1e-6}")
