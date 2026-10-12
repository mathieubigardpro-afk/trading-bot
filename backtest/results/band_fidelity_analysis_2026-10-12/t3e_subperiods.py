"""T3e -- stabilite du signe/ordre de grandeur de l'ecart S2-S1 par SOUS-PERIODE (chaque sous-periode repart a
equity de poche = 1, flat). Replica (valide bit-exact vs moteur en t3c). Montre que l'effet de CADRE
(base de dimensionnement E vs P) depend du chemin de P (donc du regime), pas d'une propriete structurelle."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import numpy as np, pandas as pd
import common as c
from backtest.strategies import pairs_ratio as pr, vol_breakout as vb

HERE = Path(__file__).parent; CACHE = HERE / "cache"
cal = pd.DatetimeIndex(pd.read_pickle(CACHE / "cal.pkl"))
OP = pd.read_pickle(CACHE / "opens.pkl"); CLP = pd.read_pickle(CACHE / "closes.pkl")
BETA = c.BAND
periods = {"2022": ("2022-01-02", "2022-12-31 23:00"), "2023": ("2023-01-01", "2023-12-31 23:00"),
           "2024": ("2024-01-01", "2024-12-31 23:00"), "2025-01..2026-09": ("2025-01-01", "2026-09-30 23:00")}

def overlay_prescale(w_raw, closes):
    va, vc = c.risk_overlay.precompute_vol_stats(closes, halflife_days=c.risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
                                                  periods_per_year=c.risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR)
    wr = w_raw.to_numpy(); sc = np.ones(len(wr))
    for i in np.flatnonzero(np.abs(wr).sum(axis=1) > 1e-12):
        sc[i] = c.risk_overlay.compute_portfolio_vol_scalar(w_raw.iloc[i], va.iloc[i], vc.iloc[i])
    return w_raw.mul(sc, axis=0)

cfgs = {}
cfgs["A_quasi_passif_eq(a=0.30)"] = (pd.read_pickle(CACHE / "W_A_quasi_passif_equilibre.pkl"), list(CLP.columns), 0.30)
cfgs["B_lowweight_volgate(a=0.25)"] = (pd.read_pickle(CACHE / "W_B_lowweight_volgate.pkl"), list(CLP.columns), 0.25)
cfgs["C_bigweight_sma200(a=0.30)"] = (pd.read_pickle(CACHE / "W_C_bigweight_sma200_equal.pkl"), list(CLP.columns), 0.30)
cfgs["pairs L2160 th1.5 (a=0.30)"] = (overlay_prescale(pr.generate_weight_decisions(CLP[["BTC", "ETH"]], pr.PairsRatioParams(l_hours=2160, theta=1.5)), CLP[["BTC", "ETH"]]), ["BTC", "ETH"], 0.30)
cfgs["vol_breakout W110 P0.2 (a=0.30)"] = (overlay_prescale(vb.generate_weight_decisions(CLP, vb.VolBreakoutParams(window_hours=110, squeeze_percentile=0.2)), CLP), list(CLP.columns), 0.30)

def met(eq):
    e = np.concatenate([[1.0], eq]); r = pd.Series(e).pct_change().dropna()
    pk = np.maximum.accumulate(e)
    return float(c.bt_metrics.sharpe_ratio(r, periods_per_year=c.PPY)), float(((pk - e) / pk).max()), float(e[-1])

out = {}
rows = []
for name, (Wdf, cols, a) in cfgs.items():
    O = OP[cols].to_numpy(); C = CLP[cols].to_numpy(); W = Wdf[cols].to_numpy()
    out[name] = {}
    for pn, (t0, t1) in periods.items():
        i0 = int(cal.searchsorted(pd.Timestamp(t0))); i1 = int(cal.searchsorted(pd.Timestamp(t1), side="right")) - 1
        i0 = max(i0, 1)
        R1 = c.replica_run(O, C, W, BETA, c.COST_BPS, i0, i1); R3 = c.replica_run(O, C, W, 0.0, c.COST_BPS, i0, i1)
        R2 = c.replica_run(O, C, W * a, BETA * a, c.COST_BPS, i0, i1); R2b = c.replica_run(O, C, W * a, 0.0, c.COST_BPS, i0, i1)
        s1, d1, e1 = met(R1["equity"]); s3, _, _ = met(R3["equity"])
        s2, d2, e2 = met(c.pocket_equity_from_wallet(R2["equity"], a)); s2b, _, _ = met(c.pocket_equity_from_wallet(R2b["equity"], a))
        # Sharpe seul est bruite sur 1 an : on rapporte aussi le rendement de poche final
        d = dict(S1_sharpe=s1, S2_sharpe=s2, dSharpe=s2 - s1, band_value_pocket=s1 - s3, band_value_wallet=s2 - s2b,
                 d_band_value=(s2 - s2b) - (s1 - s3), frame_effect=s2b - s3, S1_final_pocket_equity=e1, S2_final_pocket_equity=e2,
                 orders_S1=int(R1["traded"].sum()), orders_S2=int(R2["traded"].sum()), xor=int((R1["traded"] ^ R2["traded"]).sum()),
                 S1_maxdd=d1, S2_maxdd=d2)
        out[name][pn] = d
        rows.append(dict(cfg=name, per=pn, S1=round(s1, 2), S2=round(s2, 2), dSh=round(s2 - s1, 3), dBand=round(d["d_band_value"], 3), frame=round(d["frame_effect"], 3),
                         P1end=round(e1, 3), P2end=round(e2, 3), o1=d["orders_S1"], o2=d["orders_S2"], xor=d["xor"]))
json.dump(out, open(HERE / "results_t3_subperiods.json", "w"), indent=1)
print(pd.DataFrame(rows).to_string())
