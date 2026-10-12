"""T1 -- preuves PAR EXECUTION du comportement reel de bot/risk/manager.py (RiskManager.apply,
code de production, non modifie) et du moteur, pour l'ecart (b) 'flatten sous la bande'.

Cas testes avec le VRAI RiskManager configure comme bot/runner.py:_risk_manager_for_wallet
(vol_target 50.0, coldstart_scalar 1.0, gross_exposure_max 1.0, cap equity 1.0), wallet 'equilibre'
(alloc crypto 0.30 -> bande par symbole 0.05*0.30 = 0.015) :
  C1  cible STRATEGIQUE 0 + position sous la bande, aucun CB      -> la production RETIENT (hold)
  C2  cible strategique 0 + position au-dessus de la bande         -> la production vend (cible 0)
  C3  meme position sous la bande + flatten CB (drawdown > seuil)  -> la production FLATTEN (cible 0)
  C4  idem C3 mais seuil flatten non franchi (controle)            -> hold
Cote moteur : cible 0 sous la bande -> position conservee (derive) ; et recherche de toute notion
de flatten/circuit breaker dans backtest/engine.py + risk_overlay.py.
"""
import json, re, sys
from pathlib import Path
from datetime import datetime, timezone
import os as _os; from pathlib import Path as _P
_R = str(_P(_os.environ.get("BAND19_REPO", _P(__file__).resolve().parents[3])))
sys.path.insert(0, _R)
import numpy as np, pandas as pd
from bot import config as cfg
from bot.risk.manager import RiskManager
from bot.feeds.types import Quote

wallet = [w for w in cfg.WALLETS if w["id"] == "equilibre"][0]
r = wallet["risque"]
alloc = 0.30
mk = lambda **kw: RiskManager(
    vol_target_annualized=50.0, vol_ewma_halflife_hours=r["vol_ewma_halflife_hours"],
    vol_coldstart_min_points=r["vol_coldstart_min_points"], vol_coldstart_scalar=1.0,
    cap_per_asset_crypto=r["cap_per_asset"], cap_per_asset_equity=1.0, gross_exposure_max=1.0,
    no_trade_band=r["no_trade_band"], cb_daily_loss_freeze_pct=r["cb_daily_loss_freeze_pct"],
    cb_daily_loss_freeze_hours=r["cb_daily_loss_freeze_hours"],
    cb_consecutive_losses_trigger=r["cb_consecutive_losses_trigger"], cb_cooldown_hours=r["cb_cooldown_hours"],
    cb_dd_half_size_pct=r["cb_dd_half_size_pct"], cb_dd_flatten_pct=r["cb_dd_flatten_pct"],
    symbols_crypto=wallet["univers_crypto"], symbols_equity=[])

def quote(p):
    q = Quote.__new__(Quote)
    # on ne depend que de .mid dans RiskManager (getattr(quote,'mid'))
    object.__setattr__(q, "mid", p) if hasattr(q, "__dataclass_fields__") and getattr(Quote, "__dataclass_params__", None) and Quote.__dataclass_params__.frozen else setattr(q, "mid", p)
    return q

def run(pos_weight_wallet, target_raw_pocket, peak_equity=None, label=""):
    equity = 1000.0
    price = 50000.0
    qty = pos_weight_wallet * equity / price
    state = {"cash_usd": equity - qty * price, "positions": {"BTC": {"qty": qty, "prix_moyen": price}}}
    if peak_equity:
        state["equity_peak_usd"] = peak_equity
    cibles_brutes = {"BTC": target_raw_pocket * alloc}      # comme _combine_pockets : w_poche * alloc
    rm = mk()
    out, reasons = rm.apply(cibles_brutes, state, {"BTC": quote(price)}, {}, now=datetime(2026, 9, 1, tzinfo=timezone.utc),
                            no_trade_band_by_symbol={s: alloc for s in wallet["univers_crypto"]})
    flat = state["circuit_breakers"]["flatten_mode"]
    return dict(label=label, pos_weight_wallet=pos_weight_wallet, strategic_target_wallet=cibles_brutes["BTC"],
                band_effective=0.05 * alloc, flatten_mode=flat, cible_finale=out["BTC"],
                executes_order=abs(out["BTC"] - pos_weight_wallet) > 1e-9, reason=reasons["BTC"][-90:])

