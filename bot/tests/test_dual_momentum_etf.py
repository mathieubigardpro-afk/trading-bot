"""Tests de `bot/strategies/dual_momentum_etf.py` (SPEC : docs/config-strategies.json ->
dual_momentum_multiclasse_etf, docs/SELECTION-FINALE.md §1.2/§3/§5).

Fixtures de prix construites (pas de réseau, pas de fixtures externes) :
  - sélection correcte du "gagnant" (top_k=3 par momentum relatif) ;
  - bascule refuge sur IEF quand le momentum absolu (vs bogey) est négatif pour tous les
    sélectionnés ;
  - déclenchement mensuel (reference_date n'avance qu'une fois le mois suivant confirmé dans
    les données) et persistance des poids tout au long du mois en cours ;
  - warmup (300 jours de bourse) respecté, y compris pour le bogey IEF lui-même ;
  - postures défensives (wallet hors périmètre ETF, profil incomplet, IEF absent).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bot.strategies.dual_momentum_etf import (
    BOND_BOGEY,
    ETF_POCKET_WALLETS,
    LOOKBACK_MONTHS,
    MIN_WARMUP_TRADING_DAYS,
    RISKY_UNIVERSE,
    TOP_K,
    DualMomentumETF,
    _daily_closes,
    _last_confirmed_month_end,
    _total_return_asof,
)

STRATEGY = DualMomentumETF()


def _profile(wallet_id: str) -> dict:
    return {"id": wallet_id}


def _bdate_closes(
    daily_return: float, end: str, start: str = "2022-01-03", start_price: float = 100.0
) -> pd.Series:
    """Série de clôtures journalières à rendement CONSTANT sur des jours de bourse (lundi-
    vendredi, pas de fériés modélisés — sans impact sur la logique testée, qui ne dépend que
    des transitions de mois calendaires réellement présentes dans les données), du `start`
    au `end` inclus. Rendement constant -> l'ordre du rendement total sur N'IMPORTE QUELLE
    fenêtre glissante reflète directement l'ordre de `daily_return` entre symboles, ce qui
    rend le classement par momentum relatif entièrement prévisible dans les tests."""
    idx = pd.bdate_range(start=start, end=end, tz="UTC")
    n = len(idx)
    prices = start_price * np.cumprod(np.full(n, 1.0 + daily_return))
    return pd.Series(prices, index=idx)


def _bars(closes: pd.Series) -> pd.DataFrame:
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0},
        index=closes.index,
    )


def _history_for(
    daily_returns: dict, end: str, start: str = "2022-01-03", start_price: float = 100.0
) -> dict:
    return {
        sym: _bars(_bdate_closes(r, end=end, start=start, start_price=start_price))
        for sym, r in daily_returns.items()
    }


# Fenêtre longue (~2.3 ans de jours de bourse, largement > MIN_WARMUP_TRADING_DAYS) se
# terminant le dernier jour de bourse d'avril 2024, PUIS quelques jours ouvrés de mai pour
# confirmer avril comme "dernier mois clos" (cf. tests de déclenchement mensuel ci-dessous
# pour le détail de cette mécanique).
END_APRIL_CONFIRMED = "2024-05-06"  # avril confirmé (des bougies de mai existent)
END_APRIL_UNCONFIRMED = "2024-04-30"  # dernier bar = dernier jour ouvré d'avril, mai absent


# ---------------------------------------------------------------------------------------
# 1. Sélection correcte du gagnant (top_k=3 par momentum relatif)
# ---------------------------------------------------------------------------------------


def test_top3_selected_by_relative_momentum_and_pass_absolute_momentum():
    daily_returns = {
        "SPY": 0.0016,
        "QQQ": 0.0014,
        "IWM": 0.0012,
        "EFA": 0.0010,
        "EEM": 0.0008,
        "VNQ": 0.0006,
        "GLD": 0.0004,
        "DBC": 0.0002,
        "IEF": 0.0009,  # bogey, entre EFA et EEM -> bat par SPY/QQQ/IWM (top3)
    }
    history = _history_for(daily_returns, end=END_APRIL_CONFIRMED)

    weights = STRATEGY.target_weights(history, state={}, profile=_profile("equilibre"))

    assert weights["SPY"] == pytest.approx(1.0 / 3.0)
    assert weights["QQQ"] == pytest.approx(1.0 / 3.0)
    assert weights["IWM"] == pytest.approx(1.0 / 3.0)
    for loser in ["EFA", "EEM", "VNQ", "GLD", "DBC"]:
        assert weights[loser] == 0.0
    assert weights["IEF"] == 0.0
    assert sum(weights.values()) == pytest.approx(1.0)


def test_prudent_wallet_gets_same_selection_params_as_equilibre():
    daily_returns = {
        "SPY": 0.002, "QQQ": 0.0018, "IWM": 0.0016, "EFA": 0.001,
        "EEM": 0.0008, "VNQ": 0.0006, "GLD": 0.0004, "DBC": 0.0002, "IEF": 0.0005,
    }
    history = _history_for(daily_returns, end=END_APRIL_CONFIRMED)

    w_prudent = STRATEGY.target_weights(history, state={}, profile=_profile("prudent"))
    w_equilibre = STRATEGY.target_weights(history, state={}, profile=_profile("equilibre"))

    assert w_prudent == w_equilibre


# ---------------------------------------------------------------------------------------
# 2. Bascule refuge quand le momentum absolu est négatif (vs bogey IEF)
# ---------------------------------------------------------------------------------------


def test_all_top3_fail_absolute_momentum_switches_fully_to_bond_bogey():
    daily_returns = {
        "SPY": 0.0016, "QQQ": 0.0014, "IWM": 0.0012, "EFA": 0.0010,
        "EEM": 0.0008, "VNQ": 0.0006, "GLD": 0.0004, "DBC": 0.0002,
        "IEF": 0.006,  # bogey largement au-dessus de TOUT le panier risqué
    }
    history = _history_for(daily_returns, end=END_APRIL_CONFIRMED)

    weights = STRATEGY.target_weights(history, state={}, profile=_profile("prudent"))

    for sym in RISKY_UNIVERSE:
        assert weights[sym] == 0.0
    assert weights[BOND_BOGEY] == pytest.approx(1.0)


def test_partial_absolute_momentum_failure_splits_between_asset_and_bogey():
    # SPY/QQQ battent IEF (restent investis), IWM (3e du classement relatif) le sous-performe
    # (slot bascule vers IEF) -> 2/3 sur les actifs risqués, 1/3 sur IEF.
    daily_returns = {
        "SPY": 0.0020, "QQQ": 0.0018, "IWM": 0.0005, "EFA": 0.0004,
        "EEM": 0.0003, "VNQ": 0.0002, "GLD": 0.0001, "DBC": 0.00005,
        "IEF": 0.0010,  # > IWM, < SPY/QQQ
    }
    history = _history_for(daily_returns, end=END_APRIL_CONFIRMED)

    weights = STRATEGY.target_weights(history, state={}, profile=_profile("equilibre"))

    assert weights["SPY"] == pytest.approx(1.0 / 3.0)
    assert weights["QQQ"] == pytest.approx(1.0 / 3.0)
    assert weights["IWM"] == 0.0
    assert weights[BOND_BOGEY] == pytest.approx(1.0 / 3.0)
    assert sum(weights.values()) == pytest.approx(1.0)


# ---------------------------------------------------------------------------------------
# 3. Déclenchement mensuel + persistance (pas de recalcul avant confirmation du mois suivant)
# ---------------------------------------------------------------------------------------


def test_last_confirmed_month_end_requires_a_bar_in_the_following_month():
    closes = _bdate_closes(0.001, end=END_APRIL_UNCONFIRMED)  # dernier bar = 30 avril 2024
    ref = _last_confirmed_month_end(closes.index)
    # Avril n'est pas confirmé (aucune bougie de mai) -> référence = dernier jour ouvré de mars.
    assert ref == pd.Timestamp("2024-03-29", tz="UTC")


def test_last_confirmed_month_end_advances_once_next_month_appears():
    closes = _bdate_closes(0.001, end=END_APRIL_CONFIRMED)  # quelques jours de mai ajoutés
    ref = _last_confirmed_month_end(closes.index)
    assert ref == pd.Timestamp("2024-04-30", tz="UTC")  # dernier jour ouvré d'avril, confirmé


def test_weights_identical_throughout_the_month_until_next_month_confirmed():
    daily_returns = {
        "SPY": 0.0016, "QQQ": 0.0014, "IWM": 0.0012, "EFA": 0.0010,
        "EEM": 0.0008, "VNQ": 0.0006, "GLD": 0.0004, "DBC": 0.0002, "IEF": 0.0009,
    }
    profile = _profile("prudent")

    # Deux dates différentes, toutes deux DANS le mois d'avril (mois non confirmé tant que mai
    # n'apparaît pas) -> mêmes poids, calculés sur la référence de mars.
    history_mid_april = _history_for(daily_returns, end="2024-04-15")
    history_end_april = _history_for(daily_returns, end=END_APRIL_UNCONFIRMED)

    w_mid = STRATEGY.target_weights(history_mid_april, state={}, profile=profile)
    w_end = STRATEGY.target_weights(history_end_april, state={}, profile=profile)
    assert w_mid == w_end

    # Une fois mai confirmé (quelques bougies de mai présentes), la référence avance à fin
    # avril -> le calcul de rendement 12 mois change de fenêtre (même si le classement relatif
    # reste ici identique par construction des rendements constants).
    history_may_confirmed = _history_for(daily_returns, end=END_APRIL_CONFIRMED)
    w_confirmed = STRATEGY.target_weights(history_may_confirmed, state={}, profile=profile)

    # Le classement (rendements constants) reste le même, mais la référence temporelle a bien
    # changé : on le vérifie directement via le helper plutôt que sur les poids (qui, par
    # construction de ce fixture à rendements constants, seraient identiques dans les deux cas
    # -- ce test isole donc explicitement la mécanique de date, cf. tests dédiés ci-dessus).
    ief_closes = _daily_closes(history_may_confirmed[BOND_BOGEY])
    assert _last_confirmed_month_end(ief_closes.index) == pd.Timestamp("2024-04-30", tz="UTC")
    ief_closes_unconfirmed = _daily_closes(history_end_april[BOND_BOGEY])
    assert _last_confirmed_month_end(ief_closes_unconfirmed.index) == pd.Timestamp(
        "2024-03-29", tz="UTC"
    )
    # Les poids eux-mêmes restent d'ailleurs identiques ici (rendements constants -> même
    # classement quelle que soit la fenêtre) : confirme que le "déclenchement" ne casse rien.
    assert w_confirmed == w_end


def test_ranking_flip_only_takes_effect_after_month_confirmed():
    # DBC est en dernière position sur la longue fenêtre (rendement quotidien faible) mais
    # connaît un rallye massif seulement pendant le mois d'avril -- si le mois d'avril n'est
    # pas encore confirmé, ce rallye ne doit PAS influencer le calcul (lookback ancré sur la
    # référence de mars, qui ne "voit" pas encore les bougies d'avril... si, en fait,
    # `_total_return_asof` regarde reference_date et 12 mois avant, donc AVANT que la
    # référence n'avance à fin avril, `reference_date` est fin mars -- les données d'avril
    # existent dans l'historique mais ne sont simplement jamais utilisées comme point final).
    base_returns = {
        "SPY": 0.0010, "QQQ": 0.0009, "IWM": 0.0008, "EFA": 0.0007,
        "EEM": 0.0006, "VNQ": 0.0005, "GLD": 0.0004, "IEF": 0.0003,
    }
    # DBC : rendement plat jusqu'à fin mars, puis rallye pendant avril (donc affecte seulement
    # un calcul dont reference_date >= fin avril).
    dbc_flat = _bdate_closes(0.0001, end="2024-03-29")
    dbc_rally_idx = pd.bdate_range(start="2024-04-01", end=END_APRIL_UNCONFIRMED, tz="UTC")
    dbc_rally_prices = dbc_flat.iloc[-1] * np.cumprod(np.full(len(dbc_rally_idx), 1.05))
    dbc_full = pd.concat([dbc_flat, pd.Series(dbc_rally_prices, index=dbc_rally_idx)])

    history_unconfirmed = _history_for(base_returns, end=END_APRIL_UNCONFIRMED)
    history_unconfirmed["DBC"] = _bars(dbc_full)

    weights_unconfirmed = STRATEGY.target_weights(
        history_unconfirmed, state={}, profile=_profile("prudent")
    )
    # Mois d'avril non confirmé (dernier bar = 30 avril, pas de mai) -> reference_date = fin
    # mars -> le rallye d'avril de DBC n'a AUCUN effet, DBC reste dernier (poids nul).
    assert weights_unconfirmed["DBC"] == 0.0


# ---------------------------------------------------------------------------------------
# 4. Warmup (300 jours de bourse) respecté
# ---------------------------------------------------------------------------------------


def test_asset_with_insufficient_warmup_is_excluded_from_ranking():
    daily_returns = {
        "QQQ": 0.0014, "IWM": 0.0012, "EFA": 0.0010,
        "EEM": 0.0008, "VNQ": 0.0006, "GLD": 0.0004, "DBC": 0.0002, "IEF": 0.0003,
    }
    history = _history_for(daily_returns, end=END_APRIL_CONFIRMED)
    # SPY aurait été le grand gagnant (rendement le plus élevé) mais dispose de bien moins de
    # MIN_WARMUP_TRADING_DAYS bougies avant reference_date -> doit être exclu du classement.
    spy_short = _bdate_closes(0.0030, end=END_APRIL_CONFIRMED, start="2024-03-01")
    assert len(spy_short) < MIN_WARMUP_TRADING_DAYS
    history["SPY"] = _bars(spy_short)

    weights = STRATEGY.target_weights(history, state={}, profile=_profile("prudent"))

    assert weights["SPY"] == 0.0
    # QQQ/IWM/EFA prennent la relève comme top3 parmi les actifs éligibles restants.
    assert weights["QQQ"] == pytest.approx(1.0 / 3.0)
    assert weights["IWM"] == pytest.approx(1.0 / 3.0)
    assert weights["EFA"] == pytest.approx(1.0 / 3.0)


def test_bogey_with_insufficient_warmup_gives_no_signal_at_all():
    daily_returns = {
        "SPY": 0.0016, "QQQ": 0.0014, "IWM": 0.0012, "EFA": 0.0010,
        "EEM": 0.0008, "VNQ": 0.0006, "GLD": 0.0004, "DBC": 0.0002,
    }
    history = _history_for(daily_returns, end=END_APRIL_CONFIRMED)
    ief_short = _bdate_closes(0.0005, end=END_APRIL_CONFIRMED, start="2024-03-01")
    assert len(ief_short) < MIN_WARMUP_TRADING_DAYS
    history[BOND_BOGEY] = _bars(ief_short)

    weights = STRATEGY.target_weights(history, state={}, profile=_profile("equilibre"))

    assert weights == {sym: 0.0 for sym in RISKY_UNIVERSE + [BOND_BOGEY]}


def test_total_return_asof_none_below_min_warmup_trading_days():
    closes = _bdate_closes(0.001, end=END_APRIL_CONFIRMED, start="2024-03-01")
    assert len(closes) < MIN_WARMUP_TRADING_DAYS
    ref = closes.index[-1]
    assert _total_return_asof(closes, ref, LOOKBACK_MONTHS) is None


def test_total_return_asof_none_when_reference_date_not_in_series():
    closes = _bdate_closes(0.001, end=END_APRIL_UNCONFIRMED)
    missing_ref = pd.Timestamp("2024-05-15", tz="UTC")  # jamais présent dans `closes`
    assert _total_return_asof(closes, missing_ref, LOOKBACK_MONTHS) is None


def test_total_return_asof_none_when_lookback_window_not_covered():
    # Historique avec >= MIN_WARMUP_TRADING_DAYS bougies (300) mais ne couvrant qu'environ
    # 10,5 mois de profondeur calendaire (fréquence quotidienne 7j/7, pas jours de bourse) :
    # MIN_WARMUP_TRADING_DAYS (300) est volontairement < 12 mois de séances (~252 jours OUVRÉS
    # mais ~365 jours CALENDAIRES) -- avec des bougies à fréquence quotidienne pleine (pas
    # ouvrée), 300+ bougies ne suffisent PAS à couvrir 12 mois calendaires en arrière, ce qui
    # permet d'isoler ce cas dégénéré (impossible à obtenir avec des jours de bourse réels,
    # cf. note ci-dessous) sans dépendre de `_last_confirmed_month_end`/`target_weights`.
    idx = pd.date_range(end="2024-04-30", periods=320, freq="D", tz="UTC")
    closes = pd.Series(np.linspace(100.0, 110.0, len(idx)), index=idx)
    assert len(closes) >= MIN_WARMUP_TRADING_DAYS
    ref = closes.index[-1]
    assert _total_return_asof(closes, ref, LOOKBACK_MONTHS) is None


# ---------------------------------------------------------------------------------------
# 5. Postures défensives
# ---------------------------------------------------------------------------------------


def test_agressif_wallet_has_no_etf_pocket():
    daily_returns = {
        "SPY": 0.0016, "QQQ": 0.0014, "IWM": 0.0012, "EFA": 0.0010,
        "EEM": 0.0008, "VNQ": 0.0006, "GLD": 0.0004, "DBC": 0.0002, "IEF": 0.0003,
    }
    history = _history_for(daily_returns, end=END_APRIL_CONFIRMED)
    assert STRATEGY.target_weights(history, state={}, profile=_profile("agressif")) == {}


def test_unknown_or_missing_profile_returns_no_weights():
    assert STRATEGY.target_weights({}, state={}, profile=None) == {}
    assert STRATEGY.target_weights({}, state={}, profile={}) == {}
    assert STRATEGY.target_weights({}, state={}, profile={"id": "inconnu"}) == {}


def test_missing_bogey_history_gives_all_zero_weights():
    daily_returns = {
        "SPY": 0.0016, "QQQ": 0.0014, "IWM": 0.0012, "EFA": 0.0010,
        "EEM": 0.0008, "VNQ": 0.0006, "GLD": 0.0004, "DBC": 0.0002,
    }  # pas de IEF du tout dans `history`
    history = _history_for(daily_returns, end=END_APRIL_CONFIRMED)

    weights = STRATEGY.target_weights(history, state={}, profile=_profile("prudent"))

    assert weights == {sym: 0.0 for sym in RISKY_UNIVERSE + [BOND_BOGEY]}


def test_empty_history_returns_all_zero_not_empty_for_valid_wallet():
    weights = STRATEGY.target_weights({}, state={}, profile=_profile("prudent"))
    assert weights == {sym: 0.0 for sym in RISKY_UNIVERSE + [BOND_BOGEY]}


def test_stability_same_inputs_give_identical_weights():
    daily_returns = {
        "SPY": 0.0016, "QQQ": 0.0014, "IWM": 0.0012, "EFA": 0.0010,
        "EEM": 0.0008, "VNQ": 0.0006, "GLD": 0.0004, "DBC": 0.0002, "IEF": 0.0009,
    }
    history = _history_for(daily_returns, end=END_APRIL_CONFIRMED)
    profile = _profile("equilibre")

    w1 = STRATEGY.target_weights(history, state={}, profile=profile)
    w2 = STRATEGY.target_weights(history, state={}, profile=profile)
    assert w1 == w2


# ---------------------------------------------------------------------------------------
# Constantes SPEC (fidélité aux paramètres, cf. docs/config-strategies.json)
# ---------------------------------------------------------------------------------------


def test_spec_constants_match_config_strategies_json():
    assert RISKY_UNIVERSE == ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "GLD", "DBC"]
    assert BOND_BOGEY == "IEF"
    assert TOP_K == 3
    assert LOOKBACK_MONTHS == 12
    assert ETF_POCKET_WALLETS == {"prudent", "equilibre"}
