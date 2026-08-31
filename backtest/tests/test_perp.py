"""backtest/tests/test_perp.py — tests de l'extension short/perpétuels + funding
(`backtest/PERP-EXTENSION-SPEC.md`, `backtest/perp.py`, `backtest/engine.py`).

Fixtures synthétiques (rapides, déterministes) sauf le test d'intégration final qui utilise
les données réelles `_data/` (BTC spot + BTC-PERP, 2024-01 -> 2024-03) et se `skip` si ce
répertoire est absent (même esprit que le reste du dépôt : jamais de test qui échoue
silencieusement faute de données, un `skip` explicite et journalisé à la place)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest import engine, perp

_DATA_DIR = Path(__file__).resolve().parents[2] / "_data"


def _hourly_calendar(n: int, start: str = "2024-01-01"):
    return pd.date_range(start, periods=n, freq="h", tz="UTC")


def _flat_perp_fixture(n: int, price: float = 100.0):
    """Une seule colonne perp `X-PERP`, prix CONSTANT (isole l'effet testé de tout mouvement de
    marché), funding nul par défaut -- à surcharger ligne par ligne dans chaque test."""
    cal = _hourly_calendar(n)
    opens = pd.DataFrame({"X-PERP": price}, index=cal)
    closes = pd.DataFrame({"X-PERP": price}, index=cal)
    highs = pd.DataFrame({"X-PERP": price}, index=cal)
    lows = pd.DataFrame({"X-PERP": price}, index=cal)
    funding = pd.DataFrame({"X-PERP": 0.0}, index=cal)
    return cal, opens, closes, highs, lows, funding


# ------------------------------------------------------------------------------------------
# (a) Signe du funding, montant exact
# ------------------------------------------------------------------------------------------


def test_funding_sign_short_receives_when_rate_positive_exact_amount():
    """rate > 0 -- Binance : les LONGS paient les SHORTS (spec §1). Montant exact :
    `notionnel * rate` crédité au cash à la clôture de la bougie où le règlement s'applique."""
    n = 4
    cal, opens, closes, highs, lows, funding = _flat_perp_fixture(n)
    funding.iloc[1] = 0.002  # un seul règlement, à la bougie i=1 (j=0 du segment simulé)

    w = pd.DataFrame({"X-PERP": -0.4}, index=cal)  # short constant
    seg = engine.simulate_segment(
        cal, w, opens, closes, 1, n - 1, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
    )
    # shares = -0.4*1.0/100 = -0.004 ; notionnel = 0.4 ; funding reçu = 0.4*0.002 = 0.0008
    expected = 0.4 * 0.002
    assert seg.pnl_breakdown["funding_received"] == pytest.approx(expected, abs=1e-12)
    assert seg.equity.iloc[0] == pytest.approx(1.0 + expected, abs=1e-12)


def test_funding_sign_short_pays_when_rate_negative_exact_amount():
    """rate < 0 : les SHORTS paient (spec §1, sens inverse)."""
    n = 4
    cal, opens, closes, highs, lows, funding = _flat_perp_fixture(n)
    funding.iloc[1] = -0.002

    w = pd.DataFrame({"X-PERP": -0.4}, index=cal)
    seg = engine.simulate_segment(
        cal, w, opens, closes, 1, n - 1, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
    )
    expected = -0.4 * 0.002
    assert seg.pnl_breakdown["funding_received"] == pytest.approx(expected, abs=1e-12)


def test_funding_sign_long_pays_when_rate_positive_exact_amount():
    """Un LONG paie quand rate > 0 -- exact inverse du short (spec §1)."""
    n = 4
    cal, opens, closes, highs, lows, funding = _flat_perp_fixture(n)
    funding.iloc[1] = 0.002

    w = pd.DataFrame({"X-PERP": 0.4}, index=cal)  # long constant
    seg = engine.simulate_segment(
        cal, w, opens, closes, 1, n - 1, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
    )
    expected = -0.4 * 0.002
    assert seg.pnl_breakdown["funding_received"] == pytest.approx(expected, abs=1e-12)
    assert seg.equity.iloc[0] == pytest.approx(1.0 + expected, abs=1e-12)


# ------------------------------------------------------------------------------------------
# (b) Alignement H -> H-1h, anti-look-ahead sur l'engine
# ------------------------------------------------------------------------------------------


