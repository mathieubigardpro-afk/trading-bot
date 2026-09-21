"""AXE 5 (complement) -- verifie que la bande no_trade_band=0.05 ne reproduit pas
l'artefact "faible poids" (funding_carry) : inspecte la distribution reelle des poids
SCALES par l'overlay (post vol-targeting) pour la candidate sur une fenetre OOS a forte
vol (fenetre 14, la derniere, theta bas L court -> tilte souvent) et verifie l'ecart entre
etats NEUTRE (poids ~0.5*scalar/actif) et TILT (poids ~1.0*scalar/actif) reste
generalement > bande 0.05.
"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, "/home/claude/audit-copy")
from backtest import data_hourly as bt_data, engine, risk_overlay
from backtest.strategies import pairs_ratio as pr

UNIVERSE = ["BTC", "ETH"]
raw = bt_data.load_universe_raw("_data/crypto", UNIVERSE)
cal = bt_data.build_calendar(raw)
aligned = bt_data.align_universe_to_calendar(raw, cal)
opens = bt_data.opens_panel(aligned, UNIVERSE)
closes = bt_data.closes_panel(aligned, UNIVERSE)
windows = engine.generate_walk_forward_windows(cal, is_months=9, oos_months=3, step_months=3)
w14 = windows[14]

# window 14 chosen params per results.json / reproduction = l_hours=720, theta=2.0
params = pr.PairsRatioParams(l_hours=720, theta=2.0)
weights_decided = pr.generate_weight_decisions(closes, params)

vol_annual_full, valid_count_full = risk_overlay.precompute_vol_stats(
    closes, halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
    periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR,
)

scaled_rows = []
for i in range(w14.oos_start_idx, w14.oos_end_idx + 1):
    raw_w = weights_decided.iloc[i - 1]
    vol_scalar = risk_overlay.compute_portfolio_vol_scalar(
        raw_w, vol_annual_full.iloc[i - 1], valid_count_full.iloc[i - 1],
        target_vol_annualized=risk_overlay.DEFAULT_VOL_TARGET_ANNUALIZED,
    )
    scaled_rows.append({"idx": i, "raw_BTC": raw_w["BTC"], "raw_ETH": raw_w["ETH"], "scalar": vol_scalar,
                         "scaled_BTC": raw_w["BTC"] * vol_scalar, "scaled_ETH": raw_w["ETH"] * vol_scalar})

df = pd.DataFrame(scaled_rows)
print("vol_scalar stats over window 14 OOS:")
print(df["scalar"].describe())
print()
print("scaled weight (BTC leg) stats:")
print(df["scaled_BTC"].describe())
print()
# Compute the typical jump size at a NEUTRAL->TILT transition (0.5*scalar -> 1.0*scalar, per leg)
median_scalar = df["scalar"].median()
min_scalar = df["scalar"].min()
print(f"median vol_scalar = {median_scalar:.4f} -> typical NEUTRAL->TILT jump per leg = {0.5*median_scalar:.4f} (band=0.05)")
print(f"min vol_scalar = {min_scalar:.4f} (worst case, highest vol) -> smallest possible jump per leg = {0.5*min_scalar:.4f} (band=0.05)")
print(f"Jump always > band? {(0.5*df['scalar'] > 0.05).all()}")
