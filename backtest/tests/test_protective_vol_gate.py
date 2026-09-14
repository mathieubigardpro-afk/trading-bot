"""backtest/tests/test_protective_vol_gate.py — tests synthétiques de la COUCHE STRATÉGIE
(`backtest/strategies/protective_vol_gate.py`), jamais de données réelles ici (cf. `backtest/
tests/test_vol_breakout.py` pour la même convention). Garanties couvertes, directement issues
de `backtest/results/protective_vol_gate_6majors/SPEC.md` :

  1. Causalité : perturber les closes STRICTEMENT APRÈS `t` ne change AUCUN poids `<= t`.
  2. Machine à états : un spike de vol synthétique déclenche la coupure et le redéploiement
     linéaire en 72h après retour sous `p_out`.
  3. Warm-up (< 2160 observations de vol valides) -> état investi (`g=1`).
  4. Re-coupure pendant la rampe de redéploiement -> retour immédiat à `g=0`.
  5. Poids = `g/6` exactement, long-only, somme `<= 1`.
  6. `PARAM_GRID` a exactement 4 combinaisons conformes à la SPEC (V in {24,72} x p_in in
     {0.95,0.98}).
  7. Garde anti « equity curve trading » : la signature du module n'accepte que
     `closes`/`params`, jamais d'équity/PnL/positions.
"""

from __future__ import annotations

import inspect

import numpy as np
import pandas as pd
import pytest

from backtest.strategies import protective_vol_gate as pvg


# ------------------------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------------------------


def _flat_then_spike_closes(n: int = 3000, seed: int = 1) -> pd.DataFrame:
    """Panier de 2 symboles, calme (faible vol) longtemps (pour dépasser le warm-up de 2160
    observations de vol valides), puis un spike de vol synthétique bref, puis retour au calme
    (pour observer coupure -> redéploiement)."""
    rng = np.random.default_rng(seed)
    quiet1 = 100 + np.cumsum(rng.normal(0, 0.01, 2300))
    spike = 100 + np.cumsum(rng.normal(0, 3.0, 50))  # vol très supérieure au calme
    quiet2 = spike[-1] + np.cumsum(rng.normal(0, 0.01, n - 2300 - 50))
    close_a = np.concatenate([quiet1, spike, quiet2])[:n]
    close_b = close_a * (1.0 + rng.normal(0, 0.001, n))  # quasi identique, panier stable
    idx = pd.date_range("2022-01-01", periods=n, freq="h")
    return pd.DataFrame({"A": close_a, "B": close_b}, index=idx)


_SMALL_PARAMS = pvg.ProtectiveVolGateParams(vol_window_hours=24, p_in=0.95)


# ------------------------------------------------------------------------------------------
# 1. Causalité
# ------------------------------------------------------------------------------------------


def test_causality_future_perturbation_does_not_change_past_weights():
    closes = _flat_then_spike_closes(n=3000, seed=5)
    params = _SMALL_PARAMS

    weights_original = pvg.generate_weight_decisions(closes, params)

    t_cut = 2500
    perturbed = closes.copy()
    rng = np.random.default_rng(99)
    perturbed.iloc[t_cut + 1 :] = perturbed.iloc[t_cut + 1 :] * (
        1.0 + rng.normal(0, 0.3, size=perturbed.iloc[t_cut + 1 :].shape)
    )
    weights_perturbed = pvg.generate_weight_decisions(perturbed, params)

    pd.testing.assert_frame_equal(
        weights_original.iloc[: t_cut + 1], weights_perturbed.iloc[: t_cut + 1]
    )
    # Contrôle de non-trivialité : la perturbation doit réellement changer quelque chose après
    # t_cut, sinon le test passerait même avec un calcul non causal qui ignorerait le futur.
    assert not weights_original.iloc[t_cut + 1 :].equals(weights_perturbed.iloc[t_cut + 1 :])


def test_causality_run_state_machine_pure_function_of_past_percentile():
    """Le coeur de la machine à états (`run_state_machine`) ne doit dépendre, à `t`, que de
    `percentile[<=t]` -- vérifié directement en tronquant le tableau et en comparant le préfixe
    des résultats, jamais en passant par `generate_weight_decisions`."""
    rng = np.random.default_rng(7)
    n = 500
    percentile = rng.uniform(0.0, 1.0, n)
    percentile[:100] = np.nan  # warm-up

    state_full, g_full = pvg.run_state_machine(percentile, p_in=0.95, p_out=0.80, r_hours=72)
    t_cut = 300
    state_prefix, g_prefix = pvg.run_state_machine(percentile[: t_cut + 1], p_in=0.95, p_out=0.80, r_hours=72)

    np.testing.assert_array_equal(state_full[: t_cut + 1], state_prefix)
    np.testing.assert_allclose(g_full[: t_cut + 1], g_prefix)


