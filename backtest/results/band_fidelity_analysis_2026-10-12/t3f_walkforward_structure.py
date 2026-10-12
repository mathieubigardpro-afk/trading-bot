"""T3f -- STRUCTURE WALK-FORWARD OFFICIELLE (Porte 1) : fenetres OOS de 3 mois (9m IS / 3m OOS / pas 3m,
engine.generate_walk_forward_windows), capital de poche REMIS A 1 au debut de chaque fenetre OOS (comme
simulate_segment, y compris avec portage inter-fenetres qui renormalise l'equity a initial_capital).
=> sur les runs officiels, |P-1| est borne par la dynamique d'UNE fenetre de 3 mois, pas par 4,75 ans de compounding.
Replica (bit-exact vs moteur, cf. t3c) ; demarrage a plat a chaque fenetre pour S1 ET S2 (traitement identique ;
le portage de position n'est pas modelise ici -- limite assumee). Poids/signaux : memes matrices que t3a/t4
(signaux calcules sur l'historique complet, causalement, comme dans les runs officiels).
"""
import json, sys, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import numpy as np, pandas as pd
import common as c
from backtest.strategies import pairs_ratio as pr, vol_breakout as vb

HERE = Path(__file__).parent; CACHE = HERE / "cache"
cal = pd.DatetimeIndex(pd.read_pickle(CACHE / "cal.pkl"))
OP = pd.read_pickle(CACHE / "opens.pkl"); CLP = pd.read_pickle(CACHE / "closes.pkl")
BETA = c.BAND
windows = c.engine.generate_walk_forward_windows(cal, 9, 3, 3)
print(len(windows), "fenetres OOS", windows[0].oos_start, "->", windows[-1].oos_end)

def concat_returns(Rs_eq_list):
    rets = []
    for eq in Rs_eq_list:
        e = np.concatenate([[1.0], eq]); rets.append(np.diff(e) / e[:-1])
    return np.concatenate(rets)

def sharpe_r(r): return float(c.bt_metrics.sharpe_ratio(pd.Series(r), periods_per_year=c.PPY))
def maxdd_r(r):
    e = np.concatenate([[1.0], np.cumprod(1 + r)]); pk = np.maximum.accumulate(e); return float(((pk - e) / pk).max())

def run_cfg(label, Wdf, cols, allocs):
    O = OP[cols].to_numpy(); C = CLP[cols].to_numpy(); W = Wdf[cols].to_numpy()
    res = dict(label=label, median_nonzero_weight=float(np.median(W[W > 1e-12])), by_alloc={})
    base = {}
    for tag, band in (("S1", BETA), ("S3", 0.0)):
        eqs, tr, tv, cs = [], [], 0.0, 0.0
        for w in windows:
            R = c.replica_run(O, C, W, band, c.COST_BPS, w.oos_start_idx, w.oos_end_idx)
            eqs.append(R["equity"]); tr.append(R["traded"]); tv += R["turnover"].sum(); cs += R["cost"].sum()
        base[tag] = dict(eqs=eqs, traded=np.vstack(tr), r=concat_returns(eqs))
    r1 = base["S1"]["r"]; r3 = base["S3"]["r"]
    res["S1"] = dict(sharpe=sharpe_r(r1), max_drawdown=maxdd_r(r1), n_orders=int(base["S1"]["traded"].sum()),
                     max_abs_P_minus_1_within_window=float(max(np.abs(e - 1).max() for e in base["S1"]["eqs"])),
                     median_window_abs_P_minus_1_max=float(np.median([np.abs(e - 1).max() for e in base["S1"]["eqs"]])))
    res["S3"] = dict(sharpe=sharpe_r(r3), max_drawdown=maxdd_r(r3), n_orders=int(base["S3"]["traded"].sum()))
    for a in allocs:
        eq2, eq2b, tr2, tr2b, kabs = [], [], [], [], []
        for w in windows:
            R2 = c.replica_run(O, C, W * a, BETA * a, c.COST_BPS, w.oos_start_idx, w.oos_end_idx)
            R2b = c.replica_run(O, C, W * a, 0.0, c.COST_BPS, w.oos_start_idx, w.oos_end_idx)
            P2 = c.pocket_equity_from_wallet(R2["equity"], a); P2b = c.pocket_equity_from_wallet(R2b["equity"], a)
            eq2.append(P2); eq2b.append(P2b); tr2.append(R2["traded"]); tr2b.append(R2b["traded"])
            Pk = P2; kabs.append(np.abs(Pk / (1 + a * (Pk - 1)) - 1))
        r2 = concat_returns(eq2); r2b = concat_returns(eq2b)
        t1 = base["S1"]["traded"]; t2 = np.vstack(tr2)
        u = c.unmatched_orders(t1, t2, 24)
        ci = None
        m = min(len(r1), len(r2)); rng = np.random.default_rng(11); blk = 168; nb = m // blk; dd = []
        for _ in range(300):
            idx = np.concatenate([np.arange(s, s + blk) for s in rng.integers(0, m - blk, nb)])
            x, y = r1[idx], r2[idx]
            dd.append((y.mean() / y.std(ddof=1) - x.mean() / x.std(ddof=1)) * math.sqrt(c.PPY))
        ci = [float(np.percentile(dd, 2.5)), float(np.percentile(dd, 97.5))]
        s2, s2b = sharpe_r(r2), sharpe_r(r2b)
        res["by_alloc"][f"a={a:.2f}"] = dict(
            S2=dict(sharpe=s2, max_drawdown=maxdd_r(r2), n_orders=int(t2.sum())), S2b_sharpe=s2b,
            dSharpe_S2_minus_S1=s2 - res["S1"]["sharpe"], dSharpe_boot95=ci, dMaxDD=maxdd_r(r2) - res["S1"]["max_drawdown"],
            band_value_pocket=res["S1"]["sharpe"] - res["S3"]["sharpe"], band_value_wallet=s2 - s2b,
            d_band_value=(s2 - s2b) - (res["S1"]["sharpe"] - res["S3"]["sharpe"]), frame_effect_noband=s2b - res["S3"]["sharpe"],
            xor_cells=int((t1 ^ t2).sum()), unmatched_S1_pm24h=u[0], unmatched_S2_pm24h=u[1],
            unmatched_share_of_S1_orders=u[0] / max(1, int(t1.sum())), unmatched_share_of_S2_orders=u[1] / max(1, int(t2.sum())),
            mean_abs_k_minus_1=float(np.mean(np.concatenate(kabs))), p95_abs_k_minus_1=float(np.percentile(np.concatenate(kabs), 95)))
    return res

