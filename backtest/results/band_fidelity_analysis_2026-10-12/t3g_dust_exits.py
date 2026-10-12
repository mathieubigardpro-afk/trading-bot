"""T3g -- ecart (b) reformule : combien de SORTIES STRATEGIQUES (cible 0) sont avalees par la bande, a l'identique
dans le moteur (S1) et dans la production modelisee (S2) ? Mesure : cellules (bougie, symbole) ou la cible decidee est
exactement 0 alors qu'une position > 0 est detenue (position 'poussiere' conservee par la bande), part des
heures-expositions concernees, poids detenu moyen (en unites de poche)."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import numpy as np, pandas as pd
import common as c
HERE = Path(__file__).parent; CACHE = HERE / "cache"
O = pd.read_pickle(CACHE / "opens.pkl").to_numpy(); C = pd.read_pickle(CACHE / "closes.pkl").to_numpy(); n = len(O)
out = {}
for p, name, a in (("A", "A_quasi_passif_equilibre", 0.30), ("B", "B_lowweight_volgate", 0.25), ("C", "C_bigweight_sma200_equal", 0.30)):
    W = pd.read_pickle(CACHE / f"W_{name}.pkl").to_numpy()
    R1 = c.replica_run(O, C, W, c.BAND, c.COST_BPS, 1, n - 1)
    R2 = c.replica_run(O, C, W * a, c.BAND * a, c.COST_BPS, 1, n - 1)
    res = {}
    for tag, R, scale in (("S1", R1, 1.0), ("S2", R2, a)):
        tgt0 = R["tgt_w"] < 1e-12; held = R["cur_w"] > 1e-9
        dust = tgt0 & held & ~R["traded"]
        # nombre d'EPISODES de poussiere (runs) et duree mediane
        prev = np.vstack([np.zeros((1, dust.shape[1]), bool), dust[:-1]]); starts = dust & ~prev
        # durees
        durs = []
        for j in range(dust.shape[1]):
            run = 0
            for v in dust[:, j]:
                if v: run += 1
                elif run: durs.append(run); run = 0
            if run: durs.append(run)
        gross_hours = float(R["cur_w"].sum())
        res[tag] = dict(dust_cells=int(dust.sum()), dust_episodes=int(starts.sum()), median_episode_hours=float(np.median(durs)) if durs else 0.0,
                        max_episode_hours=int(max(durs)) if durs else 0,
                        mean_dust_weight_pocket_units=float((R["cur_w"][dust] / (1.0 if tag == "S1" else a)).mean()) if dust.any() else 0.0,
                        share_of_gross_exposure_hours=float(R["cur_w"][dust].sum() / gross_hours) if gross_hours > 0 else 0.0,
                        n_zero_target_exits_executed=int((tgt0 & R["traded"] & (R["cur_w"] > 1e-9)).sum()))
    out[p] = res
    print(p, json.dumps(res))
json.dump(out, open(HERE / "results_t3_dust_exits.json", "w"), indent=1)
