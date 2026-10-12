"""T7 -- agregats cites dans le rapport (tous calcules ici)."""
import json
from pathlib import Path
import numpy as np
H = Path(__file__).parent
J = lambda n: json.load(open(H / n))
S = {}
wf = J("results_t3_walkforward_structure.json")
rows = []
for k, v in wf.items():
    for ak, av in v["by_alloc"].items():
        rows.append((k, ak, av["dSharpe_S2_minus_S1"], av["d_band_value"], av["frame_effect_noband"], av["dSharpe_boot95"], av["unmatched_share_of_S1_orders"], av["unmatched_share_of_S2_orders"], av["dMaxDD"]))
d = np.array([r[2] for r in rows]); db = np.array([r[3] for r in rows]); fr = np.array([r[4] for r in rows])
cand = [r for r in rows if not r[0].startswith(("A_", "B_", "C_"))]
dc = np.array([r[2] for r in cand]); dbc = np.array([r[3] for r in cand]); frc = np.array([r[4] for r in cand])
um = np.array([max(r[6], r[7]) for r in rows])
S["walkforward_structure"] = dict(n_configs=len(rows), n_dSharpe_negative=int((d < 0).sum()), dSharpe_min=float(d.min()), dSharpe_max=float(d.max()), dSharpe_median=float(np.median(d)),
    dBandValue_min=float(db.min()), dBandValue_max=float(db.max()), max_abs_dBandValue=float(np.abs(db).max()), frame_min=float(fr.min()), frame_max=float(fr.max()),
    n_ci_excludes_zero=int(sum(1 for r in rows if r[5][0] > 0 or r[5][1] < 0)), configs_ci_excl_zero=[(r[0], r[1]) for r in rows if r[5][0] > 0 or r[5][1] < 0],
    unmatched_share_max=float(um.max()), unmatched_share_median=float(np.median(um)),
    candidates_only=dict(n=len(cand), dSharpe_min=float(dc.min()), dSharpe_max=float(dc.max()), max_abs_dBandValue=float(np.abs(dbc).max()), frame_min=float(frc.min()), frame_max=float(frc.max()),
                         dMaxDD_abs_max=float(max(abs(r[8]) for r in cand))))
f3 = J("results_t3_main.json"); rows = []
for p, pv in f3["profiles"].items():
    for ak, e in pv["semantics"].items():
        rows.append((p, ak, e["delta_S2_minus_S1"]["sharpe"], e["decomposition_sharpe"]["band_wallet_frame_S2_minus_S2b"] - e["decomposition_sharpe"]["band_pocket_frame_S1_minus_S3"],
                     e["decomposition_sharpe"]["frame_S2b_minus_S3"], e["delta_S2_minus_S1"]["max_drawdown"], e["delta_S2_minus_S1"]["profit_factor"]))
r = np.array([[x[2], x[3], x[4], x[5], x[6]] for x in rows])
S["fullsample_A_B_C"] = dict(n=len(rows), dSharpe_range=[float(r[:, 0].min()), float(r[:, 0].max())], dBandValue_range=[float(r[:, 1].min()), float(r[:, 1].max())],
                             frame_range=[float(r[:, 2].min()), float(r[:, 2].max())], dMaxDD_range=[float(r[:, 3].min()), float(r[:, 3].max())], dPF_range=[float(r[:, 4].min()), float(r[:, 4].max())])
t4 = J("results_t4_retro.json"); rows = []
for k, v in t4.items():
    if k.startswith("btc_seasonality"): continue
    for ak, av in v["by_alloc"].items():
        rows.append((k, ak, av["d_sharpe_S2_minus_S1"], av["delta_band_value_wallet_minus_pocket"], av["frame_effect_noband_S2b_minus_S3"], av["D2_unmatched_S1_pm24h"] / v["n_orders_S1"], av["D2_unmatched_S2_pm24h"] / av["n_orders_S2"]))
r = np.array([[x[2], x[3], x[4], x[5], x[6]] for x in rows])
S["fullsample_candidate_signals_ex_seasonality"] = dict(n=len(rows), n_dSharpe_positive=int((r[:, 0] > 0).sum()), dSharpe_range=[float(r[:, 0].min()), float(r[:, 0].max())],
    dBandValue_range=[float(r[:, 1].min()), float(r[:, 1].max())], frame_range=[float(r[:, 2].min()), float(r[:, 2].max())], unmatched_share_max=float(r[:, 3:].max()))
sp = J("results_t3_subperiods.json"); d = [v["dSharpe"] for c in sp.values() for v in c.values()]; fr = [v["frame_effect"] for c in sp.values() for v in c.values()]
S["subperiods"] = dict(n=len(d), n_positive=int(sum(x > 0 for x in d)), n_negative=int(sum(x < 0 for x in d)), dSharpe_range=[min(d), max(d)], frame_range=[min(fr), max(fr)])
t1 = J("results_t2_analytic.json"); S["t2"] = {k: t1[k] for k in ("V1_real_manager_vs_analytic_S2_rule", "V2_affine_relation_engine", "V3_bound_checks_passed")}
json.dump(S, open(H / "results_summary.json", "w"), indent=1); print(json.dumps(S, indent=1))