def test_funding_credited_at_the_correct_candle_not_before_not_after():
    """Un unique règlement non nul positionné à la bougie `i=2` du segment simulé (position
    perp constante tout du long) ne doit affecter l'équity NI avant NI après cette bougie
    précise -- décaler le règlement d'UNE bougie décale exactement l'endroit où l'équity
    bouge, ce qu'un moteur avec un bug de look-ahead (ou de retard) sur le funding ne
    reproduirait pas (cf. mission -- "décaler le funding d'une bougie change l'equity de
    façon prévisible")."""
    n = 6
    cal, opens, closes, highs, lows, funding = _flat_perp_fixture(n)
    funding.iloc[2] = 0.01
    w = pd.DataFrame({"X-PERP": -0.3}, index=cal)

    seg = engine.simulate_segment(
        cal, w, opens, closes, 1, n - 1, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
    )
    # start_idx=1 -> seg.equity[j] correspond à la bougie calendar[1+j] ; la bougie i=2 est j=1.
    diffs = np.diff(seg.equity.to_numpy())
    jump_positions = np.flatnonzero(np.abs(diffs) > 1e-15)
    assert jump_positions.tolist() == [0], (
        "le SEUL mouvement d'équity attendu est celui de j=0 -> j=1 (funding réglé à la "
        f"bougie i=2), obtenu aux positions {jump_positions.tolist()}"
    )

    # Décalage du règlement d'UNE bougie -> le saut d'équity doit se décaler EXACTEMENT d'une
    # position, pas disparaître, pas apparaître ailleurs.
    funding_shifted = funding.copy()
    funding_shifted.iloc[2] = 0.0
    funding_shifted.iloc[3] = 0.01
    seg_shifted = engine.simulate_segment(
        cal, w, opens, closes, 1, n - 1, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding_shifted, highs=highs, lows=lows,
    )
    diffs_shifted = np.diff(seg_shifted.equity.to_numpy())
    jump_positions_shifted = np.flatnonzero(np.abs(diffs_shifted) > 1e-15)
    assert jump_positions_shifted.tolist() == [1]


# ------------------------------------------------------------------------------------------
# (c) Paire couverte spot long / perp short, funding nul : delta-neutralité
# ------------------------------------------------------------------------------------------


def test_hedged_pair_delta_neutral_equity_moves_only_with_costs():
    """Spot long + perp short de même notionnel, prix IDENTIQUES entre les deux jambes (donc
    strictement corrélés), funding nul : l'équity ne doit varier QUE des coûts de transaction
    -- ni du prix (couverte), ni du funding (nul)."""
    n = 8
    cal = _hourly_calendar(n)
    rng = np.random.default_rng(3)
    price_path = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, n))
    opens = pd.DataFrame({"X": price_path, "X-PERP": price_path}, index=cal)
    closes = pd.DataFrame({"X": price_path, "X-PERP": price_path}, index=cal)
    highs = pd.DataFrame({"X-PERP": price_path * 1.5}, index=cal)  # marge large, jamais liquidé
    lows = pd.DataFrame({"X-PERP": price_path * 0.5}, index=cal)
    funding = pd.DataFrame({"X-PERP": 0.0}, index=cal)
    w = pd.DataFrame({"X": 0.4, "X-PERP": -0.4}, index=cal)

    seg_zero_cost = engine.simulate_segment(
        cal, w, opens, closes, 1, n - 1, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
    )
    assert seg_zero_cost.equity.to_numpy() == pytest.approx(1.0, abs=1e-9), (
        "couverture parfaite + coût nul + funding nul -> équity strictement constante"
    )

    seg_with_cost = engine.simulate_segment(
        cal, w, opens, closes, 1, n - 1, cost_bps=10.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows, perp_cost_bps=10.0,
    )
    # Le seul écart à 1.0 provient des coûts (établissement de la couverture au premier tour).
    assert seg_with_cost.equity.iloc[-1] < 1.0
    # Couverture parfaite -- le PnL de PRIX du spot et celui du perp sont exactement opposés
    # bougie par bougie (chacun individuellement n'est PAS nul : le prix bouge bel et bien --
    # seule leur SOMME l'est, preuve directe de la delta-neutralité).
    assert seg_with_cost.pnl_breakdown["spot_pnl"] + seg_with_cost.pnl_breakdown["perp_variation"] == pytest.approx(0.0, abs=1e-9)
    assert abs(seg_with_cost.pnl_breakdown["spot_pnl"]) > 1e-6  # le prix bouge réellement (fixture non triviale)
    assert seg_with_cost.pnl_breakdown["funding_received"] == pytest.approx(0.0, abs=1e-9)


