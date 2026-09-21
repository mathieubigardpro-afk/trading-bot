"""backtest/tests/test_pairs_ratio.py — tests synthétiques de la COUCHE STRATÉGIE
(`backtest/strategies/pairs_ratio.py`), jamais de données réelles ici (cf. `backtest/tests/
test_protective_vol_gate.py` pour la même convention). Garanties couvertes, directement issues
de `backtest/results/pairs_ethbtc_ratio_rotation/SPEC.md` :

  1. Machine à états : entrée en tilt (NEUTRE -> TILT_BTC / TILT_ETH), hystérésis de sortie
     (`|z| <= theta/2` -> NEUTRE), bascule directe (TILT_BTC <-> TILT_ETH sans repasser par
     NEUTRE), warm-up (z NaN) -> NEUTRE forcé, STD nulle -> z traité comme 0.
  2. Causalité : perturber les closes STRICTEMENT APRÈS `t` ne change AUCUN poids `<= t`.
  3. Pas de sizing interne : poids in {0.0, 0.5, 1.0}, somme des poids == 1.0 exactement, à
     chaque ligne, long-only.
"""

from __future__ import annotations

import inspect

import numpy as np
import pandas as pd
import pytest

from backtest.strategies import pairs_ratio as pr


# ------------------------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------------------------


def _flat_ratio_closes(n: int = 3000, seed: int = 1) -> pd.DataFrame:
    """BTC/ETH synthétiques dont le LOG-RATIO oscille faiblement autour d'une constante (bruit
    blanc sur x) — assez de données pour dépasser le warm-up de n'importe quelle valeur de L
    testée dans ces fixtures (toujours < n)."""
    rng = np.random.default_rng(seed)
    btc = 100 + np.cumsum(rng.normal(0, 0.01, n))
    btc = 100 + np.abs(btc - btc.min()) + 50  # strictement positif
    noise = rng.normal(0, 0.001, n)
    eth = btc * np.exp(noise)  # log-ratio = noise, faible variance autour de 0
    idx = pd.date_range("2022-01-01", periods=n, freq="h")
    return pd.DataFrame({"BTC": btc, "ETH": eth}, index=idx)


_SMALL_PARAMS = pr.PairsRatioParams(l_hours=100, theta=1.5)


# ------------------------------------------------------------------------------------------
# 1. Machine à états
# ------------------------------------------------------------------------------------------


def test_state_machine_entry_tilt_btc_and_eth():
    theta = 1.5
    n = 50
    z = np.full(n, 0.0)
    z[10] = theta  # >= theta -> TILT_BTC (ratio ETH/BTC anormalement haut -> bascule vers BTC)
    z[11:20] = 0.9  # entre hystérésis (0.75) et theta : reste TILT_BTC
    z[25] = -theta  # <= -theta -> TILT_ETH depuis NEUTRE (après retour neutre entre les deux)
    z[20:25] = 0.0  # |z|<=theta/2 -> retour NEUTRE avant le test d'entrée TILT_ETH

    state = pr.run_state_machine(z, theta=theta)
    assert state[9] == pr.STATE_NEUTRAL
    assert state[10] == pr.STATE_TILT_BTC
    assert (state[10:20] == pr.STATE_TILT_BTC).all()
    assert state[20] == pr.STATE_NEUTRAL  # hystérésis déclenchée
    assert state[25] == pr.STATE_TILT_ETH


