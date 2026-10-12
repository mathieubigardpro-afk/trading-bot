"""T4 -- impact retrospectif, estimation par REPLICA valide (pas de re-run walk-forward complet).

Pour chaque candidate crypto horaire du registre dont le signal est rejouable localement
(vol_breakout_6majors, pairs_ethbtc_ratio_rotation, btc_seasonality_2123utc, protective_vol_gate_6majors),
on reconstruit les poids bruts de la SPEC (combos de la grille), on PRE-APPLIQUE l'overlay de vol targeting
par defaut du moteur (target 0.275, coldstart 0.5 -- configuration de ces runs historiques, sessions #4-#8),
puis on compare S1/S2/S3 sur la fenetre COMPLETE (indicatif, pas OOS walk-forward) avec a=0.30 (alloc
prevue par la SPEC pairs) et a=0.25. Le replica est bit-exact vs moteur (voir t3c / replica_vs_engine) ;
pre-appliquer le scalaire est exact car le scalaire ne depend que de (poids bruts, vol EWMA) et pas de l'etat.
Non rejouables ici : funding_carry (moteur perp), xs_momentum_invvol (equities quotidien, autre pipeline) ->
estimes par argument dans le rapport.
"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import numpy as np, pandas as pd
import common as c
from backtest.strategies import vol_breakout as vb, pairs_ratio as pr, seasonality as sea

HERE = Path(__file__).parent; CACHE = HERE / "cache"
cal = pd.DatetimeIndex(pd.read_pickle(CACHE / "cal.pkl"))
OP = pd.read_pickle(CACHE / "opens.pkl"); CLP = pd.read_pickle(CACHE / "closes.pkl")
n = len(cal); START, END = 1, n - 1; BETA = c.BAND

def overlay_prescale(w_raw: pd.DataFrame, closes: pd.DataFrame):
    va, vc = c.risk_overlay.precompute_vol_stats(closes, halflife_days=c.risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
                                                  periods_per_year=c.risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR)
    wr = w_raw.to_numpy(); scal = np.ones(len(wr))
    for i in np.flatnonzero(np.abs(wr).sum(axis=1) > 1e-12):
        scal[i] = c.risk_overlay.compute_portfolio_vol_scalar(w_raw.iloc[i], va.iloc[i], vc.iloc[i])
    return w_raw.mul(scal, axis=0), scal

def k_fn(P, a): return P / (1.0 + a * (P - 1.0))

def run_cfg(label, W_df, cols, allocs):
    Ocol = OP[cols].to_numpy(); Ccol = CLP[cols].to_numpy(); W = W_df[cols].to_numpy()
    nz = np.flatnonzero(np.abs(W[START - 1:END]).sum(axis=1) > 1e-12)
    if len(nz) == 0: return None
    first_idx = int(nz[0]); v = W[W > 1e-12]
    R1 = c.replica_run(Ocol, Ccol, W, BETA, c.COST_BPS, START, END)
    R3 = c.replica_run(Ocol, Ccol, W, 0.0, c.COST_BPS, START, END)
    def met(eq, tv, cost, scale=1.0):
        e = eq[first_idx - 1:] if first_idx > 0 else np.concatenate([[1.0], eq])
        r = pd.Series(e).pct_change().dropna(); yrs = len(r) / c.PPY; pk = np.maximum.accumulate(e)
        return dict(sharpe=float(c.bt_metrics.sharpe_ratio(r, periods_per_year=c.PPY)), max_drawdown=float(((pk - e) / pk).max()),
                    cagr=float((e[-1] / e[0]) ** (1 / yrs) - 1), annual_turnover_x_equity=float(tv / scale / yrs / e.mean()),
                    cost_drag_per_year=float(cost / scale / yrs / e.mean()))
    out = dict(label=label, n_cols=len(cols), median_nonzero_weight_post_overlay=float(np.median(v)),
               share_nonzero_weights_below_band=float((v < BETA).mean()), share_nonzero_weights_below_2band=float((v < 2 * BETA).mean()),
               frac_bars_invested=float((np.abs(W).sum(axis=1) > 1e-12).mean()),
               S1=met(R1["equity"], R1["turnover"].sum(), R1["cost"].sum()), S3=met(R3["equity"], R3["turnover"].sum(), R3["cost"].sum()),
               n_orders_S1=int(R1["traded"].sum()), by_alloc={})
    for a in allocs:
        R2 = c.replica_run(Ocol, Ccol, W * a, BETA * a, c.COST_BPS, START, END)
        P2 = c.pocket_equity_from_wallet(R2["equity"], a)
        S2 = met(P2, R2["turnover"].sum(), R2["cost"].sum(), scale=a)
        P1pre = R1["equity_pre"][:, None]; wp = R1["cur_w"]; what = R1["tgt_w"]
        active = (wp > 1e-12) | (what > 1e-12)
        h1 = np.abs(what - wp) < BETA; h2 = np.abs(what - wp * k_fn(P1pre, a)) < BETA
        dis = active & (h1 != h2)
        xor = R1["traded"] ^ R2["traded"]
        R2b = c.replica_run(Ocol, Ccol, W * a, 0.0, c.COST_BPS, START, END)
        P2b = c.pocket_equity_from_wallet(R2b["equity"], a)
        S2b = met(P2b, R2b["turnover"].sum(), R2b["cost"].sum(), scale=a)
        S1_, S3_ = out["S1"], out["S3"]
        out["by_alloc"][f"a={a:.2f}"] = dict(
            S2b=S2b,
            band_value_sharpe_pocket_frame_S1_minus_S3=S1_["sharpe"] - S3_["sharpe"],
            band_value_sharpe_wallet_frame_S2_minus_S2b=S2["sharpe"] - S2b["sharpe"],
            delta_band_value_wallet_minus_pocket=(S2["sharpe"] - S2b["sharpe"]) - (S1_["sharpe"] - S3_["sharpe"]),
            frame_effect_noband_S2b_minus_S3=S2b["sharpe"] - S3_["sharpe"],
            D2_unmatched_S1_pm24h=c.unmatched_orders(R1['traded'], R2['traded'], 24)[0], D2_unmatched_S2_pm24h=c.unmatched_orders(R1['traded'], R2['traded'], 24)[1],
            D1_episodes=c.episodes(dis), min_pocket_equity_S1=float(R1["equity"].min()), min_pocket_equity_S2=float(P2.min()),
            wallet_final_equity_S2=float(R2["equity"][-1]), wallet_min_equity_S2=float(R2["equity"].min()),
            S2=S2, n_orders_S2=int(R2["traded"].sum()),
            d_sharpe_S2_minus_S1=S2["sharpe"] - out["S1"]["sharpe"], d_maxdd=S2["max_drawdown"] - out["S1"]["max_drawdown"],
            d_cagr=S2["cagr"] - out["S1"]["cagr"],
            D1_disagree_cells=int(dis.sum()), D1_per_S1_order=float(dis.sum() / max(1, R1["traded"].sum())),
            D1_share_of_active=float(dis.sum() / max(1, active.sum())), D2_xor_cells=int(xor.sum()))
    return out

res = {}
t0 = time.time()
# --- vol_breakout_6majors : 4 combos, 6 majors ---
for g in vb.PARAM_GRID:
    w_raw = vb.generate_weight_decisions(CLP, vb.VolBreakoutParams(**g))
    W, sc = overlay_prescale(w_raw, CLP)
    key = f"vol_breakout_6majors/W{g['window_hours']}_P{g['squeeze_percentile']}"
    res[key] = run_cfg(key, W, list(CLP.columns), [0.25, 0.30]); print(key, round(time.time() - t0), "s", flush=True)
# --- pairs_ethbtc : 4 combos, BTC/ETH ---
cl2 = CLP[["BTC", "ETH"]]
for g in pr.PARAM_GRID:
    w_raw = pr.generate_weight_decisions(cl2, pr.PairsRatioParams(**g))
    W, sc = overlay_prescale(w_raw, cl2)
    key = f"pairs_ethbtc/L{g['l_hours']}_th{g['theta']}"
    res[key] = run_cfg(key, W, ["BTC", "ETH"], [0.25, 0.30]); print(key, round(time.time() - t0), "s", flush=True)
# --- btc_seasonality ---
cl1 = CLP[["BTC"]]
w_raw = sea.generate_weight_decisions(cal, ["BTC"])
W, sc = overlay_prescale(w_raw, cl1)
res["btc_seasonality_2123utc"] = run_cfg("btc_seasonality_2123utc", W, ["BTC"], [0.25, 0.30]); print("seasonality", round(time.time() - t0), "s", flush=True)
# --- protective_vol_gate : 4 combos, 6 majors ---
for g in c.pvg.PARAM_GRID:
    w_raw = c.pvg.generate_weight_decisions(CLP, c.pvg.ProtectiveVolGateParams(**g))
    W, sc = overlay_prescale(w_raw, CLP)
    key = f"protective_vol_gate/V{g['vol_window_hours']}_pin{g['p_in']}"
    res[key] = run_cfg(key, W, list(CLP.columns), [0.25, 0.30]); print(key, round(time.time() - t0), "s", flush=True)
json.dump(res, open(HERE / "results_t4_retro.json", "w"), indent=1)

rows = []
for k, v in res.items():
    for ak, av in v["by_alloc"].items():
        rows.append(dict(cand=k, a=ak, med_w=round(v["median_nonzero_weight_post_overlay"], 3), S1=round(v["S1"]["sharpe"], 3), S2=round(av["S2"]["sharpe"], 3),
                         dSh=round(av["d_sharpe_S2_minus_S1"], 3), dBandValue=round(av["delta_band_value_wallet_minus_pocket"], 3),
                         dFrame=round(av["frame_effect_noband_S2b_minus_S3"], 3), D1ep=av["D1_episodes"], xor=av["D2_xor_cells"], um24=av["D2_unmatched_S1_pm24h"], ord1=v["n_orders_S1"], ord2=av["n_orders_S2"]))
print(pd.DataFrame(rows).to_string())