# ------------------------------------------------------------------------------------------
# (d) Liquidation intra-bougie
# ------------------------------------------------------------------------------------------


def test_liquidation_triggered_by_high_spike_for_short_matches_hand_calc():
    """Spike du HIGH intra-bougie au-delà du seuil de maintenance -- la jambe short est
    liquidée AU PIRE PRIX (`worst=high`), frais de liquidation inclus. Équity après = valeur
    calculée à la main (spec §3.4). Ici la perte reste INFÉRIEURE au cash disponible : le
    plafond de faillite (correctif audit 2026-08-31) ne joue pas, `bankrupt` est False."""
    n = 4
    cal, opens, closes, highs, lows, funding = _flat_perp_fixture(n)
    highs.iloc[2] = 130.0  # spike uniquement à la bougie testée

    w = pd.DataFrame({"X-PERP": -1.9}, index=cal)  # notionnel proche de la limite de marge
    seg = engine.simulate_segment(
        cal, w, opens, closes, 1, 2, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
        perp_initial_margin_frac=0.5, perp_maintenance_margin_frac=0.025, perp_liquidation_fee_bps=100.0,
    )
    shares = -1.9 * 1.0 / 100.0  # -0.019
    loss = shares * (130.0 - 100.0)  # -0.57 : cash 1.0 + loss = 0.43 < maintenance 0.025*0.019*130 ? non...
    # Seuil de maintenance : 0.025 * |shares| * worst = 0.06175 ; cash + loss = 0.43 >= 0.06175
    # -> PAS de liquidation par la perte seule. On force donc un levier plus proche de la
    # limite : voir ci-dessous (poids -1.99, high 145).
    assert seg.liquidations == []

    highs.iloc[2] = 145.0
    w = pd.DataFrame({"X-PERP": -1.99}, index=cal)
    seg = engine.simulate_segment(
        cal, w, opens, closes, 1, 2, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
        perp_initial_margin_frac=0.5, perp_maintenance_margin_frac=0.025, perp_liquidation_fee_bps=100.0,
    )
    shares = -1.99 / 100.0  # -0.0199
    loss = shares * (145.0 - 100.0)  # -0.8955 ; cash + loss = 0.1045 < 0.025*0.0199*145 = 0.0721 ? non
    # 0.1045 >= 0.0721 -> toujours pas. Pousser le high à 150 : loss = -0.995, cash+loss = 0.005
    # < 0.0746 -> liquidation, et 0.005 - fee (0.0199*150*0.01 = 0.02985) < 0 -> FAILLITE.
    highs.iloc[2] = 148.0
    seg = engine.simulate_segment(
        cal, w, opens, closes, 1, 2, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
        perp_initial_margin_frac=0.5, perp_maintenance_margin_frac=0.025, perp_liquidation_fee_bps=100.0,
    )
    loss = shares * (148.0 - 100.0)  # -0.9552 ; cash + loss = 0.0448 < 0.025*0.0199*148 = 0.07363
    fee = abs(shares * 148.0) * (0.0 + 100.0 / 1e4)  # 0.029452
    expected_equity_after = 1.0 + loss - fee  # 0.015348 > 0 : pas de faillite, plafond inactif
    assert expected_equity_after > 0
    assert len(seg.liquidations) == 1
    liq = seg.liquidations[0]
    assert liq["symbol"] == "X-PERP"
    assert liq["side"] == "short"
    assert liq["worst_price"] == pytest.approx(148.0)
    assert liq["bankrupt"] is False
    assert seg.equity.iloc[-1] == pytest.approx(expected_equity_after, abs=1e-12)
    assert seg.n_liquidations() == 1
    assert seg.n_trades_closed() == 1  # la ligne perp liquidée compte comme un trade clos


