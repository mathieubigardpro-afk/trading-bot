"""AXE 2 -- LOOK-AHEAD : perturbe les closes APRES une date t, verifie que signal/etat/poids
AVANT t restent bit-identiques (le signal ne doit dependre que de closes[<=t]).
Independent re-implementation (imports pairs_ratio directly here is fine -- this attacks
the actual candidate module, not a from-scratch reproduction).
"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, "/home/claude/audit-copy")
from backtest import data_hourly as bt_data
from backtest.strategies import pairs_ratio as pr

UNIVERSE = ["BTC", "ETH"]
raw = bt_data.load_universe_raw("_data/crypto", UNIVERSE)
cal = bt_data.build_calendar(raw)
aligned = bt_data.align_universe_to_calendar(raw, cal)
closes = bt_data.closes_panel(aligned, UNIVERSE)

# Pick a perturbation cutoff t somewhere in the middle
t_idx = 20000
t_ts = closes.index[t_idx]
print(f"Perturbation cutoff t = index {t_idx}, timestamp {t_ts}")

results_summary = []
for params in pr.PARAM_GRID:
    p = pr.PairsRatioParams(**params)
    w_orig = pr.generate_weight_decisions(closes, p)
    s_orig = pr.generate_state(closes, p)

    closes_perturbed = closes.copy()
    # Perturb ALL data strictly AFTER t_idx (never touch t_idx itself or before)
    n = len(closes_perturbed)
    rng = np.random.default_rng(42)
    factor = 1.0 + rng.uniform(-0.5, 0.5, size=(n - (t_idx + 1), len(UNIVERSE)))
    closes_perturbed.iloc[t_idx + 1:, :] = closes_perturbed.iloc[t_idx + 1:, :].to_numpy() * factor

    w_pert = pr.generate_weight_decisions(closes_perturbed, p)
    s_pert = pr.generate_state(closes_perturbed, p)

    # Compare everything up to and INCLUDING t_idx (signal/state/weights decided AT t_idx
    # must depend only on closes[<=t_idx])
    w_before_orig = w_orig.iloc[: t_idx + 1].to_numpy()
    w_before_pert = w_pert.iloc[: t_idx + 1].to_numpy()
    identical_weights = np.array_equal(w_before_orig, w_before_pert)

    s_before_orig = s_orig["state"].iloc[: t_idx + 1].to_numpy()
    s_before_pert = s_pert["state"].iloc[: t_idx + 1].to_numpy()
    identical_state = np.array_equal(s_before_orig, s_before_pert)

    z_before_orig = s_orig["z"].iloc[: t_idx + 1].to_numpy()
    z_before_pert = s_pert["z"].iloc[: t_idx + 1].to_numpy()
    identical_z = np.allclose(z_before_orig, z_before_pert, equal_nan=True)

    # sanity: AFTER t_idx should generally DIFFER (perturbation had an effect)
    w_after_orig = w_orig.iloc[t_idx + 1:].to_numpy()
    w_after_pert = w_pert.iloc[t_idx + 1:].to_numpy()
    differs_after = not np.array_equal(w_after_orig, w_after_pert)

    print(f"params={params}: weights<=t identical={identical_weights}, state<=t identical={identical_state}, "
          f"z<=t identical(allclose)={identical_z}, weights>t differ (perturbation had effect)={differs_after}")
    results_summary.append((params, identical_weights, identical_state, identical_z, differs_after))

all_ok = all(r[1] and r[2] and r[3] and r[4] for r in results_summary)
print()
print("VERDICT look-ahead (perturbation test):", "PASS -- no look-ahead detected" if all_ok else "FAIL -- LOOK-AHEAD DETECTED")
