"""backtest/tests/test_funding_carry.py — preuves de `backtest/strategies/funding_carry.py`
(`backtest/results/funding_carry_6majors/SPEC.md`). Fixtures synthétiques, rapides,
déterministes : causalité, annualisation exacte, hystérésis, poids exacts, warm-up flat."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.strategies import funding_carry as fc


def _hourly_calendar(n: int, start: str = "2024-01-01"):
    return pd.date_range(start, periods=n, freq="h")


def _make_matrices(n: int, symbols, start: str = "2024-01-01", price: float = 100.0):
    cal = _hourly_calendar(n, start)
    spot_closes = pd.DataFrame({s: price for s in symbols}, index=cal)
    perp_cols = [fc.bt_perp.perp_column_name(s) for s in symbols]
    perp_closes = pd.DataFrame({c: price for c in perp_cols}, index=cal)
    funding = pd.DataFrame(0.0, index=cal, columns=perp_cols)
    return cal, spot_closes, perp_closes, funding


# ------------------------------------------------------------------------------------------
# (a) Causalité : modifier le funding APRÈS t ne doit pas changer weights_decided <= t
# ------------------------------------------------------------------------------------------


def test_causality_future_funding_does_not_affect_past_weights():
    n = 24 * 40  # 40 jours, largement > D=7 et D=30
    symbols = ["X"]
    cal, spot_closes, perp_closes, funding = _make_matrices(n, symbols)

    rng = np.random.default_rng(0)
    funding_a = funding.copy()
    funding_a.iloc[: n // 2] = rng.normal(0.0002, 0.0005, size=(n // 2, 1))

    funding_b = funding_a.copy()
    # Modification massive du funding APRÈS le point de coupure (moitié de la série) --
    # devrait n'avoir absolument aucun effet sur les poids décidés AVANT ce point.
    cutoff = n // 2
    funding_b.iloc[cutoff:] = 10.0  # valeur absurde, largement au-dessus de tout theta_in

    params = fc.FundingCarryParams(window_days=7, theta_in=0.05)
    w_a = fc.generate_weight_decisions(spot_closes, perp_closes, funding_a, symbols, params)
    w_b = fc.generate_weight_decisions(spot_closes, perp_closes, funding_b, symbols, params)

    # `funding_a`/`funding_b` sont identiques STRICTEMENT avant `cutoff` -- mais le signal à
    # l'instant `cutoff - 1` dépend d'une fenêtre glissante de `window_days` jours en arrière,
    # qui ne touche jamais `funding.iloc[cutoff:]` : les poids doivent coïncider EXACTEMENT
    # jusqu'à `cutoff - 1` inclus.
    pd.testing.assert_frame_equal(w_a.iloc[:cutoff], w_b.iloc[:cutoff])


def test_causality_carry_ann_window_never_uses_future_rows():
    """Vérification directe et indépendante de `compute_carry_ann` : décaler UNIQUEMENT un
    règlement situé APRÈS `t` ne doit rien changer à `carry_ann.loc[t]`."""
    n = 24 * 20
    symbols = ["X"]
    perp_cols = [fc.bt_perp.perp_column_name(s) for s in symbols]
    cal = _hourly_calendar(n)
    funding = pd.DataFrame(0.0, index=cal, columns=perp_cols)
    t_idx = 24 * 10  # bougie de référence, bien après le warm-up de 7 jours

    funding_before = funding.copy()
    funding_before.iloc[t_idx] = 0.001  # réglé exactement à t (compté, borne droite incluse)

    funding_after = funding_before.copy()
    funding_after.iloc[t_idx + 1] = 5.0  # réglé une heure APRÈS t -- ne doit rien changer à t

    carry_before = fc.compute_carry_ann(funding_before, window_days=7)
    carry_after = fc.compute_carry_ann(funding_after, window_days=7)

    assert carry_before.iloc[t_idx, 0] == pytest.approx(carry_after.iloc[t_idx, 0])
    assert not carry_before.iloc[: t_idx + 1].equals(carry_after.iloc[: t_idx + 1]) is False


# ------------------------------------------------------------------------------------------
# (b) Annualisation exacte sur un cas synthétique
# ------------------------------------------------------------------------------------------


def test_annualization_exact_on_synthetic_case():
    """Deux règlements de 0.0004 chacun dans une fenêtre de 7 jours -> carry_ann =
    (0.0004+0.0004) * 365/7, calculé à la main."""
    n = 24 * 15
    symbols = ["X"]
    perp_cols = [fc.bt_perp.perp_column_name(s) for s in symbols]
    cal = _hourly_calendar(n)
    funding = pd.DataFrame(0.0, index=cal, columns=perp_cols)

    # Deux règlements dans les 7 derniers jours avant t = bougie n°239 (jour 9, largement après
    # le warm-up de 7 jours = 168 bougies).
    t_idx = 24 * 10 - 1  # jour 10, dernière heure
    funding.iloc[t_idx - 24 * 2] = 0.0004  # 2 jours avant t -- dans la fenêtre de 7 jours
    funding.iloc[t_idx - 24 * 5] = 0.0004  # 5 jours avant t -- dans la fenêtre de 7 jours
    funding.iloc[t_idx - 24 * 8] = 0.0004  # 8 jours avant t -- HORS fenêtre de 7 jours (exclu)

    carry = fc.compute_carry_ann(funding, window_days=7)
    expected = (0.0004 + 0.0004) * 365.0 / 7.0
    assert carry.iloc[t_idx, 0] == pytest.approx(expected, rel=1e-12)


def test_annualization_exact_window_30_days():
    n = 24 * 45
    symbols = ["X"]
    perp_cols = [fc.bt_perp.perp_column_name(s) for s in symbols]
    cal = _hourly_calendar(n)
    funding = pd.DataFrame(0.0, index=cal, columns=perp_cols)

    t_idx = 24 * 40
    rates_in_window = [0.0001, 0.0002, -0.00005]
    offsets_days = [1, 10, 29]  # tous <= 30 jours avant t
    for rate, off in zip(rates_in_window, offsets_days):
        funding.iloc[t_idx - 24 * off] = rate
    # Un règlement juste hors fenêtre (31 jours avant t) -- ne doit pas compter.
    funding.iloc[t_idx - 24 * 31] = 0.5

    carry = fc.compute_carry_ann(funding, window_days=30)
    expected = sum(rates_in_window) * 365.0 / 30.0
    assert carry.iloc[t_idx, 0] == pytest.approx(expected, rel=1e-9)


# ------------------------------------------------------------------------------------------
# (c) Hystérésis : entrée à 0.11 (theta_in=0.10), maintien à 0.07, sortie à 0.04
# ------------------------------------------------------------------------------------------


def test_hysteresis_entry_hold_exit_exact_sequence():
    """Reconstruit `carry_ann` directement (bypass du calcul par funding réel, on force les
    valeurs) pour tester la machine à états isolément avec exactement la séquence de la
    mission : entrée à 0.11 (> theta_in=0.10), maintien à 0.07 (entre theta_out=0.05 et
    theta_in=0.10 -- état conservé), sortie à 0.04 (< theta_out=0.05)."""
    symbols = ["X"]
    cal = _hourly_calendar(5)
    carry_values = [0.0, 0.11, 0.07, 0.07, 0.04]
    carry_ann = pd.DataFrame({"X": carry_values}, index=cal)

    theta_in = 0.10
    theta_out = 0.05
    entry_signal = carry_ann > theta_in
    exit_signal = carry_ann < theta_out
    available = pd.DataFrame(True, index=cal, columns=symbols)

    positions = fc._positions_from_carry(entry_signal, exit_signal, available)
    # t=0 (carry=0.0 < theta_in) -> flat ; t=1 (0.11 > 0.10) -> ENTREE ; t=2,3 (0.07, entre les
    # deux seuils) -> MAINTIEN (toujours actif) ; t=4 (0.04 < 0.05) -> SORTIE.
    assert positions["X"].tolist() == [False, True, True, True, False]


def test_hysteresis_via_full_generate_weight_decisions():
    """Même séquence que ci-dessus, mais via `generate_weight_decisions` de bout en bout. Le
    funding est construit pour que `carry_ann` vaille EXACTEMENT la séquence cible à 5 bougies
    consécutives bien après le warm-up : comme les 5 bougies sont à quelques heures d'écart
    (largement < 7 jours), la fenêtre glissante de `t_k` contient TOUS les règlements posés à
    `base..k` (rien n'en sort encore) -- on pose donc des règlements INCRÉMENTAUX (différences
    successives de la cible), pas la cible elle-même à chaque bougie."""
    n = 24 * 10 + 5
    symbols = ["X"]
    perp_cols = [fc.bt_perp.perp_column_name(s) for s in symbols]
    cal, spot_closes, perp_closes, funding = _make_matrices(n, symbols, start="2024-01-01")

    base = 24 * 8  # jour 8, après warm-up de 7 jours
    targets = [0.0, 0.11, 0.07, 0.07, 0.04]
    diffs = [targets[0]] + [targets[k] - targets[k - 1] for k in range(1, len(targets))]
    for k, diff in enumerate(diffs):
        rate = diff * 7.0 / 365.0
        funding.iloc[base + k, funding.columns.get_loc(perp_cols[0])] = rate

    theta_in = 0.10
    params = fc.FundingCarryParams(window_days=7, theta_in=theta_in)
    weights = fc.generate_weight_decisions(spot_closes, perp_closes, funding, symbols, params)

    observed = weights["X"].iloc[base : base + 5].tolist()
    expected_active = [False, True, True, True, False]
    expected_weights = [fc.WEIGHT_PER_SYMBOL if a else 0.0 for a in expected_active]
    assert observed == pytest.approx(expected_weights)
    perp_observed = weights[perp_cols[0]].iloc[base : base + 5].tolist()
    expected_perp = [-fc.WEIGHT_PER_SYMBOL if a else 0.0 for a in expected_active]
    assert perp_observed == pytest.approx(expected_perp)


# ------------------------------------------------------------------------------------------
# (d) Poids exacts (+0.10 spot / -0.10 perp) quand actif, 0/0 quand flat
# ------------------------------------------------------------------------------------------


def test_exact_weights_active_and_flat():
    n = 24 * 20
    symbols = ["X", "Y"]
    perp_cols = [fc.bt_perp.perp_column_name(s) for s in symbols]
    cal, spot_closes, perp_closes, funding = _make_matrices(n, symbols)

    # X : funding constant dès le début (0.0001/règlement, une "ligne" = un règlement horaire
    # ici, peu réaliste mais neutre pour le test) -> une fois le warm-up de 7 jours passé, la
    # fenêtre glissante contient 168 règlements de 0.0001 -> carry_ann = 168*0.0001*365/7 =
    # 0.876, largement > theta_in=0.05 -- actif en permanence après warm-up.
    # Y : funding nul -> reste flat en permanence.
    funding.loc[:, perp_cols[0]] = 0.0001

    params = fc.FundingCarryParams(window_days=7, theta_in=0.05)
    weights = fc.generate_weight_decisions(spot_closes, perp_closes, funding, symbols, params)

    assert set(weights["X"].unique()) <= {0.0, fc.WEIGHT_PER_SYMBOL}
    assert (weights["Y"] == 0.0).all()
    assert (weights[perp_cols[1]] == 0.0).all()
    active_mask = weights["X"] > 0
    assert np.allclose(weights.loc[active_mask, "X"].to_numpy(), fc.WEIGHT_PER_SYMBOL)
    assert np.allclose(weights.loc[active_mask, perp_cols[0]].to_numpy(), -fc.WEIGHT_PER_SYMBOL)
    flat_mask = ~active_mask
    assert (weights.loc[flat_mask, perp_cols[0]] == 0.0).all()
    # Jamais de NaN nulle part (SPEC.md §"Signal").
    assert not weights.isna().any().any()


# ------------------------------------------------------------------------------------------
# (e) Warm-up flat (fenêtre D incomplète depuis le tout début de la série fournie)
# ------------------------------------------------------------------------------------------


def test_warmup_flat_before_full_window_available():
    n = 24 * 10
    symbols = ["X"]
    perp_cols = [fc.bt_perp.perp_column_name(s) for s in symbols]
    cal, spot_closes, perp_closes, funding = _make_matrices(n, symbols)
    # Funding énorme dès la première bougie -- si le warm-up n'était pas respecté, la stratégie
    # entrerait en position dès que la fenêtre glissante produit une valeur non nulle.
    funding.iloc[0, funding.columns.get_loc(perp_cols[0])] = 1.0

    params = fc.FundingCarryParams(window_days=7, theta_in=0.05)
    weights = fc.generate_weight_decisions(spot_closes, perp_closes, funding, symbols, params)

    warmup_cutoff = cal[0] + pd.Timedelta(days=7)
    warmup_rows = weights.index < warmup_cutoff
    assert (weights.loc[warmup_rows, "X"] == 0.0).all()
    assert (weights.loc[warmup_rows, perp_cols[0]] == 0.0).all()
    assert not weights.isna().any().any()


def test_carry_ann_is_nan_during_warmup_not_partial_sum():
    n = 24 * 10
    symbols = ["X"]
    perp_cols = [fc.bt_perp.perp_column_name(s) for s in symbols]
    cal = _hourly_calendar(n)
    funding = pd.DataFrame(0.0, index=cal, columns=perp_cols)
    funding.iloc[0] = 1.0

    carry = fc.compute_carry_ann(funding, window_days=7)
    warmup_cutoff = cal[0] + pd.Timedelta(days=7)
    warmup_rows = carry.index < warmup_cutoff
    assert carry.loc[warmup_rows, perp_cols[0]].isna().all()
    assert not carry.loc[~warmup_rows, perp_cols[0]].isna().any()


# ------------------------------------------------------------------------------------------
# (f) Symbole indisponible (perp/funding/spot NaN) -> flat forcé, jamais de NaN en sortie
# ------------------------------------------------------------------------------------------


def test_symbol_unavailable_forces_flat_and_resets_state():
    n = 24 * 20
    symbols = ["X"]
    perp_cols = [fc.bt_perp.perp_column_name(s) for s in symbols]
    cal, spot_closes, perp_closes, funding = _make_matrices(n, symbols)

    base = 24 * 9
    # Funding fort et constant pendant toute la seconde moitié -> actif à `base` sans le trou.
    funding.loc[cal[base - 24 * 6] :, perp_cols[0]] = 0.10

    # Trou de données perp (NaN) exactement à `base` et `base+1`.
    perp_closes.iloc[base : base + 2, perp_closes.columns.get_loc(perp_cols[0])] = np.nan

    params = fc.FundingCarryParams(window_days=7, theta_in=0.05)
    weights = fc.generate_weight_decisions(spot_closes, perp_closes, funding, symbols, params)

    assert weights["X"].iloc[base] == 0.0
    assert weights[perp_cols[0]].iloc[base] == 0.0
    assert weights["X"].iloc[base + 1] == 0.0
    # Donnée revenue à `base+2` : le signal est toujours largement au-dessus de theta_in ->
    # nouvelle entrée franche (l'état a bien repris, pas resté bloqué à flat indéfiniment).
    assert weights["X"].iloc[base + 2] == pytest.approx(fc.WEIGHT_PER_SYMBOL)
    assert not weights.isna().any().any()


def test_no_nan_ever_in_output_random_funding():
    """Test de robustesse global : funding aléatoire (avec quelques NaN explicites), aucune
    combinaison de la grille ne doit jamais produire de NaN dans `weights_decided`."""
    rng = np.random.default_rng(42)
    n = 24 * 60
    symbols = ["BTC", "ETH"]
    perp_cols = [fc.bt_perp.perp_column_name(s) for s in symbols]
    cal, spot_closes, perp_closes, funding = _make_matrices(n, symbols)
    funding.loc[:, :] = rng.normal(0.0001, 0.001, size=(n, len(perp_cols)))
    # Quelques trous explicites.
    funding.iloc[100:105, 0] = np.nan
    perp_closes.iloc[200:210, 0] = np.nan

    for combo in fc.PARAM_GRID:
        params = fc.FundingCarryParams(**combo)
        weights = fc.generate_weight_decisions(spot_closes, perp_closes, funding, symbols, params)
        assert not weights.isna().any().any(), f"NaN produit pour params={combo}"
        # Poids toujours dans {0, +-0.10}.
        for s in symbols:
            vals = set(weights[s].unique())
            assert vals <= {0.0, fc.WEIGHT_PER_SYMBOL}
        for c in perp_cols:
            vals = set(weights[c].unique())
            assert vals <= {0.0, -fc.WEIGHT_PER_SYMBOL}
