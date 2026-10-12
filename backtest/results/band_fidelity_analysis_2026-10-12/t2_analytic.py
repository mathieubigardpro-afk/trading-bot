"""T2 -- derivation analytique de l'ecart (a) et verification NUMERIQUE de chaque formule.

Notation (unites de POCHE, base 1) : P = equity de poche, V = valeur de la position (en $ de poche),
w_p = V/P (poids poche courant), a = capital_alloc_pct, w_hat = poids cible decide (post sizing),
beta = 0.05.  Hypothese 'reste du wallet en cash' : E = 1 + a(P-1) (equity wallet, base 1).
Le wallet detient x = a*V $ ; poids wallet courant w_w = a V / E.

  Regle moteur (S1)  : hold  <=>  |w_hat - V/P| < beta
  Regle prod.  (S2)  : hold  <=>  |a w_hat - a V/E| < beta a  <=>  |w_hat - V/E| < beta
=> S2 revient a comparer la cible au poids poche 'deforme' w_eff = V/E = k(P,a) * w_p,
   k(P,a) = P / (1 + a (P-1)),  k-1 = (1-a)(P-1)/(1+a(P-1)).
   Decalage d'argument de la bande : delta = w_eff - w_p = w_p (1-a)(P-1)/(1+a(P-1)).
   Ensemble de divergence en d = w_hat - w_p : {|d|<beta} XOR {|d-delta|<beta}, mesure 2 min(|delta|, 2 beta).

Verifications executees ici :
  V1  la regle S2 ci-dessus == decision du VRAI bot.risk.manager.RiskManager.apply (N etats aleatoires)
  V2  relation affine E = 1 + a(P-1) exacte (moteur, scenario du test de garde) et k(P,a)
  V3  tables de |delta|/beta et du plafond |P-1| pour lequel |delta| < tolerance
  V4  cas limites : position nulle (entree) => aucune divergence ; sortie strategique 0 (poussiere)
"""
import json, sys
from pathlib import Path
from datetime import datetime, timezone
import os as _os; from pathlib import Path as _P
_R = str(_P(_os.environ.get("BAND19_REPO", _P(__file__).resolve().parents[3])))
sys.path.insert(0, _R)
sys.path.insert(0, str(Path(__file__).parent))
import numpy as np, pandas as pd
from bot import config as cfg
from bot.risk.manager import RiskManager
from bot.feeds.types import Quote

BETA = 0.05
out = {}

def k_fn(P, a): return P / (1.0 + a * (P - 1.0))

# ---------------- V1 : regle S2 vs VRAI RiskManager -------------------------------------------------
class Q:  # duck-typing : RiskManager ne lit que .mid
    def __init__(self, mid): self.mid = mid

def real_manager_hold(P, w_p, a, w_hat):
    """Etat du wallet (1000 $ de base) deduit de (P, w_p, a) sous 'reste en cash' ; appelle le vrai manager.
    Retourne (hold_reel, poids_wallet_courant)."""
    E = 1.0 + a * (P - 1.0)
    V = w_p * P                    # valeur de poche (pocket-$)
    x = a * V                      # $ wallet investis
    equity = 1000.0 * E
    price = 100.0
    qty = 1000.0 * x / price
    state = {"cash_usd": equity - qty * price, "positions": {"BTC": {"qty": qty, "prix_moyen": price}},
             "equity_peak_usd": equity}      # pic = equity => aucun CB
    rm = RiskManager(vol_target_annualized=50.0, vol_coldstart_scalar=1.0, cap_per_asset_crypto=1.0,
                     cap_per_asset_equity=1.0, gross_exposure_max=1.0, no_trade_band=BETA,
                     cb_dd_half_size_pct=0.9, cb_dd_flatten_pct=0.95, cb_daily_loss_freeze_pct=0.99,
                     cb_consecutive_losses_trigger=10**6, symbols_crypto=["BTC"], symbols_equity=[])
    cible, _ = rm.apply({"BTC": a * w_hat}, state, {"BTC": Q(price)}, {}, now=datetime(2026, 9, 1, tzinfo=timezone.utc),
                        no_trade_band_by_symbol={"BTC": a})
    w_w = x / E
    return abs(cible["BTC"] - w_w) < 1e-12, w_w

