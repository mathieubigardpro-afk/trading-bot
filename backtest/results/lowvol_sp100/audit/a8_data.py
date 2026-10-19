import numpy as np, pandas as pd, pickle
import indep as I
cal, O, C, raw, spy = I.load_all()
NR, V = I.panels_from_raw(raw, cal)
wins = I.windows(cal)
W = I.weights(cal, NR, V, 63, 20)
# vol des titres selectionnes, aux dates de decision OOS
vs = []; zr = []
for i in np.flatnonzero(I.decision_mask(cal)):
    if i < wins[0]["oos_s"]: continue
    sel = np.flatnonzero(W[i] > 0)
    vs += list(V[63].iloc[i].to_numpy()[sel])
vs = np.array(vs); print("vol 63j annualisee des titres selectionnes : min %.3f p5 %.3f med %.3f p95 %.3f max %.3f" % (vs.min(), np.percentile(vs, 5), np.median(vs), np.percentile(vs, 95), vs.max()))
# part de rendements nuls et sauts extremes dans les donnees brutes
zero = {t: float((raw[t]["close"].pct_change().abs() < 1e-12).mean()) for t in raw}
big = {t: float((raw[t]["close"].pct_change().abs() > 0.4).sum()) for t in raw}
print("titres avec >2%% de rendements exactement nuls : %s" % [(t, round(v, 3)) for t, v in zero.items() if v > .02])
print("titres avec |rendement quotidien| > 40%% : %s" % [(t, int(v)) for t, v in big.items() if v > 0])
# frequence de selection par titre (survie): combien de titres tardivement introduits sont selectionnes AVANT leur gloire?
import collections
cnt = collections.Counter()
for i in np.flatnonzero(I.decision_mask(cal)):
    if i < wins[0]["oos_s"]: continue
    for s in np.flatnonzero(W[i] > 0): cnt[I.UNIV[s]] += 1
print("20 titres les plus selectionnes (mois, sur 360) :", cnt.most_common(20))
# biais du survivant : controle EW vs SPY (CAGR) deja mesure : 17.1% vs 10.4%
