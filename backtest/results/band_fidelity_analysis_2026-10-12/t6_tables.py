"""T6 -- imprime les tableaux Markdown du rapport a partir des JSON de resultats (aucun chiffre saisi a la main)."""
import json
from pathlib import Path
import pandas as pd
H = Path(__file__).parent
def J(n): return json.load(open(H / n))
def md(df): return df.to_markdown(index=False) if hasattr(df, "to_markdown") else df.to_string(index=False)
f3 = J("results_t3_main.json"); out = []

# ---- T3-1 principal : metriques par semantique (fenetre complete) ----
rows = []
for p, pv in f3["profiles"].items():
    S1, S3 = pv["S1"], pv["S3"]
    for ak, e in pv["semantics"].items():
        for sem, m in (("S1 moteur", S1), ("S2 prod. modelisee", e["S2"]), ("S2b wallet sans bande", e["S2b"]), ("S3 sans bande", S3)):
            rows.append(dict(profil=p, alloc=ak, semantique=sem, Sharpe=round(m["sharpe"], 3), PF=round(m["profit_factor"], 3), MaxDD=round(m["max_drawdown"], 3),
                             CAGR=round(m["cagr"], 3), turnover_an=round(m["annual_turnover_x_equity"], 2), couts_an=round(m["cost_drag_per_year"], 4),
                             ordres=m["n_orders"], trades_clos=m["n_trades_closed"]))
t31 = pd.DataFrame(rows)
print("### T3-1 metriques par semantique (poche, fenetre complete)\n"); print(md(t31))
# ---- T3-2 divergences ----
rows = []
for p, pv in f3["profiles"].items():
    for ak, e in pv["semantics"].items():
        d1, d2 = e["D1_same_state"], e["D2_independent_paths"]
        rows.append(dict(profil=p, alloc=ak, ordres_S1=d2["n_orders_S1"], ordres_S2=d2["n_orders_S2"], bougies_ordre_S1=d2["n_bars_order_S1"], bougies_ordre_S2=d2["n_bars_order_S2"],
                         bougies_XOR=d2["n_bars_with_xor"], cellules_XOR=d2["n_xor_cells"], S1_sans_S2_pm24h=d2["unmatched_S1_orders_within_pm24h"], S2_sans_S1_pm24h=d2["unmatched_S2_orders_within_pm24h"],
                         S1_sans_S2_pm1h=d2["unmatched_S1_orders_within_pm1h"], S2_sans_S1_pm1h=d2["unmatched_S2_orders_within_pm1h"],
                         D1_episodes=d1["n_disagree_episodes"], D1_part_cellules=round(d1["share_disagree_of_active"], 3), k_abs_moy=round(d1["mean_abs_k_minus_1_when_invested"], 3)))
print("\n### T3-2 divergences de decisions\n"); print(md(pd.DataFrame(rows)))
# ---- T3-3 decomposition ----
rows = []
for p, pv in f3["profiles"].items():
    for ak, e in pv["semantics"].items():
        dc = e["decomposition_sharpe"]; ci = e["delta_Sharpe_S2_minus_S1_boot95"]; dd = e["delta_S2_minus_S1"]
        rows.append(dict(profil=p, alloc=ak, **{"dSharpe S2-S1": round(dc["total_S2_minus_S1"], 3), "IC95 boot": [round(ci[0], 2), round(ci[1], 2)],
                         "valeur bande cadre poche (S1-S3)": round(dc["band_pocket_frame_S1_minus_S3"], 3), "valeur bande cadre wallet (S2-S2b)": round(dc["band_wallet_frame_S2_minus_S2b"], 3),
                         "ecart de valeur de bande": round(dc["band_wallet_frame_S2_minus_S2b"] - dc["band_pocket_frame_S1_minus_S3"], 3), "effet de cadre sans bande (S2b-S3)": round(dc["frame_S2b_minus_S3"], 3),
                         "dMaxDD": round(dd["max_drawdown"], 4), "dCAGR": round(dd["cagr"], 4), "dPF": round(dd["profit_factor"], 4),
                         "P_fin S1": round(e["P_end_S1"], 3), "bascule Sharpe0.70": e["porte1_threshold_flips_sharpe0p70"], "bascule PF1.15": e["porte1_threshold_flips_pf1p15"],
                         "bascule trades80": e["porte1_threshold_flips_ntrades80"], "bascule MaxDD1.5xBH": e["porte1_threshold_flips_maxdd1p5xBH"]}))
print("\n### T3-3 decomposition de l'ecart (fenetre complete)\n"); print(md(pd.DataFrame(rows)))
# ---- CB ----
rows = []
for p, pv in f3["profiles"].items():
    for ak, e in pv["semantics"].items():
        cb = e["cb_exposure_pocket_only"]
        rows.append(dict(profil=p, alloc=ak, DD_wallet_max=round(cb["max_wallet_dd"], 4), seuil_half=cb["half_size_thr"], seuil_flatten=cb["flatten_thr"], h_DD_gt_half=cb["hours_dd_above_half"],
                         flatten_declenche=cb["flatten_would_trigger"], seuil_gel_24h=cb["freeze_thr"], h_perte24h_gt_seuil=cb["hours_daily_loss_above_freeze"], episodes_gel=cb["n_freeze_episodes"]))
