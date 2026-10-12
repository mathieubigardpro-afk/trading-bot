"""ANALYSIS-19/common.py -- briques communes de la session d'analyse dediee backlog #19.

Aucun fichier du depot n'est modifie : ce module IMPORTE le moteur commun (backtest/engine.py,
backtest/risk_overlay.py, backtest/metrics.py, backtest/data_hourly.py) et les helpers de
production de la strategie quasi_passif_crypto, et ajoute :

  * le chargement des 6 majors horaires (donnees locales /home/claude/data/market-data) ;
  * la construction des matrices de poids DECIDES (quasi_passif equilibre ; candidate synthetique
    "faible poids" type vol-gate) ;
  * un REPLICA instrumente de la boucle de bande du moteur (numpy, rapide) qui trace chaque
    decision hold/trade -- VALIDE bit-a-bit contre engine.simulate_segment (equity, couts) a
    chaque configuration (assert_replica_matches_engine) ;
  * les trois semantiques S1/S2/S3 (+ S2b, controle "cadre wallet sans bande").
"""
from __future__ import annotations

import math
import sys
import os
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

REPO = Path(os.environ.get("BAND19_REPO", Path(__file__).resolve().parents[3]))  # racine du dépôt (surchargable : BAND19_REPO)
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from backtest import data_hourly as bt_data  # noqa: E402
from backtest import engine  # noqa: E402
from backtest import metrics as bt_metrics  # noqa: E402
from backtest import risk_overlay  # noqa: E402
from backtest.strategies import protective_vol_gate as pvg  # noqa: E402
from backtest.strategies import quasi_passif as qp  # noqa: E402
from bot import config as bot_cfg  # noqa: E402

DATA_DIR = os.environ.get("BAND19_DATA_DIR", "/home/claude/data/market-data/data/crypto")  # export de la branche market-data (surchargable : BAND19_DATA_DIR)
UNIVERSE_6 = ["BTC", "ETH", "SOL", "DOGE", "LINK", "AVAX"]
COST_BPS = 25.0            # palier pessimiste 6 majors (consigne de la mission)
BAND = 0.05                # bot.config.NO_TRADE_BAND == risk_overlay.DEFAULT_NO_TRADE_BAND
PPY = 8760.0

# Neutralisation de l'overlay portefeuille : convention SIZING-FIDELITY-SPEC (sizing interne,
# apply_vol_targeting=False) -- le sizing est DANS la matrice de poids decides.
SIM_KW = dict(apply_vol_targeting=False)


def load_market():
    raw = bt_data.load_universe_raw(DATA_DIR, UNIVERSE_6)
    cal = bt_data.build_calendar(raw)
    aligned = bt_data.align_universe_to_calendar(raw, cal)
    opens = bt_data.opens_panel(aligned, UNIVERSE_6)
    closes = bt_data.closes_panel(aligned, UNIVERSE_6)
    assert not opens.isna().any().any() and not closes.isna().any().any(), "NaN apres alignement"
    return raw, cal, opens, closes


# ---------------------------------------------------------------------------------------------
# Profils de poids decides
# ---------------------------------------------------------------------------------------------
def weights_quasi_passif_equilibre(raw, cal) -> pd.DataFrame:
    prof = [w for w in bot_cfg.WALLETS if w["id"] == "equilibre"][0]["risque"]
    return qp.generate_weight_decisions(raw, cal, UNIVERSE_6, prof)


