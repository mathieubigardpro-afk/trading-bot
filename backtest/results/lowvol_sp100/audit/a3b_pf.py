import pickle, numpy as np, pandas as pd
import indep as I
cal, O, C, raw, spy = I.load_all()
NR, V = I.panels_from_raw(raw, cal)
Oa, Ca = O.to_numpy(), C.to_numpy()
wins = I.windows(cal)
GRID = [(L, N) for L in (63, 252) for N in (20, 30)]
Wc = {g: I.weights(cal, NR, V, *g) for g in GRID}
chosen = pickle.load(open("./a2.pkl", "rb"))["chosen"]
st = None; ev_all = []; unreal_by_window = []
for w, g in zip(wins, chosen):
    r = I.sim(Wc[g], Oa, Ca, w["oos_s"], w["oos_e"], 5.0, 0.05, state=st, record=True)
    ev_all += [e[2] for e in r["events"]]
    sh = r["shares_end"]; c = np.nan_to_num(Ca[w["oos_e"]])
    un = {s: sh[s] * (c[s] - r["avg"][s]) for s in np.flatnonzero(sh > 1e-9) if s in r["avg"]}
    unreal_by_window.append((sum(v for v in un.values()), sum(v for v in un.values() if v < 0), sum(v for v in un.values() if v > 0), len(un)))
    st = r["state"]
g_ = sum(p for p in ev_all if p > 0); l_ = -sum(p for p in ev_all if p < 0)
print("evenements realises : gains %.3f pertes %.3f PF %.2f ; nb gagnants %d perdants %d" % (g_, l_, g_ / l_, sum(p > 0 for p in ev_all), sum(p < 0 for p in ev_all)))
# unrealized en fin de chaque fenetre (equity de fenetre = 1 au debut) : pertes latentes sur lignes ouvertes
neg = np.array([u[1] for u in unreal_by_window]); pos = np.array([u[2] for u in unreal_by_window]); nopen = np.array([u[3] for u in unreal_by_window])
print("lignes ouvertes en fin de fenetre : moy %.1f ; PnL latent perdant moyen par fenetre (unites d'equity) %.3f ; latent gagnant %.3f" % (nopen.mean(), neg.mean(), pos.mean()))
print("derniere fenetre : latent net %.3f (perdant %.3f, gagnant %.3f) sur %d lignes ouvertes" % (unreal_by_window[-1][0], unreal_by_window[-1][1], unreal_by_window[-1][2], unreal_by_window[-1][3]))
# PF "mark-to-market" : realises + latent de FIN de CHAQUE fenetre compte comme realise (borne pessimiste: double compte le portage) 
