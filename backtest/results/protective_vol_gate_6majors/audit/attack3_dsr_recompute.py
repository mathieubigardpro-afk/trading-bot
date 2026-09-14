#!/usr/bin/env python3
"""ATTAQUE 3 -- Recalcul FROM SCRATCH du Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014)
sur les rendements horaires OOS concatenes de la candidate (recalcules nous-memes, pas
importes de results.json), et verification independante de K_total = 89."""
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

REPO = Path("/home/claude/audit-copy")
sys.path.insert(0, str(REPO))

EULER_MASCHERONI = 0.5772156649015329


def my_sharpe_std_error(n, skew, kurt_excess, sr_hat):
    kurt_pearson = kurt_excess + 3.0
    variance = (1.0 - skew * sr_hat + (kurt_pearson - 1.0) / 4.0 * sr_hat**2) / (n - 1)
    return math.sqrt(max(variance, 0.0))


def my_expected_max_sharpe(trials_k, sr_std):
    if trials_k <= 1:
        return 0.0
    if sr_std <= 0:
        return 0.0
    z1 = stats.norm.ppf(1.0 - 1.0 / trials_k)
    z2 = stats.norm.ppf(1.0 - 1.0 / (trials_k * math.e))
    return sr_std * ((1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)


def my_dsr(returns, trials_k):
    r = pd.Series(returns).dropna()
    n = len(r)
    sr_hat = r.mean() / r.std(ddof=1)
    skew = stats.skew(r, bias=False)
    kurt_excess = stats.kurtosis(r, fisher=True, bias=False)
    sr_std = my_sharpe_std_error(n, skew, kurt_excess, sr_hat)
    sr0 = my_expected_max_sharpe(trials_k, sr_std)
    z = (sr_hat - sr0) / sr_std
    dsr = stats.norm.cdf(z)
    return dict(n=n, sr_hat=sr_hat, skew=skew, kurt_excess=kurt_excess, sr_std=sr_std, sr0=sr0, dsr=dsr)


def main():
    results = json.load(open(REPO / "backtest/results/protective_vol_gate_6majors/results.json"))
    official_dsr = results["dsr_candidate"]

    # Reconstruct the OOS-concatenated candidate returns FROM THE PER-WINDOW DATA is not
    # directly stored (only summaries) -- so instead we independently RE-RUN the walk-forward
    # (reusing attack1's exact reproduction, already verified bit-exact) to get the returns
    # series, then compute DSR ourselves. We import the strategy module here (already verified
    # causality/reproduction independently in attacks 1-2), focus of THIS attack = the DSR
    # formula itself + K_total, not the signal.
    from backtest import data_hourly as bt_data
    from backtest import engine
    from backtest.strategies import protective_vol_gate as pvg
    from backtest import risk_overlay
    from bot import config as bot_cfg

    UNIVERSE = list(bot_cfg.SYMBOLS_CRYPTO)
    raw = bt_data.load_universe_raw(str(REPO / "_data" / "crypto"), UNIVERSE)
    calendar = bt_data.build_calendar(raw)
    aligned = bt_data.align_universe_to_calendar(raw, calendar)
    opens = bt_data.opens_panel(aligned, UNIVERSE)
    closes = bt_data.closes_panel(aligned, UNIVERSE)

    windows = engine.generate_walk_forward_windows(calendar, is_months=9, oos_months=3, step_months=3)
    print(f"n_windows independently recomputed = {len(windows)} (SPEC expects 15-16, results.json says 15)")

    sim_kwargs = dict(
        vol_ewma_halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
        vol_periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR,
    )
    weight_cache = {}

    def provider(params):
        key = tuple(sorted(params.items()))
        if key not in weight_cache:
            weight_cache[key] = pvg.generate_weight_decisions(closes, pvg.ProtectiveVolGateParams(**params))
        return weight_cache[key]

    prev_carry_out = None
    segments = []
    for w in windows:
        is_start_idx_safe = max(1, w.is_start_idx)
        sel = engine.select_params_via_is(
            provider, calendar, opens, closes, 25.0, is_start_idx_safe, w.is_end_idx,
            param_grid=pvg.PARAM_GRID, sim_kwargs=sim_kwargs,
        )
        weights_chosen = provider(sel.chosen_params)
        carry_in_this = None
        if prev_carry_out is not None and int(prev_carry_out.last_idx) + 1 == w.oos_start_idx:
            carry_in_this = prev_carry_out
        seg = engine.simulate_segment(
            calendar, weights_chosen, opens, closes, w.oos_start_idx, w.oos_end_idx, 25.0,
            carry_in=carry_in_this, **sim_kwargs,
        )
        prev_carry_out = seg.carry_out
        segments.append(seg)

    concat = engine.concatenate_segments(segments)
    my_returns = concat.returns
    print(f"n_obs OOS concatenated (independently re-simulated) = {len(my_returns)} (official n_obs={official_dsr['n_obs']})")

    k_total = 14 + len(windows) * 4 + len(windows) * 1
    print(f"K_total independently recomputed = {k_total} (official = {official_dsr['trials_k']})")

    mine = my_dsr(my_returns, k_total)
    print("\n=== My from-scratch DSR (Bailey & Lopez de Prado 2014) ===")
    for k, v in mine.items():
        print(f"  {k}: {v}")

    print("\n=== Official (results.json / backtest/metrics.py) ===")
    for k, v in official_dsr.items():
        print(f"  {k}: {v}")

    print("\n=== Diffs ===")
    print(f"  dsr diff = {abs(mine['dsr'] - official_dsr['dsr']):.10f}")
    print(f"  sharpe_hat_period diff = {abs(mine['sr_hat'] - official_dsr['sharpe_hat_period']):.10f}")
    print(f"  n_obs diff = {abs(mine['n'] - official_dsr['n_obs'])}")
    print(f"  skew diff = {abs(mine['skew'] - official_dsr['skew']):.10f}")
    print(f"  kurtosis diff = {abs(mine['kurt_excess'] - official_dsr['kurtosis_excess']):.10f}")

    # Sensitivity: does the DSR verdict change under plausible alternate registry counts (e.g.
    # +/- 1 or 2 rows, or excluding the control combo from K_total)?
    print("\n=== Sensitivite K_total ===")
    for k_alt, label in [
        (14 + 15 * 4, "sans le controle (K = registry + candidate seule)"),
        (14 + 15 * 4 + 15 * 1, "formule SPEC (candidate+controle)"),
        (9 + 15 * 4 + 15 * 1, "si on utilisait k_total_at_registry_init=9 au lieu de 14"),
        (1, "K=1 (borne basse absurde, aucune deflation)"),
    ]:
        m = my_dsr(my_returns, k_alt)
        print(f"  K_total={k_alt} ({label}) -> DSR={m['dsr']:.6f}  (seuil 0.50 -> {'PASS' if m['dsr']>=0.5 else 'FAIL'})")


if __name__ == "__main__":
    main()
