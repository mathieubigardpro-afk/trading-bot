import pickle, numpy as np, pandas as pd
import indep as I
cal, O, C, raw, spy = I.load_all()
NR, V = I.panels_from_raw(raw, cal)
Oa, Ca = np.nan_to_num(O.to_numpy()), np.nan_to_num(C.to_numpy())
wins = I.windows(cal)
GRID = [(L, N) for L in (63, 252) for N in (20, 30)]
Wc = {g: I.weights(cal, NR, V, *g) for g in GRID}
chosen = pickle.load(open("./a2.pkl", "rb"))["chosen"]
k = Oa.shape[1]; rate = 5e-4
def orders(scale, band):
    sh = np.zeros(k); cash = 1.0; flags = []; n_ord = 0
    for w, g in zip(wins, chosen):
        W = Wc[g] * scale
        for i in range(w["oos_s"], w["oos_e"] + 1):
            o, c, wd = Oa[i], Ca[i], W[i - 1]
            Vv = sh * o; eqb = cash + Vv.sum(); safe = np.where(o == 0, np.nan, o)
            tgt = np.nan_to_num(wd * eqb / safe, nan=0.0)
            tgt = np.where(np.abs(wd - Vv / eqb) < band, sh, tgt)
            tr = tgt - sh; ch = np.abs(tr) > 1e-9
            cash = cash - float((tr * o).sum()) - float((np.abs(tr[ch]) * o[ch]).sum()) * rate
            sh = tgt; flags.append(ch.copy()); n_ord += int(ch.sum())
        e = cash + float((sh * np.nan_to_num(Ca[w["oos_e"]])).sum()); sh = sh / e; cash = cash / e
    return np.array(flags), n_ord
f1, n1 = orders(1.0, 0.05); f2, n2 = orders(0.3, 0.015)
x=f1^f2
print('D2 par cellule titre-barre: barres avec >=1 cellule divergente = %d ; cellules divergentes = %d' % (x.any(axis=1).sum(), x.sum()))
f1=f1.any(axis=1); f2=f2.any(axis=1)
print("barres avec ordre S1 %d / S2 %d ; ordres S1 %d / S2 %d ; D2 (XOR) = %d  (candidat: 394 / 543 ; 801 / 1468 ; 619)" % (f1.sum(), f2.sum(), n1, n2, (f1 ^ f2).sum()))