print("\n### CB exposure (poche seule, reste en cash = borne basse)\n"); print(md(pd.DataFrame(rows)))
# ---- WF structure ----
wf = J("results_t3_walkforward_structure.json"); rows = []
for k, v in wf.items():
    for ak, av in v["by_alloc"].items():
        rows.append(dict(config=k, alloc=ak, poids_med=round(v["median_nonzero_weight"], 3), S1=round(v["S1"]["sharpe"], 3), S2=round(av["S2"]["sharpe"], 3), S3=round(v["S3"]["sharpe"], 3),
                         dSharpe=round(av["dSharpe_S2_minus_S1"], 3), IC95=[round(x, 2) for x in av["dSharpe_boot95"]], ecart_valeur_bande=round(av["d_band_value"], 3),
                         effet_cadre=round(av["frame_effect_noband"], 3), dMaxDD=round(av["dMaxDD"], 3), ordres_S1=v["S1"]["n_orders"], ordres_S2=av["S2"]["n_orders"],
                         part_S1_sans_S2_pm24h=round(av["unmatched_share_of_S1_orders"], 3), part_S2_sans_S1_pm24h=round(av["unmatched_share_of_S2_orders"], 3),
                         k_abs_moy=round(av["mean_abs_k_minus_1"], 3), maxP_fenetre=round(v["S1"]["max_abs_P_minus_1_within_window"], 2)))
print("\n### T3-4 structure walk-forward (fenetres OOS 3 mois, capital remis a 1)\n"); print(md(pd.DataFrame(rows)))
# ---- sous-periodes ----
sp = J("results_t3_subperiods.json"); rows = []
for cfg, d in sp.items():
    for per, v in d.items():
        rows.append(dict(config=cfg, periode=per, S1=round(v["S1_Sharpe"] if "S1_Sharpe" in v else v["S1_sharpe"], 2), S2=round(v["S2_sharpe"], 2), dSharpe=round(v["dSharpe"], 3),
                         ecart_valeur_bande=round(v["d_band_value"], 3), effet_cadre=round(v["frame_effect"], 3), P_fin_S1=round(v["S1_final_pocket_equity"], 3), ordres_S1=v["orders_S1"], ordres_S2=v["orders_S2"]))
print("\n### T3-5 sous-periodes (capital remis a 1)\n"); print(md(pd.DataFrame(rows)))
# ---- reste du wallet ----
rw = J("results_t3_rest_of_wallet.json"); rows = []
for k, v in rw.items():
    s1 = v["S1_pocket_frame"]
    for rn, sv in v.items():
        if not rn.startswith("S2"): continue
        rows.append(dict(config=k, reste=rn.replace("S2_rest=", ""), S1_Sharpe=round(s1["sharpe"], 3), S2_Sharpe=round(sv["sharpe"], 3), dSharpe=round(sv["delta_sharpe_vs_S1"], 3),
                         dMaxDD=round(sv["delta_maxdd_vs_S1"], 3), k_abs_moy=round(sv["mean_abs_k_minus_1"], 3), ordres=sv["n_orders"], ordres_S1=sv["n_orders_S1"], XOR=sv["xor_cells_vs_S1"], plafond_liquidite_bougies=sv["liquidity_cap_bars"]))
print("\n### T3-6 sensibilite au reste du wallet\n"); print(md(pd.DataFrame(rows)))
# ---- T4 retro full-sample ----
t4 = J("results_t4_retro.json"); rows = []
for k, v in t4.items():
    for ak, av in v["by_alloc"].items():
        rows.append(dict(candidate=k, alloc=ak, poids_med=round(v["median_nonzero_weight_post_overlay"], 3), part_sous_bande=round(v["share_nonzero_weights_below_band"], 2), S1=round(v["S1"]["sharpe"], 3), S2=round(av["S2"]["sharpe"], 3),
                         dSharpe=round(av["d_sharpe_S2_minus_S1"], 3), ecart_valeur_bande=round(av["delta_band_value_wallet_minus_pocket"], 3), effet_cadre=round(av["frame_effect_noband_S2b_minus_S3"], 3),
                         ordres_S1=v["n_orders_S1"], ordres_S2=av["n_orders_S2"], S1_sans_S2_pm24h=av["D2_unmatched_S1_pm24h"], S2_sans_S1_pm24h=av["D2_unmatched_S2_pm24h"]))
print("\n### T4 rejeu des signaux (fenetre complete)\n"); print(md(pd.DataFrame(rows)))
