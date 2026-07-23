"""Tests de `bot/strategies/quasi_passif_crypto.py` (SPEC : docs/config-strategies.json ->
crypto_quasi_passif_vol_targete, docs/SELECTION-FINALE.md §2).

Fixtures de prix construites (pas de réseau, pas de fixtures externes) :
  - actif sous SMA200 -> poids 0 ;
  - vol du panier doublée -> poids brut moitié (formule vol_target / vol_réalisée) ;
  - caps (par actif ET exposition brute) respectés ;
  - aucune utilisation de la bougie/jour en cours (pas de look-ahead) ;
  - stabilité : mêmes données -> mêmes poids (fonction pure, déterministe).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bot.strategies.quasi_passif_crypto import (
    REGIME_SMA_DAYS,
    SPEC_UNIVERSE_BY_WALLET,
    QuasiPassifCrypto,
    _daily_closes,
    _is_trend_on,
)

STRATEGY = QuasiPassifCrypto()

# Point de départ arbitraire, aligné sur minuit UTC, pour que N*24 bougies horaires forment
# exactement N jours calendaires COMPLETS (00h..23h) sans jour partiel involontaire.
START = pd.Timestamp("2024-01-01T00:00:00Z")


def _profile(wallet_id: str, universe, vol_target, gross_exposure_max, cap_per_asset, halflife=60.0):
    return {
        "id": wallet_id,
        "univers_crypto": list(universe),
        "risque": {
            "vol_target_annualized": vol_target,
            "gross_exposure_max": gross_exposure_max,
            "cap_per_asset": cap_per_asset,
            "vol_ewma_halflife_hours": halflife,
        },
    }


def _hourly_history_from_returns(returns: np.ndarray, start_price: float = 100.0, start=START) -> pd.DataFrame:
    """DataFrame `close` (+ open/high/low/volume triviaux) indexé par heure d'ouverture UTC
    croissante, à partir d'une série de rendements horaires déjà construite (permet un
    contrôle exact des propriétés — trend, vol — testées)."""
    prices = start_price * np.cumprod(1.0 + returns)
    idx = pd.date_range(start=start, periods=len(prices), freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": prices, "high": prices, "low": prices, "close": prices, "volume": 1.0},
        index=idx,
    )


def _trending_returns(n_hours: int, drift_per_hour: float, vol_per_hour: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(loc=drift_per_hour, scale=vol_per_hour, size=n_hours)


def make_complete_days_history(
    n_days: int,
    drift_per_hour: float,
    vol_per_hour: float,
    seed: int,
    start_price: float = 100.0,
    start=START,
) -> pd.DataFrame:
    """`n_days` jours calendaires COMPLETS (24 bougies chacun), aucune heure du jour "courant"
    -- reproduit exactement ce que `bot.feeds.get_history()` retournerait juste avant le
    premier cycle horaire suivant minuit UTC (dernier jour calendaire tout juste complété)."""
    n_hours = n_days * 24
    returns = _trending_returns(n_hours, drift_per_hour, vol_per_hour, seed)
    return _hourly_history_from_returns(returns, start_price=start_price, start=start)


# ---------------------------------------------------------------------------------------
# 1. Filtre de tendance SMA200 : actif sous sa SMA200 -> poids 0
# ---------------------------------------------------------------------------------------


def test_asset_below_sma200_gets_zero_weight_asset_above_gets_nonzero():
    # BTC : tendance baissière nette et longue -> dernière clôture bien SOUS sa SMA200.
    btc_hist = make_complete_days_history(
        n_days=REGIME_SMA_DAYS + 60, drift_per_hour=-0.0025, vol_per_hour=0.001, seed=1, start_price=60000.0
    )
    # ETH : tendance haussière nette -> dernière clôture bien AU-DESSUS de sa SMA200.
    eth_hist = make_complete_days_history(
        n_days=REGIME_SMA_DAYS + 60, drift_per_hour=0.0025, vol_per_hour=0.001, seed=2, start_price=2000.0
    )

    history = {"BTC": btc_hist, "ETH": eth_hist}
    profile = _profile("prudent", ["BTC", "ETH"], vol_target=0.10, gross_exposure_max=0.40, cap_per_asset=0.20)

    weights = STRATEGY.target_weights(history, state={}, profile=profile)

    assert weights["BTC"] == 0.0
    assert weights["ETH"] > 0.0


def test_all_assets_off_returns_all_zero_not_empty():
    btc_hist = make_complete_days_history(
        n_days=REGIME_SMA_DAYS + 10, drift_per_hour=-0.002, vol_per_hour=0.001, seed=1
    )
    eth_hist = make_complete_days_history(
        n_days=REGIME_SMA_DAYS + 10, drift_per_hour=-0.002, vol_per_hour=0.001, seed=2, start_price=2000.0
    )
    history = {"BTC": btc_hist, "ETH": eth_hist}
    profile = _profile("prudent", ["BTC", "ETH"], vol_target=0.10, gross_exposure_max=0.40, cap_per_asset=0.20)

    weights = STRATEGY.target_weights(history, state={}, profile=profile)

    assert weights == {"BTC": 0.0, "ETH": 0.0}


def test_is_trend_on_none_below_200_complete_days():
    hist = make_complete_days_history(n_days=REGIME_SMA_DAYS - 1, drift_per_hour=0.001, vol_per_hour=0.001, seed=1)
    daily = _daily_closes(hist)
    assert len(daily) == REGIME_SMA_DAYS - 1
    assert _is_trend_on(daily) is None


# ---------------------------------------------------------------------------------------
# 2. Vol du panier doublée -> poids brut moitié
# ---------------------------------------------------------------------------------------


def test_doubling_basket_vol_halves_raw_weight():
    n_hours = (REGIME_SMA_DAYS + 20) * 24
    base_returns = _trending_returns(n_hours, drift_per_hour=0.0015, vol_per_hour=0.01, seed=7)

    hist_calm = _hourly_history_from_returns(base_returns, start_price=100.0)
    # Rendements horaires EXACTEMENT doublés (drift ET vol) -> l'écart-type EWMA (donc la vol
    # annualisée) est mathématiquement exactement doublé (std(k*X) = k*std(X)), tendance
    # préservée (drift toujours positif) -> même statut "on" dans les deux cas.
    hist_turbulent = _hourly_history_from_returns(2.0 * base_returns, start_price=100.0)

    # cap_per_asset et gross_exposure_max volontairement larges pour isoler le comportement
    # du sizing par la vol, sans qu'un cap n'interfère avec la comparaison.
    profile = _profile("prudent", ["BTC"], vol_target=0.20, gross_exposure_max=1.0, cap_per_asset=1.0)

    w_calm = STRATEGY.target_weights({"BTC": hist_calm}, state={}, profile=profile)
    w_turbulent = STRATEGY.target_weights({"BTC": hist_turbulent}, state={}, profile=profile)

    assert w_calm["BTC"] > 0.0
    assert w_turbulent["BTC"] == pytest.approx(w_calm["BTC"] / 2.0, rel=1e-6)


# ---------------------------------------------------------------------------------------
# 3. Caps respectés (par actif ET exposition brute)
# ---------------------------------------------------------------------------------------


def test_per_asset_cap_is_respected_when_raw_weight_would_exceed_it():
    n_hours = (REGIME_SMA_DAYS + 20) * 24
    # Très faible vol -> vol_target/vol_réalisée très supérieur à 1 -> sans cap, le poids par
    # actif exploserait largement au-delà de tout cap réaliste.
    returns = _trending_returns(n_hours, drift_per_hour=0.0005, vol_per_hour=0.00005, seed=11)
    hist = _hourly_history_from_returns(returns, start_price=100.0)

    profile = _profile("prudent", ["BTC"], vol_target=0.10, gross_exposure_max=0.90, cap_per_asset=0.20)
    weights = STRATEGY.target_weights({"BTC": hist}, state={}, profile=profile)

    assert weights["BTC"] == pytest.approx(0.20)


def test_gross_exposure_max_bounds_the_equal_weight_sum():
    n_hours = (REGIME_SMA_DAYS + 20) * 24
    returns_by_symbol = {
        sym: _trending_returns(n_hours, drift_per_hour=0.0005, vol_per_hour=0.00005, seed=seed)
        for seed, sym in enumerate(["BTC", "ETH", "SOL", "DOGE", "LINK", "AVAX"])
    }
    history = {
        sym: _hourly_history_from_returns(rets, start_price=100.0 + i * 10)
        for i, (sym, rets) in enumerate(returns_by_symbol.items())
    }

    # cap par actif volontairement large pour isoler le cap d'exposition BRUTE (0.70) : sans
    # lui, la répartition équipondérée d'un poids brut portefeuille énorme (vol quasi nulle)
    # dépasserait 0.70 au total.
    profile = _profile(
        "equilibre",
        list(returns_by_symbol.keys()),
        vol_target=0.20,
        gross_exposure_max=0.70,
        cap_per_asset=0.99,
    )
    weights = STRATEGY.target_weights(history, state={}, profile=profile)

    assert sum(weights.values()) == pytest.approx(0.70, rel=1e-6)
    for w in weights.values():
        assert w <= 0.70 + 1e-9


# ---------------------------------------------------------------------------------------
# 4. Aucune utilisation de la bougie/jour en cours (pas de look-ahead)
# ---------------------------------------------------------------------------------------


def test_incomplete_current_day_never_used_for_trend_or_sizing():
    n_days = REGIME_SMA_DAYS + 30
    returns = _trending_returns(n_days * 24, drift_per_hour=-0.0025, vol_per_hour=0.001, seed=3)
    complete_hist = _hourly_history_from_returns(returns, start_price=60000.0)

    # Weights de référence, calculés uniquement sur les jours complets (BTC nettement "off").
    profile = _profile("prudent", ["BTC"], vol_target=0.10, gross_exposure_max=0.40, cap_per_asset=0.20)
    weights_reference = STRATEGY.target_weights({"BTC": complete_hist}, state={}, profile=profile)
    assert weights_reference["BTC"] == 0.0

    # On ajoute quelques heures d'un jour "aujourd'hui" INCOMPLET avec une flambée de prix
    # massive : si le code utilisait, par erreur, la toute dernière bougie disponible (plutôt
    # que la dernière clôture de jour COMPLET), le filtre de tendance basculerait à "on".
    today_hours = 5
    spike_returns = np.full(today_hours, 0.20)  # +20%/heure pendant 5h -> flambée énorme
    last_ts = complete_hist.index[-1] + pd.Timedelta(hours=1)
    spike_hist = _hourly_history_from_returns(spike_returns, start_price=complete_hist["close"].iloc[-1], start=last_ts)
    augmented_hist = pd.concat([complete_hist, spike_hist])

    weights_augmented = STRATEGY.target_weights({"BTC": augmented_hist}, state={}, profile=profile)

    assert weights_augmented["BTC"] == 0.0
    assert weights_augmented == weights_reference


def test_daily_closes_excludes_partial_current_day():
    n_days = 3
    returns = _trending_returns(n_days * 24, drift_per_hour=0.0, vol_per_hour=0.001, seed=4)
    hist = _hourly_history_from_returns(returns, start_price=100.0)
    # 5 heures supplémentaires d'un 4e jour, incomplet.
    partial = _trending_returns(5, 0.0, 0.001, seed=5)
    partial_hist = _hourly_history_from_returns(
        partial, start_price=hist["close"].iloc[-1], start=hist.index[-1] + pd.Timedelta(hours=1)
    )
    augmented = pd.concat([hist, partial_hist])

    daily = _daily_closes(augmented)
    assert len(daily) == n_days  # le 4e jour (partiel) n'apparaît jamais


# ---------------------------------------------------------------------------------------
# 5. Stabilité : mêmes données -> mêmes poids
# ---------------------------------------------------------------------------------------


def test_stability_same_inputs_give_identical_weights():
    n_hours = (REGIME_SMA_DAYS + 20) * 24
    history = {
        "BTC": _hourly_history_from_returns(
            _trending_returns(n_hours, 0.001, 0.01, seed=42), start_price=60000.0
        ),
        "ETH": _hourly_history_from_returns(
            _trending_returns(n_hours, 0.001, 0.01, seed=43), start_price=2000.0
        ),
    }
    profile = _profile("prudent", ["BTC", "ETH"], vol_target=0.10, gross_exposure_max=0.40, cap_per_asset=0.20)

    w1 = STRATEGY.target_weights(history, state={}, profile=profile)
    w2 = STRATEGY.target_weights(history, state={}, profile=profile)

    assert w1 == w2


def test_same_data_across_all_hours_of_a_day_gives_identical_decision():
    """Simule le SPEC : 'décision une fois par jour, les autres cycles horaires ne changent
    rien'. Comme `bot.feeds.get_history()` renvoie exactement les mêmes jours COMPLETS pour
    n'importe quelle heure d'une même journée UTC (cf. docstring module), appeler la
    stratégie avec la MÊME history (représentant n'importe quel cycle horaire de la journée)
    donne toujours le même résultat -- il n'y a rien à figer explicitement."""
    n_hours = (REGIME_SMA_DAYS + 5) * 24
    history = {
        "BTC": _hourly_history_from_returns(_trending_returns(n_hours, 0.001, 0.01, seed=9), start_price=60000.0),
    }
    profile = _profile("prudent", ["BTC"], vol_target=0.10, gross_exposure_max=0.40, cap_per_asset=0.20)

    results = [STRATEGY.target_weights(history, state={}, profile=profile) for _ in range(24)]
    assert all(r == results[0] for r in results)


# ---------------------------------------------------------------------------------------
# Univers par wallet (panier resserré de l'agressif, cf. docstring module)
# ---------------------------------------------------------------------------------------


def test_spec_universe_matches_wallets():
    assert SPEC_UNIVERSE_BY_WALLET["prudent"] == ["BTC", "ETH"]
    assert SPEC_UNIVERSE_BY_WALLET["equilibre"] == ["BTC", "ETH", "SOL", "DOGE", "LINK", "AVAX"]
    assert len(SPEC_UNIVERSE_BY_WALLET["agressif"]) == 12
    assert set(["BTC", "ETH"]).issubset(SPEC_UNIVERSE_BY_WALLET["agressif"])


def test_agressif_wallet_never_targets_symbols_outside_the_12_basket():
    crypto_symbols_30 = SPEC_UNIVERSE_BY_WALLET["agressif"] + ["ADA", "DOT", "ETC", "APT", "ARB", "AAVE"]
    n_hours = (REGIME_SMA_DAYS + 5) * 24
    history = {
        sym: _hourly_history_from_returns(
            _trending_returns(n_hours, 0.001, 0.01, seed=hash(sym) % 1000), start_price=100.0
        )
        for sym in crypto_symbols_30
    }
    profile = _profile(
        "agressif", crypto_symbols_30, vol_target=0.35, gross_exposure_max=0.90, cap_per_asset=0.30
    )

    weights = STRATEGY.target_weights(history, state={}, profile=profile)

    assert set(weights.keys()) == set(SPEC_UNIVERSE_BY_WALLET["agressif"])
    for extra in ["ADA", "DOT", "ETC", "APT", "ARB", "AAVE"]:
        assert extra not in weights


# ---------------------------------------------------------------------------------------
# Posture défensive : profil incomplet / historique insuffisant
# ---------------------------------------------------------------------------------------


def test_missing_risk_profile_returns_no_weights():
    assert STRATEGY.target_weights({}, state={}, profile=None) == {}
    assert STRATEGY.target_weights({}, state={}, profile={"id": "prudent"}) == {}


def test_unknown_wallet_id_returns_no_weights():
    profile = _profile("inconnu", ["BTC"], vol_target=0.10, gross_exposure_max=0.40, cap_per_asset=0.20)
    assert STRATEGY.target_weights({"BTC": pd.DataFrame()}, state={}, profile=profile) == {}


def test_insufficient_common_history_for_basket_vol_freezes_instead_of_zeroing():
    """Correctif ARCHITECTURE.md §12.4 : vol de panier non estimable -> les actifs "on" sont
    GELÉS (omis du dict retourné, `bot.risk.manager.RiskManager.apply` conserve alors
    `poids_actuel`), PAS mis à 0.0 explicite (qui forcerait une liquidation à tort, cf.
    ancien comportement testé par `test_insufficient_common_history_for_basket_vol_gives_
    zero_weight`, ci-dessous remplacé)."""
    n_days = REGIME_SMA_DAYS + 5
    btc_hist = make_complete_days_history(n_days=n_days, drift_per_hour=0.001, vol_per_hour=0.01, seed=1, start_price=60000.0)
    # ETH également "on" (assez de jours complets, tendance haussière) mais sur une plage
    # calendaire totalement DISJOINTE de celle de BTC -> aucun timestamp horaire commun aux
    # deux actifs éligibles -> vol de panier équipondéré non estimable (aligned vide).
    eth_start = btc_hist.index[-1] + pd.Timedelta(days=400)
    eth_hist = make_complete_days_history(
        n_days=n_days, drift_per_hour=0.001, vol_per_hour=0.01, seed=2, start_price=2000.0, start=eth_start
    )

    profile = _profile("prudent", ["BTC", "ETH"], vol_target=0.10, gross_exposure_max=0.40, cap_per_asset=0.20)
    state: dict = {}
    weights = STRATEGY.target_weights({"BTC": btc_hist, "ETH": eth_hist}, state=state, profile=profile)

    assert weights == {}  # gelés, pas liquidés
    counters = state["strategy_state"]["quasi_passif_crypto"]["missing_data_cycles"]
    assert counters == {"BTC": 1, "ETH": 1}


# ---------------------------------------------------------------------------------------
# Correctif ARCHITECTURE.md §12.4 — gel (cas 3) vs sortie légitime (cas 1), garde-fou N cycles
# ---------------------------------------------------------------------------------------


def test_missing_history_for_one_symbol_freezes_only_that_symbol():
    """Un SEUL symbole (BTC) a une donnée manquante ce cycle (moins de 200 jours complets) ;
    ETH, lui, a un signal normal ("on") calculé. BTC doit être OMIS du dict (gelé), ETH garde
    son poids normal — pas de contamination croisée."""
    btc_hist = make_complete_days_history(
        n_days=REGIME_SMA_DAYS - 1, drift_per_hour=0.001, vol_per_hour=0.001, seed=1, start_price=60000.0
    )
    eth_hist = make_complete_days_history(
        n_days=REGIME_SMA_DAYS + 60, drift_per_hour=0.001, vol_per_hour=0.001, seed=2, start_price=2000.0
    )
    profile = _profile("prudent", ["BTC", "ETH"], vol_target=0.10, gross_exposure_max=0.40, cap_per_asset=0.20)
    state: dict = {}

    weights = STRATEGY.target_weights({"BTC": btc_hist, "ETH": eth_hist}, state=state, profile=profile)

    assert "BTC" not in weights  # gelé (poids_actuel conservé par RiskManager)
    assert weights["ETH"] > 0.0
    counters = state["strategy_state"]["quasi_passif_crypto"]["missing_data_cycles"]
    assert counters == {"BTC": 1}


def test_symbol_recovering_data_resets_missing_counter():
    profile = _profile("prudent", ["BTC"], vol_target=0.10, gross_exposure_max=0.40, cap_per_asset=0.20)
    state: dict = {"strategy_state": {"quasi_passif_crypto": {"missing_data_cycles": {"BTC": 5}}}}
    btc_hist = make_complete_days_history(
        n_days=REGIME_SMA_DAYS + 60, drift_per_hour=0.001, vol_per_hour=0.001, seed=1, start_price=60000.0
    )

    weights = STRATEGY.target_weights({"BTC": btc_hist}, state=state, profile=profile)

    assert weights["BTC"] > 0.0
    assert state["strategy_state"]["quasi_passif_crypto"]["missing_data_cycles"] == {}


def test_missing_data_for_max_cycles_liquidates_by_prudence():
    """Garde-fou : au 24e cycle CONSÉCUTIF de donnée manquante pour un même symbole, la
    position est liquidée par prudence (poids 0.0 explicite), pas gelée indéfiniment."""
    from bot.strategies import MISSING_DATA_MAX_CYCLES_DEFAULT

    btc_hist_missing = make_complete_days_history(
        n_days=REGIME_SMA_DAYS - 1, drift_per_hour=0.001, vol_per_hour=0.001, seed=1, start_price=60000.0
    )
    profile = _profile("prudent", ["BTC"], vol_target=0.10, gross_exposure_max=0.40, cap_per_asset=0.20)
    state: dict = {}

    for cycle in range(1, MISSING_DATA_MAX_CYCLES_DEFAULT):
        weights = STRATEGY.target_weights({"BTC": btc_hist_missing}, state=state, profile=profile)
        assert "BTC" not in weights, f"cycle {cycle}: devrait encore être gelé"

    weights = STRATEGY.target_weights({"BTC": btc_hist_missing}, state=state, profile=profile)
    assert weights == {"BTC": 0.0}  # 24e cycle consécutif -> liquidation par prudence
    assert state["strategy_state"]["quasi_passif_crypto"]["missing_data_cycles"] == {}


def test_xlm_t18_t19_t20_scenario_position_survives_transient_history_gap():
    """Reproduit EXACTEMENT le scénario production (ARCHITECTURE.md §12.4, wallet agressif,
    2026-07-23T18/T19) : historique XLM présent à T18 (achat, tendance on), ABSENT à T19 (raté
    de fetch transitoire), présent de nouveau à T20 — la position ne doit PAS être liquidée à
    T19 (gelée), et le signal T20 doit être intact (identique à ce qu'il aurait été sans le
    trou de données à T19, mémoire d'état correctement réinitialisée)."""
    xlm_hist = make_complete_days_history(
        n_days=REGIME_SMA_DAYS + 60, drift_per_hour=0.0006, vol_per_hour=0.002, seed=7, start_price=0.18
    )
    profile = _profile("agressif", ["XLM"], vol_target=0.275, gross_exposure_max=0.80, cap_per_asset=0.25)
    state: dict = {}

    # T18 : historique disponible -> XLM "on", poids normal (achat en production).
    weights_t18 = STRATEGY.target_weights({"XLM": xlm_hist}, state=state, profile=profile)
    assert weights_t18["XLM"] > 0.0
    assert state["strategy_state"]["quasi_passif_crypto"]["missing_data_cycles"] == {}

    # T19 : historique XLM absent ce cycle (raté de fetch transitoire, cf. get_history_crypto).
    weights_t19 = STRATEGY.target_weights({"XLM": pd.DataFrame()}, state=state, profile=profile)
    assert "XLM" not in weights_t19  # gelé : RiskManager conservera poids_actuel (pas de SELL)
    assert state["strategy_state"]["quasi_passif_crypto"]["missing_data_cycles"] == {"XLM": 1}

    # T20 : historique de nouveau disponible -> signal intact, identique à T18 (même série),
    # compteur remis à zéro.
    weights_t20 = STRATEGY.target_weights({"XLM": xlm_hist}, state=state, profile=profile)
    assert weights_t20["XLM"] == pytest.approx(weights_t18["XLM"])
    assert state["strategy_state"]["quasi_passif_crypto"]["missing_data_cycles"] == {}
