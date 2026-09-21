"""AXE 1/7 -- Recalcul independant ADF / Engle-Granger / demi-vie sur la periode pre-OOS
(avant la 1ere fenetre OOS), comparaison a results.json.
"""
import json
import math
import sys
import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller, coint
import statsmodels.api as sm

sys.path.insert(0, "/home/claude/audit-copy")
from backtest import data_hourly as bt_data, engine

UNIVERSE = ["BTC", "ETH"]
raw = bt_data.load_universe_raw("_data/crypto", UNIVERSE)
cal = bt_data.build_calendar(raw)
aligned = bt_data.align_universe_to_calendar(raw, cal)
closes = bt_data.closes_panel(aligned, UNIVERSE)

windows = engine.generate_walk_forward_windows(cal, is_months=9, oos_months=3, step_months=3)
pre_oos_end_idx = windows[0].oos_start_idx

x = np.log(closes["ETH"] / closes["BTC"])
x_pre = x.iloc[:pre_oos_end_idx].dropna()
log_eth_pre = np.log(closes["ETH"]).iloc[:pre_oos_end_idx].dropna()
log_btc_pre = np.log(closes["BTC"]).iloc[:pre_oos_end_idx].dropna()

adf_stat, adf_p, usedlag, nobs, crit, _ = adfuller(x_pre.to_numpy(), autolag="AIC", regression="c")
print(f"ADF: stat={adf_stat:.6f} p={adf_p:.6f} usedlag={usedlag} nobs={nobs}")

eg_stat, eg_p, eg_crit = coint(log_eth_pre.to_numpy(), log_btc_pre.to_numpy())
print(f"Engle-Granger: stat={eg_stat:.6f} p={eg_p:.6f}")

x_lag = x_pre.shift(1)
dx = x_pre.diff()
reg_df = pd.DataFrame({"dx": dx, "x_lag": x_lag}).dropna()
X = sm.add_constant(reg_df["x_lag"].to_numpy())
ols = sm.OLS(reg_df["dx"].to_numpy(), X).fit()
beta = float(ols.params[1])
half_life = -math.log(2.0) / beta if beta < 0 else float("inf")
print(f"AR(1): beta={beta:.8f} pvalue={ols.pvalues[1]:.6f} half_life_hours={half_life:.4f} days={half_life/24:.4f}")

official = json.load(open("/home/claude/audit-copy/backtest/results/pairs_ethbtc_ratio_rotation/results.json"))["stationarity"]
print()
print("Official ADF p:", official["adf_log_ratio_pre_oos"]["p_value"], " match:", abs(official["adf_log_ratio_pre_oos"]["p_value"]-adf_p) < 1e-6)
print("Official EG p:", official["engle_granger_log_prices_pre_oos"]["p_value"], " match:", abs(official["engle_granger_log_prices_pre_oos"]["p_value"]-eg_p) < 1e-6)
print("Official half-life h:", official["half_life_mean_reversion_pre_oos"]["half_life_hours"], " match:", abs(official["half_life_mean_reversion_pre_oos"]["half_life_hours"]-half_life) < 1e-3)