def weights_lowweight_volgate(opens, closes, vol_target=None) -> pd.DataFrame:
    """Candidate SYNTHETIQUE 'faible poids nominal', profil type vol-gate session #7.

    Signal = protective_vol_gate_6majors (combo (72h, 0.98), la plus souvent selectionnee en IS
    selon l'audit, cf. backtest/results/protective_vol_gate_6majors/audit/attack4_band_artifact.py),
    poids bruts g/6 par symbole. Le sizing 'vol targeting' de la session #7 (overlay par defaut
    du moteur, target 0.275) est ici PRE-APPLIQUE dans la matrice de poids avec la MEME fonction
    (risk_overlay.compute_portfolio_vol_scalar) -> candidate 'sizing interne' au sens de
    SIZING-FIDELITY-SPEC, simulee avec apply_vol_targeting=False. Les poids post-overlay sont donc
    identiques a ceux de l'audit session #7 (mediane ~0.064 sur les bougies investies)."""
    params = pvg.ProtectiveVolGateParams(vol_window_hours=72, p_in=0.98)
    w_raw = pvg.generate_weight_decisions(closes, params)
    va, vc = risk_overlay.precompute_vol_stats(
        closes, halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
        periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR)
    tv = risk_overlay.DEFAULT_VOL_TARGET_ANNUALIZED if vol_target is None else vol_target
    scal = np.ones(len(w_raw))
    wr = w_raw.to_numpy()
    for i in range(len(w_raw)):
        if np.abs(wr[i]).sum() < 1e-12:
            continue
        scal[i] = risk_overlay.compute_portfolio_vol_scalar(
            w_raw.iloc[i], va.iloc[i], vc.iloc[i], target_vol_annualized=tv)
    return w_raw.mul(scal, axis=0), w_raw, scal


# ---------------------------------------------------------------------------------------------
# Replica instrumente de la boucle du moteur (spot long-only, bande plate, sans vol targeting)
# ---------------------------------------------------------------------------------------------
def replica_run(opens: np.ndarray, closes: np.ndarray, W: np.ndarray, band: float, cost_bps: float,
                start_idx: int, end_idx: int, initial_capital: float = 1.0):
    n, k = W.shape
    cost_rate = cost_bps / 10000.0
    shares = np.zeros(k)
    cash = float(initial_capital)
    m = end_idx - start_idx + 1
    eq = np.empty(m)
    eq_pre = np.empty(m)
    traded = np.zeros((m, k), dtype=bool)
    held_nonzero = np.zeros((m, k), dtype=bool)
    turnover = np.zeros(m)
    cost = np.zeros(m)
    cur_w = np.zeros((m, k))
    tgt_w = np.zeros((m, k))
    for j in range(m):
        i = start_idx + j
        o = opens[i]
        c = closes[i]
        scaled_w = W[i - 1]
        eq_before = cash + float((shares * o).sum())
        eq_pre[j] = eq_before
        target_shares = scaled_w * eq_before / o
        if band and band > 0:
            cw = (shares * o) / eq_before if eq_before > 0 else np.zeros(k)
            hold = np.abs(scaled_w - cw) < band
            cur_w[j] = cw
            target_shares = np.where(hold, shares, target_shares)
        else:
            hold = np.zeros(k, dtype=bool)
            cur_w[j] = (shares * o) / eq_before if eq_before > 0 else 0.0
        tgt_w[j] = scaled_w
        trade = target_shares - shares
        ch = np.abs(trade) > 1e-9
        traded[j] = ch
        # "hold effectif" = la bande a retenu un ordre qui, sans bande, aurait eu lieu
        held_nonzero[j] = hold & (np.abs(scaled_w * eq_before / o - shares) > 1e-9)
        tv = float((np.abs(trade[ch]) * o[ch]).sum())
        turnover[j] = tv
        cost[j] = tv * cost_rate
        cash = cash - float((trade * o).sum()) - cost[j]   # NB: engine soustrait sum(trade*open) sur toutes les colonnes spot
        shares = target_shares
        eq[j] = cash + float((shares * c).sum())
    return dict(equity=eq, equity_pre=eq_pre, traded=traded, held=held_nonzero, turnover=turnover,
                cost=cost, cur_w=cur_w, tgt_w=tgt_w)


def engine_run(cal, W_df, opens_df, closes_df, band, cost_bps=COST_BPS):
    n = len(cal)
    return engine.simulate_segment(cal, W_df, opens_df, closes_df, 1, n - 1, cost_bps,
                                   initial_capital=1.0, no_trade_band=band, **SIM_KW)