def test_state_machine_hysteresis_exit_band_exact():
    """Sortie EXACTEMENT à `|z| <= theta/2` (bord inclus des deux côtés), jamais avant."""
    theta = 2.0
    exit_band = theta / 2.0  # 1.0
    n = 10
    z = np.array([theta, 1.01, 1.01, exit_band, 0.0, -exit_band, -1.01, -1.01, -theta, 0.0])
    state = pr.run_state_machine(z, theta=theta)
    assert state[0] == pr.STATE_TILT_BTC
    assert state[1] == pr.STATE_TILT_BTC  # 1.01 > exit_band=1.0 -> reste tilté
    assert state[2] == pr.STATE_TILT_BTC
    assert state[3] == pr.STATE_NEUTRAL  # exactement à la borne -> hystérésis déclenchée
    assert state[4] == pr.STATE_NEUTRAL
    # Depuis NEUTRE : -exit_band = -1.0 < theta=2.0 en valeur absolue -> pas d'entrée, reste NEUTRE
    assert state[5] == pr.STATE_NEUTRAL
    assert state[6] == pr.STATE_NEUTRAL
    assert state[7] == pr.STATE_NEUTRAL
    assert state[8] == pr.STATE_TILT_ETH  # -theta atteint depuis NEUTRE -> entrée TILT_ETH
    assert state[9] == pr.STATE_NEUTRAL  # 0.0, |z|<=exit_band -> retour neutre


def test_state_machine_direct_switch_btc_to_eth_and_back():
    """TILT_BTC, `z_t <= -theta` -> bascule DIRECTE TILT_ETH (jamais un passage par NEUTRE),
    et symétriquement (SPEC.md §3.3, point 4)."""
    theta = 1.5
    z = np.array([theta, theta, -theta, -theta, theta])
    state = pr.run_state_machine(z, theta=theta)
    assert state[0] == pr.STATE_TILT_BTC
    assert state[1] == pr.STATE_TILT_BTC
    assert state[2] == pr.STATE_TILT_ETH  # bascule directe, sans jamais passer par NEUTRE
    assert state[3] == pr.STATE_TILT_ETH
    assert state[4] == pr.STATE_TILT_BTC  # bascule directe symétrique


def test_state_machine_warmup_nan_forces_neutral_even_mid_stream():
    """La règle warm-up (z NaN -> NEUTRE) s'applique à CHAQUE `t` où `z[t]` est NaN, pas
    seulement au tout début de la série (même garde que protective_vol_gate)."""
    theta = 1.5
    z = np.array([theta, theta, np.nan, theta, -theta])
    state = pr.run_state_machine(z, theta=theta)
    assert state[0] == pr.STATE_TILT_BTC
    assert state[1] == pr.STATE_TILT_BTC
    assert state[2] == pr.STATE_NEUTRAL  # NaN -> forcé NEUTRE, même en cours de route
    assert state[3] == pr.STATE_TILT_BTC  # repart de NEUTRE -> nouvelle entrée
    assert state[4] == pr.STATE_TILT_ETH  # bascule directe depuis TILT_BTC


def test_zscore_std_zero_treated_as_zero_not_nan():
    """`STD_L = 0` (fenêtre pleine, log-ratio parfaitement constant dessus) -> `z` traité comme
    `0` (SPEC.md §3.2) -- jamais NaN, jamais +-inf, distinct du warm-up (fenêtre pas encore
    pleine -> NaN, cf. test suivant)."""
    l_hours = 20
    n = 50
    # Log-ratio parfaitement constant sur toute la série -> STD_L=0 partout dès que la fenêtre
    # est pleine.
    idx = pd.date_range("2022-01-01", periods=n, freq="h")
    x = pd.Series(0.1234, index=idx)  # constante
    z = pr.zscore(x, l_hours)
    assert z.iloc[: l_hours - 1].isna().all(), "warm-up (fenêtre pas pleine) -> NaN, pas 0"
    assert (z.iloc[l_hours - 1 :] == 0.0).all(), "STD nulle (fenêtre pleine) -> z traité comme 0"


def test_zscore_min_periods_equals_l_full_window_required():
    l_hours = 30
    n = 100
    idx = pd.date_range("2022-01-01", periods=n, freq="h")
    rng = np.random.default_rng(4)
    x = pd.Series(np.cumsum(rng.normal(0, 0.01, n)), index=idx)
    z = pr.zscore(x, l_hours)
    assert z.iloc[: l_hours - 1].isna().all()
    assert z.iloc[l_hours - 1 :].notna().all()


