"""Reproduction indépendante du signal pairs_ethbtc_ratio_rotation, SANS importer
backtest/strategies/pairs_ratio.py — réimplémentation from scratch à partir de la SPEC.md
uniquement, pour comparaison bit-à-bit avec les résultats du runner officiel.
"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, "/home/claude/audit-copy")
from backtest import data_hourly as bt_data, engine

UNIVERSE = ["BTC", "ETH"]
GRID = [
    {"l_hours": 720, "theta": 1.5},
    {"l_hours": 720, "theta": 2.0},
    {"l_hours": 2160, "theta": 1.5},
    {"l_hours": 2160, "theta": 2.0},
]


def independent_log_ratio(closes: pd.DataFrame) -> pd.Series:
    # x_t = ln(close_ETH / close_BTC) -- reimplemented independently
    return np.log(closes["ETH"].to_numpy() / closes["BTC"].to_numpy())


def independent_zscore(x: np.ndarray, L: int) -> np.ndarray:
    xs = pd.Series(x)
    mean = xs.rolling(L, min_periods=L).mean().to_numpy()
    # sample std, ddof=1, manual rolling via pandas but independently coded (no reuse of pairs_ratio)
    std = xs.rolling(L, min_periods=L).std(ddof=1).to_numpy()
    z = (x - mean) / std
    # std == 0 (full window, exactly constant) -> z = 0 ; NaN std (warmup) stays NaN
    z = np.where(std == 0, 0.0, z)
    return z


def independent_state_machine(z: np.ndarray, theta: float, hyst_frac: float = 0.5) -> np.ndarray:
    n = len(z)
    NEUTRAL, TILT_BTC, TILT_ETH = 0, 1, 2
    state = np.empty(n, dtype=np.int8)
    s = NEUTRAL
    exit_band = theta * hyst_frac
    for t in range(n):
        zt = z[t]
        if np.isnan(zt):
            s = NEUTRAL
        elif s == NEUTRAL:
            if zt >= theta:
                s = TILT_BTC
            elif zt <= -theta:
                s = TILT_ETH
        elif s == TILT_BTC:
            if zt <= -theta:
                s = TILT_ETH
            elif abs(zt) <= exit_band:
                s = NEUTRAL
        else:  # TILT_ETH
            if zt >= theta:
                s = TILT_BTC
            elif abs(zt) <= exit_band:
                s = NEUTRAL
        state[t] = s
    return state


def independent_weights(closes: pd.DataFrame, l_hours: int, theta: float) -> pd.DataFrame:
    x = independent_log_ratio(closes)
    z = independent_zscore(x, l_hours)
    state = independent_state_machine(z, theta)
    NEUTRAL, TILT_BTC, TILT_ETH = 0, 1, 2
    w_btc = np.where(state == TILT_BTC, 1.0, np.where(state == TILT_ETH, 0.0, 0.5))
    w_eth = np.where(state == TILT_ETH, 1.0, np.where(state == TILT_BTC, 0.0, 0.5))
    return pd.DataFrame({"BTC": w_btc, "ETH": w_eth}, index=closes.index)


if __name__ == "__main__":
    raw = bt_data.load_universe_raw("_data/crypto", UNIVERSE)
    cal = bt_data.build_calendar(raw)
    aligned = bt_data.align_universe_to_calendar(raw, cal)
    opens = bt_data.opens_panel(aligned, UNIVERSE)
    closes = bt_data.closes_panel(aligned, UNIVERSE)

    # Sanity: compare independent weights to pairs_ratio.py's own output for one combo
    from backtest.strategies import pairs_ratio as pr
    for combo in GRID:
        w_indep = independent_weights(closes, combo["l_hours"], combo["theta"])
        w_official = pr.generate_weight_decisions(closes, pr.PairsRatioParams(**combo))
        same = np.allclose(w_indep.to_numpy(), w_official.to_numpy(), equal_nan=True)
        diff_count = int((w_indep.to_numpy() != w_official.to_numpy()).sum())
        print(f"combo={combo} weights identical to pairs_ratio.py: {same} (diff cells={diff_count})")
