"""Point 3 (fidelite) : le VRAI RiskManager (bot/risk/manager.py) et le VRAI Ledger (bot/sim/ledger.py).
Aucune modification du code de production : lecture/execution seule."""
import sys
sys.path.insert(0, "/home/claude/audit-copy")
from collections import namedtuple
from bot.risk.manager import RiskManager
from bot.sim.ledger import Ledger
from bot.sim.fills import Fill
import inspect

Q = namedtuple("Q", "mid bid ask ts source delayed synthetic_spread")
def q(p): return Q(p, p * .999, p * 1.001, "2026-01-01T00:00:00+00:00", "t", False, False)

a = 0.30
rm = RiskManager(vol_target_annualized=50.0, vol_coldstart_scalar=1.0, cap_per_asset_equity=1.0, gross_exposure_max=1.0,
                 no_trade_band=0.05, cb_dd_half_size_pct=0.15, cb_dd_flatten_pct=0.25, cb_daily_loss_freeze_pct=0.03)
syms = [f"S{i:02d}" for i in range(40)]
prices = {s: q(100.0) for s in syms}
equity = 10000.0
# etat : 25 titres detenus ; S00..S19 = cibles (poids 0.05*a = 0.015) ; S20..S24 = ORPHELINS (cible 0)
pos = {}
w_hold = {**{s: 0.015 for s in syms[:20]}, "S20": 0.0145, "S21": 0.0120, "S22": 0.0149, "S23": 0.0151, "S24": 0.0300}
cash = equity - sum(w_hold.values()) * equity
for s, w in w_hold.items():
    pos[s] = {"qty": w * equity / 100.0, "prix_moyen": 100.0}
state = {"cash_usd": cash, "positions": pos}
cibles = {s: 0.05 * a for s in syms[:20]}
cibles.update({s: 0.0 for s in syms[20:]})  # sorties explicites (cas le plus favorable a la sortie)
scale = {s: a for s in syms}
fin, reasons = rm.apply(cibles, state, prices, {}, no_trade_band_by_symbol=scale)
print("Production (RiskManager reel), bande = 0.05*a = %.4f" % (0.05 * a))
for s in ("S20", "S21", "S22", "S23", "S24"):
    print(f"  orphelin {s}: poids actuel {w_hold[s]:.4f}, cible 0 -> cible finale {fin[s]:.4f}  ({'VENDU' if fin[s] < w_hold[s] - 1e-12 else 'CONSERVE (jamais vendu sous la bande)'})")
# entree depuis plat a exactement 0.05*a
state2 = {"cash_usd": equity, "positions": {}}
fin2, _ = rm.apply({"S00": 0.05 * a, "S01": 1/30 * a}, state2, prices, {}, no_trade_band_by_symbol=scale)
print("Entree depuis plat : cible 0.05*a ->", fin2["S00"], "(execute)" if fin2["S00"] > 0 else "(BLOQUE)",
      "; cible (1/30)*a ->", fin2["S01"], "(execute)" if fin2["S01"] > 0 else "(BLOQUE : N=30 inentrable en production aussi)")
fin3, _ = rm.apply({"S00": 0.01 * a}, state2, prices, {}, no_trade_band_by_symbol=scale)
print("Controle equipondere (1/100)*a ->", fin3["S00"], "(BLOQUE : le controle ne peut pas non plus etre incube)" if fin3["S00"] == 0 else "(execute)")
# achat sans cash au niveau du Ledger de production
led = Ledger(cash_usd=100.0)
try:
    led.apply_fill(Fill(run_id="r", ts="t", symbol="S00", strategy="x", side="BUY", qty=2.0, notional_usd=200.0, price_fill=100.0,
                        price_mid_ideal=100.0, fees_usd=0.0, slippage_usd=0.0, quote_source="t", quote_ts="t", quote_delayed=False, quote_synthetic_spread=False))
    print("Ledger : achat sans cash ACCEPTE (cash=%.2f)" % led.cash_usd)
except Exception as e:
    print("Ledger : achat sans cash REJETE ->", type(e).__name__, str(e)[:90])
