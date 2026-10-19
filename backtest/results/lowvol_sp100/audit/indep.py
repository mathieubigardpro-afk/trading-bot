"""Reimplementation INDEPENDANTE (auditeur) de lowvol_sp100 : n'importe NI backtest.engine NI
backtest.strategies.lowvol NI backtest.metrics. Seule la liste d'univers (UNIVERSE_SP100) est lue.
Sert au point 2 du mandat (reproduction from scratch) et aux attaques A3/S2/cb."""
from __future__ import annotations
import math, sys, gzip, io
from pathlib import Path
import numpy as np
import pandas as pd
from scipy import stats

DATA = Path("/home/claude/mdata/data")
sys.path.insert(0, "/home/claude/audit-copy")
from bot.strategies.xs_momentum_sp100 import UNIVERSE_SP100  # liste seule

UNIV = sorted(UNIVERSE_SP100)
SQ252 = math.sqrt(252)


def load_csv(path):
    df = pd.read_csv(path, compression="gzip")
    idx = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(None).dt.normalize()
    df = df[["open", "close"]].astype(float)
    df.index = idx
    df = df.sort_index()
    return df[~df.index.duplicated(keep="last")]


def load_all():
    spy = load_csv(DATA / "etf" / "SPY.csv.gz")
    cal = spy.index
    raw = {t: load_csv(DATA / "equities" / f"{t}.csv.gz") for t in UNIV}
    O = pd.DataFrame({t: raw[t]["open"].reindex(cal).ffill(limit=5) for t in UNIV})
    C = pd.DataFrame({t: raw[t]["close"].reindex(cal).ffill(limit=5) for t in UNIV})
    return cal, O, C, raw, spy


# ---------------- signal ------------------
def decision_mask(cal):
    m = np.zeros(len(cal), bool)
    per = cal.to_period("M")
    m[:-1] = per[:-1] != per[1:]
    m[-1] = True
    return m


def panels_from_raw(raw, cal):
    """vol_L et nb de rendements reels, calcules sur la serie PROPRE de chaque titre."""
    n_ret = {}
    vol = {63: {}, 252: {}}
    for t in UNIV:
        s = raw[t]["close"].dropna()
        r = s.pct_change()
        n_ret[t] = pd.Series(np.arange(len(s), dtype=float), index=s.index).reindex(cal).ffill(limit=5)
        for L in (63, 252):
            vol[L][t] = (r.rolling(L, min_periods=L).std(ddof=1) * SQ252).reindex(cal).ffill(limit=5)
    NR = pd.DataFrame(n_ret)[UNIV]
    V = {L: pd.DataFrame(vol[L])[UNIV] for L in vol}
    return NR, V


def weights(cal, NR, V, L, N):
    """poids decides (index calendrier). N=None -> controle (tous les eligibles)."""
    dm = decision_mask(cal)
    elig = ((NR >= 252) & V[L].notna()).to_numpy()
    vol = V[L].to_numpy()
    out = np.zeros((len(cal), len(UNIV)))
    last = np.zeros(len(UNIV))
    for i in range(len(cal)):
        if dm[i]:
            e = elig[i]
            row = np.zeros(len(UNIV))
            ne = e.sum()
            if ne:
                if N is None or ne <= N:
                    row[e] = 1.0 / ne
                else:
                    idx = np.flatnonzero(e)
                    order = idx[np.argsort(vol[i, idx], kind="stable")][:N]
                    row[order] = 1.0 / N
            last = row
        out[i] = last
    return out


