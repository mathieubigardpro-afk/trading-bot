"""EXPERIENCE LOCALE (non decisionnelle) : S1 avec contrainte de cash >= 0 comme le Ledger de production
(bot/sim/ledger.py : achat sans cash impossible). Ventes executees d'abord, puis achats par ordre alphabetique
(l'ordre de bot/runner.py : full_universe trie) dans la limite du cash disponible."""
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

def sim_funded(W, start, end, band, st=None):
    sh = np.zeros(k) if st is None else st[0].copy(); cash = 1.0 if st is None else st[1]
    ret = []; gross = []; nheld = []; blocked = 0; prev = None
    for i in range(start, end + 1):
        o, c, w = Oa[i], Ca[i], W[i - 1]
        Vv = sh * o; eqb = cash + Vv.sum(); safe = np.where(o == 0, np.nan, o)
        tgt = np.nan_to_num(w * eqb / safe, nan=0.0); cw = Vv / eqb
        tgt = np.where(np.abs(w - cw) < band, sh, tgt)
        tr = tgt - sh
        sells = np.where(tr < -1e-9, tr, 0.0); buys = np.where(tr > 1e-9, tr, 0.0)
        cash += float((-sells * o).sum()) - float((np.abs(sells) * o).sum()) * rate
        for s in np.flatnonzero(buys > 0):  # ordre alphabetique des colonnes
            cost = buys[s] * o[s] * (1 + rate)
            if cost > cash:
                q = max(cash, 0.0) / (o[s] * (1 + rate)); blocked += 1
            else:
                q = buys[s]
            sh[s] += q; cash -= q * o[s] * (1 + rate)
        sh = np.where(sells < 0, sh + sells, sh) if False else sh
        # appliquer les ventes sur les parts (deja soustraites du cash)
        sh = sh + sells
        e = cash + float((sh * c).sum()); ret.append(e); gross.append(float((sh * c).sum()) / e); nheld.append(int((sh > 1e-9).sum()))
        if prev is None and st is None: pass
    eq = np.array(ret)
    r = np.empty(len(eq)); r[0] = eq[0] - 1; r[1:] = eq[1:] / eq[:-1] - 1
    return r, np.array(gross), np.array(nheld), blocked, (sh / eq[-1], cash / eq[-1])

st = None; R = []; G = []; NH = []; B = 0
for w, g in zip(wins, chosen):
    r, g_, nh, b, st = sim_funded(Wc[g], w["oos_s"], w["oos_e"], 0.05, st)
    R.append(r); G.append(g_); NH.append(nh); B += b
R = np.concatenate(R); G = np.concatenate(G); NH = np.concatenate(NH)
print("S1 FINANCE (cash>=0) : Sharpe %.4f MaxDD %.4f expo moy %.3f max %.3f nTitres moy %.1f max %d ; achats bloques/tronques par manque de cash : %d" %
      (I.sharpe(R), I.maxdd(R), G.mean(), G.max(), NH.mean(), NH.max(), B))