def assert_replica_matches_engine(seg, rep, tol=1e-12):
    d = float(np.max(np.abs(seg.equity.to_numpy() - rep["equity"])))
    assert d <= tol, f"replica != moteur : ecart equity max {d:.3e}"
    dc = abs(seg.pnl_breakdown["costs_spot"] - rep["cost"].sum())
    assert dc <= 1e-10, f"replica != moteur : ecart couts {dc:.3e}"
    return d


# ---------------------------------------------------------------------------------------------
# Metriques (en unites de POCHE)
# ---------------------------------------------------------------------------------------------
def pocket_equity_from_wallet(eq_wallet: np.ndarray, a: float) -> np.ndarray:
    """P = 1 + (E_w - 1)/a : equity en unites de poche (hypothese 'reste du wallet en cash')."""
    return 1.0 + (eq_wallet - 1.0) / a


def metrics_from_equity(eq: np.ndarray, pnls: List[float], first_idx: int = 0) -> dict:
    """Sharpe/MaxDD/CAGR depuis une serie d'equity de CLOTURE (base 1 a t0), a partir de
    first_idx (debut de la periode 'active', apres warm-up). PF depuis les PnL realises du moteur."""
    e = np.concatenate([[1.0], eq]) if first_idx == 0 else eq[first_idx - 1 :]
    s = pd.Series(e)
    r = s.pct_change().dropna()
    eq_series = s / s.iloc[0]
    return dict(
        sharpe=float(bt_metrics.sharpe_ratio(r, periods_per_year=PPY)),
        max_drawdown=float(bt_metrics.max_drawdown(eq_series)),
        cagr=float(bt_metrics.cagr(eq_series, periods_per_year=PPY)),
        profit_factor=float(bt_metrics.profit_factor(pnls)) if pnls else float("nan"),
        total_return=float(eq_series.iloc[-1] - 1.0),
    )


# ---------------------------------------------------------------------------------------------
# Replica HYBRIDE en unites de poche : plan factoriel 2x2 (base de dimensionnement x argument de bande)
# ---------------------------------------------------------------------------------------------
def replica_hybrid(opens: np.ndarray, closes: np.ndarray, W: np.ndarray, a: float, sizing_base: str, band_arg: str,
                   band: float, cost_bps: float, start_idx: int, end_idx: int):
    """Simulation en unites de POCHE (base 1). Etat : shares, cash (de poche). P = cash + shares*open (equity de
    poche), E = 1 + a(P-1) (equity wallet, reste en cash).
      sizing_base 'P' : cible $ = w_hat * P      (moteur S1)      | 'E' : cible $ = w_hat * E   (production S2)
      band_arg  'pocket': |w_hat - V/P| < band   (moteur S1)      | 'wallet': |w_hat - V/E| < band (production S2)
    (P,pocket) == S1 ; (E,wallet) == S2 (en unites de poche : equity = 1 + (E_wallet-1)/a) ; les deux autres
    cases isolent l'effet de chaque facteur. Couts en unites de poche (== couts wallet / a)."""
    n, k = W.shape
    cost_rate = cost_bps / 10000.0
    shares = np.zeros(k); cash = 1.0
    m = end_idx - start_idx + 1
    eq = np.empty(m); traded = np.zeros((m, k), dtype=bool); turnover = np.zeros(m); cost = np.zeros(m)
    for j in range(m):
        i = start_idx + j
        o = opens[i]; cl = closes[i]
        what = W[i - 1]
        V = shares * o
        P = cash + float(V.sum())
        E = 1.0 + a * (P - 1.0)
        base = P if sizing_base == "P" else E
        denom = P if band_arg == "pocket" else E
        cw = V / denom if denom > 0 else np.zeros(k)
        hold = np.abs(what - cw) < band if band > 0 else np.zeros(k, dtype=bool)
        tgt = what * base / o
        tsh = np.where(hold, shares, tgt)
        trade = tsh - shares
        ch = np.abs(trade) > 1e-9
        traded[j] = ch
        tv = float((np.abs(trade[ch]) * o[ch]).sum()); turnover[j] = tv; cost[j] = tv * cost_rate
        cash = cash - float((trade * o).sum()) - cost[j]
        shares = tsh
        eq[j] = cash + float((shares * cl).sum())
    return dict(equity=eq, traded=traded, turnover=turnover, cost=cost)