rng = np.random.default_rng(19)
N = 4000
mismatch = 0; s1_s2_disagree = 0; rows = []
for _ in range(N):
    a = rng.choice([0.1, 0.25, 0.3, 0.6])
    P = float(np.exp(rng.normal(0, 0.5)))                 # P log-normal autour de 1 (0.4 .. 2.7)
    w_p = float(rng.uniform(0.0, 0.35))
    d = float(rng.normal(0, 0.05))
    w_hat = max(0.0, w_p + d)
    hold_real, w_w = real_manager_hold(P, w_p, a, w_hat)
    hold_s2 = abs(w_hat - w_p * k_fn(P, a)) < BETA        # forme analytique
    hold_s1 = abs(w_hat - w_p) < BETA
    mismatch += int(hold_real != hold_s2)
    s1_s2_disagree += int(hold_s1 != hold_s2)
    # coherence du poids wallet courant
    assert abs(w_w - a * w_p * k_fn(P, a)) < 1e-12
out["V1_real_manager_vs_analytic_S2_rule"] = dict(n_states=N, mismatches=mismatch,
    comment="mismatch=0 => la regle analytique |w_hat - V/E| < beta reproduit EXACTEMENT RiskManager.apply (hors CB/caps/gel)",
    share_S1_S2_disagree_in_sample=s1_s2_disagree / N)

# ---------------- V2 : affine exact dans le moteur (scenario du test de garde) ------------------------
from backtest import engine
n = 4
cal = pd.bdate_range("2020-01-06", periods=n)
close = pd.Series([100.0, 100.0, 300.0, 300.0], index=cal)
opens = pd.DataFrame({"X": close.shift(1).bfill()}, index=cal); closes = pd.DataFrame({"X": close}, index=cal)
alloc = 0.2
pw = pd.DataFrame({"X": [0.5, 0.5, 0.75, 0.75]}, index=cal)
sp = engine.simulate_segment(cal, pw, opens, closes, 1, 3, 0.0, no_trade_band=BETA, apply_vol_targeting=False)
sw = engine.simulate_segment(cal, pw * alloc, opens, closes, 1, 3, 0.0, no_trade_band=BETA * alloc, apply_vol_targeting=False)
Pp = sp.equity.to_numpy(); Ew = sw.equity.to_numpy()
out["V2_affine_relation_engine"] = dict(max_abs_err_E_vs_1_plus_a_Pm1=float(np.max(np.abs(Ew - (1 + alloc * (Pp - 1))))),
    P_end=float(Pp[-1]), E_end=float(Ew[-1]), k_end=float(k_fn(Pp[-1], alloc)), homothety_value_a_times_P=float(alloc * Pp[-1]))

# ---------------- V3 : tables ---------------------------------------------------------------------------
Ps = [0.5, 0.7, 0.85, 0.95, 1.0, 1.05, 1.15, 1.3, 1.5, 2.0, 3.0]
As = [0.10, 0.25, 0.30, 0.60]
WPs = [0.03, 0.06, 0.10, 0.17, 0.25]
tab = []
for a in As:
    for P in Ps:
        k = k_fn(P, a)
        row = dict(a=a, P=P, k=k, k_minus_1=k - 1)
        for wp in WPs:
            row[f"delta_over_beta@wp={wp}"] = wp * (k - 1) / BETA
        tab.append(row)
out["V3_table_delta_over_beta"] = tab

