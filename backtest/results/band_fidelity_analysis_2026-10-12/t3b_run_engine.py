"""T3b -- lance le MOTEUR COMMUN (engine.simulate_segment, non modifie) sur toutes les configs.
Une config = (profil de poids, semantique, alloc). Resultats caches dans ANALYSIS-19/cache/eng_*.pkl
(equity de cloture, couts spot $, PnL realises par ligne, n trades clos). Reprise possible
(un job deja cache est saute). 2 processus (2 coeurs)."""
import sys, time, pickle
from pathlib import Path
from multiprocessing import Pool
sys.path.insert(0, str(Path(__file__).parent))
import numpy as np, pandas as pd
import common as c

CACHE = Path(__file__).parent / "cache"
PROFILES = {
    "A": ("A_quasi_passif_equilibre", [0.10, 0.25, 0.30, 0.60]),
    "B": ("B_lowweight_volgate", [0.10, 0.25, 0.60]),
    "C": ("C_bigweight_sma200_equal", [0.25, 0.60]),
}

def jobs():
    L = []
    for p, (_, allocs) in PROFILES.items():
        L.append((p, "S1", 1.0)); L.append((p, "S3", 1.0))
    for p, (_, allocs) in PROFILES.items():
        for a in allocs:
            L.append((p, "S2", a)); L.append((p, "S2b", a))
    return L

def key(job): return f"eng_{job[0]}_{job[1]}_{job[2]:.2f}.pkl"

def run(job):
    p, sem, a = job
    f = CACHE / key(job)
    if f.exists():
        return job, "cached"
    name = PROFILES[p][0]
    W = pd.read_pickle(CACHE / f"W_{name}.pkl")
    opens = pd.read_pickle(CACHE / "opens.pkl"); closes = pd.read_pickle(CACHE / "closes.pkl")
    cal = pd.DatetimeIndex(pd.read_pickle(CACHE / "cal.pkl"))
    if sem == "S1":   Wr, band = W, c.BAND
    elif sem == "S3": Wr, band = W, 0.0
    elif sem == "S2": Wr, band = W * a, c.BAND * a
    elif sem == "S2b":Wr, band = W * a, 0.0
    t = time.time()
    seg = c.engine_run(cal, Wr, opens, closes, band)
    out = dict(job=job, band=band, equity=seg.equity.to_numpy(), costs_spot=seg.pnl_breakdown["costs_spot"],
               pnls=[e["pnl"] for e in seg.realized_events], n_trades_closed=len(seg.trades_closed),
               gross_exposure=seg.gross_exposure.to_numpy(), secs=time.time() - t)
    pickle.dump(out, open(f, "wb"))
    return job, f"done {out['secs']:.0f}s"

if __name__ == "__main__":
    J = jobs()
    print(len(J), "jobs", flush=True)
    with Pool(2) as pool:
        for job, st in pool.imap_unordered(run, J):
            print(job, st, flush=True)