out = {}
cfgs = [("A_quasi_passif_equilibre", pd.read_pickle(CACHE / "W_A_quasi_passif_equilibre.pkl"), list(CLP.columns), [0.10, 0.25, 0.30, 0.60]),
        ("B_lowweight_volgate", pd.read_pickle(CACHE / "W_B_lowweight_volgate.pkl"), list(CLP.columns), [0.10, 0.25, 0.60]),
        ("C_bigweight_sma200_equal", pd.read_pickle(CACHE / "W_C_bigweight_sma200_equal.pkl"), list(CLP.columns), [0.25, 0.60])]
for g in vb.PARAM_GRID:
    cfgs.append((f"vol_breakout/W{g['window_hours']}_P{g['squeeze_percentile']}", c.overlay_prescale(vb.generate_weight_decisions(CLP, vb.VolBreakoutParams(**g)), CLP), list(CLP.columns), [0.30]))
for g in pr.PARAM_GRID:
    cfgs.append((f"pairs/L{g['l_hours']}_th{g['theta']}", c.overlay_prescale(pr.generate_weight_decisions(CLP[["BTC", "ETH"]], pr.PairsRatioParams(**g)), CLP[["BTC", "ETH"]]), ["BTC", "ETH"], [0.30]))
for g in c.pvg.PARAM_GRID:
    cfgs.append((f"pvg/V{g['vol_window_hours']}_pin{g['p_in']}", c.overlay_prescale(c.pvg.generate_weight_decisions(CLP, c.pvg.ProtectiveVolGateParams(**g)), CLP), list(CLP.columns), [0.25]))
rows = []
for name, Wdf, cols, allocs in cfgs:
    out[name] = run_cfg(name, Wdf, cols, allocs)
    for ak, av in out[name]["by_alloc"].items():
        rows.append(dict(cfg=name, a=ak, medw=round(out[name]["median_nonzero_weight"], 3), S1=round(out[name]["S1"]["sharpe"], 3), S2=round(av["S2"]["sharpe"], 3),
                         dSh=round(av["dSharpe_S2_minus_S1"], 3), ci=[round(x, 2) for x in av["dSharpe_boot95"]], dBand=round(av["d_band_value"], 3), frame=round(av["frame_effect_noband"], 3),
                         o1=out[name]["S1"]["n_orders"], o2=av["S2"]["n_orders"], um1=av["unmatched_S1_pm24h"], um2=av["unmatched_S2_pm24h"],
                         k=round(av["mean_abs_k_minus_1"], 3), maxP=round(out[name]["S1"]["max_abs_P_minus_1_within_window"], 2)))
    print(rows[-1], flush=True)
json.dump(out, open(HERE / "results_t3_walkforward_structure.json", "w"), indent=1)
print(pd.DataFrame(rows).to_string())