def episodes(mask: np.ndarray) -> int:
    """Nombre d'episodes (runs consecutifs de True, par colonne) d'un masque (m, k)."""
    prev = np.vstack([np.zeros((1, mask.shape[1]), dtype=bool), mask[:-1]])
    return int((mask & ~prev).sum())


def pocket_metrics(eq: np.ndarray, first_idx: int, turnover: float, cost: float, n_orders: int):
    e = eq[first_idx - 1:] if first_idx > 0 else np.concatenate([[1.0], eq])
    r = pd.Series(e).pct_change().dropna(); yrs = len(r) / PPY; pk = np.maximum.accumulate(e)
    return dict(sharpe=float(bt_metrics.sharpe_ratio(r, periods_per_year=PPY)), max_drawdown=float(((pk - e) / pk).max()),
                cagr=float((e[-1] / e[0]) ** (1 / yrs) - 1) if e[-1] > 0 else -1.0,
                annual_turnover_x_equity=float(turnover / yrs / e.mean()), cost_drag_per_year=float(cost / yrs / e.mean()),
                n_orders=int(n_orders), final_pocket_equity=float(e[-1] / e[0]))


def factorial(opens, closes, W, a, first_idx, start_idx, end_idx, cost_bps=COST_BPS, beta=BAND):
    """Plan 2x2 + marges sans bande : renvoie metriques pour chaque cellule."""
    out = {}
    for sb in ("P", "E"):
        for ba in ("pocket", "wallet"):
            H = replica_hybrid(opens, closes, W, a, sb, ba, beta, cost_bps, start_idx, end_idx)
            out[f"size={sb}|bandarg={ba}"] = dict(metrics=pocket_metrics(H["equity"], first_idx, H["turnover"].sum(), H["cost"].sum(), H["traded"].sum()), traded=H["traded"])
    for sb in ("P", "E"):
        H = replica_hybrid(opens, closes, W, a, sb, "pocket", 0.0, cost_bps, start_idx, end_idx)
        out[f"size={sb}|noband"] = dict(metrics=pocket_metrics(H["equity"], first_idx, H["turnover"].sum(), H["cost"].sum(), H["traded"].sum()), traded=H["traded"])
    return out


def dilate(mask: np.ndarray, tol: int) -> np.ndarray:
    """Dilate temporellement (+-tol bougies) un masque (m, k) par colonne."""
    cs = np.vstack([np.zeros((1, mask.shape[1])), np.cumsum(mask, axis=0)])
    m = mask.shape[0]
    lo = np.clip(np.arange(m) - tol, 0, m); hi = np.clip(np.arange(m) + tol + 1, 0, m)
    return (cs[hi] - cs[lo]) > 0


def unmatched_orders(t1: np.ndarray, t2: np.ndarray, tol: int = 24):
    """Ordres de t1 sans ordre de t2 sur le MEME symbole dans +-tol bougies, et reciproquement."""
    u1 = int((t1 & ~dilate(t2, tol)).sum()); u2 = int((t2 & ~dilate(t1, tol)).sum())
    return u1, u2


def overlay_prescale(w_raw: pd.DataFrame, closes: pd.DataFrame):
    """Pre-applique l'overlay de vol targeting PAR DEFAUT du moteur (target 0.275, coldstart 0.5, parametres
    horaires) dans la matrice de poids -- exact, car le scalaire ne depend que des poids bruts et de la vol EWMA."""
    va, vc = risk_overlay.precompute_vol_stats(closes, halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
                                               periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR)
    wr = w_raw.to_numpy(); sc = np.ones(len(wr))
    for i in np.flatnonzero(np.abs(wr).sum(axis=1) > 1e-12):
        sc[i] = risk_overlay.compute_portfolio_vol_scalar(w_raw.iloc[i], va.iloc[i], vc.iloc[i])
    return w_raw.mul(sc, axis=0)
