"""Point 3 : A3 par execution. Instrumentation de la mecanique de bande (entree vs sortie), cash negatif,
exposition brute, nombre de titres, orphelins (titres detenus dont la cible est 0)."""
import pickle, json
import numpy as np, pandas as pd
import indep as I

cal, O, C, raw, spy = I.load_all()
NR, V = I.panels_from_raw(raw, cal)
Oa, Ca = np.nan_to_num(O.to_numpy()), np.nan_to_num(C.to_numpy())
Oraw, Craw = O.to_numpy(), C.to_numpy()
wins = I.windows(cal)
GRID = [(L, N) for L in (63, 252) for N in (20, 30)]
Wc = {g: I.weights(cal, NR, V, *g) for g in GRID}
Wctl = I.weights(cal, NR, V, 63, None)
chosen = pickle.load(open("./a2.pkl", "rb"))["chosen"]
BAND = 0.05
k = Oa.shape[1]
rate = 5e-4

def run_instrumented(Wsel, chosen_list, scale=1.0, band=BAND):
    sh = np.zeros(k); cash = 1.0
    log = []        # un enregistrement par ordre
    daily = []      # (date_idx, gross, nheld, cash, orphan_w, nheld_target)
    for w, g in zip(wins, chosen_list):
        W = Wsel(g) * scale
        eq_last = None
        for i in range(w["oos_s"], w["oos_e"] + 1):
            o, c, wd = Oa[i], Ca[i], W[i - 1]
            Vv = sh * o
            eqb = cash + Vv.sum()
            safe = np.where(o == 0, np.nan, o)
            tgt = np.nan_to_num(wd * eqb / safe, nan=0.0)
            cw = Vv / eqb
            hold = np.abs(wd - cw) < band
            tgt = np.where(hold, sh, tgt)
            tr = tgt - sh
            for s in np.flatnonzero(np.abs(tr) > 1e-9):
                old, new = sh[s], tgt[s]
                kind = ("entry" if old <= 1e-9 else "exit_full" if new <= 1e-9 else "increase" if new > old else "trim")
                log.append(dict(i=i, w=w["k"], sym=s, kind=kind, tgt_w=wd[s], cur_w=cw[s], hold_would=bool(hold[s])))
            turn = float((np.abs(tr[np.abs(tr) > 1e-9]) * o[np.abs(tr) > 1e-9]).sum())
            cash = cash - float((tr * o).sum()) - turn * rate
            sh = tgt
            e = cash + float((sh * c).sum())
            daily.append((i, float((sh * c).sum()) / e, int((sh > 1e-9).sum()), cash / e,
                          float((sh * c)[wd == 0].sum()) / e, int((wd > 0).sum())))
            eq_last = e
        sh = sh / eq_last; cash = cash / eq_last
    return pd.DataFrame(log), pd.DataFrame(daily, columns=["i", "gross", "nheld", "cash", "orphan_w", "ntgt"])

log, daily = run_instrumented(lambda g: Wc[g], chosen)
print("== S1 (moteur, bande 0.05 plate, poids 1/N) ==")
print("ordres executes par type :\n", log.kind.value_counts().to_string())
ex = log[log.kind == "exit_full"]
print("sorties completes executees : poids courant au moment de la sortie  min/med/max = %.4f %.4f %.4f ; part avec poids >= 0.05 : %.3f" %
      (ex.cur_w.min(), ex.cur_w.median(), ex.cur_w.max(), (ex.cur_w >= 0.05 - 1e-12).mean()))
en = log[log.kind == "entry"]
print("entrees : cible = %.4f (min %.4f max %.4f)" % (en.tgt_w.median(), en.tgt_w.min(), en.tgt_w.max()))
print("entrees dont la cible vaut EXACTEMENT 0.05 (egalite stricte avec la bande) :", int((en.tgt_w == 0.05).sum()), "/", len(en))
print("expo brute moy/max : %.4f / %.4f ; part jours > 1.05 : %.3f" % (daily.gross.mean(), daily.gross.max(), (daily.gross > 1.05).mean()))
print("n titres moy/max : %.2f / %d (cible 20)" % (daily.nheld.mean(), daily.nheld.max()))
print("cash/equity moy/min : %.4f / %.4f ; part des jours cash<0 : %.4f" % (daily.cash.mean(), daily.cash.min(), (daily.cash < -1e-9).mean()))
print("poids total des ORPHELINS (titres detenus a cible 0) : moy %.3f max %.3f (part de l'equity)" % (daily.orphan_w.mean(), daily.orphan_w.max()))
# duree de vie des orphelins : combien de decisions de rebalance sans sortie ?
# probabilite qu'un titre sorti du top-20 soit vendu au rebalance suivant
print("\nlignes executees au 1er jour de bourse apres chaque decision mensuelle (360) :",
      log[log.i.isin([j + 1 for j in np.flatnonzero(I.decision_mask(cal))])].shape[0], "ordres sur", len(log), "au total")
