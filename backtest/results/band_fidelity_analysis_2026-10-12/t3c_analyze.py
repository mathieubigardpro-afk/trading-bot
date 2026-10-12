"""T3c -- analyse appariee S1 / S2 / S3 (+ S2b controle) a partir des runs MOTEUR caches (t3b) et
d'un REPLICA instrumente (common.replica_run), VALIDE bit-a-bit contre le moteur.

Semantiques (toutes sur la MEME matrice de poids decides W, pocket frame, cout 25 bps/cote) :
  S1  moteur actuel   : W, bande 0.05 (poids poche, base = equity de la poche)
  S2  prod. modelisee : a*W, bande 0.05*a (poids wallet, base = equity wallet = 1 + a(P-1), reste en cash),
                        flatten/CB NON simules ; metriques ramenees en unites de poche P = 1 + (E-1)/a
  S2b controle        : a*W, bande 0  (cadre wallet sans bande) -> isole l'effet du CADRE (base d'equity)
  S3  sans bande      : W, bande 0   (limite)
Decomposition : effet bande (cadre poche) = S1-S3 ; effet bande (cadre wallet) = S2-S2b ;
                effet de cadre (base d'equity) = S2b-S3 ; ecart total moteur/prod = S2-S1.

Divergences de decisions (deux mesures, definies dans le rapport) :
  D1 'meme etat' (independant du chemin) : sur la trajectoire S1, regle de bande S1 vs regle S2 evaluee
     sur le MEME etat de detention (via k(P,a)) ; symetrique D1' sur la trajectoire S2.
  D2 'chemins independants' : XOR des drapeaux 'ordre emis' entre les runs S1 et S2 (dependant du chemin).
"""
import json, pickle, sys, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import numpy as np, pandas as pd
import common as c

HERE = Path(__file__).parent
CACHE = HERE / "cache"
PROFILES = {"A": ("A_quasi_passif_equilibre", [0.10, 0.25, 0.30, 0.60]),
            "B": ("B_lowweight_volgate", [0.10, 0.25, 0.60]),
            "C": ("C_bigweight_sma200_equal", [0.25, 0.60])}
BETA = c.BAND
cal = pd.DatetimeIndex(pd.read_pickle(CACHE / "cal.pkl"))
O = pd.read_pickle(CACHE / "opens.pkl").to_numpy(); CL = pd.read_pickle(CACHE / "closes.pkl").to_numpy()
n = len(cal); START, END = 1, n - 1
years = (cal[END] - cal[START]).total_seconds() / (365.25 * 86400)

def load_eng(p, sem, a):
    f = CACHE / f"eng_{p}_{sem}_{a:.2f}.pkl"
    return pickle.load(open(f, "rb")) if f.exists() else None

def k_fn(P, a): return P / (1.0 + a * (P - 1.0))

# Benchmark B&H EW 6 majors (MaxDD de reference pour le ratio Porte 1 -- ici sur la fenetre complete)
bh = (CL / CL[0]).mean(axis=1)
def maxdd(e):
    e = np.asarray(e); pk = np.maximum.accumulate(e); return float(((pk - e) / pk).max())
BH_MAXDD = maxdd(bh)

def sharpe_of(eq_pocket, first_idx):
    e = np.concatenate([[1.0], eq_pocket]) if first_idx == 0 else eq_pocket[first_idx - 1:]
    r = pd.Series(e).pct_change().dropna()
    return float(c.bt_metrics.sharpe_ratio(r, periods_per_year=c.PPY)), r.to_numpy()

