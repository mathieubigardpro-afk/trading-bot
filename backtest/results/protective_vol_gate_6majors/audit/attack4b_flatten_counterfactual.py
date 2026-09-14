#!/usr/bin/env python3
"""ATTAQUE 4 (suite) -- Contre-factuel : que se passerait-il si un ordre de sortie complete
(cible=0 pour un symbole) executait TOUJOURS, bande ou pas (comme le flatten_mode des circuit
breakers de bot/risk/manager.py, meme si -- verifie separement -- ce n'est PAS le mecanisme qui
s'applique reellement a une coupure de signal ordinaire en production) ?

Simulation continue (pas walk-forward officiel, juste pour quantifier l'ORDRE DE GRANDEUR de
l'effet de la bande sur cette candidate) sur le combo le plus souvent selectionne (72, 0.98),
calendrier complet, cost=25bps, vol targeting horaire actif -- DEUX variantes :
  (a) bande standard actuelle du moteur (comme attack4_band_artifact.py)
  (b) bande standard SAUF si le poids cible d'un symbole est EXACTEMENT 0 (coupure ou etat
      REDEPLOY jamais commence) -> execution forcee (comme un flatten), qui reste dans la bande
      pour tout le reste (increments de rampe, renforcements).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path("/home/claude/audit-copy")
sys.path.insert(0, str(REPO))

from backtest import data_hourly as bt_data
from backtest import metrics as bt_metrics
from backtest import risk_overlay
from backtest.strategies import protective_vol_gate as pvg
from bot import config as bot_cfg

UNIVERSE = list(bot_cfg.SYMBOLS_CRYPTO)
BAND = risk_overlay.DEFAULT_NO_TRADE_BAND
VOL_TARGET = risk_overlay.DEFAULT_VOL_TARGET_ANNUALIZED


def run(closes, opens, weights, vol_annual_full, valid_count_full, force_flatten_on_zero: bool):
    n = len(closes)
    universe = UNIVERSE
    shares = pd.Series(0.0, index=universe)
    cash = 1.0
    equity_hist = np.empty(n)
    equity_hist[0] = 1.0
    realized = []
    n_trades_closed = 0
    open_flag = pd.Series(False, index=universe)

    for i in range(1, n):
        open_price = opens.iloc[i].fillna(0.0)
        raw_w = weights.iloc[i - 1]
        vol_scalar = risk_overlay.compute_portfolio_vol_scalar(
            raw_w, vol_annual_full.iloc[i - 1], valid_count_full.iloc[i - 1],
            target_vol_annualized=VOL_TARGET,
        )
        scaled_w = raw_w * vol_scalar
        equity_before_trade = cash + float((shares * open_price).sum())
        safe_open = open_price.replace(0.0, np.nan)
        target_dollars = scaled_w * equity_before_trade
        target_shares = (target_dollars / safe_open).fillna(0.0)

        if equity_before_trade > 0:
            current_w = (shares * open_price) / equity_before_trade
        else:
            current_w = pd.Series(0.0, index=universe)
        hold = (scaled_w - current_w).abs() < BAND
        if force_flatten_on_zero:
            is_zero_target = scaled_w.abs() < 1e-12
            hold = hold & ~is_zero_target
        target_shares_final = target_shares.where(~hold, shares)

        trade_shares = target_shares_final - shares
        changed = trade_shares[trade_shares.abs() > 1e-9]
        turnover = float((changed.abs() * open_price.reindex(changed.index)).sum())
        cost = turnover * 0.0025

        for sym, d in changed.items():
            old_sh = float(shares[sym])
            new_sh = old_sh + float(d)
            if old_sh <= 1e-9 and new_sh > 1e-9:
                open_flag[sym] = True
            elif old_sh > 1e-9 and new_sh <= 1e-9:
                n_trades_closed += 1
                open_flag[sym] = False

        cash = cash - float((trade_shares * open_price).sum()) - cost
        shares = target_shares_final

        close_price = closes.iloc[i].fillna(0.0)
        equity_hist[i] = cash + float((shares * close_price).sum())

    returns = pd.Series(equity_hist).pct_change().dropna()
    sharpe = bt_metrics.sharpe_ratio(returns, periods_per_year=8760.0)
    maxdd = bt_metrics.max_drawdown(pd.Series(equity_hist))
    total_return = equity_hist[-1] / equity_hist[0] - 1.0
    return {
        "sharpe_full_period": sharpe,
        "max_drawdown": maxdd,
        "total_return": total_return,
        "n_trades_closed_approx": n_trades_closed,
        "final_equity": equity_hist[-1],
    }


def main():
    raw = bt_data.load_universe_raw(str(REPO / "_data" / "crypto"), UNIVERSE)
    calendar = bt_data.build_calendar(raw)
    aligned = bt_data.align_universe_to_calendar(raw, calendar)
    opens = bt_data.opens_panel(aligned, UNIVERSE)
    closes = bt_data.closes_panel(aligned, UNIVERSE)

    combo = {"vol_window_hours": 72, "p_in": 0.98}
    params = pvg.ProtectiveVolGateParams(**combo)
    weights = pvg.generate_weight_decisions(closes, params)

    vol_annual_full, valid_count_full = risk_overlay.precompute_vol_stats(
        closes, halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
        periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR,
    )

    print("[baseline] bande standard (comportement actuel du moteur) ...")
    res_a = run(closes, opens, weights, vol_annual_full, valid_count_full, force_flatten_on_zero=False)
    print(res_a)

    print("\n[contrefactuel] bande standard SAUF flatten (cible=0) toujours execute ...")
    res_b = run(closes, opens, weights, vol_annual_full, valid_count_full, force_flatten_on_zero=True)
    print(res_b)

    print("\n=== delta ===")
    for k in res_a:
        try:
            print(f"{k}: baseline={res_a[k]:.6f} contrefactuel={res_b[k]:.6f} delta={res_b[k]-res_a[k]:.6f}")
        except TypeError:
            print(f"{k}: baseline={res_a[k]} contrefactuel={res_b[k]}")


if __name__ == "__main__":
    main()
