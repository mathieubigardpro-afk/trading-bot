"""Point 2 : reproduction independante du walk-forward (sans engine/lowvol/metrics du depot)."""
import json, pickle, time
import numpy as np, pandas as pd
import indep as I

t0 = time.time()
cal, O, C, raw, spy = I.load_all()
NR, V = I.panels_from_raw(raw, cal)
Oa, Ca = O.to_numpy(), C.to_numpy()
GRID = [(L, N) for L in (63, 252) for N in (20, 30)]
Wc = {(L, N): I.weights(cal, NR, V, L, N) for (L, N) in GRID}
Wctl = I.weights(cal, NR, V, 63, None)
wins = I.windows(cal)
print("fenetres:", len(wins), cal[wins[0]['oos_s']].date(), cal[wins[-1]['oos_e']].date(), f"{time.time()-t0:.0f}s")

# selection IS (a plat, memes reglages)
chosen, is_tab = [], []
for w in wins:
    s0 = max(1, w["is_s"])
    shs = {}
    for g in GRID:
        r = I.sim(Wc[g], Oa, Ca, s0, w["is_e"], 5.0, 0.05)
        shs[g] = I.sharpe(r["ret"])
    best = None; bs = -1e9
    for g in GRID:
        if not np.isnan(shs[g]) and shs[g] > bs:
            bs, best = shs[g], g
    if best is None:
        best = GRID[0]
    chosen.append(best); is_tab.append(shs)

# chaine OOS avec portage
def chain(chosen, cost=5.0, band=0.05, scale=1.0, W_override=None, record=False):
    state = None; rets = []; gross = []; nheld = []; cashs = []; evs = []; eqs = []; orders = 0
    seg = []
    for w, g in zip(wins, chosen):
        W = (Wctl if g is None else Wc[g]) if W_override is None else W_override
        W = W * scale
        r = I.sim(W, Oa, Ca, w["oos_s"], w["oos_e"], cost, band, state=state, record=record)
        state = r["state"]
        rets.append(r["ret"]); gross.append(r["gross"]); nheld.append(r["nheld"]); cashs.append(r["cash"]); eqs.append(r["eq"])
        evs.append(r["events"]); orders += r["orders"]; seg.append(r)
    return dict(ret=np.concatenate(rets), gross=np.concatenate(gross), nheld=np.concatenate(nheld),
                cash=np.concatenate(cashs), events=evs, orders=orders, seg=seg, eqs=eqs)

res = chain(chosen, record=True)
sh = I.sharpe(res["ret"])
print("Sharpe OOS concatene (independant) :", sh, " (candidat: 0.8127136874)")
print("MaxDD :", I.maxdd(res["ret"]), " (0.51880)")
pnls = [e[2] for ev in res["events"] for e in ev]
ntr = sum(1 for ev in res["events"] for e in ev if e[3])
print("PF (evenements realises, hors portage inter-fenetres) :", I.profit_factor(pnls), "n_realises", len(pnls), "lignes closes (hors portage)", ntr)
print("Expo brute moyenne/max :", res["gross"].mean(), res["gross"].max())
print("n titres moyen/max :", res["nheld"].mean(), res["nheld"].max())
print("cash min/moyen :", res["cash"].min(), res["cash"].mean(), "part des jours cash<0:", (res["cash"] < -1e-9).mean())
print("choix IS :", [f"{g}" for g in chosen])

# comparaison avec les choix du candidat
r = json.load(open("/home/claude/audit-copy/backtest/results/lowvol_sp100/results.json"))
cand_choice = [(p["chosen_params"]["vol_window"], p["chosen_params"]["n_holdings"]) for p in r["candidate_lowvol"]["per_window"]]
print("meme sequence de combos choisies que le candidat :", cand_choice == chosen)
diffs = [(i, a, b) for i, (a, b) in enumerate(zip(cand_choice, chosen)) if a != b]
print("diffs:", diffs)
print("Sharpe OOS par fenetre (independant vs candidat):")
for k, (ret, p) in enumerate(zip([s["ret"] for s in res["seg"]], r["candidate_lowvol"]["per_window"])):
    print(k, round(I.sharpe(ret), 4), round(p["sharpe"], 4))

pickle.dump(dict(chosen=chosen, is_tab=is_tab, ret=res["ret"], gross=res["gross"], nheld=res["nheld"], cash=res["cash"]),
            open("./a2.pkl", "wb"))
print("total", time.time() - t0)