res = {}
res["C1_strategic_zero_under_band_no_cb"] = run(0.010, 0.0, label="cible strategique 0, position 1.0% wallet (3.3% poche) < bande 1.5%")
res["C2_strategic_zero_above_band_no_cb"] = run(0.030, 0.0, label="cible strategique 0, position 3.0% wallet >= bande 1.5%")
# drawdown 30% depuis le pic (> cb_dd_flatten_pct=0.25 du wallet equilibre) -> flatten_mode
res["C3_same_position_under_band_with_cb_flatten"] = run(0.010, 0.0, peak_equity=1000.0 / (1 - 0.30), label="idem C1 + drawdown wallet 30% > 25% => flatten CB")
res["C3b_flatten_with_nonzero_strategy_target"] = run(0.010, 0.05, peak_equity=1000.0 / (1 - 0.30), label="cible strategique NON nulle 5% poche + flatten CB")
res["C4_control_drawdown_below_flatten"] = run(0.010, 0.0, peak_equity=1000.0 / (1 - 0.10), label="idem C1 + drawdown 10% (< flatten 25%)")

# C5 : entree depuis le flat d'une candidate a faible poids nominal (poids poche 0.04 < 0.05) avec alloc 0.25 :
# la bande de production (0.05*0.25=0.0125) vs cible wallet 0.04*0.25=0.01 -> RETENU aussi en production
# (contredit l'idee 'le moteur est plus severe que la production pour les faibles poids nominaux' pour l'ENTREE).
alloc_save = alloc
alloc = 0.25
res["C5_entry_from_flat_low_nominal_weight_a0.25"] = run(0.0, 0.04, label="entree depuis flat, poids poche 0.04 (<0.05), alloc 0.25 : cible wallet 0.01 < bande 0.0125")
res["C5b_entry_from_flat_weight_0.06"] = run(0.0, 0.06, label="entree depuis flat, poids poche 0.06 (>0.05), alloc 0.25 : cible wallet 0.015 >= bande 0.0125")
alloc = alloc_save

# ---- cote moteur ---------------------------------------------------------------------------
from backtest import engine
n = 6
cal = pd.bdate_range("2020-01-06", periods=n)
price = pd.DataFrame({"X": 100.0}, index=cal)
w = pd.DataFrame({"X": [0.04, 0.04, 0.0, 0.0, 0.0, 0.0]}, index=cal)   # entree 4% (<5%) puis cible strategique 0
seg = engine.simulate_segment(cal, w, price, price, 1, n - 1, 0.0, no_trade_band=0.05, apply_vol_targeting=False)
seg0 = engine.simulate_segment(cal, w, price, price, 1, n - 1, 0.0, no_trade_band=0.0, apply_vol_targeting=False)
# bande 0.05 : a la bougie 1 l'ecart |0.04-0| < 0.05 => la position n'est JAMAIS ouverte (hold sur l'entree aussi)
w2 = pd.DataFrame({"X": [0.30, 0.30, 0.04, 0.04, 0.04, 0.04]}, index=cal)   # entree 30% puis cible 4% (ecart 26% > bande) puis ...
w3 = pd.DataFrame({"X": [0.30, 0.30, 0.0, 0.0, 0.0, 0.0]}, index=cal)       # sortie strategique complete depuis 30%
w4 = pd.DataFrame({"X": [0.30, 0.30, 0.03, 0.0, 0.0, 0.0]}, index=cal)      # descend a 3% (ecart 27% -> vend), puis cible 0 depuis 3% (<5%)
seg4 = engine.simulate_segment(cal, w4, price, price, 1, n - 1, 0.0, no_trade_band=0.05, apply_vol_targeting=False)
res["engine"] = dict(
    exposure_with_band_0p05_w4=[round(float(x), 6) for x in seg4.gross_exposure],
    comment_w4="poids decides 0.30,0.30,0.03,0.0,... : sortie 0.30->0.03 executee (ecart 0.27>bande) puis la 'sortie' 0.03->0 est RETENUE par la bande (exposition residuelle 3% conservee ad vitam)",
    exposure_without_band_w4=[round(float(x), 6) for x in engine.simulate_segment(cal, w4, price, price, 1, n - 1, 0.0, no_trade_band=0.0, apply_vol_targeting=False).gross_exposure],
)
src = (Path(_R + "/backtest/engine.py").read_text() + Path(_R + "/backtest/risk_overlay.py").read_text())
res["engine"]["mentions_of_flatten_in_engine_and_overlay_code"] = [
    (i + 1, l.strip()[:110]) for i, l in enumerate(Path(_R + "/backtest/engine.py").read_text().splitlines()) if re.search(r"flatten", l, re.I)]
res["engine"]["mentions_of_flatten_in_risk_overlay"] = [
    (i + 1, l.strip()[:110]) for i, l in enumerate(Path(_R + "/backtest/risk_overlay.py").read_text().splitlines()) if re.search(r"flatten", l, re.I)]
json.dump(res, open(Path(__file__).parent / "results_t1_code_proofs.json", "w"), indent=1, ensure_ascii=False)
print(json.dumps(res, indent=1, ensure_ascii=False))
