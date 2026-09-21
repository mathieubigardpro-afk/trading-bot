"""AXE 6 -- verification rapide (1 fenetre) que le controle 50/50 passe par EXACTEMENT le meme
pipeline (meme overlay, memes couts, meme moteur) que la candidate -- reproduction independante
de la fenetre 0 du controle."""
import json
import sys
import pandas as pd

sys.path.insert(0, "/home/claude/audit-copy")
from backtest import data_hourly as bt_data, engine, metrics as bt_metrics, risk_overlay

UNIVERSE = ["BTC", "ETH"]
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
w0 = windows[0]

control_weights = pd.DataFrame({sym: 0.5 for sym in UNIVERSE}, index=cal)
seg = engine.simulate_segment(cal, control_weights, opens, closes, w0.oos_start_idx, w0.oos_end_idx, COST_BPS,
                               carry_in=None, **sim_kwargs)
returns = seg.returns
pnls = [e["pnl"] for e in seg.realized_events]
equity = (1.0 + returns).cumprod()
equity_with_base = pd.concat([pd.Series([1.0]), equity])
sharpe = bt_metrics.sharpe_ratio(returns, periods_per_year=8760.0)
maxdd = bt_metrics.max_drawdown(equity_with_base)
trades = len(seg.trades_closed)

official = json.load(open("/home/claude/audit-copy/backtest/results/pairs_ethbtc_ratio_rotation/results.json"))
off_control_w0 = official["control_constant_weights_50_50"]["per_window"][0]
print(f"repro:    sharpe={sharpe:.6f} maxdd={maxdd:.6f} trades={trades}")
print(f"official: sharpe={off_control_w0['sharpe']:.6f} maxdd={off_control_w0['max_drawdown']:.6f} trades={off_control_w0['n_trades_closed']}")
print("MATCH:", abs(sharpe - off_control_w0['sharpe']) < 1e-6 and abs(maxdd - off_control_w0['max_drawdown']) < 1e-6)
