"""EXPERIENCES LOCALES (hors protocole, NON decisionnelles) : que donnerait la strategie TELLE QU'ECRITE
dans la SPEC (bottom-N equipondere, rebalance mensuel REEL, sorties executees) -- sans bande ?
But : mesurer l'ampleur de l'artefact A3 et rendre evaluable la domination vs controle. Jamais un critere de sauvetage."""
import pickle, numpy as np, pandas as pd
import indep as I
cal, O, C, raw, spy = I.load_all()
NR, V = I.panels_from_raw(raw, cal)
Oa, Ca = O.to_numpy(), C.to_numpy()
wins = I.windows(cal)
dm = I.decision_mask(cal)
GRID = [(L, N) for L in (63, 252) for N in (20, 30)]
Wc = {g: I.weights(cal, NR, V, *g) for g in GRID}
Wctl = I.weights(cal, NR, V, 63, None)

def chain(Wsel, chosen, cost=5.0, mode='monthly', band=0.0):
    st = None; R = []; G = []; NH = []; CS = []; ev = []
    for w, g in zip(wins, chosen):
        r = I.sim(Wsel(g), Oa, Ca, w["oos_s"], w["oos_e"], cost, band, state=st, mode=mode, rebal=dm, record=True)
        st = r["state"]; R.append(r["ret"]); G.append(r["gross"]); NH.append(r["nheld"]); CS.append(r["cash"]); ev.append(r["events"])
    return np.concatenate(R), np.concatenate(G), np.concatenate(NH), np.concatenate(CS), ev

def sortino(r):
    d = np.sqrt(np.mean(np.where(r < 0, r ** 2, 0)));  return r.mean() / d * np.sqrt(252)

def select(mode, band):
    ch = []
    for w in wins:
        s0 = max(1, w["is_s"]); best = None; bs = -9
        for g in GRID:
            r = I.sim(Wc[g], Oa, Ca, s0, w["is_e"], 5.0, band, mode=mode, rebal=dm)
            sh = I.sharpe(r["ret"])
            if not np.isnan(sh) and sh > bs: bs, best = sh, g
        ch.append(best)
    return ch

# SPY benchmark identique au runner : B&H 3 bps, capital remis a 1 chaque fenetre
spyO = spy[["open"]].to_numpy(); spyC = spy[["close"]].to_numpy()
Wspy = np.ones((len(cal), 1))
Rspy = np.concatenate([I.sim(Wspy, spyO, spyC, w["oos_s"], w["oos_e"], 3.0, 0.05)["ret"] for w in wins])
print("SPY  : Sharpe %.3f Sortino %.3f MaxDD %.4f" % (I.sharpe(Rspy), sortino(Rspy), I.maxdd(Rspy)))

orig = pickle.load(open("./a2.pkl", "rb"))["chosen"]
out = {}
for label, ch in (("combos IS d'origine (L,N de la candidate)", orig), ("selection IS rejouee sous rebalance reel", None)):
    if ch is None:
        ch = select('monthly', 0.0)
    r, g, nh, cs, ev = chain(lambda gg: Wc[gg], ch)
    rc, gc, nhc, csc, evc = chain(lambda gg: Wctl, [None] * 30)
    pn = [e[2] for evw in ev for e in evw]
    ntr = sum(1 for evw in ev for e in evw if e[3])
    print(f"\n== {label} ==")
    print("combos:", sorted(set(ch)), "x", {c: ch.count(c) for c in set(ch)})
    print("CANDIDATE (rebalance mensuel reel) : Sharpe %.3f Sortino %.3f MaxDD %.4f  CAGR %.4f  PF %.2f  trades clos %d  expo moy %.3f max %.3f  nTitres moy %.1f max %d  cash min %.4f" %
          (I.sharpe(r), sortino(r), I.maxdd(r), (np.prod(1+r))**(252/len(r))-1, I.profit_factor(pn), ntr, g.mean(), g.max(), nh.mean(), nh.max(), cs.min()))
    print("CONTROLE  (EW tous eligibles, meme rebalance) : Sharpe %.3f Sortino %.3f MaxDD %.4f  CAGR %.4f  expo moy %.3f nTitres moy %.1f" %
          (I.sharpe(rc), sortino(rc), I.maxdd(rc), (np.prod(1+rc))**(252/len(rc))-1, gc.mean(), nhc.mean()))
    print("candidate domine par le controle (Sharpe ET Sortino ET MaxDD tous pires) ? ->",
          bool(I.sharpe(r) < I.sharpe(rc) and sortino(r) < sortino(rc) and I.maxdd(r) > I.maxdd(rc)))
    print("MaxDD ratio vs SPY : %.3f ; seuil Sharpe 0.70: %s ; PF>1.15: %s ; trades>=80: %s" %
          (I.maxdd(r) / I.maxdd(Rspy), I.sharpe(r) >= .7, I.profit_factor(pn) > 1.15, ntr >= 80))
    dd = I.dsr(r, 166); print("DSR(K=166) : %.4f  (SR quotidien %.5f, SR0 %.5f)" % (dd["dsr"], dd["sr"], dd["sr0"]))
    out[label] = dict(r=r)
    # sous-periodes
    idx = pd.DatetimeIndex(np.concatenate([cal[w["oos_s"]:w["oos_e"]+1] for w in wins]))
    for nm, m in (("<2010", idx < "2010-01-01"), (">=2010", idx >= "2010-01-01"), (">=2020", idx >= "2020-01-01")):
        print("   sous-periode %-7s Sharpe cand %.3f ctl %.3f spy %.3f" % (nm, I.sharpe(r[m]), I.sharpe(rc[m]), I.sharpe(Rspy[m])))
    print("   corr(cand,ctl)=%.3f corr(cand,spy)=%.3f ; IR cand vs ctl = %.3f" % (np.corrcoef(r, rc)[0,1], np.corrcoef(r, Rspy)[0,1], (r-rc).mean()/(r-rc).std(ddof=1)*np.sqrt(252)))