def summarize(eq_pocket, pnls, first_idx, turnover_pocket, cost_pocket, n_orders, n_order_bars, n_trades_closed):
    e = eq_pocket[first_idx - 1:] if first_idx > 0 else np.concatenate([[1.0], eq_pocket])
    sh, r = sharpe_of(eq_pocket, first_idx)
    act_years = len(r) / c.PPY
    meq = float(np.mean(e))
    return dict(
        sharpe=sh, max_drawdown=maxdd(e), cagr=float((e[-1] / e[0]) ** (1 / act_years) - 1) if e[-1] > 0 else -1.0,
        profit_factor=float(c.bt_metrics.profit_factor(pnls)) if pnls else float("nan"),
        total_return=float(e[-1] / e[0] - 1), n_trades_closed=int(n_trades_closed),
        annual_turnover_x_equity=float(turnover_pocket / act_years / meq),
        cost_drag_per_year=float(cost_pocket / act_years / meq), cost_total_pocket_units=float(cost_pocket),
        n_orders=int(n_orders), n_order_bars=int(n_order_bars),
        maxdd_over_bh_maxdd=float(maxdd(e) / BH_MAXDD),
    )

def block_boot_dsharpe(rA, rB, block=168, reps=400, seed=7):
    """IC bootstrap par blocs APPARIE du Sharpe(B)-Sharpe(A) sur series de rendements de meme longueur."""
    m = min(len(rA), len(rB)); rA, rB = rA[-m:], rB[-m:]
    rng = np.random.default_rng(seed); nb = m // block
    out = []
    for _ in range(reps):
        idx = np.concatenate([np.arange(s, s + block) for s in rng.integers(0, m - block, nb)])
        a_, b_ = rA[idx], rB[idx]
        sa = a_.mean() / a_.std(ddof=1) if a_.std() > 0 else 0; sb = b_.mean() / b_.std(ddof=1) if b_.std() > 0 else 0
        out.append((sb - sa) * math.sqrt(c.PPY))
    return [float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))]

CB = {  # seuils par profil de wallet (bot/config.py:WALLETS[*]['risque'])
    "equilibre/labo": dict(half=0.15, flat=0.25, freeze=0.03), "agressif": dict(half=0.20, flat=0.35, freeze=0.05),
    "prudent": dict(half=0.10, flat=0.15, freeze=0.02)}
def cb_profile_for(a): return CB["agressif"] if a >= 0.5 else CB["equilibre/labo"]
def cb_exposure(E, a):
    """Combien de fois les seuils CB du wallet seraient franchis par la SEULE contribution de la poche
    (reste en cash : c'est une BORNE BASSE du DD wallet reel). Non simule dans S2."""
    th = cb_profile_for(a); pk = np.maximum.accumulate(np.concatenate([[1.0], E]))[1:]
    dd = (pk - E) / pk
    loss24 = np.zeros(len(E)); loss24[24:] = (E[:-24] - E[24:]) / E[:-24]
    return dict(max_wallet_dd=float(dd.max()), half_size_thr=th["half"], flatten_thr=th["flat"],
                hours_dd_above_half=int((dd > th["half"]).sum()), hours_dd_above_flatten=int((dd > th["flat"]).sum()),
                flatten_would_trigger=bool((dd > th["flat"]).any()),
                freeze_thr=th["freeze"], hours_daily_loss_above_freeze=int((loss24 > th["freeze"]).sum()),
                n_freeze_episodes=int((np.diff((loss24 > th["freeze"]).astype(int)) == 1).sum()))

def hold_rule(w_hat, w_cur_eff): return np.abs(w_hat - w_cur_eff) < BETA

results = {"meta": dict(window=f"{cal[START]} -> {cal[END]}", n_bars=int(END - START + 1), years=years, cost_bps=c.COST_BPS,
                        band=BETA, bh_6majors_maxdd_full_window=BH_MAXDD,
                        validation="replica == moteur (equity max abs diff, couts) pour chaque config : voir 'replica_vs_engine'"),
           "profiles": {}}
replica_checks = {}

