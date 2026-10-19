"""Point 1 : attaque par perturbation. On modifie des donnees STRICTEMENT posterieures a une date de
decision D et on exige l'egalite bit-a-bit des poids decides pour toutes les lignes <= D (candidat
ET reimplementation independante). Controle de puissance : perturber la donnee a D (ou D-1) DOIT
changer la ligne D."""
import sys
sys.path.insert(0, "/home/claude/audit-copy")
import numpy as np, pandas as pd
import indep as I
from backtest.strategies import lowvol
from backtest import data as bt_data

cal, O, C, raw, spy = I.load_all()
rng = np.random.default_rng(12345)
universe = lowvol.effective_universe()
raw_closes = {t: raw[t]["close"] for t in universe}

dm = lowvol.decision_day_mask(cal)
dec_idx = np.flatnonzero(dm)
# dates de decision tirees dans toute l'histoire (>= 1995 pour que des titres soient eligibles)
cands = [i for i in dec_idx if cal[i] >= pd.Timestamp("1995-01-01") and i < len(cal) - 30]
picks = sorted(rng.choice(cands, size=18, replace=False))

# poids de reference (candidat) pour 4 combos + controle ; reference independante
ref_panels = lowvol.compute_signal_panels(raw_closes, cal, universe)
ref = {}
for L in (63, 252):
    for N in (20, 30):
        ref[(L, N)] = lowvol.generate_weight_decisions(raw_closes, cal, lowvol.LowVolParams(L, N), universe, ref_panels).to_numpy()
ref_ctl = lowvol.generate_control_weights(raw_closes, cal, universe, ref_panels).to_numpy()
NR0, V0 = I.panels_from_raw(raw, cal)
ref_ind = {(L, N): I.weights(cal, NR0, V0, L, N) for L in (63, 252) for N in (20, 30)}

# les deux implementations sont-elles identiques sur toute la serie ?
for g in ref:
    assert np.array_equal(ref[g], ref_ind[g]), g
print("Poids candidat == poids reimplementation independante (4 combos, serie complete) : bit-a-bit OK")

def perturb(D_idx, mode):
    out = {}
    for t, s in raw_closes.items():
        s2 = s.copy()
        fut = s2.index > cal[D_idx]
        if mode == "noise":
            s2[fut] = s2[fut] * np.exp(rng.normal(0, 0.5, fut.sum()))
        elif mode == "x10":
            s2[fut] = s2[fut] * 10.0
        elif mode == "nan":
            s2[fut] = np.nan
        out[t] = s2
    return out

n_fail = 0; n_tests = 0
for D in picks:
    for mode in ("noise", "x10", "nan"):
        pc = perturb(D, mode)
        pan = lowvol.compute_signal_panels(pc, cal, universe)
        for L in (63, 252):
            for N in (20, 30):
                w = lowvol.generate_weight_decisions(pc, cal, lowvol.LowVolParams(L, N), universe, pan).to_numpy()
                ok = np.array_equal(w[: D + 1], ref[(L, N)][: D + 1])
                n_tests += 1
                if not ok:
                    n_fail += 1
                    print("LOOKAHEAD !!", cal[D].date(), mode, L, N)
        wc = lowvol.generate_control_weights(pc, cal, universe, pan).to_numpy()
        n_tests += 1
        if not np.array_equal(wc[: D + 1], ref_ctl[: D + 1]):
            n_fail += 1; print("LOOKAHEAD control", cal[D].date(), mode)
        # idem pour la reimplementation independante
        rw = {t: pd.DataFrame({"close": pc[t]}) for t in universe}
        NRp, Vp = I.panels_from_raw(rw, cal)
        for L, N in ((63, 20), (252, 20)):
            wi = I.weights(cal, NRp, Vp, L, N)
            n_tests += 1
            if not np.array_equal(wi[: D + 1], ref_ind[(L, N)][: D + 1]):
                n_fail += 1; print("LOOKAHEAD indep", cal[D].date(), mode, L, N)
print(f"{n_tests} comparaisons (donnees > D perturbees : bruit/x10/NaN), echecs = {n_fail}")

# controle de puissance : perturber la donnee a D (cloture de la date de decision) doit changer la ligne D
changed = 0; tot = 0
for D in picks:
    pc = {}
    for t, s in raw_closes.items():
        s2 = s.copy()
        m = s2.index == cal[D]
        s2[m] = s2[m] * np.exp(rng.normal(0, 0.3, m.sum()))
        pc[t] = s2
    pan = lowvol.compute_signal_panels(pc, cal, universe)
    w = lowvol.generate_weight_decisions(pc, cal, lowvol.LowVolParams(63, 20), universe, pan).to_numpy()
    tot += 1
    if not np.array_equal(w[D], ref[(63, 20)][D]):
        changed += 1
print(f"puissance : perturber la cloture de D change la ligne D dans {changed}/{tot} cas")

# perturber la cloture D+1 seulement : la ligne D ne doit pas changer, la ligne D+1.. peut
ok1 = 0
for D in picks:
    pc = {}
    for t, s in raw_closes.items():
        s2 = s.copy()
        m = s2.index == cal[D + 1]
        s2[m] = s2[m] * 3.0
        pc[t] = s2
    pan = lowvol.compute_signal_panels(pc, cal, universe)
    w = lowvol.generate_weight_decisions(pc, cal, lowvol.LowVolParams(63, 20), universe, pan).to_numpy()
    ok1 += int(np.array_equal(w[: D + 1], ref[(63, 20)][: D + 1]))
print(f"perturbation de la seule cloture D+1 : lignes <= D inchangees dans {ok1}/{len(picks)} cas")

# Test de causalite de l'execution : le moteur utilise weights_decided[t-1] a l'open t
import inspect
from backtest import engine
src = inspect.getsource(engine.simulate_segment)
print("moteur : raw_w = weights_decided.iloc[i - 1] present :", "weights_decided.iloc[i - 1]" in src)
