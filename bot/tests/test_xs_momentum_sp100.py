"""Tests de `bot/strategies/xs_momentum_sp100.py` (SPEC : docs/config-strategies.json ->
xs_momentum_sp100, docs/SELECTION-FINALE.md §1.1/§3/§5 ; implémentation de référence auditée :
bt-final/xs-momentum-sp100/strategy.py).

Fixtures de prix construites à la main (aucun réseau, aucune fixture externe) :
  - classement momentum : formule exacte `close[t-skip]/close[t-skip-lookback]-1` reproduite
    via une série "en escalier" dont les deux seules valeurs qui comptent sont contrôlées ;
  - déclenchement mensuel : la sélection ne change qu'au premier "mois-fin" confirmé suivant,
    jamais avant (pas de rebalancement intempestif), et n'est jamais influencée par des données
    postérieures à la date de décision (pas de fuite) ;
  - filtre marché SPY>SMA200 : réévalué à CHAQUE appel, indépendamment du gel mensuel du
    classement ;
  - persistance du comportement entre cycles : fonction pure, mêmes entrées -> mêmes poids.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import List

import pandas as pd
import pytest

from bot.strategies.xs_momentum_sp100 import (
    LOOKBACK_DAYS,
    MARKET_FILTER_SYMBOL,
    SKIP_DAYS,
    SMA_DAYS,
    SPEC_EQUITIES_WALLETS,
    TOP_K,
    UNIVERSE_SP100,
    WARMUP_BUFFER_DAYS,
    XsMomentumSp100,
    _daily_closes,
    _decision_date,
    _is_last_trading_day_of_month,
    _is_nyse_trading_day,
    _market_regime_on,
    _momentum_as_of,
    _rank_and_select,
)

STRATEGY = XsMomentumSp100()

EQUILIBRE = {"id": "equilibre"}
AGRESSIF = {"id": "agressif"}
PRUDENT = {"id": "prudent"}

# Sous-ensemble réel de l'univers (15 titres), suffisant pour tester top_k=10 sans construire
# les 103 tickers à chaque test.
SAMPLE_SYMBOLS: List[str] = UNIVERSE_SP100[:15]


# ---------------------------------------------------------------------------------------
# Helpers de fixtures
# ---------------------------------------------------------------------------------------


def _trading_days(start: date, n: int) -> List[date]:
    """`n` jours de bourse NYSE réels consécutifs à partir de `start` (inclus si ouvré),
    construits avec la MÊME fonction de calendrier que le module testé (source unique)."""
    days: List[date] = []
    d = start
    while len(days) < n:
        if _is_nyse_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    return days


def _days_ending_at_confirmed_month_end(start: date, min_len: int) -> List[date]:
    """`min_len` jours de bourse ou plus, à partir de `start`, TRONQUÉS pour que le DERNIER
    élément soit lui-même un mois-fin CONFIRMÉ (`_is_last_trading_day_of_month`, calcul de
    calendrier pur, cf. module testé). Indispensable pour que `_decision_date()` sur ce tableau
    retourne exactement `days[-1]` — condition dont dépendent les fixtures `_step_closes()`
    ci-dessous (qui placent le palier de momentum relativement à la DERNIÈRE position du
    tableau fourni)."""
    buffer_days = _trading_days(start, min_len + 40)
    for i in range(min_len - 1, len(buffer_days)):
        if _is_last_trading_day_of_month(buffer_days[i]):
            return buffer_days[: i + 1]
    raise AssertionError("aucun mois-fin confirmé trouvé dans le tampon (augmenter le tampon)")


def _extend_through_next_month_end(days: List[date]) -> List[date]:
    """Étend `days` (déjà terminé sur un mois-fin confirmé) jusqu'au PROCHAIN mois-fin
    confirmé (calendrier pur, cf. `_is_last_trading_day_of_month`)."""
    extended = list(days)
    d = extended[-1] + timedelta(days=1)
    while True:
        if _is_nyse_trading_day(d):
            extended.append(d)
            if _is_last_trading_day_of_month(d):
                break
        d += timedelta(days=1)
    return extended


def _daily_df(days: List[date], closes: List[float]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in days])
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0},
        index=idx,
    )


def _step_closes(n_days: int, momentum: float, base_price: float = 100.0) -> List[float]:
    """Série "en escalier" : `base_price` jusqu'à l'indice `n_days-1-SKIP_DAYS` (exclu), puis
    `base_price*(1+momentum)` ensuite. Conçue pour que `close[-1-skip]/close[-1-skip-lookback]-1`
    vaille EXACTEMENT `momentum`, quelle que soit la valeur des autres points (non utilisés par
    la formule) — cf. docstring module de xs_momentum_sp100.py, point dur (1)."""
    step_idx = n_days - 1 - SKIP_DAYS
    assert step_idx >= 0, "n_days trop court pour placer le palier"
    target = base_price * (1.0 + momentum)
    return [base_price if i < step_idx else target for i in range(n_days)]


def _spy_trend_closes(n_days: int, uptrend: bool, base_price: float = 1000.0) -> List[float]:
    """Série SPY strictement monotone : garantit `last > SMA200` (uptrend) ou `last < SMA200`
    (downtrend) sans ambiguïté, quelle que soit la fenêtre SMA choisie."""
    if uptrend:
        return [base_price + i * 1.0 for i in range(n_days)]
    return [base_price - i * 1.0 for i in range(n_days)]


MIN_ELIGIBLE_DAYS = SKIP_DAYS + LOOKBACK_DAYS + 1  # 148
MIN_SPY_DAYS = SMA_DAYS  # 200


def _sample_universe_history(days: List[date], momentum_by_symbol: dict) -> dict:
    history = {}
    for i, symbol in enumerate(SAMPLE_SYMBOLS):
        mom = momentum_by_symbol.get(symbol, -0.5)  # défaut : nettement négatif, jamais gagnant
        history[symbol] = _daily_df(days, _step_closes(len(days), mom, base_price=50.0 + i))
    return history


# ---------------------------------------------------------------------------------------
# 1. Calendrier NYSE pur (aucune dépendance aux données de prix)
# ---------------------------------------------------------------------------------------


def test_is_last_trading_day_of_month_basic():
    days = _trading_days(date(2026, 3, 2), 40)  # traverse mars -> avril 2026
    march_days = [d for d in days if d.month == 3]
    assert len(march_days) >= 2
    assert _is_last_trading_day_of_month(march_days[-1]) is True
    assert _is_last_trading_day_of_month(march_days[-2]) is False


def test_decision_date_finds_most_recent_confirmed_month_end():
    days = _trading_days(date(2026, 1, 5), 60)  # traverse janvier -> mars 2026
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in days])
    decision = _decision_date(idx)
    assert decision is not None
    assert _is_last_trading_day_of_month(decision.date()) is True
    # Doit être le PLUS RÉCENT mois-fin confirmé, pas le premier trouvé dans l'historique.
    later_confirmed = [d for d in days if d > decision.date() and _is_last_trading_day_of_month(d)]
    assert later_confirmed == []


def test_decision_date_none_when_no_month_end_yet_confirmed():
    # Une poignée de jours consécutifs pris bien à l'intérieur d'un mois (loin de sa fin) ne
    # confirme aucun mois-fin -> None, jamais un rebalancement anticipé.
    days = _trading_days(date(2026, 3, 2), 5)
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in days])
    assert _decision_date(idx) is None


# ---------------------------------------------------------------------------------------
# 2. Formule exacte du momentum (lookback/skip du SPEC), warmup, pas de fuite
# ---------------------------------------------------------------------------------------


def test_momentum_as_of_exact_formula():
    days = _trading_days(date(2026, 1, 5), MIN_ELIGIBLE_DAYS)
    closes_list = _step_closes(len(days), momentum=0.2345, base_price=80.0)
    closes = _daily_closes(_daily_df(days, closes_list))
    decision_date = pd.Timestamp(days[-1], tz="UTC")

    mom = _momentum_as_of(closes, decision_date, SKIP_DAYS, LOOKBACK_DAYS)
    assert mom == pytest.approx(0.2345, rel=1e-9)


def test_momentum_as_of_none_when_history_one_day_short():
    days = _trading_days(date(2026, 1, 5), MIN_ELIGIBLE_DAYS - 1)  # un jour de moins que requis
    closes_list = _step_closes(len(days) + 1, momentum=0.10, base_price=80.0)[: len(days)]
    closes = _daily_closes(_daily_df(days, closes_list))
    decision_date = pd.Timestamp(days[-1], tz="UTC")

    assert _momentum_as_of(closes, decision_date, SKIP_DAYS, LOOKBACK_DAYS) is None


def test_momentum_as_of_ignores_data_after_decision_date_no_lookahead():
    days = _trading_days(date(2026, 1, 5), MIN_ELIGIBLE_DAYS)
    closes_list = _step_closes(len(days), momentum=0.05, base_price=80.0)
    decision_date = pd.Timestamp(days[-1], tz="UTC")
    mom_reference = _momentum_as_of(
        _daily_closes(_daily_df(days, closes_list)), decision_date, SKIP_DAYS, LOOKBACK_DAYS
    )

    # On ajoute des jours FUTURS (après decision_date) avec un prix totalement différent : la
    # formule ne doit strictement rien en tirer (elle tronque à `.loc[:decision_date]`).
    extra_days = _trading_days(days[-1] + timedelta(days=1), 10)
    augmented_days = days + extra_days
    augmented_closes_list = closes_list + [9999.0] * len(extra_days)
    closes_augmented = _daily_closes(_daily_df(augmented_days, augmented_closes_list))

    mom_augmented = _momentum_as_of(closes_augmented, decision_date, SKIP_DAYS, LOOKBACK_DAYS)
    assert mom_augmented == pytest.approx(mom_reference, rel=1e-9)


# ---------------------------------------------------------------------------------------
# 3. Classement top_k + filtre "momentum strictement positif"
# ---------------------------------------------------------------------------------------


def test_rank_and_select_top_k_and_positive_only():
    days = _trading_days(date(2026, 1, 5), MIN_ELIGIBLE_DAYS)
    decision_date = pd.Timestamp(days[-1], tz="UTC")

    # 15 candidats : 12 momentums positifs distincts (seuls les 10 meilleurs doivent survivre),
    # 3 momentums négatifs (jamais sélectionnés même s'ils entraient dans le "top 10" par rang).
    momentums = {}
    for i, symbol in enumerate(SAMPLE_SYMBOLS[:12]):
        momentums[symbol] = 0.01 * (i + 1)  # 0.01 .. 0.12, tous positifs
    for symbol in SAMPLE_SYMBOLS[12:15]:
        momentums[symbol] = -0.10

    history = _sample_universe_history(days, momentums)
    winners = _rank_and_select(SAMPLE_SYMBOLS, history, decision_date)

    assert len(winners) == TOP_K
    winner_symbols = {sym for sym, _ in winners}
    # Les 2 momentums positifs les plus FAIBLES (0.01, 0.02 -> SAMPLE_SYMBOLS[0], [1]) sont
    # exclus du top 10 (seuls les 10 meilleurs sur 12 positifs survivent).
    assert SAMPLE_SYMBOLS[0] not in winner_symbols
    assert SAMPLE_SYMBOLS[1] not in winner_symbols
    for symbol in SAMPLE_SYMBOLS[2:12]:
        assert symbol in winner_symbols
    for symbol in SAMPLE_SYMBOLS[12:15]:
        assert symbol not in winner_symbols
    # Trié par momentum décroissant.
    assert [m for _, m in winners] == sorted([m for _, m in winners], reverse=True)


def test_rank_and_select_no_positive_candidate_gives_empty_list():
    days = _trading_days(date(2026, 1, 5), MIN_ELIGIBLE_DAYS)
    decision_date = pd.Timestamp(days[-1], tz="UTC")
    momentums = {s: -0.01 * (i + 1) for i, s in enumerate(SAMPLE_SYMBOLS)}
    history = _sample_universe_history(days, momentums)

    assert _rank_and_select(SAMPLE_SYMBOLS, history, decision_date) == []


# ---------------------------------------------------------------------------------------
# 4. Filtre de régime marché (point dur 2)
# ---------------------------------------------------------------------------------------


def test_market_regime_on_off_and_insufficient_history():
    days = _trading_days(date(2026, 1, 5), MIN_SPY_DAYS + 20)
    up = _daily_closes(_daily_df(days, _spy_trend_closes(len(days), uptrend=True)))
    down = _daily_closes(_daily_df(days, _spy_trend_closes(len(days), uptrend=False)))
    short = _daily_closes(_daily_df(days[: MIN_SPY_DAYS - 1], _spy_trend_closes(MIN_SPY_DAYS - 1, uptrend=True)))

    assert _market_regime_on(up) is True
    assert _market_regime_on(down) is False
    assert _market_regime_on(short) is None


# ---------------------------------------------------------------------------------------
# 5. Stratégie complète : sélection + pondération équipondérée
# ---------------------------------------------------------------------------------------


def test_full_strategy_selects_top_k_positive_momentum_equal_weight():
    days = _days_ending_at_confirmed_month_end(date(2026, 1, 5), max(MIN_ELIGIBLE_DAYS, MIN_SPY_DAYS))
    spy_hist = _daily_df(days, _spy_trend_closes(len(days), uptrend=True))

    momentums = {s: 0.05 + 0.01 * i for i, s in enumerate(SAMPLE_SYMBOLS)}  # tous positifs, 15 candidats
    history = _sample_universe_history(days, momentums)
    history[MARKET_FILTER_SYMBOL] = spy_hist

    weights = STRATEGY.target_weights(history, state={}, profile=EQUILIBRE)

    selected = {sym: w for sym, w in weights.items() if w > 0.0}
    assert len(selected) == TOP_K
    for w in selected.values():
        assert w == pytest.approx(1.0 / TOP_K)
    assert sum(weights.values()) == pytest.approx(1.0)
    # Tous les symboles de l'univers complet sont présents (poids explicite, pas d'omission).
    assert set(weights.keys()) == set(UNIVERSE_SP100)


def test_full_strategy_market_filter_off_gives_all_cash():
    days = _days_ending_at_confirmed_month_end(date(2026, 1, 5), max(MIN_ELIGIBLE_DAYS, MIN_SPY_DAYS))
    spy_hist = _daily_df(days, _spy_trend_closes(len(days), uptrend=False))  # régime OFF

    momentums = {s: 0.20 for s in SAMPLE_SYMBOLS}  # momentum excellent, mais régime coupe tout
    history = _sample_universe_history(days, momentums)
    history[MARKET_FILTER_SYMBOL] = spy_hist

    weights = STRATEGY.target_weights(history, state={}, profile=EQUILIBRE)

    assert weights == {symbol: 0.0 for symbol in UNIVERSE_SP100}


def test_full_strategy_missing_spy_returns_empty_not_forced_liquidation():
    days = _trading_days(date(2026, 1, 5), MIN_ELIGIBLE_DAYS)
    history = _sample_universe_history(days, {s: 0.20 for s in SAMPLE_SYMBOLS})
    # Pas de clé "SPY" dans `history`.

    weights = STRATEGY.target_weights(history, state={}, profile=EQUILIBRE)
    assert weights == {}


def test_full_strategy_insufficient_spy_warmup_gives_all_cash():
    days = _trading_days(date(2026, 1, 5), MIN_SPY_DAYS - 5)  # trop court pour SMA200
    spy_hist = _daily_df(days, _spy_trend_closes(len(days), uptrend=True))
    history = {MARKET_FILTER_SYMBOL: spy_hist}
    history.update(_sample_universe_history(days, {s: 0.20 for s in SAMPLE_SYMBOLS}))

    weights = STRATEGY.target_weights(history, state={}, profile=EQUILIBRE)
    assert weights == {symbol: 0.0 for symbol in UNIVERSE_SP100}


def test_ineligible_symbol_gets_zero_weight_without_breaking_others():
    days = _days_ending_at_confirmed_month_end(date(2026, 1, 5), max(MIN_ELIGIBLE_DAYS, MIN_SPY_DAYS))
    spy_hist = _daily_df(days, _spy_trend_closes(len(days), uptrend=True))

    momentums = {s: 0.10 + 0.01 * i for i, s in enumerate(SAMPLE_SYMBOLS)}
    history = _sample_universe_history(days, momentums)
    # Un titre du panier a un historique bien trop court (nouvellement "coté") : ne doit
    # jamais faire planter le calcul des autres, simplement rester à 0.
    short_symbol = SAMPLE_SYMBOLS[-1]
    history[short_symbol] = _daily_df(days[-10:], [50.0] * 10)
    history[MARKET_FILTER_SYMBOL] = spy_hist

    weights = STRATEGY.target_weights(history, state={}, profile=EQUILIBRE)
    assert weights[short_symbol] == 0.0


# ---------------------------------------------------------------------------------------
# 6. Rebalancement mensuel : gel intra-mois, recalcul exact au mois-fin suivant (point dur 1)
# ---------------------------------------------------------------------------------------


def test_frozen_within_month_extra_data_and_noise_do_not_change_weights():
    days_1 = _days_ending_at_confirmed_month_end(date(2026, 1, 5), max(MIN_ELIGIBLE_DAYS, MIN_SPY_DAYS))
    decision_date_1 = pd.Timestamp(days_1[-1], tz="UTC")

    momentums = {s: 0.05 + 0.01 * i for i, s in enumerate(SAMPLE_SYMBOLS)}
    spy_1 = _daily_df(days_1, _spy_trend_closes(len(days_1), uptrend=True))
    history_1 = _sample_universe_history(days_1, momentums)
    history_1[MARKET_FILTER_SYMBOL] = spy_1
    weights_1 = STRATEGY.target_weights(history_1, state={}, profile=EQUILIBRE)

    # On ajoute 3 jours de bourse supplémentaires (dans le mois SUIVANT, mais loin d'en être
    # le mois-fin) avec des prix complètement différents pour TOUS les titres + SPY (garde le
    # régime "on") : la décision de rebalancement ne doit PAS changer.
    extra_days = _trading_days(days_1[-1] + timedelta(days=1), 3)
    days_2 = days_1 + extra_days
    idx_2 = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in days_2])
    decision_date_2 = _decision_date(idx_2)
    assert decision_date_2 == decision_date_1  # pas encore un nouveau mois-fin confirmé

    # SPY continue simplement sa tendance haussière (régime doit rester "on" ici — le
    # basculement de régime en cours de mois est testé séparément, isolément, par
    # `test_regime_reevaluated_every_cycle_independent_of_frozen_ranking`).
    spy_base = _spy_trend_closes(len(days_1), uptrend=True)
    spy_2 = _daily_df(days_2, spy_base + [spy_base[-1] + 1.0, spy_base[-1] + 2.0, spy_base[-1] + 3.0])
    history_2 = {}
    for symbol in SAMPLE_SYMBOLS:
        base_closes = history_1[symbol]["close"].tolist()
        history_2[symbol] = _daily_df(days_2, base_closes + [123456.0, 0.001, 987.0])
    history_2[MARKET_FILTER_SYMBOL] = spy_2

    weights_2 = STRATEGY.target_weights(history_2, state={}, profile=EQUILIBRE)
    assert weights_2 == weights_1


def test_recompute_happens_exactly_at_next_confirmed_month_end():
    days_1 = _days_ending_at_confirmed_month_end(date(2026, 1, 5), max(MIN_ELIGIBLE_DAYS, MIN_SPY_DAYS))
    decision_date_1 = pd.Timestamp(days_1[-1], tz="UTC")

    days_2 = _extend_through_next_month_end(days_1)
    idx_2 = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in days_2])
    decision_date_2 = _decision_date(idx_2)
    assert decision_date_2 is not None
    assert decision_date_2 > decision_date_1

    winner_x, loser_y = SAMPLE_SYMBOLS[0], SAMPLE_SYMBOLS[1]

    # Snapshot mois 1 : X gagnant (momentum positif fort), Y perdant (négatif).
    spy_1 = _daily_df(days_1, _spy_trend_closes(len(days_1), uptrend=True))
    history_1 = {
        winner_x: _daily_df(days_1, _step_closes(len(days_1), momentum=0.30, base_price=100.0)),
        loser_y: _daily_df(days_1, _step_closes(len(days_1), momentum=-0.10, base_price=100.0)),
        MARKET_FILTER_SYMBOL: spy_1,
    }
    weights_1 = STRATEGY.target_weights(history_1, state={}, profile=EQUILIBRE)
    assert weights_1[winner_x] > 0.0
    assert weights_1[loser_y] == 0.0

    # Snapshot mois 2 (nouveau mois-fin confirmé) : les rôles s'inversent.
    spy_2 = _daily_df(days_2, _spy_trend_closes(len(days_2), uptrend=True))
    history_2 = {
        winner_x: _daily_df(days_2, _step_closes(len(days_2), momentum=-0.10, base_price=100.0)),
        loser_y: _daily_df(days_2, _step_closes(len(days_2), momentum=0.30, base_price=100.0)),
        MARKET_FILTER_SYMBOL: spy_2,
    }
    weights_2 = STRATEGY.target_weights(history_2, state={}, profile=EQUILIBRE)
    assert weights_2[loser_y] > 0.0
    assert weights_2[winner_x] == 0.0


def test_regime_reevaluated_every_cycle_independent_of_frozen_ranking():
    """Le filtre SPY>SMA200 est réévalué CHAQUE cycle (point dur 2) — un basculement du régime
    coupe immédiatement toute la poche à 0, sans attendre le prochain mois-fin confirmé, alors
    même que le classement mensuel (quels titres seraient sélectionnés) reste inchangé."""
    days_1 = _days_ending_at_confirmed_month_end(date(2026, 1, 5), max(MIN_ELIGIBLE_DAYS, MIN_SPY_DAYS))
    decision_date_1 = pd.Timestamp(days_1[-1], tz="UTC")

    momentums = {s: 0.05 + 0.01 * i for i, s in enumerate(SAMPLE_SYMBOLS)}
    history_stocks = _sample_universe_history(days_1, momentums)

    spy_on = _daily_df(days_1, _spy_trend_closes(len(days_1), uptrend=True))
    history_on = dict(history_stocks)
    history_on[MARKET_FILTER_SYMBOL] = spy_on
    weights_on = STRATEGY.target_weights(history_on, state={}, profile=EQUILIBRE)
    assert any(w > 0.0 for w in weights_on.values())

    # Quelques jours de plus (même mois-fin confirmé, cf. test précédent), mais un SPY qui
    # plonge nettement sous sa SMA200 sur ces derniers jours.
    extra_days = _trading_days(days_1[-1] + timedelta(days=1), 3)
    days_2 = days_1 + extra_days
    idx_2 = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in days_2])
    assert _decision_date(idx_2) == decision_date_1  # classement toujours figé

    spy_closes_on = _spy_trend_closes(len(days_1), uptrend=True)
    spy_crash = _daily_df(days_2, spy_closes_on + [1.0, 1.0, 1.0])
    history_off = {}
    for symbol in SAMPLE_SYMBOLS:
        base_closes = history_stocks[symbol]["close"].tolist()
        # Les titres eux-mêmes ne changent pas de valeur "utile" (juste prolongés à plat) :
        # seule SPY plonge, pour isoler l'effet du régime.
        history_off[symbol] = _daily_df(days_2, base_closes + [base_closes[-1]] * 3)
    history_off[MARKET_FILTER_SYMBOL] = spy_crash

    weights_off = STRATEGY.target_weights(history_off, state={}, profile=EQUILIBRE)
    assert weights_off == {symbol: 0.0 for symbol in UNIVERSE_SP100}


# ---------------------------------------------------------------------------------------
# 7. Restriction par wallet (SPEC §3 : prudent exclu)
# ---------------------------------------------------------------------------------------


def test_prudent_wallet_never_receives_equity_targets():
    days = _days_ending_at_confirmed_month_end(date(2026, 1, 5), max(MIN_ELIGIBLE_DAYS, MIN_SPY_DAYS))
    spy_hist = _daily_df(days, _spy_trend_closes(len(days), uptrend=True))
    history = _sample_universe_history(days, {s: 0.20 for s in SAMPLE_SYMBOLS})
    history[MARKET_FILTER_SYMBOL] = spy_hist

    assert STRATEGY.target_weights(history, state={}, profile=PRUDENT) == {}
    assert STRATEGY.target_weights(history, state={}, profile={"id": "inconnu"}) == {}
    assert STRATEGY.target_weights(history, state={}, profile=None) == {}


def test_equilibre_and_agressif_wallets_receive_equity_targets():
    days = _days_ending_at_confirmed_month_end(date(2026, 1, 5), max(MIN_ELIGIBLE_DAYS, MIN_SPY_DAYS))
    spy_hist = _daily_df(days, _spy_trend_closes(len(days), uptrend=True))
    history = _sample_universe_history(days, {s: 0.20 for s in SAMPLE_SYMBOLS})
    history[MARKET_FILTER_SYMBOL] = spy_hist

    for profile in (EQUILIBRE, AGRESSIF):
        weights = STRATEGY.target_weights(history, state={}, profile=profile)
        assert any(w > 0.0 for w in weights.values())


def test_spec_equities_wallets_matches_selection_finale():
    assert SPEC_EQUITIES_WALLETS == {"equilibre", "agressif"}


# ---------------------------------------------------------------------------------------
# 8. Constantes SPEC (fidélité, docs/config-strategies.json)
# ---------------------------------------------------------------------------------------


def test_spec_constants():
    assert len(UNIVERSE_SP100) == 103
    assert MARKET_FILTER_SYMBOL not in UNIVERSE_SP100
    assert TOP_K == 10
    assert SKIP_DAYS == 21
    assert LOOKBACK_DAYS == 126  # 6 mois * 21 jours de bourse/mois
    assert SMA_DAYS == 200
    assert WARMUP_BUFFER_DAYS == 400


# ---------------------------------------------------------------------------------------
# 9. Stabilité : fonction pure, mêmes entrées -> mêmes poids
# ---------------------------------------------------------------------------------------


def test_stability_same_inputs_give_identical_weights():
    days = _days_ending_at_confirmed_month_end(date(2026, 1, 5), max(MIN_ELIGIBLE_DAYS, MIN_SPY_DAYS))
    spy_hist = _daily_df(days, _spy_trend_closes(len(days), uptrend=True))
    history = _sample_universe_history(days, {s: 0.05 + 0.01 * i for i, s in enumerate(SAMPLE_SYMBOLS)})
    history[MARKET_FILTER_SYMBOL] = spy_hist

    w1 = STRATEGY.target_weights(history, state={}, profile=EQUILIBRE)
    w2 = STRATEGY.target_weights(history, state={}, profile=EQUILIBRE)
    assert w1 == w2