def test_liquidation_loss_capped_at_cash_bankruptcy_and_ruin():
    """Correctif audit 2026-08-31 (MAJEUR) : une perte de liquidation supérieure au cash de
    marge est PLAFONNÉE au cash (prix de faillite) -- jamais d'équity négative qui inverserait
    le signe des rendements. Ici : short à la limite de marge, spike ×2 -> cash 1.0 - 1.9 - frais
    < 0 -> cash 0, `bankrupt=True`, puis ruine (équity 0, figée, rendements suivants nuls)."""
    n = 5
    cal, opens, closes, highs, lows, funding = _flat_perp_fixture(n)
    highs.iloc[2] = 200.0
    w = pd.DataFrame({"X-PERP": -1.9}, index=cal)
    seg = engine.simulate_segment(
        cal, w, opens, closes, 1, 4, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
        perp_initial_margin_frac=0.5, perp_maintenance_margin_frac=0.025, perp_liquidation_fee_bps=100.0,
    )
    liq = [e for e in seg.liquidations if e["symbol"] == "X-PERP"]
    assert len(liq) == 1 and liq[0]["bankrupt"] is True
    assert liq[0]["loss_applied"] >= -1.0  # jamais plus que le cash disponible
    ruin = [e for e in seg.liquidations if e["side"] == "ruin"]
    assert len(ruin) == 1
    assert (seg.equity >= 0.0).all()
    assert seg.equity.iloc[-1] == 0.0
    assert seg.returns.iloc[1] == pytest.approx(-1.0)
    assert (seg.returns.iloc[2:] == 0.0).all()
    # Identité comptable conservée avec la perte plafonnée
    bd = seg.pnl_breakdown
    total = bd["spot_pnl"] + bd["perp_variation"] + bd["funding_received"] - bd["costs_spot"] - bd["costs_perp"] - bd["liquidation_fees"]
    assert total == pytest.approx(seg.equity.iloc[-1] - 1.0, abs=1e-12)


def test_funding_payable_enters_liquidation_test():
    """Correctif audit 2026-08-31 (MAJEUR, spec §3.4 amendée) : un funding PAYABLE réglé à la
    même bougie entre dans le test de marge ; prix parfaitement plats (aucune perte de prix
    possible), funding -0.9 sur un short w=-1.9 -> cash 1.0 - 0.9*1.9 = -0.71 < seuil ->
    liquidation (au lieu d'un cash négatif silencieux)."""
    n = 4
    cal, opens, closes, highs, lows, funding = _flat_perp_fixture(n)
    funding.iloc[2] = -0.9
    w = pd.DataFrame({"X-PERP": -1.9}, index=cal)
    seg = engine.simulate_segment(
        cal, w, opens, closes, 1, 2, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
        perp_initial_margin_frac=0.5, perp_maintenance_margin_frac=0.025, perp_liquidation_fee_bps=100.0,
    )
    assert seg.n_liquidations() >= 1
    assert seg.liquidations[0]["symbol"] == "X-PERP"
    # Un funding FAVORABLE de même ampleur n'entre jamais dans le test (pas de collatéral fictif)
    funding.iloc[2] = +0.9
    seg2 = engine.simulate_segment(
        cal, w, opens, closes, 1, 2, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
        perp_initial_margin_frac=0.5, perp_maintenance_margin_frac=0.025, perp_liquidation_fee_bps=100.0,
    )
    assert seg2.liquidations == []


def test_nan_perp_price_refused_when_engaged_but_tolerated_when_flat():
    """Correctif audit 2026-08-31 (CRITIQUE) : un prix perp NaN à une bougie où le symbole est
    en position ou a un poids cible non nul lève ValueError (jamais un gain fictif de short à
    prix 0) ; un symbole flat sans poids cible traverse le trou sans erreur."""
    n = 6
    cal, opens, closes, highs, lows, funding = _flat_perp_fixture(n)
    opens.iloc[3, opens.columns.get_loc("X-PERP")] = float("nan")
    w = pd.DataFrame({"X-PERP": -0.5}, index=cal)
    with pytest.raises(ValueError, match="manquant"):
        engine.simulate_segment(
            cal, w, opens, closes, 1, 5, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
            perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
        )
    w0 = pd.DataFrame({"X-PERP": 0.0}, index=cal)
    seg = engine.simulate_segment(
        cal, w0, opens, closes, 1, 5, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
    )
    assert (seg.equity == 1.0).all()


def test_liquidation_not_triggered_below_threshold():
    """Même position, spike du high qui reste EN-DEÇÀ du seuil de maintenance -- aucune
    liquidation, l'équity suit simplement la variation margin normale (contrôle négatif du
    test précédent)."""
    n = 4
    cal, opens, closes, highs, lows, funding = _flat_perp_fixture(n)
    highs.iloc[2] = 101.0  # +1% seulement, très loin du seuil de maintenance

    w = pd.DataFrame({"X-PERP": -1.9}, index=cal)
    seg = engine.simulate_segment(
        cal, w, opens, closes, 1, 2, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
        perp_initial_margin_frac=0.5, perp_maintenance_margin_frac=0.025, perp_liquidation_fee_bps=100.0,
    )
    assert seg.liquidations == []
    assert seg.n_trades_closed() == 0


