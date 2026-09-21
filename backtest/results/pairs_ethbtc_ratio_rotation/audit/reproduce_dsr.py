"""AXE 1 -- Recalcul independant du DSR (Bailey & Lopez de Prado 2014) from scratch, sans
utiliser backtest/metrics.py, a partir des rendements OOS concatenes officiels stockes en
sous-produit (on reconstruit les rendements depuis... on ne les a pas directement en JSON,
donc on reimplemente la formule sur les memes moments statistiques que rapportes, PLUS une
reimplementation independante de la formule elle-meme pour verifier metrics.py).
"""
import json
import math
import numpy as np
from scipy import stats

official = json.load(open("/home/claude/audit-copy/backtest/results/pairs_ethbtc_ratio_rotation/results.json"))
dsr = official["dsr_candidate"]
n = dsr["n_obs"]
skew = dsr["skew"]
kurt_excess = dsr["kurtosis_excess"]
sr_hat = dsr["sharpe_hat_period"]
K = dsr["trials_k"]

EULER = 0.5772156649015329

def sharpe_std_error(n, skew, kurt_excess, sr_hat):
    kurt_pearson = kurt_excess + 3.0
    var = (1.0 - skew*sr_hat + (kurt_pearson - 1.0)/4.0*sr_hat**2) / (n - 1)
    return math.sqrt(max(var, 0.0))

def expected_max_sharpe(K, sr_std):
    if K <= 1:
        return 0.0
    z1 = stats.norm.ppf(1.0 - 1.0/K)
    z2 = stats.norm.ppf(1.0 - 1.0/(K*math.e))
    return sr_std * ((1.0 - EULER)*z1 + EULER*z2)

def psr(sr_hat, sr_bench, n, skew, kurt_excess):
    sr_std = sharpe_std_error(n, skew, kurt_excess, sr_hat)
    z = (sr_hat - sr_bench) / sr_std
    return stats.norm.cdf(z)

sr_std = sharpe_std_error(n, skew, kurt_excess, sr_hat)
sr0 = expected_max_sharpe(K, sr_std)
dsr_indep = psr(sr_hat, sr0, n, skew, kurt_excess)

print(f"n={n} skew={skew:.6f} kurt_excess={kurt_excess:.6f} sr_hat={sr_hat:.8f} K={K}")
print(f"sr_std={sr_std:.8f} sr0(expected max sharpe)={sr0:.8f}")
print(f"DSR independant = {dsr_indep:.8f}")
print(f"DSR officiel     = {dsr['dsr']:.8f}")
print(f"match: {abs(dsr_indep - dsr['dsr']) < 1e-9}")

# K_total formula check
k_detail = official["meta"]["k_total_detail"]
k_formula = k_detail["registry_rows"] + k_detail["n_windows"]*k_detail["n_grid_combos_candidate"] + k_detail["n_windows"]*k_detail["n_grid_combos_control"]
print(f"K_total formula check: {k_formula} vs reported {official['meta']['k_total']} match={k_formula==official['meta']['k_total']}")
