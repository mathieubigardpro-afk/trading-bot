"""Point 6 : annexe band_fidelity. S2 recalcule independamment, reconversion en unites de poche, bloc cb, deux lectures."""
import pickle, json, numpy as np, pandas as pd
import indep as I
cal, O, C, raw, spy = I.load_all()
NR, V = I.panels_from_raw(raw, cal)
Oa, Ca = O.to_numpy(), C.to_numpy()
wins = I.windows(cal)
GRID = [(L, N) for L in (63, 252) for N in (20, 30)]
Wc = {g: I.weights(cal, NR, V, *g) for g in GRID}
chosen = pickle.load(open("./a2.pkl", "rb"))["chosen"]
a = 0.3
st = None; Es = []; segs = []
w0 = []  # poids de la poche (positions) dans le wallet au 1er jour de chaque fenetre
for w, g in zip(wins, chosen):
    if st is not None:
        w0.append(float((st[0] * np.nan_to_num(Ca[w["oos_s"] - 1])).sum()))   # valeur des positions / equity(=1)
    else:
        w0.append(0.0)
    r = I.sim(Wc[g] * a, Oa, Ca, w["oos_s"], w["oos_e"], 5.0, 0.05 * a, state=st, record=True)
    st = r["state"]; segs.append(r)
E_ret = np.concatenate([s["ret"] for s in segs])
E_eq = np.concatenate([s["eq"] for s in segs])
print("S2 : cash min (fraction de l'equity wallet) = %.4f ; cash moyen %.4f ; positif toujours : %s" %
      (min(s["cash"].min() for s in segs), np.mean(np.concatenate([s["cash"] for s in segs])), all((s['cash'] > 0).all() for s in segs)))
G = np.concatenate([s["gross"] for s in segs]); NH = np.concatenate([s["nheld"] for s in segs])
print("S2 : expo brute wallet moy %.4f max %.4f ; => en unites de poche moy %.3f max %.3f ; nTitres moy %.1f max %d" %
      (G.mean(), G.max(), G.mean() / a, G.max() / a, NH.mean(), NH.max()))
print("poids de la poche dans le wallet au 1er jour de chaque fenetre (a=0.30) : min %.3f med %.3f max %.3f" % (min(w0[1:]), np.median(w0[1:]), max(w0[1:])))
print("   fenetres: ", [round(x, 2) for x in w0])

# reconversion de la SPEC : P = 1 + (E-1)/a par fenetre
P_ret = []
for s in segs:
    p = 1 + (s["eq"] - 1) / a
    rr = np.empty(len(p)); rr[0] = p[0] - 1; rr[1:] = p[1:] / p[:-1] - 1
    P_ret.append(rr)
P_ret = np.concatenate(P_ret)
print("\nS2 reconverti (formule SPEC P=1+(E-1)/a) : Sharpe %.4f (candidat 0.7703) MaxDD %.4f (0.5741)" % (I.sharpe(P_ret), I.maxdd(P_ret)))
# reconversion "correcte" : diviser par le poids REEL de la poche au debut de chaque fenetre (au lieu de a)
P2 = []
for s, w0k in zip(segs, w0):
    base = a if w0k == 0 else w0k
    p = 1 + (s["eq"] - 1) / base
    rr = np.empty(len(p)); rr[0] = p[0] - 1; rr[1:] = p[1:] / p[:-1] - 1
    P2.append(rr)
P2 = np.concatenate(P2)
print("S2 reconverti avec le poids reel de poche en debut de fenetre : Sharpe %.4f MaxDD %.4f" % (I.sharpe(P2), I.maxdd(P2)))
# Sharpe du wallet S2 non reconverti
print("S2 wallet brut (E concatene, c-a-d wallet CONTINU a rendements chaines) : Sharpe %.4f MaxDD %.4f CAGR %.4f" %
      (I.sharpe(E_ret), I.maxdd(E_ret), np.prod(1 + E_ret) ** (252 / len(E_ret)) - 1))

r_over_a = E_ret / a
print("S2 reconverti par r_E/a quotidien (poche = a x equity wallet, resize mensuel) : Sharpe %.4f MaxDD %.4f" % (I.sharpe(r_over_a), I.maxdd(r_over_a)))
d = I.dsr(P_ret, 166); print("DSR sur P_ret (formule SPEC) : %.4f ; sur r_E/a : %.4f" % (d['dsr'], I.dsr(r_over_a, 166)['dsr']))
pn = [e[2] for s_ in segs for e in s_['events']]; print("S2 PF (evenements realises) : %.2f ; nb evenements %d ; lignes closes %d" % (I.profit_factor(pn), len(pn), sum(1 for s_ in segs for e in s_['events'] if e[3])))
# lectures du wallet pour le bloc cb
def dd_stats(ret, half=.15, flat=.25, freeze=.03):
    eq = np.concatenate([[1], np.cumprod(1 + ret)]); pk = np.maximum.accumulate(eq); dd = 1 - eq / pk
    d1 = dd[1:]
    def runs(m):
        prev = np.concatenate([[False], m[:-1]]); return int((m & ~prev).sum())
    return dict(maxdd=float(dd.max()), days_half=int((d1 > half).sum()), ep_half=runs(d1 > half), days_flat=int((d1 > flat).sum()), ep_flat=runs(d1 > flat),
                ep_freeze=runs(ret < -freeze), n_freeze_days=int((ret < -freeze).sum()))
idx = np.concatenate([np.arange(w["oos_s"], w["oos_e"] + 1) for w in wins])
# (1) formule SPEC : E = 1 + a(P-1), P = equity de poche concatenee (unites de poche)
Pc = np.cumprod(1 + P_ret); E1 = 1 + a * (Pc - 1); r1 = np.concatenate([[E1[0] - 1], E1[1:] / E1[:-1] - 1])
print("\n(1) E = 1+a(P-1), P concatene     :", {k: (round(v, 4) if isinstance(v, float) else v) for k, v in dd_stats(r1).items()})
# (2) a re-equilibre a chaque fenetre : rendement wallet = a * rendement de poche, chaine
r2 = a * P_ret
print("(2) a reequilibre chaque fenetre   :", {k: (round(v, 4) if isinstance(v, float) else v) for k, v in dd_stats(r2).items()})
# (3) wallet simule S2 tel quel (positions et cash portes, poche non reequilibree vs cash)
print("(3) wallet S2 continu (E concatene):", {k: (round(v, 4) if isinstance(v, float) else v) for k, v in dd_stats(E_ret).items()})
# (4) wallet S2 si le cash inactif est re-equilibre vers la poche chaque mois = production : targets a*W*equity (deja le cas) -> (3)

# Par date des pires drawdowns (3)
eq3 = np.concatenate([[1], np.cumprod(1 + E_ret)]); pk = np.maximum.accumulate(eq3); dd3 = 1 - eq3 / pk
j = int(dd3.argmax()); print("pire DD (3) a la date", cal[idx[j - 1]].date(), "valeur", dd3[j])
for nm, rr in (("(1)", r1), ("(2)", r2), ("(3)", E_ret)):
    eq = np.concatenate([[1], np.cumprod(1 + rr)]); dd = 1 - eq / np.maximum.accumulate(eq)
    ab = dd[1:] > .25
    firsts = cal[idx[np.flatnonzero(ab)[0]]].date() if ab.any() else None
    print(nm, "1er franchissement flatten 25 % :", firsts)
pickle.dump(dict(E_ret=E_ret, P_ret=P_ret, w0=w0), open("./s2.pkl", "wb"))