# ------------------------------------------------------------------------------------------
# 2. Machine à états : coupure + redéploiement linéaire sur 72h
# ------------------------------------------------------------------------------------------


def test_state_machine_cut_then_linear_redeploy_over_72h():
    """Percentile construit à la main : calme (percentile bas) longtemps -> spike (percentile
    >= p_in, coupure immédiate) -> retour calme (percentile <= p_out, redéploiement linéaire
    sur 72h)."""
    p_in, p_out, r_hours = 0.95, 0.80, 72
    n = 300
    percentile = np.full(n, 0.50)  # calme : jamais de transition
    spike_t = 150
    percentile[spike_t] = 0.99  # >= p_in -> coupure immédiate à spike_t
    calm_t = spike_t + 20
    percentile[spike_t + 1 : calm_t] = 0.90  # entre p_out et p_in : reste protégé
    percentile[calm_t:] = 0.10  # <= p_out dès calm_t -> déclenche le redéploiement

    state, g = pvg.run_state_machine(percentile, p_in=p_in, p_out=p_out, r_hours=r_hours)

    # Coupure IMMÉDIATE à spike_t (pas de rampe à la coupure, SPEC.md §3.4).
    assert state[spike_t] == pvg.STATE_PROTECTED
    assert g[spike_t] == 0.0
    # Reste protégé (g=0) tant que percentile > p_out.
    assert (g[spike_t : calm_t] == 0.0).all()
    assert (state[spike_t:calm_t] == pvg.STATE_PROTECTED).all()

    # Redéploiement : g=0 à l'instant du déclenchement (calm_t, interprétation §2 de la
    # docstring module), puis incréments de 1/72 par heure, g==1.0 exactement 72h plus tard.
    assert state[calm_t] == pvg.STATE_REDEPLOY
    assert g[calm_t] == 0.0
    for k in range(1, r_hours):
        expected = k / r_hours
        assert g[calm_t + k] == pytest.approx(expected), f"g[{calm_t + k}] incorrect"
        assert state[calm_t + k] == pvg.STATE_REDEPLOY
    assert g[calm_t + r_hours] == pytest.approx(1.0)
    assert state[calm_t + r_hours] == pvg.STATE_INVESTED
    # Reste investi après la fin de la rampe tant que percentile < p_in.
    assert (g[calm_t + r_hours :] == 1.0).all()


def test_end_to_end_spike_triggers_protection_then_full_redeployment():
    """Bout en bout sur `generate_weight_decisions` : le panier synthétique passe bien en
    protection (poids nuls) après le spike de vol, puis revient à pleine exposition (1/6) après
    la rampe de 72h une fois le calme revenu."""
    closes = _flat_then_spike_closes(n=3000, seed=1)
    params = pvg.ProtectiveVolGateParams(vol_window_hours=24, p_in=0.95)
    weights = pvg.generate_weight_decisions(closes, params)
    sg = pvg.generate_state_and_g(closes, params)

    # Un épisode de protection doit apparaître peu après le spike (vers l'indice 2300-2350).
    protected_mask = sg["state"].iloc[2300:2450] != pvg.STATE_INVESTED
    assert protected_mask.any(), "aucune protection déclenchée après le spike de vol synthétique"

    # Longtemps après le retour au calme (bien plus de 72h), la stratégie doit être repassée
    # pleinement investie (g=1, poids=1/6 par symbole).
    tail = weights.iloc[-50:]
    assert (tail["A"] == pvg.WEIGHT_PER_SYMBOL).all()
    assert (tail["B"] == pvg.WEIGHT_PER_SYMBOL).all()


# ------------------------------------------------------------------------------------------
# 3. Warm-up : < 2160 observations de vol valides -> investi (g=1)
# ------------------------------------------------------------------------------------------


