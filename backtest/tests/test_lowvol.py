"""backtest/tests/test_lowvol.py — preuves offline (données synthétiques, aucune donnée de marché)
du signal `backtest/strategies/lowvol.py` et de la plomberie `backtest/run_lowvol_sp100.py`
(`backtest/LOWVOL-SP100-SPEC.md`).

Couvert : causalité (perturber les données APRÈS t ne change pas le signal à t), éligibilité
≥ 252 jours (uniforme en L), bottom-N construit à la main, gel mensuel des poids cibles,
contrôle = équipondéré de TOUS les éligibles, repli dégénéré (< N éligibles), grille
pré-enregistrée, tie-break déterministe, dates de décision, turnover du signal ; plus la preuve
documentée (anomalie A3 de la SPEC) que la bande 0,05 du moteur bloque l'entrée de poids < 0,05,
et l'équivalence réplica numpy ↔ moteur utilisée par l'annexe band_fidelity.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backtest import engine
from backtest import run_lowvol_sp100 as run
from backtest.strategies import lowvol
from bot.strategies.xs_momentum_sp100 import UNIVERSE_SP100


# ------------------------------------------------------------------------------------------
# Univers synthétique : vol_i = SIGMA_i * choc commun => classement des vols = classement des SIGMA
# ------------------------------------------------------------------------------------------

N_STOCKS = 25
N_DAYS = 700


def _calendar(n=N_DAYS, start="2000-01-03"):
    return pd.bdate_range(start, periods=n)


def _synthetic_closes(sigmas, n=N_DAYS, seed=7, start_offsets=None):
    """Clôtures dont le rendement du titre i = sigma_i * z_t (z_t commun) : écart-type réalisé
    strictement proportionnel à sigma_i sur toute fenêtre => classement des vols sans ambiguïté."""
    cal = _calendar(n)
    rng = np.random.default_rng(seed)
    z = rng.standard_normal(n)
    closes = {}
    for i, s in enumerate(sigmas):
        rets = s * z
        px = 100.0 * np.cumprod(1.0 + rets)
        ser = pd.Series(px, index=cal)
        off = 0 if start_offsets is None else start_offsets.get(i, 0)
        closes[f"T{i:02d}"] = ser.iloc[off:]
    return cal, closes


def _sigmas():
    return np.linspace(0.004, 0.016, N_STOCKS)  # T00 = plus faible vol ... T24 = plus forte


def _universe():
    return [f"T{i:02d}" for i in range(N_STOCKS)]


# ------------------------------------------------------------------------------------------
# Grille et univers
# ------------------------------------------------------------------------------------------


def test_grid_is_the_four_preregistered_combos():
    assert sorted((p["vol_window"], p["n_holdings"]) for p in lowvol.PARAM_GRID) == [
        (63, 20), (63, 30), (252, 20), (252, 30),
    ]
    with pytest.raises(ValueError):
        lowvol.LowVolParams(vol_window=126, n_holdings=20)
    with pytest.raises(ValueError):
        lowvol.LowVolParams(vol_window=63, n_holdings=25)


def test_effective_universe_excludes_declared_tickers_and_is_sorted():
    eff = lowvol.effective_universe()
    assert eff == sorted(eff)
    assert all(t not in eff for t in lowvol.EXCLUDED_TICKERS)
    n_excluded_present = sum(1 for t in lowvol.EXCLUDED_TICKERS if t in UNIVERSE_SP100)
    assert len(eff) == len(UNIVERSE_SP100) - n_excluded_present
    assert lowvol.effective_universe(["AAA", "BK", "ZZZ"]) == ["AAA", "ZZZ"]


def test_decision_day_mask_is_last_trading_day_of_each_month():
    cal = pd.DatetimeIndex(["2020-01-30", "2020-01-31", "2020-02-03", "2020-02-27", "2020-02-28", "2020-03-02"])
    mask = lowvol.decision_day_mask(cal)
    assert mask.tolist() == [False, True, False, False, True, True]  # dernier élément : artefact documenté


# ------------------------------------------------------------------------------------------
# Bottom-N correct sur un cas construit à la main
# ------------------------------------------------------------------------------------------


def test_bottom_n_selects_lowest_vol_names_equal_weight():
    cal, closes = _synthetic_closes(_sigmas())
    uni = _universe()
    for L in lowvol.VOL_WINDOWS:
        w = lowvol.generate_weight_decisions(closes, cal, lowvol.LowVolParams(L, 20), uni)
        dec = np.flatnonzero(lowvol.decision_day_mask(cal))
        dec = [i for i in dec if i >= 400]  # tous les titres sont éligibles bien après 252 jours
        assert dec
        for i in dec[:-1]:
            row = w.iloc[i]
            held = set(row[row > 0].index)
            assert held == set(uni[:20]), f"L={L} date {cal[i]}: {sorted(held)}"
            assert np.allclose(row[uni[:20]].to_numpy(), 1.0 / 20)
            assert np.allclose(row[uni[20:]].to_numpy(), 0.0)
            assert row.sum() == pytest.approx(1.0)


def test_n30_selects_thirty_when_enough_eligible():
    sig = np.linspace(0.004, 0.02, 40)
    cal, closes = _synthetic_closes(sig)
    uni = [f"T{i:02d}" for i in range(40)]
    w = lowvol.generate_weight_decisions(closes, cal, lowvol.LowVolParams(63, 30), uni)
    i = [j for j in np.flatnonzero(lowvol.decision_day_mask(cal)) if j >= 400][0]
    row = w.iloc[i]
    assert set(row[row > 0].index) == set(uni[:30])
    assert np.allclose(row[row > 0].to_numpy(), 1.0 / 30)


def test_tie_break_is_alphabetical():
    # 22 titres identiques (même série => même vol) : les 20 premiers par ordre alphabétique sont retenus.
    cal, closes = _synthetic_closes(np.full(22, 0.01))
    uni = sorted(closes)
    w = lowvol.generate_weight_decisions(closes, cal, lowvol.LowVolParams(63, 20), uni)
    i = [j for j in np.flatnonzero(lowvol.decision_day_mask(cal)) if j >= 400][0]
    row = w.iloc[i]
    assert set(row[row > 0].index) == set(uni[:20])


# ------------------------------------------------------------------------------------------
# Éligibilité >= 252 jours réels, uniforme en L
# ------------------------------------------------------------------------------------------


def test_eligibility_requires_252_real_returns_and_is_independent_of_L():
    sig = _sigmas()
    # T00 (la plus faible vol : serait toujours retenue) n'apparaît qu'au jour 300 de l'histoire
    cal, closes = _synthetic_closes(sig, start_offsets={0: 300})
    uni = _universe()
    panels = lowvol.compute_signal_panels(closes, cal, uni)
    first_listing = closes["T00"].index[0]
    n_ret = panels["n_returns"]["T00"]
    pos_listing = cal.get_loc(first_listing)
    # 252 rendements réels => 253 clôtures => position pos_listing + 252
    first_eligible_pos = pos_listing + lowvol.MIN_HISTORY_DAYS
    assert n_ret.iloc[first_eligible_pos - 1] == lowvol.MIN_HISTORY_DAYS - 1
    assert n_ret.iloc[first_eligible_pos] == lowvol.MIN_HISTORY_DAYS
    elig63 = (panels["n_returns"] >= lowvol.MIN_HISTORY_DAYS) & panels["vol_63"].notna()
    elig252 = (panels["n_returns"] >= lowvol.MIN_HISTORY_DAYS) & panels["vol_252"].notna()
    assert elig63.equals(elig252), "l'éligibilité ne doit jamais dépendre de L"
    assert not elig63["T00"].iloc[:first_eligible_pos].any()
    assert elig63["T00"].iloc[first_eligible_pos:].all()

    for L in lowvol.VOL_WINDOWS:
        w = lowvol.generate_weight_decisions(closes, cal, lowvol.LowVolParams(L, 20), uni, panels)
        dec = [j for j in np.flatnonzero(lowvol.decision_day_mask(cal))]
        before = [j for j in dec if 300 <= j < first_eligible_pos]
        after = [j for j in dec if j >= first_eligible_pos and j < len(cal) - 1]
        assert before and after
        for j in before:
            assert w["T00"].iloc[j] == 0.0
        for j in after:
            assert w["T00"].iloc[j] == pytest.approx(1.0 / 20)  # devient retenue dès qu'éligible


def test_not_eligible_before_252_returns_means_flat_or_degenerate_equal_weight():
    cal, closes = _synthetic_closes(_sigmas())
    uni = _universe()
    w = lowvol.generate_weight_decisions(closes, cal, lowvol.LowVolParams(63, 20), uni)
    # avant le 253e jour du calendrier : aucun titre éligible => poids nuls (cash)
    assert (w.iloc[: lowvol.MIN_HISTORY_DAYS].abs().sum().sum()) == 0.0


# ------------------------------------------------------------------------------------------
# Gel mensuel des poids cibles
# ------------------------------------------------------------------------------------------


def test_weights_frozen_between_monthly_decision_dates():
    cal, closes = _synthetic_closes(_sigmas())
    uni = _universe()
    w = lowvol.generate_weight_decisions(closes, cal, lowvol.LowVolParams(63, 20), uni)
    mask = lowvol.decision_day_mask(cal)
    dec = list(np.flatnonzero(mask))
    for a, b in zip(dec[:-1], dec[1:]):
        block = w.iloc[a : b]  # de la décision a incluse à la suivante exclue
        assert (block.to_numpy() == block.iloc[0].to_numpy()).all(), f"poids non gelés entre {cal[a]} et {cal[b]}"
    first = dec[0]
    assert (w.iloc[:first].to_numpy() == 0.0).all()


def test_weights_change_only_on_decision_dates():
    # Les prix évoluent chaque jour : les poids ne doivent changer que sur une date de décision.
    rng = np.random.default_rng(3)
    n = N_DAYS
    cal = _calendar(n)
    closes = {f"T{i:02d}": pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.005 + 0.0005 * i, n)), index=cal) for i in range(N_STOCKS)}
    w = lowvol.generate_weight_decisions(closes, cal, lowvol.LowVolParams(63, 20), _universe())
    changed = (w.diff().abs().sum(axis=1) > 1e-15).to_numpy()
    mask = lowvol.decision_day_mask(cal)
    assert not (changed & ~mask).any(), "un poids a changé hors d'une date de décision"


# ------------------------------------------------------------------------------------------
# Causalité
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("L", [63, 252])
def test_causality_future_perturbation_does_not_change_signal_up_to_t(L):
    rng = np.random.default_rng(11)
    n = N_DAYS
    cal = _calendar(n)
    base = {f"T{i:02d}": pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.004 + 0.0007 * i, n)), index=cal) for i in range(N_STOCKS)}
    uni = _universe()
    t0 = 520
    perturbed = {}
    for sym, s in base.items():
        s2 = s.copy()
        s2.iloc[t0 + 1 :] = s2.iloc[t0 + 1 :] * rng.uniform(0.3, 3.0, n - t0 - 1)  # change violemment le futur
        perturbed[sym] = s2
    params = lowvol.LowVolParams(L, 20)
    w_base = lowvol.generate_weight_decisions(base, cal, params, uni)
    w_pert = lowvol.generate_weight_decisions(perturbed, cal, params, uni)
    pd.testing.assert_frame_equal(w_base.iloc[: t0 + 1], w_pert.iloc[: t0 + 1])
    # et les panneaux bruts (valeur à t = f(données <= t))
    p_base = lowvol.compute_signal_panels(base, cal, uni)
    p_pert = lowvol.compute_signal_panels(perturbed, cal, uni)
    for key in p_base:
        pd.testing.assert_frame_equal(p_base[key].iloc[: t0 + 1], p_pert[key].iloc[: t0 + 1])
    # sanité : la perturbation a bien un effet APRÈS t0 (sinon le test ne prouverait rien)
    assert not w_base.iloc[t0 + 1 :].equals(w_pert.iloc[t0 + 1 :])


def test_causality_truncating_history_does_not_change_past_weights():
    rng = np.random.default_rng(5)
    n = N_DAYS
    cal = _calendar(n)
    closes = {f"T{i:02d}": pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.004 + 0.0007 * i, n)), index=cal) for i in range(N_STOCKS)}
    uni = _universe()
    params = lowvol.LowVolParams(63, 20)
    full = lowvol.generate_weight_decisions(closes, cal, params, uni)
    cut = 500
    cal_cut = cal[:cut]
    closes_cut = {k: v.iloc[:cut] for k, v in closes.items()}
    short = lowvol.generate_weight_decisions(closes_cut, cal_cut, params, uni)
    # on exclut la toute dernière date (marquée décision par construction du calendrier tronqué)
    pd.testing.assert_frame_equal(full.iloc[: cut - 1], short.iloc[: cut - 1])


# ------------------------------------------------------------------------------------------
# Contrôle apparié et repli dégénéré
# ------------------------------------------------------------------------------------------


def test_control_is_equal_weight_of_all_eligible_at_same_dates():
    sig = _sigmas()
    cal, closes = _synthetic_closes(sig, start_offsets={3: 250, 7: 330})
    uni = _universe()
    panels = lowvol.compute_signal_panels(closes, cal, uni)
    ctl = lowvol.generate_control_weights(closes, cal, uni, panels)
    cand = lowvol.generate_weight_decisions(closes, cal, lowvol.LowVolParams(63, 20), uni, panels)
    mask = lowvol.decision_day_mask(cal)
    elig = (panels["n_returns"] >= lowvol.MIN_HISTORY_DAYS) & panels["vol_63"].notna()
    for i in np.flatnonzero(mask):
        e = elig.iloc[i]
        row = ctl.iloc[i]
        n_e = int(e.sum())
        if n_e == 0:
            assert row.sum() == 0.0
            continue
        assert np.allclose(row[e].to_numpy(), 1.0 / n_e)
        assert np.allclose(row[~e].to_numpy(), 0.0)
        assert row.sum() == pytest.approx(1.0)
    # mêmes dates de rebalance : le contrôle ne change que sur les dates de décision, comme la candidate
    changed_ctl = (ctl.diff().abs().sum(axis=1) > 1e-15).to_numpy()
    assert not (changed_ctl & ~mask).any()
    # le contrôle n'est PAS la candidate (aucune sélection) : il détient strictement plus de titres
    i_late = [j for j in np.flatnonzero(mask) if j >= 500][0]
    assert (ctl.iloc[i_late] > 0).sum() > (cand.iloc[i_late] > 0).sum()


def test_degenerate_fewer_than_n_eligible_equal_weights_all_eligible():
    sig = np.linspace(0.004, 0.012, 10)  # seulement 10 titres, N = 20 / 30
    cal, closes = _synthetic_closes(sig)
    uni = [f"T{i:02d}" for i in range(10)]
    i = [j for j in np.flatnonzero(lowvol.decision_day_mask(cal)) if j >= 400][0]
    for N in lowvol.N_HOLDINGS:
        w = lowvol.generate_weight_decisions(closes, cal, lowvol.LowVolParams(63, N), uni)
        assert np.allclose(w.iloc[i].to_numpy(), 1.0 / 10)
    ctl = lowvol.generate_control_weights(closes, cal, uni)
    assert np.allclose(ctl.iloc[i].to_numpy(), 1.0 / 10)


# ------------------------------------------------------------------------------------------
# Turnover du signal
# ------------------------------------------------------------------------------------------


def test_signal_turnover_one_way_fraction_replaced():
    cal = pd.DatetimeIndex(pd.bdate_range("2020-01-01", "2020-04-30"))
    mask = lowvol.decision_day_mask(cal)
    dec = np.flatnonzero(mask)
    cols = ["A", "B", "C", "D"]
    w = pd.DataFrame(0.0, index=cal, columns=cols)
    # décision 0 : A,B ; décision 1 : A,C (la moitié remplacée) ; décision 2 : D,C... (A->D : moitié)
    sets = [("A", "B"), ("A", "C"), ("D", "C"), ("D", "C")]
    for k, d in enumerate(dec[: len(sets)]):
        end = dec[k + 1] if k + 1 < len(dec) else len(cal)
        for s in sets[k]:
            w.iloc[d:end, cols.index(s)] = 0.5
    out = lowvol.signal_turnover_one_way(w, cal, 0, len(cal) - 1)
    assert out[0] == pytest.approx(0.5) and out[1] == pytest.approx(0.5) and out[2] == pytest.approx(0.0)


# ------------------------------------------------------------------------------------------
# Anomalie A3 de la SPEC : la bande 0,05 du moteur bloque les entrées de poids < 0,05
# ------------------------------------------------------------------------------------------


def _flat_market(n_names, n=80, seed=1):
    cal = _calendar(n)
    rng = np.random.default_rng(seed)
    px = pd.DataFrame(
        {f"S{i:02d}": 50 * np.cumprod(1 + rng.normal(0.0004, 0.01, n)) for i in range(n_names)}, index=cal
    )
    return cal, px.copy(), px.copy()  # opens = closes (suffit pour la mécanique)


def _exposure(n_names, weight, band):
    cal, opens, closes = _flat_market(n_names)
    w = pd.DataFrame(weight, index=cal, columns=opens.columns)
    seg = engine.simulate_segment(
        cal, w, opens, closes, 5, 60, 5.0, no_trade_band=band, apply_vol_targeting=False
    )
    return float(seg.gross_exposure.mean())


def test_engine_band_blocks_entry_of_weights_below_band_and_documented_knife_edge_at_N20():
    # N=30 (1/30 = 0,033 < 0,05) : jamais investi avec la bande du moteur ; bande 0 : investi à ~100 %
    assert _exposure(30, 1.0 / 30, 0.05) == pytest.approx(0.0, abs=1e-12)
    assert _exposure(30, 1.0 / 30, 0.0) == pytest.approx(1.0, abs=0.02)
    # contrôle (~100 titres, 0,01) : idem
    assert _exposure(100, 1.0 / 100, 0.05) == pytest.approx(0.0, abs=1e-12)
    # N=20 (1/20 = 0,05 EXACTEMENT = la bande, inégalité stricte) : l'ordre d'entrée est émis
    assert _exposure(20, 1.0 / 20, 0.05) == pytest.approx(1.0, abs=0.05)
    # S2 (a = 0,3) : mêmes décisions d'entrée, poids et bande mis à l'échelle de façon identique
    a = 0.30
    assert _exposure(20, (1.0 / 20) * a, 0.05 * a) > 0.2
    assert _exposure(30, (1.0 / 30) * a, 0.05 * a) == pytest.approx(0.0, abs=1e-12)


# ------------------------------------------------------------------------------------------
# Plomberie de l'annexe band_fidelity : réplica numpy == moteur, reconversion de poche, cb
# ------------------------------------------------------------------------------------------


def _synthetic_run_ctx(n_names=25, n=330, seed=2):
    cal = _calendar(n)
    rng = np.random.default_rng(seed)
    cols = [f"S{i:02d}" for i in range(n_names)]
    closes = pd.DataFrame(
        {c: 60 * np.cumprod(1 + rng.normal(0.0003, 0.012, n)) for c in cols}, index=cal
    )
    opens = closes.shift(1).fillna(closes.iloc[0]) * (1 + rng.normal(0, 0.002, (n, n_names)))
    # poids décidés : 20 titres tirés au hasard rebalancés tous les 21 jours
    W = pd.DataFrame(0.0, index=cal, columns=cols)
    for start in range(0, n, 21):
        pick = rng.choice(n_names, size=20, replace=False)
        W.iloc[start : start + 21, pick] = 1.0 / 20
    windows = [
        SimpleNamespace(index=0, oos_start_idx=40, oos_end_idx=139),
        SimpleNamespace(index=1, oos_start_idx=140, oos_end_idx=239),
        SimpleNamespace(index=2, oos_start_idx=240, oos_end_idx=329),
    ]
    run._CTX.clear()
    run._CTX.update(calendar=cal, opens=opens, closes=closes)
    return windows, W


@pytest.mark.parametrize("a", [1.0, 0.30])
def test_replica_matches_engine_with_carry_for_S1_and_S2(a):
    windows, W = _synthetic_run_ctx()
    provider = lambda params: W  # noqa: E731
    chosen = [{} for _ in windows]
    band = 0.05 * a
    segs, events = run.simulate_chain(windows, chosen, provider, 5.0, band=band, scale=a)
    assert events == []  # fenêtres contiguës
    assert segs[1][1] and segs[2][1]  # portage actif sur les frontières
    rep = run.replica_chain(windows, chosen, provider, band, a, 5.0)
    for rw, (seg, _) in zip(rep["window_equity"], segs):
        assert np.max(np.abs(rw - seg.equity.to_numpy())) < 1e-9
    assert rep["traded"].shape[0] == sum(w.oos_end_idx - w.oos_start_idx + 1 for w in windows)


def test_pocket_reconversion_identity_and_affine_relation():
    windows, W = _synthetic_run_ctx()
    provider = lambda params: W  # noqa: E731
    chosen = [{} for _ in windows]
    # a = 1 : la reconversion est l'identité
    segs1, _ = run.simulate_chain(windows, chosen, provider, 5.0, band=0.05, scale=1.0)
    r_id = run.pocket_returns_from_wallet_segments([s for s, _ in segs1], 1.0)
    r_ref = engine.concatenate_segments([s for s, _ in segs1]).returns
    assert np.allclose(r_id.to_numpy(), r_ref.to_numpy())
    # a = 0,3 : P = 1 + (E - 1)/a sur chaque fenêtre
    a = 0.30
    segs2, _ = run.simulate_chain(windows, chosen, provider, 5.0, band=0.05 * a, scale=a)
    r2 = run.pocket_returns_from_wallet_segments([s for s, _ in segs2], a)
    start = 0
    for seg, _ in segs2:
        n = len(seg.equity)
        p = 1.0 + (seg.equity.to_numpy() - 1.0) / a
        got = np.cumprod(1.0 + r2.to_numpy()[start : start + n])
        assert np.allclose(got, p)
        start += n


def test_cb_exposure_counts_episodes_and_drawdown():
    dates = pd.bdate_range("2020-01-01", periods=10)
    eq = np.array([1.0, 1.0, 0.9, 0.8, 0.7, 0.9, 1.0, 1.1, 0.8, 0.7, 0.6])
    cfg = {"half": 0.15, "flatten": 0.25, "freeze": 0.03}
    out = run.cb_exposure(eq, dates, cfg)
    assert out["max_wallet_drawdown"] == pytest.approx(1 - 0.6 / 1.1)
    # dd par jour (aligné sur dates) : 0, 0.1, 0.2, 0.3, 0.1818.., 0.1, 0, 0.2727.., 0.3636.., 0.4545..
    assert out["n_episodes_half_size"] == 2
    assert out["n_episodes_flatten"] == 2
    assert out["flatten_would_trigger"] is True
    assert out["first_flatten_date"] == str(dates[3].date())
    assert out["n_episodes_freeze_entries"] >= 1


def test_run_lengths():
    assert run.run_lengths(np.array([0, 1, 1, 0, 1, 0, 0, 1, 1, 1], dtype=bool)) == 3
    assert run.run_lengths(np.array([], dtype=bool)) == 0


def test_run_script_fixes_vol_targeting_explicitly_and_parameters_are_spec_values():
    assert run.APPLY_VOL_TARGETING is False
    assert run.NO_TRADE_BAND == 0.05
    assert run.PERIODS_PER_YEAR == 252
    assert run.COST_BPS_EQUITY == 5.0 and run.COST_BPS_BENCHMARK == 3.0
    assert (run.IS_MONTHS, run.OOS_MONTHS, run.STEP_MONTHS) == (36, 12, 12)
    assert run.CAPITAL_ALLOC_PCT == 0.30
    assert run.K_REGISTRY_ROWS == 16
    assert run.N_GRID_COMBOS_CANDIDATE == 4 and run.N_GRID_COMBOS_CONTROL == 1
    # K_total = 16 + 30*4 + 30*1 = 166 (SPEC §3)
    assert run.K_REGISTRY_ROWS + 30 * run.N_GRID_COMBOS_CANDIDATE + 30 * run.N_GRID_COMBOS_CONTROL == 166


def test_paired_comparison_flags_degenerate_control_as_not_evaluable():
    idx = pd.bdate_range("2020-01-01", periods=300)
    rng = np.random.default_rng(0)
    cand = pd.Series(rng.normal(0.0005, 0.01, 300), index=idx)
    zero = pd.Series(0.0, index=idx)
    out = run.paired_comparison(cand, zero)
    assert out["control_degenerate"] is True
    assert out["candidate_dominated_by_control"] is None
    # contrôle non dégénéré et strictement meilleur sur les 3 axes => dominée
    better = cand + 0.002
    better = better.where(better > -0.9, -0.9)
    out2 = run.paired_comparison(cand, better)
    assert out2["control_degenerate"] is False
    assert out2["candidate_dominated_by_control"] is True