# ------------------------------------------------------------------------------------------
# (e) Contrainte de faisabilité de marge -> ValueError
# ------------------------------------------------------------------------------------------


def test_margin_infeasibility_raises_value_error():
    n = 4
    cal, opens, closes, highs, lows, funding = _flat_perp_fixture(n)
    # notionnel cible = 3x l'équity -> marge exigée = 1.5x l'équity, infaisable.
    w = pd.DataFrame({"X-PERP": -3.0}, index=cal)
    with pytest.raises(ValueError, match="faisabilité de marge"):
        engine.simulate_segment(
            cal, w, opens, closes, 1, n - 1, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
            perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
        )


# ------------------------------------------------------------------------------------------
# (f) Poids négatif sur une colonne spot -> ValueError
# ------------------------------------------------------------------------------------------


def test_negative_spot_weight_raises_value_error():
    n = 4
    cal = _hourly_calendar(n)
    opens = pd.DataFrame({"X": 100.0}, index=cal)
    closes = pd.DataFrame({"X": 100.0}, index=cal)
    w = pd.DataFrame({"X": [-0.1, 0.1, 0.1, 0.1]}, index=cal)
    with pytest.raises(ValueError, match="long-only"):
        engine.simulate_segment(cal, w, opens, closes, 1, n - 1, cost_bps=0.0, apply_vol_targeting=False)


def test_perp_symbols_without_funding_highs_lows_raises_value_error():
    n = 4
    cal, opens, closes, highs, lows, funding = _flat_perp_fixture(n)
    w = pd.DataFrame({"X-PERP": 0.3}, index=cal)
    with pytest.raises(ValueError, match="funding, highs ET lows"):
        engine.simulate_segment(
            cal, w, opens, closes, 1, n - 1, cost_bps=0.0, apply_vol_targeting=False,
            perp_symbols={"X-PERP"},
        )


# ------------------------------------------------------------------------------------------
# (g) Coûts perp proportionnels au turnover perp
# ------------------------------------------------------------------------------------------


def test_perp_costs_proportional_to_perp_turnover():
    """Même construction que `test_costs_reduce_equity_proportionally_to_turnover` de
    `test_engine.py`, transposée à la jambe perp : prix constants, bascule périodique
    short<->flat, coût `perp_cost_bps` variable."""
    n = 41
    switch_every = 5
    cal, opens, closes, highs, lows, funding = _flat_perp_fixture(n)
    weights = []
    for i in range(n):
        cycle = i // switch_every
        weights.append(-0.5 if cycle % 2 == 0 else 0.0)
    w = pd.DataFrame({"X-PERP": weights}, index=cal)

    common = dict(
        no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
    )
    seg_zero = engine.simulate_segment(cal, w, opens, closes, 1, n - 1, cost_bps=0.0, perp_cost_bps=0.0, **common)
    seg_low = engine.simulate_segment(cal, w, opens, closes, 1, n - 1, cost_bps=0.0, perp_cost_bps=25.0, **common)
    seg_high = engine.simulate_segment(cal, w, opens, closes, 1, n - 1, cost_bps=0.0, perp_cost_bps=100.0, **common)

    assert seg_zero.equity.iloc[-1] == pytest.approx(1.0, abs=1e-9)
    assert seg_zero.equity.iloc[-1] > seg_low.equity.iloc[-1] > seg_high.equity.iloc[-1]
    assert seg_low.pnl_breakdown["costs_perp"] > 0.0
    # Approximativement x4 (100bps vs 25bps) -- pas EXACTEMENT, la composition de l'équity
    # bougie après bougie fait légèrement dévier la mise à l'échelle stricte (même tolérance
    # que `test_costs_reduce_equity_proportionally_to_turnover` de test_engine.py, rel=0.05).
    assert seg_high.pnl_breakdown["costs_perp"] == pytest.approx(seg_low.pnl_breakdown["costs_perp"] * 4.0, rel=0.05)
    # cost_bps spot n'a jamais bougé -- costs_spot reste nul dans les trois runs.
    assert seg_high.pnl_breakdown["costs_spot"] == pytest.approx(0.0, abs=1e-12)


# ------------------------------------------------------------------------------------------
# (h) Comptage des lignes perp + cohérence PnL réalisé net / pnl_breakdown
# ------------------------------------------------------------------------------------------


