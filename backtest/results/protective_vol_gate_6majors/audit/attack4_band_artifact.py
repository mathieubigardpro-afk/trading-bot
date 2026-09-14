#!/usr/bin/env python3
"""ATTAQUE 4 -- Artefact bande de non-negotiation (precedent funding_carry).

Quantifie : distribution du poids cible post-overlay (vol targeting) pour la candidate
protective_vol_gate, et nombre d'heures ou la bande de non-negociation (0.05, flat, non
proportionnelle au poids nominal) bloque un ordre alors que le signal a reellement change
d'etat (coupure ou increment de rampe). Reproduit fidelement la boucle de
`backtest/engine.py::simulate_segment` (overlay puis bande, spot pur, cost=25bps) pour rester
comparable, mais instrumente chaque barre (current_w, scaled_w, hold) -- n'importe le moteur
commun (risk_overlay, engine) tel quel, pas de reimplementation de la formule vol targeting.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path("/home/claude/audit-copy")
sys.path.insert(0, str(REPO))

from backtest import data_hourly as bt_data
from backtest import risk_overlay
from backtest.strategies import protective_vol_gate as pvg
from bot import config as bot_cfg

UNIVERSE = list(bot_cfg.SYMBOLS_CRYPTO)
BAND = risk_overlay.DEFAULT_NO_TRADE_BAND  # 0.05
VOL_TARGET = risk_overlay.DEFAULT_VOL_TARGET_ANNUALIZED  # 0.275


def main():
    raw = bt_data.load_universe_raw(str(REPO / "_data" / "crypto"), UNIVERSE)
    calendar = bt_data.build_calendar(raw)
    aligned = bt_data.align_universe_to_calendar(raw, calendar)
    opens = bt_data.opens_panel(aligned, UNIVERSE)
    closes = bt_data.closes_panel(aligned, UNIVERSE)

    combo = {"vol_window_hours": 72, "p_in": 0.98}  # most frequently selected IS combo
    params = pvg.ProtectiveVolGateParams(**combo)
    sg = pvg.generate_state_and_g(closes, params)
    weights = pvg.generate_weight_decisions(closes, params)
    print(f"[signal] combo={combo}  g stats: {sg['g'].describe()}")

    vol_annual_full, valid_count_full = risk_overlay.precompute_vol_stats(
        closes, halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
        periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR,
    )

    n = len(calendar)
    universe = UNIVERSE
    shares = pd.Series(0.0, index=universe)
    cash = 1.0
    equity_hist = np.empty(n)
    scaled_w_hist = np.empty((n, 6))
    current_w_hist = np.empty((n, 6))
    hold_hist = np.zeros((n, 6), dtype=bool)
    vol_scalar_hist = np.empty(n)
    g_arr = sg["g"].to_numpy()

    # warmup: start_idx=1 as engine requires
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
        target_shares_final = target_shares.where(~hold, shares)

        # cost + cash update (25 bps/side)
        trade_shares = target_shares_final - shares
        changed = trade_shares[trade_shares.abs() > 1e-9]
        turnover = float((changed.abs() * open_price.reindex(changed.index)).sum())
        cost = turnover * 0.0025
        cash = cash - float((trade_shares * open_price).sum()) - cost
        shares = target_shares_final

        close_price = closes.iloc[i].fillna(0.0)
        equity_hist[i] = cash + float((shares * close_price).sum())
        scaled_w_hist[i] = scaled_w.to_numpy()
        current_w_hist[i] = current_w.to_numpy()
        hold_hist[i] = hold.to_numpy()
        vol_scalar_hist[i] = vol_scalar

    print("\n=== Distribution du vol_scalar (post targeting) ===")
    vs = pd.Series(vol_scalar_hist[1:])
    print(vs.describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]))

    print("\n=== Distribution du poids cible SCALE par symbole (scaled_w, tous symboles empiles) ===")
    sw = pd.Series(scaled_w_hist[1:].flatten())
    print(sw.describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99]))
    frac_below_band_when_invested = float((sw[sw > 1e-9] < BAND).mean())
    print(f"\nFraction des barres AVEC poids cible NON-NUL mais < bande 0.05 : {frac_below_band_when_invested:.4f}")

    # Focus on bars around state transitions: find state change points
    state_arr = sg["state"].to_numpy()
    transitions = np.where(np.diff(state_arr) != 0)[0] + 1  # index of the bar AFTER transition
    print(f"\nNombre de transitions d'etat sur tout le calendrier : {len(transitions)}")

    n_cut_events = 0  # investi -> protege
    n_cut_blocked = 0
    n_ramp_start_events = 0  # protege -> redeploiement
    n_ramp_start_blocked = 0
    n_full_reinvest_events = 0  # redeploiement -> investi (g atteint 1.0)
    n_full_reinvest_blocked = 0

    for t in transitions:
        prev_state = state_arr[t - 1]
        new_state = state_arr[t]
        if t >= n:
            continue
        blocked_all_syms = bool(hold_hist[t].all())  # tous les symboles bloques par la bande
        blocked_any_sym = bool(hold_hist[t].any())
        if prev_state == pvg.STATE_INVESTED and new_state == pvg.STATE_PROTECTED:
            n_cut_events += 1
            n_cut_blocked += int(blocked_all_syms)
        elif prev_state == pvg.STATE_PROTECTED and new_state == pvg.STATE_REDEPLOY:
            n_ramp_start_events += 1
            n_ramp_start_blocked += int(blocked_all_syms)
        elif prev_state == pvg.STATE_REDEPLOY and new_state == pvg.STATE_INVESTED:
            n_full_reinvest_events += 1
            n_full_reinvest_blocked += int(blocked_all_syms)

    print(f"\nEvenements de COUPURE (investi->protege) : {n_cut_events}, dont bloques par la bande (tous symboles) : {n_cut_blocked}")
    print(f"Evenements de DEBUT DE RAMPE (protege->redeploiement) : {n_ramp_start_events}, dont bloques : {n_ramp_start_blocked}")
    print(f"Evenements FIN DE RAMPE (redeploiement->investi) : {n_full_reinvest_events}, dont bloques : {n_full_reinvest_blocked}")

    # Overall: fraction of REDEPLOY-state hours where all 6 symbols are held (band binds during ramp)
    redeploy_mask = (state_arr == pvg.STATE_REDEPLOY)
    redeploy_mask_valid = redeploy_mask[1:]  # align with hold_hist[1:]
    hold_all_hist = hold_hist[1:].all(axis=1)
    if redeploy_mask_valid.sum() > 0:
        frac_redeploy_held = float(hold_all_hist[redeploy_mask_valid].mean())
    else:
        frac_redeploy_held = float("nan")
    print(f"\nFraction des heures EN REDEPLOIEMENT ou la bande bloque TOUS les ordres (position figee malgre g qui augmente) : {frac_redeploy_held:.4f}")

    # And: how many DISTINCT executed rebalances actually happen during redeploy episodes vs
    # the "72 hourly increments" the signal intends.
    n_orders_during_redeploy = int((~hold_all_hist[redeploy_mask_valid]).sum()) if redeploy_mask_valid.sum() else 0
    n_hours_redeploy = int(redeploy_mask_valid.sum())
    print(f"Heures en redeploiement : {n_hours_redeploy}, dont heures avec AU MOINS UN ordre execute : {n_orders_during_redeploy}")


if __name__ == "__main__":
    main()
