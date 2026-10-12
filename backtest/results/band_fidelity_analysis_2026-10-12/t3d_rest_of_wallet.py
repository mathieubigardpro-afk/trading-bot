"""T3d -- robustesse de S2 a l'hypothese 'reste du wallet en cash' (limite du modele affine).

En vrai E = (1-a) R + Q  (R = valeur relative du reste du wallet, Q = valeur wallet-$ de la poche,
P' = Q/a l'equity de poche), et k = P'/E.  R=1 : S2 (cash).  Un backtest poche-standalone ne CONNAIT PAS R.
On borne l'effet avec deux 'restes' exogenes passifs plausibles :
  R_crypto : B&H equipondere des 6 majors (reste fortement correle a la poche, vol elevee)
  R_6040   : 0.6 SPY + 0.4 IEF (B&H, prix quotidiens ffill horaire) -- proxy de la partie non-crypto du wallet equilibre
Le reste est PASSIF (il ne trade pas, pas de cout) -- simplification assumee. Le sizing wallet est
a*w_hat*E (E = equity totale), bande 0.05*a, exactement la regle de production. Contrainte de realisme :
achats plafonnes au cash disponible de la poche (pas d'emprunt contre le reste), compteur de plafonnement rapporte.
"""
import json, sys, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import numpy as np, pandas as pd
import common as c

HERE = Path(__file__).parent; CACHE = HERE / "cache"
cal = pd.DatetimeIndex(pd.read_pickle(CACHE / "cal.pkl"))
O = pd.read_pickle(CACHE / "opens.pkl").to_numpy(); CL = pd.read_pickle(CACHE / "closes.pkl").to_numpy()
n = len(cal); START, END = 1, n - 1
BETA = c.BAND

def etf_hourly(sym):
    df = pd.read_csv(f"{_os.environ.get('BAND19_DATA_DIR', '/home/claude/data/market-data/data/crypto').rsplit('/crypto',1)[0]}/etf/{sym}.csv.gz")
    ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(None).dt.normalize()
    s = pd.Series(df["close"].to_numpy(), index=ts).groupby(level=0).last()
    # valeur 'connue' : la cloture du jour D n'est utilisee qu'a partir de D+1 00:00 (pas de look-ahead)
    s.index = s.index + pd.Timedelta(days=1)
    return s.reindex(cal, method="ffill")
spy, ief = etf_hourly("SPY"), etf_hourly("IEF")
r6040 = (0.6 * spy / spy.dropna().iloc[0] + 0.4 * ief / ief.dropna().iloc[0])
r6040 = r6040.ffill().bfill().to_numpy()
# normalisation a 1 au debut de la simulation
r6040 = r6040 / r6040[START - 1]
rcrypto_close = (CL / CL[0]).mean(axis=1); rcrypto_open = (O / CL[0]).mean(axis=1)
rcrypto_close = rcrypto_close / rcrypto_open[START]; rcrypto_open = rcrypto_open / rcrypto_open[START]
RESTS = {"cash": (np.ones(n), np.ones(n)), "crypto_EW_BH": (rcrypto_open, rcrypto_close), "SPY60_IEF40": (r6040, r6040)}

def replica_rest(W, a, band, cost_bps, Ro, Rc, cap=True):
    k = W.shape[1]; cost_rate = cost_bps / 1e4
    shares = np.zeros(k); cash = a   # liquidites de la poche (wallet-$), reste = (1-a) investi dans R
    m = END - START + 1
    E = np.empty(m); Q = np.empty(m); traded = np.zeros((m, k), dtype=bool); turnover = np.zeros(m); cost = np.zeros(m)
    kk = np.empty(m); capped = 0
    for j in range(m):
        i = START + j; o = O[i]; cl = CL[i]
        what_w = a * W[i - 1]
        Eb = cash + float((shares * o).sum()) + (1 - a) * Ro[i]
        Qb = cash + float((shares * o).sum())
        cur = (shares * o) / Eb
        hold = np.abs(what_w - cur) < band if band > 0 else np.zeros(k, bool)
        tgt = what_w * Eb / o
        tsh = np.where(hold, shares, tgt)
        # liquidite de la poche : les ACHATS sont plafonnes au cash disponible (ventes + cash), comme un
        # ledger qui rejette/reduit un ordre non finance ; les positions detenues ne sont jamais re-echelonnees
        tr0 = tsh - shares
        buys = np.where(tr0 > 0, tr0 * o, 0.0); sells = np.where(tr0 < 0, -tr0 * o, 0.0)
        avail = cash + sells.sum() * (1 - cost_rate)
        if cap and buys.sum() * (1 + cost_rate) > avail + 1e-15:
            capped += 1
            f = max(0.0, avail / (buys.sum() * (1 + cost_rate)))
            tr0 = np.where(tr0 > 0, tr0 * f, tr0); tsh = shares + tr0
        trade = tsh - shares
        ch = np.abs(trade) > 1e-9; traded[j] = ch
        tv = float((np.abs(trade[ch]) * o[ch]).sum()); turnover[j] = tv; cost[j] = tv * cost_rate
        cash = cash - float((trade * o).sum()) - cost[j]; shares = tsh
        Q[j] = cash + float((shares * cl).sum()); E[j] = Q[j] + (1 - a) * Rc[i]
        kk[j] = (Q[j] / a) / E[j]
    return dict(E=E, Pp=Q / a, traded=traded, turnover=turnover, cost=cost, k=kk, capped=capped)