def test_end_to_end_ratio_spike_triggers_tilt_then_returns_neutral():
    """Bout en bout sur `generate_weight_decisions` : un choc bref et ponctuel du log-ratio
    (ETH devient cher vs BTC) déclenche un tilt vers BTC, puis un retour à NEUTRE une fois le
    ratio revenu dans sa fourchette normale. Construction ENTIÈREMENT DÉTERMINISTE (log-ratio
    exactement nul avant/après un choc constant, jamais de bruit aléatoire) : une fois la
    fenêtre glissante de `L` heures entièrement sortie du choc, `STD_L` redevient EXACTEMENT nul
    (log-ratio de nouveau parfaitement constant sur toute la fenêtre) -> `z=0` par construction
    (SPEC.md §3.2) -> retour NEUTRE garanti (`|0| <= theta/2`), sans dépendre d'un tirage
    aléatoire qui pourrait franchir `theta` par hasard sur du bruit i.i.d. (le z-score d'un bruit
    i.i.d. franchit `1.5 sigma` ~13% du temps par pur hasard, ce qui rendrait une assertion "reste
    NEUTRE" statistiquement fragile plutôt que déterministe)."""
    n = 3000
    l_hours = 200
    theta = 1.5
    shock_start, shock_len = 2500, 10
    btc = np.full(n, 40000.0)
    log_ratio = np.zeros(n)
    log_ratio[shock_start : shock_start + shock_len] = 2.0  # choc ponctuel, retour exact à 0 après
    eth = btc * np.exp(log_ratio)
    idx = pd.date_range("2022-01-01", periods=n, freq="h")
    closes = pd.DataFrame({"BTC": btc, "ETH": eth}, index=idx)
    params = pr.PairsRatioParams(l_hours=l_hours, theta=theta)

    sg = pr.generate_state(closes, params)
    weights = pr.generate_weight_decisions(closes, params)

    # Un tilt doit apparaître peu après le choc (ratio anormalement haut -> TILT_BTC).
    tilt_mask = sg["state"].iloc[shock_start : shock_start + 60] == pr.STATE_TILT_BTC
    assert tilt_mask.any(), "aucun tilt déclenché après le choc synthétique du log-ratio"
    # Bien après que la fenêtre glissante de L heures soit entièrement sortie du choc
    # (shock_start + shock_len + l_hours), STD_L redevient exactement nul -> z=0 -> NEUTRE.
    window_clear_idx = shock_start + shock_len + l_hours
    assert window_clear_idx + 50 < n
    tail = weights.iloc[window_clear_idx + 10 : window_clear_idx + 50]
    assert (tail["BTC"] == pr.WEIGHT_NEUTRAL).all()
    assert (tail["ETH"] == pr.WEIGHT_NEUTRAL).all()


# ------------------------------------------------------------------------------------------
# 2. Causalité
# ------------------------------------------------------------------------------------------


def test_causality_future_perturbation_does_not_change_past_weights():
    closes = _flat_ratio_closes(n=3000, seed=5)
    params = _SMALL_PARAMS

    weights_original = pr.generate_weight_decisions(closes, params)

    t_cut = 2500
    perturbed = closes.copy()
    rng = np.random.default_rng(99)
    perturbed.iloc[t_cut + 1 :] = perturbed.iloc[t_cut + 1 :] * (
        1.0 + rng.normal(0, 0.3, size=perturbed.iloc[t_cut + 1 :].shape)
    )
    weights_perturbed = pr.generate_weight_decisions(perturbed, params)

    pd.testing.assert_frame_equal(
        weights_original.iloc[: t_cut + 1], weights_perturbed.iloc[: t_cut + 1]
    )
    # Contrôle de non-trivialité : la perturbation doit réellement changer quelque chose après
    # t_cut, sinon le test passerait même avec un calcul non causal qui ignorerait le futur.
    assert not weights_original.iloc[t_cut + 1 :].equals(weights_perturbed.iloc[t_cut + 1 :])


def test_causality_run_state_machine_pure_function_of_past_z():
    """Le coeur de la machine à états (`run_state_machine`) ne doit dépendre, à `t`, que de
    `z[<=t]` -- vérifié directement en tronquant le tableau et en comparant le préfixe des
    résultats, jamais en passant par `generate_weight_decisions`."""
    rng = np.random.default_rng(7)
    n = 500
    z = rng.uniform(-3.0, 3.0, n)
    z[:50] = np.nan  # warm-up

    state_full = pr.run_state_machine(z, theta=1.5)
    t_cut = 300
    state_prefix = pr.run_state_machine(z[: t_cut + 1], theta=1.5)

    np.testing.assert_array_equal(state_full[: t_cut + 1], state_prefix)


