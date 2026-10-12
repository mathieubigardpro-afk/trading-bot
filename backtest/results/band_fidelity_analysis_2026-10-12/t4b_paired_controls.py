"""T4b -- les verdicts historiques de pairs_ethbtc (rejet: 'dominee par le controle 50/50') et de
protective_vol_gate ('ecartee', 'dominee par le controle sans gate') reposent sur une comparaison APPARIEE
candidate-vs-controle. Test : ce verdict de domination (Sharpe ET Sortino ET MaxDD) change-t-il quand on passe
de S1 (moteur) a S2 (production modelisee, reste en cash) ? Structure walk-forward officielle (fenetres OOS
3 mois, capital remis a 1), replica valide, combos de la grille. Sans portage de position (limite assumee)."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import numpy as np, pandas as pd
import common as c
from backtest.strategies import pairs_ratio as pr

HERE = Path(__file__).parent; CACHE = HERE / "cache"
cal = pd.DatetimeIndex(pd.read_pickle(CACHE / "cal.pkl"))
OP = pd.read_pickle(CACHE / "opens.pkl"); CLP = pd.read_pickle(CACHE / "closes.pkl")
BETA = c.BAND
windows = c.engine.generate_walk_forward_windows(cal, 9, 3, 3)

def wf_returns(O, C, W, a, frame):
    rets = []
    for w in windows:
        if frame == "S1": R = c.replica_run(O, C, W, BETA, c.COST_BPS, w.oos_start_idx, w.oos_end_idx); eq = R["equity"]
        else:
            R = c.replica_run(O, C, W * a, BETA * a, c.COST_BPS, w.oos_start_idx, w.oos_end_idx); eq = c.pocket_equity_from_wallet(R["equity"], a)
        e = np.concatenate([[1.0], eq]); rets.append(np.diff(e) / e[:-1])
    return np.concatenate(rets)

def stats(r):
    s = pd.Series(r)
    e = np.concatenate([[1.0], np.cumprod(1 + r)]); pk = np.maximum.accumulate(e)
    return dict(sharpe=float(c.bt_metrics.sharpe_ratio(s, periods_per_year=c.PPY)), sortino=float(c.bt_metrics.sortino_ratio(s, periods_per_year=c.PPY)), maxdd=float(((pk - e) / pk).max()))

out = {}
def compare(name, W_cand, W_ctrl, cols, a):
    O = OP[cols].to_numpy(); C = CLP[cols].to_numpy(); res = {}
    for frame in ("S1", "S2"):
        rc = wf_returns(O, C, W_cand[cols].to_numpy(), a, frame); rk = wf_returns(O, C, W_ctrl[cols].to_numpy(), a, frame)
        sc, sk = stats(rc), stats(rk)
        dominated = bool(sc["sharpe"] < sk["sharpe"] and sc["sortino"] < sk["sortino"] and sc["maxdd"] > sk["maxdd"])
        res[frame] = dict(cand=sc, ctrl=sk, dSharpe_cand_minus_ctrl=sc["sharpe"] - sk["sharpe"], dSortino=sc["sortino"] - sk["sortino"], dMaxDD=sc["maxdd"] - sk["maxdd"], dominated_by_control=dominated)
    out[name] = res
    print(name, {f: (round(v["dSharpe_cand_minus_ctrl"], 3), round(v["dSortino"], 3), round(v["dMaxDD"], 3), v["dominated_by_control"]) for f, v in res.items()}, flush=True)

cl2 = CLP[["BTC", "ETH"]]
ctrl_pairs = c.overlay_prescale(pd.DataFrame({"BTC": 0.5, "ETH": 0.5}, index=cal), cl2)
for g in pr.PARAM_GRID:
    Wc = c.overlay_prescale(pr.generate_weight_decisions(cl2, pr.PairsRatioParams(**g)), cl2)
    compare(f"pairs/L{g['l_hours']}_th{g['theta']}(a=0.30)", Wc, ctrl_pairs, ["BTC", "ETH"], 0.30)
ctrl_pvg = c.overlay_prescale(pd.DataFrame(1.0 / 6, index=cal, columns=list(CLP.columns)), CLP)
for g in c.pvg.PARAM_GRID:
    Wc = c.overlay_prescale(c.pvg.generate_weight_decisions(CLP, c.pvg.ProtectiveVolGateParams(**g)), CLP)
    compare(f"pvg/V{g['vol_window_hours']}_pin{g['p_in']}(a=0.25)", Wc, ctrl_pvg, list(CLP.columns), 0.25)
json.dump(out, open(HERE / "results_t4b_paired_controls.json", "w"), indent=1)