for p, (name, allocs) in PROFILES.items():
    W = pd.read_pickle(CACHE / f"W_{name}.pkl").to_numpy()
    Wsub = W[START - 1:END]
    nz_bars = np.flatnonzero(np.abs(Wsub).sum(axis=1) > 1e-12)
    first_idx = int(nz_bars[0]) if len(nz_bars) else 0   # index j de la 1re bougie avec poids decide non nul
    results["profiles"][p] = dict(name=name, first_active_bar=str(cal[START + first_idx]), semantics={})
    R1 = c.replica_run(O, CL, W, BETA, c.COST_BPS, START, END)
    R3 = c.replica_run(O, CL, W, 0.0, c.COST_BPS, START, END)
    e1 = load_eng(p, "S1", 1.0); e3 = load_eng(p, "S3", 1.0)
    if e1 is None or e3 is None:
        print("moteur S1/S3 manquant pour", p); continue
    for tag, R, e in (("S1", R1, e1), ("S3", R3, e3)):
        replica_checks[f"{p}/{tag}"] = dict(max_abs_equity_diff=float(np.max(np.abs(e["equity"] - R["equity"]))),
                                            cost_diff=float(abs(e["costs_spot"] - R["cost"].sum())))
        assert replica_checks[f"{p}/{tag}"]["max_abs_equity_diff"] < 1e-9
    S1 = summarize(e1["equity"], e1["pnls"], first_idx, R1["turnover"].sum(), R1["cost"].sum(),
                   R1["traded"].sum(), R1["traded"].any(axis=1).sum(), e1["n_trades_closed"])
    S3 = summarize(e3["equity"], e3["pnls"], first_idx, R3["turnover"].sum(), R3["cost"].sum(),
                   R3["traded"].sum(), R3["traded"].any(axis=1).sum(), e3["n_trades_closed"])
    results["profiles"][p]["S1"] = S1; results["profiles"][p]["S3"] = S3
    sh1, r1 = sharpe_of(e1["equity"], first_idx)
    for a in allocs:
        e2 = load_eng(p, "S2", a); e2b = load_eng(p, "S2b", a)
        if e2 is None or e2b is None:
            print("moteur S2/S2b manquant", p, a); continue
        R2 = c.replica_run(O, CL, W * a, BETA * a, c.COST_BPS, START, END)
        R2b = c.replica_run(O, CL, W * a, 0.0, c.COST_BPS, START, END)
        for tag, R, e in (("S2", R2, e2), ("S2b", R2b, e2b)):
            replica_checks[f"{p}/{tag}/a={a}"] = dict(max_abs_equity_diff=float(np.max(np.abs(e["equity"] - R["equity"]))),
                                                      cost_diff=float(abs(e["costs_spot"] - R["cost"].sum())))
            assert replica_checks[f"{p}/{tag}/a={a}"]["max_abs_equity_diff"] < 1e-9
        P2 = c.pocket_equity_from_wallet(e2["equity"], a); P2b = c.pocket_equity_from_wallet(e2b["equity"], a)
        S2 = summarize(P2, e2["pnls"], first_idx, R2["turnover"].sum() / a, R2["cost"].sum() / a,
                       R2["traded"].sum(), R2["traded"].any(axis=1).sum(), e2["n_trades_closed"])
        S2b = summarize(P2b, e2b["pnls"], first_idx, R2b["turnover"].sum() / a, R2b["cost"].sum() / a,
                        R2b["traded"].sum(), R2b["traded"].any(axis=1).sum(), e2b["n_trades_closed"])
        # ---- D1 : meme etat, trajectoire S1 -> regle S2 evaluee via k(P,a) ----------------------
        P1pre = R1["equity_pre"][:, None]; wp = R1["cur_w"]; what = R1["tgt_w"]
        active = (wp > 1e-12) | (what > 1e-12)
        h1 = hold_rule(what, wp); h2cf = hold_rule(what, wp * k_fn(P1pre, a))
        dis = active & (h1 != h2cf)
        s1_hold_s2_trade = active & h1 & ~h2cf; s1_trade_s2_hold = active & ~h1 & h2cf
        # symetrique : trajectoire S2 -> regle S1 evaluee sur le meme etat
        Ew = R2["equity_pre"][:, None]; wwal = R2["cur_w"]   # poids wallet
        Pp = 1.0 + (Ew - 1.0) / a; wp_from_w = wwal / a * (Ew / Pp)    # w_p = V/P = (x/a)/P ; x = wwal*E
        what2 = R2["tgt_w"] / a
        act2 = (wp_from_w > 1e-12) | (what2 > 1e-12)
        h2 = hold_rule(what2, wwal / a); h1cf = hold_rule(what2, wp_from_w)
        dis2 = act2 & (h1cf != h2)
        # ---- D2 : drapeaux d'ordre, chemins independants -------------------------------------
        xor = R1["traded"] ^ R2["traded"]
        # ---- bootstrap apparie Delta Sharpe S2 - S1 --------------------------------------------
        sh2, r2 = sharpe_of(P2, first_idx)
        ci = block_boot_dsharpe(r1, r2)
        Pcur = P2
        cb = cb_exposure(e2["equity"], a)
        fac = c.factorial(O, CL, W, a, first_idx, START, END)
        fac_m = {k_: v_["metrics"] for k_, v_ in fac.items()}
        # validation : cellule (E,wallet) == S2 ; (P,pocket) == S1
        assert abs(fac_m["size=E|bandarg=wallet"]["sharpe"] - S2["sharpe"]) < 1e-9
        assert abs(fac_m["size=P|bandarg=pocket"]["sharpe"] - S1["sharpe"]) < 1e-9
        F = fac_m
        factorial_decomp = dict(
            cells=F,
            effect_of_band_argument_at_pocket_sizing=dict(dSharpe=F["size=P|bandarg=wallet"]["sharpe"] - F["size=P|bandarg=pocket"]["sharpe"],
                                                         dCAGR=F["size=P|bandarg=wallet"]["cagr"] - F["size=P|bandarg=pocket"]["cagr"],
                                                         dMaxDD=F["size=P|bandarg=wallet"]["max_drawdown"] - F["size=P|bandarg=pocket"]["max_drawdown"],
                                                         d_orders=F["size=P|bandarg=wallet"]["n_orders"] - F["size=P|bandarg=pocket"]["n_orders"]),
            effect_of_band_argument_at_wallet_sizing=dict(dSharpe=F["size=E|bandarg=wallet"]["sharpe"] - F["size=E|bandarg=pocket"]["sharpe"],
                                                         dCAGR=F["size=E|bandarg=wallet"]["cagr"] - F["size=E|bandarg=pocket"]["cagr"]),
            effect_of_sizing_base_at_pocket_bandarg=dict(dSharpe=F["size=E|bandarg=pocket"]["sharpe"] - F["size=P|bandarg=pocket"]["sharpe"],
                                                        dCAGR=F["size=E|bandarg=pocket"]["cagr"] - F["size=P|bandarg=pocket"]["cagr"],
                                                        dMaxDD=F["size=E|bandarg=pocket"]["max_drawdown"] - F["size=P|bandarg=pocket"]["max_drawdown"]),
            effect_of_sizing_base_noband=dict(dSharpe=F["size=E|noband"]["sharpe"] - F["size=P|noband"]["sharpe"]),
            total_S2_minus_S1=dict(dSharpe=F["size=E|bandarg=wallet"]["sharpe"] - F["size=P|bandarg=pocket"]["sharpe"]),
            orders_xor_band_argument_only_at_pocket_sizing=int((fac["size=P|bandarg=wallet"]["traded"] ^ fac["size=P|bandarg=pocket"]["traded"]).sum()),
            orders_P_pocket=F["size=P|bandarg=pocket"]["n_orders"],
        )
        # episodes de desaccord (D1)
        d1_episodes = c.episodes(dis)
        entry = dict(
            S2=S2, S2b=S2b,
            delta_S2_minus_S1={k: S2[k] - S1[k] for k in ("sharpe", "max_drawdown", "cagr", "profit_factor", "annual_turnover_x_equity", "cost_drag_per_year", "n_orders", "n_trades_closed")},
            delta_Sharpe_S2_minus_S1_boot95=ci,
            decomposition_sharpe=dict(band_pocket_frame_S1_minus_S3=S1["sharpe"] - S3["sharpe"],
                                      band_wallet_frame_S2_minus_S2b=S2["sharpe"] - S2b["sharpe"],
                                      frame_S2b_minus_S3=S2b["sharpe"] - S3["sharpe"], total_S2_minus_S1=S2["sharpe"] - S1["sharpe"]),
            decomposition_cagr=dict(band_pocket_frame_S1_minus_S3=S1["cagr"] - S3["cagr"], band_wallet_frame_S2_minus_S2b=S2["cagr"] - S2b["cagr"],
                                    frame_S2b_minus_S3=S2b["cagr"] - S3["cagr"], total_S2_minus_S1=S2["cagr"] - S1["cagr"]),
            diagnostic_mixed_cells_NOT_production_semantics_do_not_attribute=factorial_decomp,
            D1_same_state=dict(
                n_disagree_episodes=d1_episodes, n_s1_orders_ref=int(R1["traded"].sum()),
                n_active_cells=int(active.sum()), n_disagree_cells=int(dis.sum()),
                share_disagree_of_active=float(dis.sum() / max(1, active.sum())),
                n_bars_with_disagreement=int(dis.any(axis=1).sum()),
                n_s1_hold_s2_trade=int(s1_hold_s2_trade.sum()), n_s1_trade_s2_hold=int(s1_trade_s2_hold.sum()),
                n_s1_orders=int(R1["traded"].sum()),
                disagree_per_s1_order=float(dis.sum() / max(1, R1["traded"].sum())),
                n_disagree_cells_symmetric_on_S2_path=int(dis2.sum()), n_active_cells_S2_path=int(act2.sum()),
                mean_abs_k_minus_1_when_invested=float(np.abs(k_fn(P1pre, a) - 1)[active.any(axis=1)].mean())),
            D2_independent_paths=dict(n_orders_S1=int(R1["traded"].sum()), n_orders_S2=int(R2["traded"].sum()),
                                      n_xor_cells=int(xor.sum()), xor_per_S1_order=float(xor.sum() / max(1, R1["traded"].sum())),
                                      n_bars_with_xor=int(xor.any(axis=1).sum()),
                                      n_bars_order_S1=int(R1["traded"].any(axis=1).sum()), n_bars_order_S2=int(R2["traded"].any(axis=1).sum()),
                                      unmatched_S1_orders_within_pm24h=c.unmatched_orders(R1["traded"], R2["traded"], 24)[0],
                                      unmatched_S2_orders_within_pm24h=c.unmatched_orders(R1["traded"], R2["traded"], 24)[1],
                                      unmatched_S1_orders_within_pm1h=c.unmatched_orders(R1["traded"], R2["traded"], 1)[0],
                                      unmatched_S2_orders_within_pm1h=c.unmatched_orders(R1["traded"], R2["traded"], 1)[1]),
            pocket_equity_range_S1=[float(e1["equity"].min()), float(e1["equity"].max())],
            pocket_equity_S2_range=[float(P2.min()), float(P2.max())], P_end_S1=float(e1["equity"][-1]),
            cb_exposure_pocket_only=cb,
            porte1_threshold_flips_sharpe0p70=bool((S1["sharpe"] >= 0.70) != (S2["sharpe"] >= 0.70)),
            porte1_threshold_flips_pf1p15=bool((S1["profit_factor"] > 1.15) != (S2["profit_factor"] > 1.15)),
            porte1_threshold_flips_ntrades80=bool((S1["n_trades_closed"] >= 80) != (S2["n_trades_closed"] >= 80)),
            porte1_threshold_flips_maxdd1p5xBH=bool((S1["maxdd_over_bh_maxdd"] <= 1.5) != (S2["maxdd_over_bh_maxdd"] <= 1.5)),
        )
        results["profiles"][p]["semantics"][f"a={a:.2f}"] = entry
        print(p, f"a={a}", "dSharpe(S2-S1)=%.3f [%.3f, %.3f]" % (S2["sharpe"] - S1["sharpe"], *ci),
              "| D1 disagree/active=%.4f, per S1 order=%.3f | orders S1=%d S2=%d" % (
                  entry["D1_same_state"]["share_disagree_of_active"], entry["D1_same_state"]["disagree_per_s1_order"],
                  R1["traded"].sum(), R2["traded"].sum()))
results["replica_vs_engine"] = replica_checks
json.dump(results, open(HERE / "results_t3_main.json", "w"), indent=1)
print("OK ->", HERE / "results_t3_main.json")