# ---------------- simulateur ------------------
def sim(W, O, C, start, end, cost_bps, band, state=None, record=False, funded=False, mode='band', rebal=None):
    """Simule [start,end] (indices inclus) ; W[i-1] execute a l'open i. state=(shares,cash) en
    unites d'equity=1. Retourne dict. Le capital est remis a 1 en entree (shares/cash deja
    normalises par l'appelant)."""
    k = O.shape[1]
    sh = np.zeros(k) if state is None else state[0].copy()
    cash = 1.0 if state is None else state[1]
    rate = cost_bps / 1e4
    n = end - start + 1
    eq = np.empty(n); gross = np.empty(n); nheld = np.empty(n, int); cashs = np.empty(n)
    orders = 0
    ledger = [] if record else None
    avg = {} if (state is None or len(state) < 3) else dict(state[2])  # cost basis par titre pour PF
    events = []
    for j in range(n):
        i = start + j
        o = np.nan_to_num(O[i], nan=0.0); c = np.nan_to_num(C[i], nan=0.0)
        w = W[i - 1]
        V = sh * o
        eqb = cash + V.sum()
        safe = np.where(o == 0, np.nan, o)
        tgt = np.nan_to_num(w * eqb / safe, nan=0.0)
        cw = V / eqb if eqb > 0 else np.zeros(k)
        if mode == 'monthly':
            # variante d'EXPERIENCE (hors protocole) : on ne trade qu'au lendemain d'une decision
            # (ligne de poids changee) ou au tout premier jour d'une simulation a plat.
            changed_row = bool(rebal[i - 1])
            if not (changed_row or (state is None and j == 0)):
                tgt = sh.copy()
        elif band > 0:
            hold = np.abs(w - cw) < band
            tgt = np.where(hold, sh, tgt)
        tr = tgt - sh
        ch = np.abs(tr) > 1e-9
        turn = float((np.abs(tr[ch]) * o[ch]).sum())
        cst = turn * rate
        if record and ch.any():
            for s_i in np.flatnonzero(ch):
                old, new, p = sh[s_i], tgt[s_i], o[s_i]
                if old <= 1e-9 and new > 1e-9:
                    avg[s_i] = p * (1 + rate)
                elif old > 1e-9 and new <= 1e-9:
                    events.append((j, s_i, old * (p * (1 - rate) - avg[s_i]), True))
                    avg.pop(s_i, None)
                elif new > old > 1e-9:
                    avg[s_i] = (old * avg[s_i] + (new - old) * p * (1 + rate)) / new
                elif 0 < new < old:
                    events.append((j, s_i, (old - new) * (p * (1 - rate) - avg[s_i]), False))
            orders += int(ch.sum())
        cash = cash - float((tr * o).sum()) - cst
        sh = tgt
        e = cash + float((sh * c).sum())
        eq[j] = e
        gross[j] = float((sh * c).sum()) / e
        nheld[j] = int((sh > 1e-9).sum())
        cashs[j] = cash
    ret = np.empty(n)
    ret[0] = eq[0] - 1.0
    ret[1:] = eq[1:] / eq[:-1] - 1.0
    return dict(eq=eq, ret=ret, gross=gross, nheld=nheld, cash=cashs, state=(sh / eq[-1], cash / eq[-1], dict(avg)),
                events=events, orders=orders, avg=avg, shares_end=sh, eq_end=eq[-1])


# ---------------- metriques ------------------
def sharpe(r):
    r = np.asarray(r, float); r = r[~np.isnan(r)]
    if len(r) < 2: return float("nan")
    sd = r.std(ddof=1)
    return float(r.mean() / sd * SQ252) if sd > 0 else float("nan")


def maxdd(ret):
    eq = np.concatenate([[1.0], np.cumprod(1 + np.asarray(ret))])
    return float(-(eq / np.maximum.accumulate(eq) - 1).min())


def profit_factor(pnls):
    g = sum(p for p in pnls if p > 0); l = -sum(p for p in pnls if p < 0)
    return g / l if l > 0 else float("inf")


# ---------------- fenetres walk-forward (reecrit depuis la SPEC) ------------------
def windows(cal, is_m=36, oos_m=12, step=12):
    out = []
    start0 = cal[0]
    k = 0
    while True:
        a = start0 + pd.DateOffset(months=step * k)
        b = a + pd.DateOffset(months=is_m)
        c = b + pd.DateOffset(months=oos_m)
        if c > cal[-1]:
            break
        ia = int(cal.searchsorted(a, side="left"))
        ib = int(cal.searchsorted(b, side="left"))
        ic = int(cal.searchsorted(c, side="left")) - 1
        out.append(dict(k=k, is_s=ia, is_e=ib - 1, oos_s=ib, oos_e=ic))
        k += 1
    return out


# ---------------- DSR (Bailey & Lopez de Prado 2014) ------------------
def dsr(returns, K):
    r = np.asarray(returns, float); r = r[~np.isnan(r)]
    n = len(r)
    sr = r.mean() / r.std(ddof=1)
    g3 = stats.skew(r, bias=False)
    g4 = stats.kurtosis(r, fisher=False, bias=False)  # Pearson (non excess)
    se = math.sqrt((1 - g3 * sr + (g4 - 1) / 4 * sr ** 2) / (n - 1))
    gam = 0.5772156649015329
    sr0 = se * ((1 - gam) * stats.norm.ppf(1 - 1 / K) + gam * stats.norm.ppf(1 - 1 / (K * math.e)))
    z = (sr - sr0) / se
    return dict(sr=sr, n=n, skew=g3, kurt_pearson=g4, se=se, sr0=sr0, dsr=float(stats.norm.cdf(z)), z=z)