def test_perp_line_counting_and_realized_pnl_matches_pnl_breakdown():
    """Ouverture short -> réduction partielle -> fermeture -> ouverture long -> fermeture,
    prix mouvant + funding non nul. À la fin, PLUS AUCUNE ligne perp n'est ouverte : la somme
    de tous les évènements réalisés de la jambe perp doit alors égaler EXACTEMENT
    `perp_variation + funding_received - costs_perp - liquidation_fees` (rien ne reste
    "en cours" non réalisé)."""
    n = 6
    cal = _hourly_calendar(n)
    rng = np.random.default_rng(5)
    price_path = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, n))
    opens = pd.DataFrame({"X-PERP": price_path}, index=cal)
    closes = pd.DataFrame({"X-PERP": price_path}, index=cal)
    highs = pd.DataFrame({"X-PERP": price_path * 2.0}, index=cal)  # jamais liquidé
    lows = pd.DataFrame({"X-PERP": price_path * 0.5}, index=cal)
    funding = pd.DataFrame({"X-PERP": 0.0}, index=cal)
    funding.iloc[2] = 0.001
    funding.iloc[4] = -0.0005

    # weights_decided.iloc[k] exécuté à la bougie i=k+1.
    w = pd.DataFrame({"X-PERP": [-0.3, -0.15, 0.0, 0.2, 0.0, 0.0]}, index=cal)

    seg = engine.simulate_segment(
        cal, w, opens, closes, 1, n - 1, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows, perp_cost_bps=5.0,
    )

    assert seg.n_trades_closed() == 2  # ligne short fermée, ligne long fermée
    perp_realized = [e for e in seg.realized_events if e.get("leg") == "perp"]
    assert len(perp_realized) == 3  # 1 réduction partielle + 2 fermetures complètes
    assert sum(1 for e in perp_realized if e["closes_line"]) == 2
    assert sum(1 for e in perp_realized if not e["closes_line"]) == 1

    assert seg.gross_exposure.iloc[-1] == pytest.approx(0.0, abs=1e-9)  # plus aucune ligne ouverte
    total_realized = sum(e["pnl"] for e in perp_realized)
    total_breakdown = (
        seg.pnl_breakdown["perp_variation"]
        + seg.pnl_breakdown["funding_received"]
        - seg.pnl_breakdown["costs_perp"]
        - seg.pnl_breakdown["liquidation_fees"]
    )
    assert total_realized == pytest.approx(total_breakdown, abs=1e-9)


# ------------------------------------------------------------------------------------------
# (i) Rétro-compatibilité bit-à-bit
# ------------------------------------------------------------------------------------------


def test_perp_symbols_none_reproduces_historical_path_bit_for_bit():
    """EXIGENCE ABSOLUE (mission) : `simulate_segment` sur données synthétiques aléatoires
    (seed fixe), avec et sans les nouveaux kwargs perp explicitement à leurs défauts, doit
    produire une `equity` IDENTIQUE BIT À BIT (`np.array_equal`, jamais `allclose`)."""
    rng = np.random.default_rng(2026)
    n = 300
    cal = pd.bdate_range("2018-03-01", periods=n)
    rets_a = rng.normal(0.0003, 0.012, n)
    rets_b = rng.normal(0.0001, 0.02, n)
    close_a = 50.0 * np.cumprod(1.0 + rets_a)
    close_b = 200.0 * np.cumprod(1.0 + rets_b)
    closes = pd.DataFrame({"A": close_a, "B": close_b}, index=cal)
    opens = closes.shift(1).bfill()
    w = pd.DataFrame(
        {"A": rng.random(n) * 0.6, "B": rng.random(n) * 0.6}, index=cal
    )

    seg_implicit = engine.simulate_segment(cal, w, opens, closes, 1, n - 1, cost_bps=7.0)
    seg_explicit_defaults = engine.simulate_segment(
        cal, w, opens, closes, 1, n - 1, cost_bps=7.0,
        perp_symbols=None, funding=None, highs=None, lows=None,
        perp_cost_bps=None, perp_initial_margin_frac=0.50,
        perp_maintenance_margin_frac=0.025, perp_liquidation_fee_bps=100.0,
    )
    seg_empty_set = engine.simulate_segment(
        cal, w, opens, closes, 1, n - 1, cost_bps=7.0, perp_symbols=set(),
    )

    assert np.array_equal(seg_implicit.equity.to_numpy(), seg_explicit_defaults.equity.to_numpy())
    assert np.array_equal(seg_implicit.equity.to_numpy(), seg_empty_set.equity.to_numpy())
    assert np.array_equal(seg_implicit.returns.to_numpy(), seg_explicit_defaults.returns.to_numpy())
    assert np.array_equal(
        seg_implicit.gross_exposure.to_numpy(), seg_explicit_defaults.gross_exposure.to_numpy()
    )
    # Champs perp neufs : présents mais neutres (défauts rétro-compatibles, cf. mission point 3).
    assert seg_implicit.liquidations == []
    assert seg_implicit.pnl_breakdown["perp_variation"] == 0.0
    assert seg_implicit.pnl_breakdown["funding_received"] == 0.0
    assert seg_implicit.pnl_breakdown["costs_perp"] == 0.0


