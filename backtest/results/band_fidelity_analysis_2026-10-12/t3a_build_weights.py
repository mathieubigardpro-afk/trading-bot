"""T3a -- construit et met en cache les 3 matrices de poids DECIDES (pocket frame, base 1)
et imprime leurs statistiques de 'taille de poids' (pertinentes pour la bande 0.05)."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import numpy as np, pandas as pd
import common as c

OUT = Path(__file__).parent / "cache"
raw, cal, opens, closes = c.load_market()
print("calendrier", cal[0], "->", cal[-1], len(cal), "heures")

# (A) quasi_passif_crypto equilibre, 6 majors, poids reels de la strategie (sizing interne)
WA = c.weights_quasi_passif_equilibre(raw, cal)

# (B) candidate synthetique faible poids : vol-gate (72h, 0.98) * overlay vol-targeting 0.275 pre-applique
WB, WB_raw, WB_scal = c.weights_lowweight_volgate(opens, closes)

# (C) candidate synthetique 'gros poids' : filtre SMA200 jour (helpers de PRODUCTION) puis 1/#on par actif,
#     sans vol targeting (gross_max=1, cap=1, vol_target enorme) => poids par actif = 1/#actifs 'on'
prof = dict([w for w in c.bot_cfg.WALLETS if w["id"] == "equilibre"][0]["risque"])
prof.update(vol_target_annualized=1e6, gross_exposure_max=1.0, cap_per_asset=1.0)
WC = c.qp.generate_weight_decisions(raw, cal, c.UNIVERSE_6, prof)

for name, W in (("A_quasi_passif_equilibre", WA), ("B_lowweight_volgate", WB), ("C_bigweight_sma200_equal", WC)):
    W.to_pickle(OUT / f"W_{name}.pkl")
opens.to_pickle(OUT / "opens.pkl"); closes.to_pickle(OUT / "closes.pkl")
pd.Series(cal).to_pickle(OUT / "cal.pkl")

def stats(W):
    v = W.to_numpy()
    nz = v[v > 1e-12]
    # poids cible par (bougie, actif) quand non nul ; et 'poids du plus gros actif' par bougie investie
    rowmax = v.max(axis=1)
    inv = rowmax > 1e-12
    return dict(
        frac_bars_invested=float(inv.mean()),
        median_nonzero_weight=float(np.median(nz)),
        p10_nonzero=float(np.percentile(nz, 10)), p90_nonzero=float(np.percentile(nz, 90)),
        frac_nonzero_weights_below_band=float((nz < c.BAND).mean()),
        frac_nonzero_weights_below_2band=float((nz < 2 * c.BAND).mean()),
        median_rowmax=float(np.median(rowmax[inv])),
        median_gross=float(np.median(v.sum(axis=1)[inv])),
    )
out = {n: stats(W) for n, W in (("A_quasi_passif_equilibre", WA), ("B_lowweight_volgate", WB), ("C_bigweight_sma200_equal", WC))}
out["B_vol_scalar_median_on_invested_bars"] = float(np.median(WB_scal[WB_raw.to_numpy().sum(axis=1) > 1e-12]))
out["B_raw_weight_per_symbol"] = 1 / 6
print(json.dumps(out, indent=1))
json.dump(out, open(Path(__file__).parent / "results_weights_stats.json", "w"), indent=1)