print("ordres hors lendemain de decision (derive de poids / rattrapage) :",
      int((~log.i.isin([j + 1 for j in np.flatnonzero(I.decision_mask(cal))])).sum()))
log.to_csv("/home/claude/audit-copy/backtest/results/lowvol_sp100/audit/a3_orders_s1.csv", index=False)

# --- Quelle part du risque/rendement vient des orphelins ? PnL contributions
print("\n== Contribution des orphelins au rendement ==")
# rendement journalier du sous-portefeuille orphelin = somme(sh*(c_t - c_{t-1}))/E ; on le recalcule via un second passage
def contrib():
    sh = np.zeros(k); cash = 1.0; rows = []
    for w, g in zip(wins, chosen):
        W = Wc[g]
        eq_prev = None
        for i in range(w["oos_s"], w["oos_e"] + 1):
            o, c, wd = Oa[i], Ca[i], W[i - 1]
            Vv = sh * o; eqb = cash + Vv.sum()
            safe = np.where(o == 0, np.nan, o)
            tgt = np.nan_to_num(wd * eqb / safe, nan=0.0)
            hold = np.abs(wd - Vv / eqb) < BAND
            tgt = np.where(hold, sh, tgt)
            tr = tgt - sh; ch = np.abs(tr) > 1e-9
            cash = cash - float((tr * o).sum()) - float((np.abs(tr[ch]) * o[ch]).sum()) * rate
            sh = tgt
            e = cash + float((sh * c).sum())
            # P&L open->close par groupe (cible / orphelin) en fraction de equity d'ouverture
            pnl_t = float((sh * (c - o))[wd > 0].sum()); pnl_o = float((sh * (c - o))[wd == 0].sum())
            rows.append((i, pnl_t / eqb, pnl_o / eqb))
            eq_last = e
        sh = sh / eq_last; cash = cash / eq_last
    return pd.DataFrame(rows, columns=["i", "r_tgt", "r_orph"])
cb = contrib()
print("somme des rendements open->close : cibles %.3f ; orphelins %.3f (en fraction d'equity, additif)" % (cb.r_tgt.sum(), cb.r_orph.sum()))
print("Sharpe annualise de la jambe cibles seule (open->close, approx) : %.3f ; orphelins seuls : %.3f" %
      (cb.r_tgt.mean() / cb.r_tgt.std() * np.sqrt(252), cb.r_orph.mean() / cb.r_orph.std() * np.sqrt(252)))

# --- Controle apparie : 100 % cash ?
print("\n== Controle apparie ==")
rc = I.sim(Wctl, Oraw, Craw, wins[3]["oos_s"], wins[3]["oos_e"], 5.0, 0.05, record=True)
print("fenetre 3 : nb d'ordres du controle =", rc["orders"], " ; equity finale =", rc["eq_end"], " ; max poids cible =", Wctl[wins[3]['oos_s']:wins[3]['oos_e']].max())
tot = 0
state = None
for w in wins:
    r = I.sim(Wctl, Oraw, Craw, w["oos_s"], w["oos_e"], 5.0, 0.05, state=state, record=True)
    state = r["state"]; tot += r["orders"]
print("ordres du controle sur les 30 fenetres OOS :", tot, " ; poids cible max sur l'OOS (1/n_elig) = %.4f ; min = %.4f" %
      (max(Wctl[w['oos_s']-1:w['oos_e']].max() for w in wins), min(Wctl[w['oos_s']-1:w['oos_e']][Wctl[w['oos_s']-1:w['oos_e']] > 0].min() for w in wins)))
# nombre d'eligibles aux dates de decision OOS
ne = []
for i in np.flatnonzero(I.decision_mask(cal)):
    if i >= wins[0]["oos_s"]:
        ne.append(int((Wctl[i] > 0).sum()))
print("nb d'eligibles aux decisions OOS : min %d med %d max %d ; 1/n_eligibles max = %.4f (<0.05 => jamais d'entree)" % (min(ne), np.median(ne), max(ne), 1 / min(ne)))