# ------------------------------------------------------------------------------------------
# (j) Parsing des timestamps funding à formats mixtes / jitter
# ------------------------------------------------------------------------------------------


def test_funding_timestamp_mixed_formats_and_jitter_round_correctly(tmp_path):
    """Reproduit le format constaté sur les données réelles (spec §1) : certaines lignes sans
    millisecondes, d'autres avec un jitter positif ET négatif de quelques millisecondes autour
    de l'heure ronde -- toutes doivent être arrondies à LA MÊME heure ronde après parsing."""
    raw = pd.DataFrame(
        {
            "timestamp": [
                "2024-01-01T00:00:00+00:00",  # pile à l'heure
                "2024-01-01T08:00:00.009000+00:00",  # jitter positif (+9ms)
                "2024-01-01T15:59:59.999000+00:00",  # jitter négatif (-1ms) -> doit arrondir à 16h
                "2024-01-02T00:00:00.003000+00:00",  # jitter positif (+3ms)
            ],
            "funding_rate": [0.0001, 0.0002, 0.0003, 0.0004],
            "funding_interval_hours": [8.0, 8.0, 8.0, 8.0],
        }
    )
    funding_dir = tmp_path / "funding"
    funding_dir.mkdir()
    raw.to_csv(funding_dir / "X.csv.gz", index=False, compression="gzip")

    loaded = perp.load_funding(tmp_path, ["X"])
    series = loaded["X-PERP"]
    expected_index = pd.DatetimeIndex(
        ["2024-01-01 00:00:00", "2024-01-01 08:00:00", "2024-01-01 16:00:00", "2024-01-02 00:00:00"]
    )
    assert list(series.index) == list(expected_index)
    assert series.tolist() == pytest.approx([0.0001, 0.0002, 0.0003, 0.0004])
    # Aucune minute résiduelle après arrondi (cf. docstring backtest/perp.py -- vérifié aussi
    # empiriquement sur les 30 symboles réels).
    assert all(ts.minute == 0 and ts.second == 0 for ts in series.index)


def test_align_funding_to_calendar_orphan_settlement_reported_and_ignored():
    """Un règlement dont la bougie `H-1h` est absente du calendrier fourni doit être EXCLU de
    la matrice alignée (0.0 sur toutes les bougies du calendrier) et compté dans
    `funding_orphans`/`funding_alignment_report` -- jamais silencieusement perdu sans trace."""
    cal = pd.date_range("2024-01-01 01:00:00", periods=5, freq="h")  # démarre à 01:00
    funding_raw = {
        "X-PERP": pd.Series(
            [0.001, 0.002],
            index=pd.DatetimeIndex(["2024-01-01 01:00:00", "2024-01-01 03:00:00"]),
        )
        # Le règlement de 01:00 vise la bougie 00:00 (H-1h), ABSENTE de `cal` -> orphelin.
        # Celui de 03:00 vise la bougie 02:00, présente -> aligné normalement.
    }
    aligned, orphans = perp.align_funding_to_calendar(funding_raw, cal)
    assert aligned.loc["2024-01-01 02:00:00", "X-PERP"] == pytest.approx(0.002)
    assert float(aligned.sum().sum()) == pytest.approx(0.002)  # le règlement orphelin n'est nulle part
    report = perp.funding_alignment_report(orphans)
    assert report["X-PERP"] == 1
    assert report["total"] == 1


# ------------------------------------------------------------------------------------------
# Intégration données réelles (BTC spot + BTC-PERP, 2024-01 -> 2024-03) -- skip si absent
# ------------------------------------------------------------------------------------------


