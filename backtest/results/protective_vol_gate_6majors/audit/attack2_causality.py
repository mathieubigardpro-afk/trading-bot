#!/usr/bin/env python3
"""ATTAQUE 2 -- Look-ahead : perturbe les closes STRICTEMENT APRES une date t (bruit aleatoire)
et verifie bit-a-bit que les poids decides <= t (signal + machine a etats + percentile rolling
rank) sont inchanges. Importe backtest.strategies.protective_vol_gate directement (l'attaque
porte sur la causalite du CODE tel quel, pas sur une reimplementation) -- complementaire de
l'attaque 1 (reproduction independante).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path("/home/claude/audit-copy")
sys.path.insert(0, str(REPO))

from backtest import data_hourly as bt_data
from backtest.strategies import protective_vol_gate as pvg
from bot import config as bot_cfg

UNIVERSE = list(bot_cfg.SYMBOLS_CRYPTO)


def main():
    raw = bt_data.load_universe_raw(str(REPO / "_data" / "crypto"), UNIVERSE)
    calendar = bt_data.build_calendar(raw)
    aligned = bt_data.align_universe_to_calendar(raw, calendar)
    closes = bt_data.closes_panel(aligned, UNIVERSE)

    n = len(calendar)
    cut_positions = [n // 4, n // 2, 3 * n // 4, n - 200]  # several cut points across history

    combo = {"vol_window_hours": 24, "p_in": 0.95}
    params = pvg.ProtectiveVolGateParams(**combo)

    baseline = pvg.generate_state_and_g(closes, params)
    baseline_w = pvg.generate_weight_decisions(closes, params)

    rng = np.random.default_rng(1234)
    all_ok = True
    for cut in cut_positions:
        t_cut = calendar[cut]
        perturbed = closes.copy()
        # Replace ALL data strictly AFTER t_cut (positions cut+1..end) with random noise
        # (positive, unrelated to real prices) -- also perturb warm-up sensitive quantities by
        # using a wildly different scale to make any leak obvious.
        noise_shape = perturbed.iloc[cut + 1 :].shape
        noise = rng.uniform(1.0, 1_000_000.0, size=noise_shape)
        perturbed.iloc[cut + 1 :] = noise

        perturbed_sg = pvg.generate_state_and_g(perturbed, params)
        perturbed_w = pvg.generate_weight_decisions(perturbed, params)

        # Compare EVERYTHING at positions <= cut (must be bit-identical: r, vol, percentile,
        # state, g -- and NaN must match NaN, not just equal numbers)
        cols_to_check = ["r", "vol", "percentile", "state", "g"]
        mismatches = {}
        for c in cols_to_check:
            a = baseline[c].iloc[: cut + 1].to_numpy()
            b = perturbed_sg[c].iloc[: cut + 1].to_numpy()
            both_nan = np.isnan(a) & np.isnan(b) if a.dtype.kind == "f" else np.zeros_like(a, dtype=bool)
            eq = (a == b) | both_nan
            n_bad = int((~eq).sum())
            if n_bad > 0:
                mismatches[c] = n_bad

        w_a = baseline_w.iloc[: cut + 1].to_numpy()
        w_b = perturbed_w.iloc[: cut + 1].to_numpy()
        n_bad_w = int((w_a != w_b).sum())
        if n_bad_w > 0:
            mismatches["weights"] = n_bad_w

        ok = len(mismatches) == 0
        all_ok = all_ok and ok
        print(
            f"cut@{cut} (t={t_cut}) : {'OK bit-exact <= t' if ok else 'LEAK DETECTED: ' + str(mismatches)}"
        )

    # Extra: verify warm-up boundary explicitly -- state must be INVESTED (g=1) whenever
    # percentile is NaN, at EVERY position, not just at the very start.
    pct = baseline["percentile"]
    nan_mask = pct.isna()
    g = baseline["g"]
    bad_warmup = int(((nan_mask) & (g != 1.0)).sum())
    print(f"\nWarm-up rule (percentile NaN => g==1.0 forced) violations: {bad_warmup} (expected 0)")
    n_nan = int(nan_mask.sum())
    print(f"Number of NaN percentile rows (warm-up length): {n_nan} (expected == MIN_VALID_VOL_OBS-related, ~2160+23 for V=24)")

    print(f"\nALL CAUSALITY CHECKS PASSED: {all_ok and bad_warmup == 0}")


if __name__ == "__main__":
    main()