def test_warmup_below_min_valid_vol_obs_forces_invested():
    n = pvg.MIN_VALID_VOL_OBS - 10  # strictement sous le minimum requis, pour TOUTE la série
    idx = pd.date_range("2022-01-01", periods=n, freq="h")
    rng = np.random.default_rng(3)
    # Volatilité délibérément extrême pour vérifier que même un choc massif ne déclenche AUCUNE
    # protection tant que l'historique de vol est insuffisant (SPEC.md §3.3, explicite).
    close = 100 + np.cumsum(rng.normal(0, 5.0, n))
    closes = pd.DataFrame({"A": close, "B": close * 1.001}, index=idx)

    params = pvg.ProtectiveVolGateParams(vol_window_hours=24, p_in=0.95)
    weights = pvg.generate_weight_decisions(closes, params)
    sg = pvg.generate_state_and_g(closes, params)

    assert (sg["percentile"].isna()).all(), "le percentile ne devrait jamais être défini ici"
    assert (sg["state"] == pvg.STATE_INVESTED).all()
    assert (sg["g"] == 1.0).all()
    assert (weights["A"] == pvg.WEIGHT_PER_SYMBOL).all()
    assert (weights["B"] == pvg.WEIGHT_PER_SYMBOL).all()
    assert not weights.isna().any().any()


def test_run_state_machine_nan_percentile_forces_invested_even_mid_stream():
    """La règle warm-up s'applique à CHAQUE `t` où `percentile[t]` est `NaN`, pas seulement au
    tout début de la série (un `NaN` qui réapparaîtrait plus tard doit aussi forcer investi)."""
    percentile = np.array([0.99, 0.99, np.nan, 0.99, 0.10])
    state, g = pvg.run_state_machine(percentile, p_in=0.95, p_out=0.80, r_hours=72)
    assert state[0] == pvg.STATE_PROTECTED and g[0] == 0.0
    assert state[1] == pvg.STATE_PROTECTED and g[1] == 0.0
    assert state[2] == pvg.STATE_INVESTED and g[2] == 1.0  # NaN -> forcé investi
    assert state[3] == pvg.STATE_PROTECTED and g[3] == 0.0  # repart de investi -> coupure


# ------------------------------------------------------------------------------------------
# 4. Re-coupure pendant la rampe -> retour immédiat à g=0
# ------------------------------------------------------------------------------------------


def test_recut_during_ramp_returns_immediately_to_zero():
    p_in, p_out, r_hours = 0.95, 0.80, 72
    n = 200
    # Baseline STRICTEMENT ENTRE p_out et p_in (0.85) : ni coupure ni redéploiement déclenchés
    # par défaut -- seuls les points explicitement fixés ci-dessous doivent provoquer une
    # transition (évite tout redéclenchement accidentel du redéploiement après la re-coupure,
    # puisque `percentile <= p_out` redéclencherait immédiatement un NOUVEAU redéploiement).
    percentile = np.full(n, 0.85)
    percentile[50] = 0.99  # coupure
    percentile[51:60] = 0.85  # reste protégé (entre p_out et p_in)
    percentile[60] = 0.10  # <= p_out -> déclenche le redéploiement
    percentile[61:70] = 0.85  # continue de ramper (ni p_out ni p_in)
    recut_t = 70
    percentile[recut_t] = 0.97  # >= p_in PENDANT la rampe -> re-coupure immédiate
    percentile[recut_t + 1 :] = 0.85  # reste protégé après la re-coupure (ni p_out ni p_in)

    state, g = pvg.run_state_machine(percentile, p_in=p_in, p_out=p_out, r_hours=r_hours)

    assert state[60] == pvg.STATE_REDEPLOY
    assert g[69] > 0.0, "la rampe doit avoir progressé avant la re-coupure"
    assert state[recut_t] == pvg.STATE_PROTECTED
    assert g[recut_t] == 0.0
    # Reste protégé tant que percentile > p_out après la re-coupure.
    assert state[recut_t + 1] == pvg.STATE_PROTECTED
    assert g[recut_t + 1] == 0.0


# ------------------------------------------------------------------------------------------
# 5. Poids = g/6 exactement, long-only, somme <= 1
# ------------------------------------------------------------------------------------------


