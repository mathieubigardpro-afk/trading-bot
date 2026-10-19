"""Points 5 et 7 : DSR from scratch, sensibilite aux couts, fragilite flottante, eligibilite, fenetres, retouche."""
import pickle, json, math, subprocess, numpy as np, pandas as pd
from scipy import stats
import indep as I
cal, O, C, raw, spy = I.load_all()
NR, V = I.panels_from_raw(raw, cal)
Oa, Ca = O.to_numpy(), C.to_numpy()
wins = I.windows(cal); dm = I.decision_mask(cal)
GRID = [(L, N) for L in (63, 252) for N in (20, 30)]
Wc = {g: I.weights(cal, NR, V, *g) for g in GRID}
pk = pickle.load(open("./a2.pkl", "rb"))
chosen, ret = pk["chosen"], pk["ret"]

print("== DSR from scratch (Bailey & Lopez de Prado 2014) ==")
for K in (166, 16 + 30 * 4, 16 + 30 * 4 + 30, 216, 1):
    d = I.dsr(ret, K) if K > 1 else None
    if d: print(f"K={K}: SR_quot={d['sr']:.6f} n={d['n']} skew={d['skew']:.4f} kurt_excess={d['kurt_pearson']-3:.4f} se={d['se']:.6f} SR0={d['sr0']:.6f} z={d['z']:.3f} DSR={d['dsr']:.4f}")
print("nombre d'obs = somme des longueurs OOS :", sum(w['oos_e'] - w['oos_s'] + 1 for w in wins))
# variante fidele au papier : SR0 base sur la DISPERSION INTER-ESSAIS des Sharpe (4 combos IS-selection ; N=30 jamais investi)
srs = []
for g in GRID:
    st = None; R = []
    for w in wins:
        r = I.sim(Wc[g], Oa, Ca, w["oos_s"], w["oos_e"], 5.0, 0.05, state=st); st = r["state"]; R.append(r["ret"])
    R = np.concatenate(R); sr = R.mean() / R.std(ddof=1) if R.std() > 0 else float('nan'); srs.append(sr)
    print("combo fixe", g, "OOS Sharpe annualise sur tout l'OOS:", None if np.isnan(sr) else round(sr * math.sqrt(252), 3))
valid = [s for s in srs if not np.isnan(s)]
print("(N=30: NaN => jamais investi ; seulement", len(valid), "combos sur 4 reellement distinctes dans la grille)")

print("\n== Sensibilite aux couts (combos choisis, portage) : Sharpe et PF ==")
for c in (0, 5, 15, 25, 50):
    st = None; R = []; ev = []
    for w, g in zip(wins, chosen):
        r = I.sim(Wc[g], Oa, Ca, w["oos_s"], w["oos_e"], c, 0.05, state=st, record=True); st = r["state"]; R.append(r["ret"]); ev += [e[2] for e in r["events"]]
    R = np.concatenate(R); print(f"S1 band {c:>2} bps: Sharpe {I.sharpe(R):.4f}  PF {I.profit_factor(ev):.2f}  MaxDD {I.maxdd(R):.4f}")
print("-- mecanique voulue (rebalance mensuel reel, sorties executees) --")
for c in (0, 5, 15, 25, 50):
    st = None; R = []; ev = []
    for w, g in zip(wins, chosen):
        r = I.sim(Wc[g], Oa, Ca, w["oos_s"], w["oos_e"], c, 0.0, state=st, record=True, mode='monthly', rebal=dm); st = r["state"]; R.append(r["ret"]); ev += [e[2] for e in r["events"]]
    R = np.concatenate(R); print(f"monthly {c:>2} bps: Sharpe {I.sharpe(R):.4f}  PF {I.profit_factor(ev):.2f}  MaxDD {I.maxdd(R):.4f}")

print("\n== Fragilite flottante : entree a EXACTEMENT 0.05 vs bande 0.05 (inegalite stricte) ==")
for eps in (0.0, 1e-12, 1e-9):
    st = None; R = []; orders = 0
    for w, g in zip(wins[:6], chosen[:6]):
        r = I.sim(Wc[g] * (1 - eps), Oa, Ca, w["oos_s"], w["oos_e"], 5.0, 0.05, state=st, record=True); st = r["state"]; R.append(r["ret"]); orders += r["orders"]
    R = np.concatenate(R)
    print(f"poids x (1 - {eps:g}) sur les 6 premieres fenetres OOS : ordres = {orders}, Sharpe = {I.sharpe(R)}")
print("1/20 == 0.05 en flottant :", 1/20 == 0.05, "; 0.3*0.05 == 0.05*0.3 :", 0.3*0.05 == 0.05*0.3, "; (1/20)*0.3 == 0.05*0.3 :", (1/20)*0.3 == 0.05*0.3)

print("\n== Eligibilite 252 j / univers ==")
ne = []
for i in np.flatnonzero(dm):
    if i >= wins[0]['oos_s']:
        ne.append(int(((NR.iloc[i] >= 252) & V[63].iloc[i].notna() & V[252].iloc[i].notna()).sum()))
print("eligibles aux dates de decision OOS: min %d med %d max %d" % (min(ne), np.median(ne), max(ne)))
# un titre ayant 252 rendements exactement : le dernier rendement utilise est bien <= t ; 253 closes
first = {t: raw[t]['close'].dropna().index[0] for t in raw}
late = sorted(first.items(), key=lambda kv: kv[1])[-8:]
print("8 introductions les plus tardives :", [(t, d.date().isoformat()) for t, d in late])
print("n_windows =", len(wins), "; K_total = 16 + 30*4 + 30*1 =", 16 + len(wins) * 5)
print("fenetres OOS contigues :", all(wins[k]['oos_e'] + 1 == wins[k + 1]['oos_s'] for k in range(len(wins) - 1)))
print("dernier jour OOS", cal[wins[-1]['oos_e']].date(), "/ fin des donnees", cal[-1].date(), "(~9 mois de donnees non utilisees)")