def stats(Pp, first_idx, tv, cost):
    e = Pp[first_idx - 1:] if first_idx > 0 else np.concatenate([[1.0], Pp])
    r = pd.Series(e).pct_change().dropna(); yrs = len(r) / c.PPY
    pk = np.maximum.accumulate(e)
    return dict(sharpe=float(c.bt_metrics.sharpe_ratio(r, periods_per_year=c.PPY)), max_drawdown=float(((pk - e) / pk).max()),
                cagr=float((e[-1] / e[0]) ** (1 / yrs) - 1), annual_turnover_x_equity=float(tv / yrs / e.mean()),
                cost_drag_per_year=float(cost / yrs / e.mean()), total_return=float(e[-1] / e[0] - 1))

out = {}
cfgs = [("A", "A_quasi_passif_equilibre", 0.30), ("A", "A_quasi_passif_equilibre", 0.60), ("B", "B_lowweight_volgate", 0.25), ("B", "B_lowweight_volgate", 0.10)]
for p, name, a in cfgs:
    W = pd.read_pickle(CACHE / f"W_{name}.pkl").to_numpy()
    Wsub = W[START - 1:END]; nz = np.flatnonzero(np.abs(Wsub).sum(axis=1) > 1e-12); first_idx = int(nz[0])
    R1 = c.replica_run(O, CL, W, BETA, c.COST_BPS, START, END)
    s1 = stats(R1["equity"], first_idx, R1["turnover"].sum(), R1["cost"].sum())
    entry = {"S1_pocket_frame": s1}
    for rn, (Ro, Rc) in RESTS.items():
        res = replica_rest(W, a, BETA * a, c.COST_BPS, Ro, Rc, cap=(rn != 'cash'))   # reste en cash = liquide, pas de plafond
        st = stats(res["Pp"], first_idx, res["turnover"].sum() / a, res["cost"].sum() / a)
        xor = R1["traded"] ^ res["traded"]
        inv = np.abs(res["k"] - 1)
        st.update(n_orders=int(res["traded"].sum()), n_orders_S1=int(R1["traded"].sum()), xor_cells_vs_S1=int(xor.sum()),
                  mean_abs_k_minus_1=float(inv.mean()), p95_abs_k_minus_1=float(np.percentile(inv, 95)),
                  liquidity_cap_bars=int(res["capped"]), delta_sharpe_vs_S1=st["sharpe"] - s1["sharpe"],
                  delta_cagr_vs_S1=st["cagr"] - s1["cagr"], delta_maxdd_vs_S1=st["max_drawdown"] - s1["max_drawdown"],
                  wallet_total_return=float(res["E"][-1] - 1), wallet_maxdd=float((np.maximum.accumulate(res["E"]) - res["E"]).max() / np.maximum.accumulate(res["E"]).max()))
        if rn == 'cash':
            R2 = c.replica_run(O, CL, W * a, BETA * a, c.COST_BPS, START, END)
            st['validation_max_abs_diff_vs_S2_replica_equity'] = float(np.max(np.abs(res['E'] - R2['equity'])))
            assert st['validation_max_abs_diff_vs_S2_replica_equity'] < 1e-9
        entry[f"S2_rest={rn}"] = st
    out[f"{p}/a={a:.2f}"] = entry
    print(p, a, {k: (round(v["sharpe"], 3), v["n_orders"], round(v["mean_abs_k_minus_1"], 3), v["liquidity_cap_bars"]) for k, v in entry.items() if k.startswith("S2")}, "S1:", round(s1["sharpe"], 3))
json.dump(out, open(HERE / "results_t3_rest_of_wallet.json", "w"), indent=1)