def test_weights_equal_g_over_6_long_only_sum_le_1():
    closes = _flat_then_spike_closes(n=3000, seed=2)
    universe = ["A", "B"]
    # Univers étendu à 6 colonnes synthétiques pour vérifier la division par 6 SYMBOLES fixe
    # (WEIGHT_PER_SYMBOL = 1/6, SPEC.md §3.5), pas 1/len(closes.columns) dynamique.
    closes6 = closes.copy()
    for extra in ["C", "D", "E", "F"]:
        closes6[extra] = closes["A"] * (1.0 + 0.0001)
    params = pvg.ProtectiveVolGateParams(vol_window_hours=24, p_in=0.95)
    sg = pvg.generate_state_and_g(closes6, params)
    weights = pvg.generate_weight_decisions(closes6, params)

    assert pvg.WEIGHT_PER_SYMBOL == pytest.approx(1.0 / 6.0)
    for sym in closes6.columns:
        np.testing.assert_allclose(weights[sym].to_numpy(), (sg["g"] * pvg.WEIGHT_PER_SYMBOL).to_numpy())
    assert (weights.to_numpy() >= 0.0).all(), "long-only : jamais de poids négatif"
    row_sums = weights.sum(axis=1)
    assert (row_sums <= 1.0 + 1e-9).all(), "somme des poids jamais > 1 (g in [0,1], 6 x g/6 = g)"
    assert not weights.isna().any().any()


# ------------------------------------------------------------------------------------------
# 6. PARAM_GRID conforme à la SPEC
# ------------------------------------------------------------------------------------------


def test_param_grid_exactly_4_combos_matching_spec():
    assert len(pvg.PARAM_GRID) == 4
    expected = {
        (24, 0.95),
        (24, 0.98),
        (72, 0.95),
        (72, 0.98),
    }
    observed = {(c["vol_window_hours"], c["p_in"]) for c in pvg.PARAM_GRID}
    assert observed == expected
    for combo in pvg.PARAM_GRID:
        assert set(combo.keys()) == {"vol_window_hours", "p_in"}
    # Fixés (SPEC.md §4) : p_out=0.80, r_hours=72, pct_window_hours=4320, min_valid_vol_obs=2160.
    assert pvg.P_OUT == 0.80
    assert pvg.R_HOURS == 72
    assert pvg.PCT_WINDOW_HOURS == 4320
    assert pvg.MIN_VALID_VOL_OBS == 2160


# ------------------------------------------------------------------------------------------
# 7. Garde anti « equity curve trading »
# ------------------------------------------------------------------------------------------


def test_signature_anti_equity_curve_trading():
    """Garde structurelle (SPEC.md §1 : "vérifiable structurellement") : AUCUNE fonction
    PUBLIQUE du module ne doit accepter de paramètre d'équity/PnL/position/rendements de
    stratégie -- seulement des données de MARCHÉ (`closes`) et des paramètres de grille. Le
    scan du CODE SOURCE (hors docstrings, qui décrivent légitimement cette garde en prose) ne
    doit référencer aucun module de simulation qui exposerait ces quantités."""
    sig = inspect.signature(pvg.generate_weight_decisions)
    param_names = list(sig.parameters.keys())
    assert param_names == ["closes", "params"]

    sig2 = inspect.signature(pvg.generate_state_and_g)
    assert list(sig2.parameters.keys()) == ["closes", "params"]

    # Toute fonction publique exportée par `__all__` : aucun paramètre nommé
    # equity/pnl/position/returns_strategy/nav (seuls des noms de données de marché/paramètres
    # sont attendus : closes, params, returns [rendement de MARCHÉ], vol, percentile, ...).
    forbidden_param_names = {"equity", "pnl", "position", "positions", "portfolio_value", "nav", "strategy_returns"}
    for name in pvg.__all__:
        obj = getattr(pvg, name)
        if not inspect.isfunction(obj):
            continue
        sig_obj = inspect.signature(obj)
        bad = forbidden_param_names & set(sig_obj.parameters.keys())
        assert not bad, f"{name} accepte un paramètre interdit : {bad}"

    # Le module ne doit importer AUCUN module de simulation/moteur (qui exposerait de
    # l'équity/PnL/positions) -- seulement numpy/pandas/dataclasses/typing. Recherche limitée
    # aux lignes `import`/`from ... import` du code source (jamais les docstrings, qui décrivent
    # légitimement cette garde en prose).
    source = inspect.getsource(pvg)
    import_lines = [ln.strip() for ln in source.splitlines() if ln.strip().startswith(("import ", "from "))]
    forbidden_modules = ["backtest.engine", "backtest.risk_overlay", "backtest.metrics", "bot.risk", "bot.config"]
    for ln in import_lines:
        for mod in forbidden_modules:
            assert mod not in ln, f"import interdit trouvé dans le module stratégie : {ln!r}"