def test_causality_log_ratio_and_zscore_only_use_past_closes():
    closes = _flat_ratio_closes(n=1000, seed=2)
    x_full = pr.log_ratio(closes)
    z_full = pr.zscore(x_full, l_hours=100)

    t_cut = 600
    x_prefix = pr.log_ratio(closes.iloc[: t_cut + 1])
    z_prefix = pr.zscore(x_prefix, l_hours=100)

    pd.testing.assert_series_equal(x_full.iloc[: t_cut + 1], x_prefix, check_names=False)
    pd.testing.assert_series_equal(z_full.iloc[: t_cut + 1], z_prefix, check_names=False)


# ------------------------------------------------------------------------------------------
# 3. Pas de sizing interne : poids in {0, 0.5, 1}, somme == 1
# ------------------------------------------------------------------------------------------


def test_weights_in_allowed_set_sum_to_one_long_only():
    closes = _flat_ratio_closes(n=3000, seed=3)
    params = pr.PairsRatioParams(l_hours=200, theta=1.5)
    weights = pr.generate_weight_decisions(closes, params)

    allowed = {0.0, 0.5, 1.0}
    observed_btc = set(np.round(weights["BTC"].to_numpy(), 8))
    observed_eth = set(np.round(weights["ETH"].to_numpy(), 8))
    assert observed_btc <= allowed, f"poids BTC hors de {{0, 0.5, 1}} : {observed_btc - allowed}"
    assert observed_eth <= allowed, f"poids ETH hors de {{0, 0.5, 1}} : {observed_eth - allowed}"

    row_sums = weights.sum(axis=1)
    np.testing.assert_allclose(row_sums.to_numpy(), 1.0, atol=1e-9)
    assert (weights.to_numpy() >= 0.0).all(), "long-only : jamais de poids négatif"
    assert not weights.isna().any().any()
    assert list(weights.columns) == pr.UNIVERSE == ["BTC", "ETH"]


def test_param_grid_exactly_4_combos_matching_spec():
    assert len(pr.PARAM_GRID) == 4
    expected = {(720, 1.5), (720, 2.0), (2160, 1.5), (2160, 2.0)}
    observed = {(c["l_hours"], c["theta"]) for c in pr.PARAM_GRID}
    assert observed == expected
    for combo in pr.PARAM_GRID:
        assert set(combo.keys()) == {"l_hours", "theta"}
    assert pr.HYSTERESIS_FRAC == 0.5


# ------------------------------------------------------------------------------------------
# 4. Garde anti « equity curve trading »
# ------------------------------------------------------------------------------------------


def test_signature_anti_equity_curve_trading():
    sig = inspect.signature(pr.generate_weight_decisions)
    assert list(sig.parameters.keys()) == ["closes", "params"]

    sig2 = inspect.signature(pr.generate_state)
    assert list(sig2.parameters.keys()) == ["closes", "params"]

    forbidden_param_names = {"equity", "pnl", "position", "positions", "portfolio_value", "nav", "strategy_returns"}
    for name in pr.__all__:
        obj = getattr(pr, name)
        if not inspect.isfunction(obj):
            continue
        sig_obj = inspect.signature(obj)
        bad = forbidden_param_names & set(sig_obj.parameters.keys())
        assert not bad, f"{name} accepte un paramètre interdit : {bad}"

    source = inspect.getsource(pr)
    import_lines = [ln.strip() for ln in source.splitlines() if ln.strip().startswith(("import ", "from "))]
    forbidden_modules = ["backtest.engine", "backtest.risk_overlay", "backtest.metrics", "bot.risk", "bot.config"]
    for ln in import_lines:
        for mod in forbidden_modules:
            assert mod not in ln, f"import interdit trouvé dans le module stratégie : {ln!r}"