@pytest.mark.skipif(not _DATA_DIR.exists(), reason="_data/ absent -- test d'intégration données réelles ignoré")
def test_real_data_btc_covered_pair_funding_matches_rate_times_notional():
    """BTC spot long / BTC-PERP short, notionnel CONSTANT, sur 2024-01 -> 2024-03 (données
    réelles) : le funding total encaissé doit être proche de `Σ rate_t × notionnel` (tolérance
    large pour absorber l'arrondi discret en actions/shares d'une bougie à l'autre). Publie
    aussi `n_orphelins=0` sur cette fenêtre (cf. `backtest/perp.py`, mesuré empiriquement)."""
    from backtest import data_hourly

    # Calendrier COMPLET (natif, pas tronqué) : aligner le funding dessus, puis ne simuler que
    # la sous-fenêtre 2024-01 -> 2024-03 via `start_idx`/`end_idx` (même convention que
    # `generate_walk_forward_windows`/`simulate_segment` partout ailleurs dans ce moteur) --
    # tronquer le CALENDRIER avant alignement créerait un orphelin artificiel de bord de
    # fenêtre (le tout premier règlement de la fenêtre viserait une bougie juste AVANT le début
    # arbitraire de la fenêtre), qui ne reflète aucun vrai trou de données, cf. docstring de
    # `backtest/perp.py` -- ce n'est pas ainsi que ce nombre doit se lire.
    spot_raw = data_hourly.load_universe_raw(_DATA_DIR / "crypto", ["BTC"])
    perp_raw = perp.load_perp_klines(_DATA_DIR, ["BTC"])
    calendar_full = data_hourly.build_calendar({**spot_raw, **perp_raw})

    spot_aligned = data_hourly.align_to_calendar(spot_raw["BTC"], calendar_full)
    perp_aligned = data_hourly.align_to_calendar(perp_raw["BTC-PERP"], calendar_full)
    opens = pd.DataFrame({"BTC": spot_aligned["open"], "BTC-PERP": perp_aligned["open"]})
    closes = pd.DataFrame({"BTC": spot_aligned["close"], "BTC-PERP": perp_aligned["close"]})
    highs = pd.DataFrame({"BTC-PERP": perp_aligned["high"]})
    lows = pd.DataFrame({"BTC-PERP": perp_aligned["low"]})

    funding_raw = perp.load_funding(_DATA_DIR, ["BTC"])
    funding_aligned, orphans = perp.align_funding_to_calendar(funding_raw, calendar_full)

    start_idx = int(np.searchsorted(calendar_full.values, pd.Timestamp("2024-01-01").to_datetime64()))
    end_idx = int(np.searchsorted(calendar_full.values, pd.Timestamp("2024-04-01").to_datetime64())) - 1
    window_orphans = orphans[(orphans.index >= calendar_full[start_idx - 1]) & (orphans.index <= calendar_full[end_idx])]
    report = perp.funding_alignment_report(window_orphans)
    assert report["total"] == 0, f"orphelins inattendus sur la fenêtre d'intégration : {report}"

    w = pd.DataFrame({"BTC": 0.3, "BTC-PERP": -0.3}, index=calendar_full)

    seg = engine.simulate_segment(
        calendar_full, w, opens, closes, start_idx, end_idx, cost_bps=5.0,
        no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"BTC-PERP"}, funding=funding_aligned, highs=highs, lows=lows, perp_cost_bps=5.0,
    )

    # Notionnel perp approximativement constant (couverture 30% d'une équity qui bouge peu vu
    # les coûts modérés) -- on le mesure directement bougie par bougie plutôt que de le
    # supposer fixe, pour un calcul de référence honnête.
    shares_perp = (
        w["BTC-PERP"].reindex(seg.dates) * seg.equity.shift(1).fillna(1.0)
    ) / opens["BTC-PERP"].reindex(seg.dates)
    approx_notional = (shares_perp.abs() * closes["BTC-PERP"].reindex(seg.dates)).to_numpy()
    rates = funding_aligned["BTC-PERP"].reindex(seg.dates).to_numpy()
    reference_funding = float(np.sum(approx_notional * rates))

    measured_funding = seg.pnl_breakdown["funding_received"]
    # Tolérance large (10% ou 1e-3 en absolu) : `approx_notional` est une RECONSTRUCTION
    # approximative (poids cible théorique, pas les shares perp réellement exécutées après
    # bande de non-négociation/arrondis) -- seule la cohérence D'ORDRE DE GRANDEUR et de SIGNE
    # est attendue ici, pas une égalité exacte.
    assert measured_funding != 0.0
    assert abs(measured_funding - reference_funding) <= max(1e-3, 0.10 * abs(reference_funding))