# Plafond de |P-1| pour que |delta| <= tol (tol = fraction de la bande), dans le sens P>1 et P<1
def p_ceiling(a, wp, tol):
    # delta = wp (1-a)(P-1)/(1+a(P-1)), |delta| = tol
    up = tol / (wp * (1 - a) - a * tol)        # P-1 > 0 : (P-1) (wp(1-a) - a tol) = tol
    dn = None
    # P-1 = -u, u>0 : wp (1-a) u / (1 - a u) = tol -> u = tol / (wp (1-a) + a tol)
    dn = tol / (wp * (1 - a) + a * tol)
    return (up if wp * (1 - a) > a * tol else float("inf")), dn
ceil = []
for a in As:
    for wp in WPs:
        for tol_frac in (0.1, 0.25, 0.5, 1.0):
            up, dn = p_ceiling(a, wp, tol_frac * BETA)
            ceil.append(dict(a=a, w_p=wp, tol_over_beta=tol_frac, P_minus_1_up_ceiling=up, one_minus_P_down_ceiling=dn))
out["V3_ceiling_abs_P_minus_1"] = ceil

# borne simple en |P-1| : |delta| <= w_p (1-a) |P-1| pour P>=1 ; <= w_p (1-a)(1-P)/(1-a(1-P)) pour P<1
chk = 0
for a in As:
    for P in np.linspace(0.3, 3.0, 55):
        for wp in WPs:
            dlt = abs(wp * (k_fn(P, a) - 1))
            if P >= 1: bound = wp * (1 - a) * (P - 1)
            else: bound = wp * (1 - a) * (1 - P) / (1 - a * (1 - P))
            assert dlt <= bound + 1e-12                      # P>=1 : majorant ; P<1 : egalite exacte
            if P < 1: assert abs(dlt - bound) < 1e-12
            chk += 1
out["V3_bound_checks_passed"] = chk

# ---------------- V4 : cas limites ------------------------------------------------------------------
# (i) entree depuis flat : w_p = 0 => delta = 0 pour tout (P,a)  (regle identique)
assert all(abs(0 * (k_fn(P, a) - 1)) == 0 for a in As for P in Ps)
# (ii) sortie strategique w_hat=0 depuis une position 'poussiere' : S1 tient si w_p < beta ; S2 tient si k w_p < beta
dust = []
for a in (0.25, 0.30):
    for P in (0.7, 1.0, 1.5, 2.0):
        k = k_fn(P, a)
        dust.append(dict(a=a, P=P, k=k, S1_holds_dust_below_w_p=BETA, S2_holds_dust_below_w_p=BETA / k))
out["V4_zero_target_dust_thresholds"] = dust
out["V4_flat_entry_identical"] = True

# ---------------- V5 : bracket R (reste du wallet) --------------------------------------------------
# E = (1-a) R + a P ; k_R = P / E. R=1 (cash) -> S2 ; R=P (reste = meme perf que la poche) -> k=1 (homothetie, S1)
br = []
for a in (0.25, 0.30):
    for P in (0.7, 1.5, 2.0):
        for lam in (0.0, 0.5, 1.0, 1.5):
            R = 1 + lam * (P - 1)
            br.append(dict(a=a, P=P, lam=lam, R=R, k=P / ((1 - a) * R + a * P)))
out["V5_k_as_function_of_rest_of_wallet_perf"] = br

json.dump(out, open(Path(__file__).parent / "results_t2_analytic.json", "w"), indent=1)
print(json.dumps({k: v for k, v in out.items() if k.startswith("V1") or k.startswith("V2") or k.startswith("V3_bound")}, indent=1))
print("\nTable delta/beta (a=0.25):")
df = pd.DataFrame([r for r in tab if r["a"] == 0.25]).set_index("P")
print(df.round(3).to_string())
print("\nTable delta/beta (a=0.60):")
df = pd.DataFrame([r for r in tab if r["a"] == 0.60]).set_index("P")
print(df.round(3).to_string())
print("\nPlafond |P-1| (tol=25% bande):")
print(pd.DataFrame([c for c in ceil if c["tol_over_beta"] == 0.25]).round(3).to_string())
print(pd.DataFrame(out["V4_zero_target_dust_thresholds"]).round(4).to_string())
