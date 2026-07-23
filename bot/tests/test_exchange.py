"""Tests de bot/sim/exchange.py — ExchangeSim.

Couvre : conservatisme systématique du fill (toujours pire ou égal au mid), calcul exact des
frais, et les trois motifs de rejet obligatoires (quote périmée, notionnel trop petit,
quantité arrondie à zéro / pas réaliste). Le rejet "survente" est testé côté Ledger
(test_ledger.py) car ExchangeSim n'a pas connaissance des positions détenues.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest

from bot.sim import ExchangeSim, Fill, Quote, Reject
from bot.sim.exchange import floor_to_step


NOW = datetime(2026, 7, 22, 14, 0, 0, tzinfo=timezone.utc)


def make_quote(bid, ask, ts=None, source="binance", delayed=False):
    ts = ts or NOW.isoformat()
    mid = (bid + ask) / 2
    return Quote(bid=bid, ask=ask, mid=mid, ts=ts, source=source, delayed=delayed)


def make_sim(**kwargs):
    defaults = dict(fee_taker_bps=10, slippage_penalty_bps=5)
    defaults.update(kwargs)
    return ExchangeSim(**defaults)


# ---------------------------------------------------------------------------
# Conservatisme du fill
# ---------------------------------------------------------------------------

def test_buy_fill_is_ask_plus_slippage_and_worse_than_mid():
    sim = make_sim()
    quote = make_quote(bid=100.0, ask=100.20)
    fill = sim.execute_order("BUY", "BTC", 1.0, quote, "test_strat", "2026-07-22T14", now=NOW)

    assert isinstance(fill, Fill)
    expected_price = quote.ask * (1 + 5 / 1e4)
    assert fill.price_fill == pytest.approx(expected_price)
    assert fill.price_fill >= quote.mid
    assert fill.price_fill >= quote.ask


def test_sell_fill_is_bid_minus_slippage_and_worse_than_mid():
    sim = make_sim()
    quote = make_quote(bid=100.0, ask=100.20)
    fill = sim.execute_order("SELL", "BTC", 1.0, quote, "test_strat", "2026-07-22T14", now=NOW)

    assert isinstance(fill, Fill)
    expected_price = quote.bid * (1 - 5 / 1e4)
    assert fill.price_fill == pytest.approx(expected_price)
    assert fill.price_fill <= quote.mid
    assert fill.price_fill <= quote.bid


@pytest.mark.parametrize("seed", range(50))
def test_property_fill_always_worse_or_equal_than_mid(seed):
    """Propriété générale : quel que soit bid/ask/qty/side (dans des bornes réalistes), le
    prix de fill ne doit JAMAIS avantager le bot par rapport au mid idéal."""
    rng = random.Random(seed)
    sim = make_sim()
    mid = rng.uniform(0.01, 100_000)
    half_spread = mid * rng.uniform(0.0001, 0.01)
    bid = mid - half_spread
    ask = mid + half_spread
    quote = make_quote(bid=bid, ask=ask)
    qty = rng.uniform(1, 1000)
    side = rng.choice(["BUY", "SELL"])

    result = sim.execute_order(side, "BTC", qty, quote, "prop_test", "2026-07-22T14", now=NOW)

    if isinstance(result, Fill):
        if side == "BUY":
            assert result.price_fill >= quote.mid - 1e-9
        else:
            assert result.price_fill <= quote.mid + 1e-9


# ---------------------------------------------------------------------------
# Frais exacts
# ---------------------------------------------------------------------------

def test_fees_are_exact_bps_of_notional():
    sim = make_sim(fee_taker_bps=10, slippage_penalty_bps=5)
    quote = make_quote(bid=100.0, ask=101.0)
    fill = sim.execute_order("BUY", "BTC", 2.0, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(fill, Fill)
    expected_fees = fill.notional_usd * 10 / 1e4
    assert fill.fees_usd == pytest.approx(expected_fees)


def test_slippage_usd_matches_definition():
    sim = make_sim()
    quote = make_quote(bid=100.0, ask=101.0)
    fill = sim.execute_order("SELL", "ETH", 3.0, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(fill, Fill)
    expected_slippage = abs(fill.price_fill - quote.mid) * fill.qty
    assert fill.slippage_usd == pytest.approx(expected_slippage)


# ---------------------------------------------------------------------------
# Rejets obligatoires
# ---------------------------------------------------------------------------

def test_reject_stale_quote_older_than_120s():
    sim = make_sim()
    old_ts = (NOW - timedelta(seconds=121)).isoformat()
    quote = make_quote(bid=100.0, ask=100.2, ts=old_ts)

    result = sim.execute_order("BUY", "BTC", 1.0, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(result, Reject)
    assert "périmée" in result.reason


def test_accept_quote_at_boundary_119s_is_fresh_enough():
    sim = make_sim()
    fresh_ts = (NOW - timedelta(seconds=119)).isoformat()
    quote = make_quote(bid=100.0, ask=100.2, ts=fresh_ts)

    result = sim.execute_order("BUY", "BTC", 1.0, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(result, Fill)


def test_reject_notional_below_minimum():
    sim = make_sim(min_notional_usd=10.0)
    quote = make_quote(bid=100.0, ask=100.2)
    # qty * price_fill sera très inférieur à 10$
    result = sim.execute_order("BUY", "BTC", 0.00005, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(result, Reject)
    assert "notionnel" in result.reason


def test_accept_notional_just_above_minimum():
    sim = make_sim(min_notional_usd=10.0, qty_steps={"XYZ": 0.0001})
    quote = make_quote(bid=100.0, ask=100.2)
    # qty=0.2 -> notional ~ 20$, largement au-dessus du minimum
    result = sim.execute_order("BUY", "XYZ", 0.2, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(result, Fill)
    assert result.notional_usd >= 10.0


def test_reject_quantity_rounds_down_to_zero():
    sim = make_sim(qty_steps={"BTC": 1.0})  # pas grossier exprès
    quote = make_quote(bid=100.0, ask=100.2)
    result = sim.execute_order("BUY", "BTC", 0.4, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(result, Reject)
    assert "pas réaliste" in result.reason or "arrondie" in result.reason


def test_reject_invalid_quote_bid_gte_ask():
    sim = make_sim()
    quote = make_quote(bid=101.0, ask=100.0)  # bid >= ask, quote incohérente
    result = sim.execute_order("BUY", "BTC", 1.0, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(result, Reject)


def test_reject_none_quote():
    sim = make_sim()
    result = sim.execute_order("BUY", "BTC", 1.0, None, "s", "2026-07-22T14", now=NOW)

    assert isinstance(result, Reject)
    assert "indisponible" in result.reason


def test_reject_invalid_side():
    sim = make_sim()
    quote = make_quote(bid=100.0, ask=100.2)
    result = sim.execute_order("HOLD", "BTC", 1.0, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(result, Reject)


def test_reject_future_quote_timestamp():
    sim = make_sim()
    future_ts = (NOW + timedelta(seconds=60)).isoformat()
    quote = make_quote(bid=100.0, ask=100.2, ts=future_ts)
    result = sim.execute_order("BUY", "BTC", 1.0, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(result, Reject)


# ---------------------------------------------------------------------------
# Arrondi de quantité (pas réaliste par actif)
# ---------------------------------------------------------------------------

def test_floor_to_step_basic():
    assert floor_to_step(1.234567, 0.00001) == pytest.approx(1.23456)
    assert floor_to_step(1.999, 1.0) == pytest.approx(1.0)
    assert floor_to_step(0.0009, 0.001) == pytest.approx(0.0)


def test_qty_rounded_down_never_exceeds_requested():
    sim = make_sim()
    quote = make_quote(bid=100.0, ask=100.2)
    fill = sim.execute_order("BUY", "DOGE", 5.7, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(fill, Fill)
    assert fill.qty <= 5.7
    assert fill.qty == pytest.approx(5.0)  # step DOGE = 1.0 par défaut


# ---------------------------------------------------------------------------
# Régression audit critique #2 : sans qty_steps dédié, un symbole action/ETF hors des 6
# megacaps historiques (`DEFAULT_QTY_STEPS`) retombe sur `DEFAULT_UNKNOWN_SYMBOL_STEP=1.0`
# (lot entier) — une position typique de budget ~30-200$ sur un titre >30-40$/action se voit
# alors arrondie à zéro et rejetée à coup sûr (vérifié empiriquement avec SPY sur `bot.runner`).
# `bot.config.QTY_STEPS_EQUITIES`, fusionné par le constructeur `ExchangeSim(qty_steps=...)`
# (câblé par `bot.runner._exchange_for_wallet`), doit lever ce blocage.
# ---------------------------------------------------------------------------

def test_unknown_equity_symbol_without_dedicated_qty_step_is_rejected_at_small_size():
    """Caractérise le bug AVANT correctif (comportement par défaut d'ExchangeSim, sans passer
    `qty_steps`) : SPY (hors DEFAULT_QTY_STEPS) sur une taille de position réaliste (~150$ /
    450$/action = 0.33 action) est rejeté au pas grossier par défaut (1.0)."""
    sim = make_sim()  # pas de qty_steps custom -> DEFAULT_UNKNOWN_SYMBOL_STEP=1.0 pour SPY
    quote = make_quote(bid=449.5, ask=450.5)
    result = sim.execute_order("BUY", "SPY", 0.33, quote, "dual_momentum_etf", "2026-07-22T14", now=NOW)

    assert isinstance(result, Reject)
    assert "arrondie à zéro" in result.reason


def test_equity_etf_qty_steps_from_config_allow_fractional_positions():
    """Correctif : `bot.config.QTY_STEPS_EQUITIES` fournit un pas fractionnaire pour TOUT
    `bot.config.SYMBOLS_EQUITY` (S&P100 + SPY + les 8 ETF risqués + IEF) -- la même position
    SPY de ~150$ passe désormais."""
    from bot import config

    assert "SPY" in config.QTY_STEPS_EQUITIES
    sim = make_sim(qty_steps=config.QTY_STEPS_EQUITIES)
    quote = make_quote(bid=449.5, ask=450.5)
    result = sim.execute_order("BUY", "SPY", 0.33, quote, "dual_momentum_etf", "2026-07-22T14", now=NOW)

    assert isinstance(result, Fill)
    assert 0 < result.qty <= 0.33


def test_equity_etf_qty_steps_cover_full_sp100_and_etf_universe():
    from bot import config

    for sym in config.SYMBOLS_EQUITY:
        assert sym in config.QTY_STEPS_EQUITIES
        assert config.QTY_STEPS_EQUITIES[sym] < 1.0  # fractionnaire, pas un lot entier


def test_runner_exchange_for_wallet_wires_equity_etf_qty_steps():
    """`bot.runner._exchange_for_wallet` doit câbler `config.QTY_STEPS_EQUITIES` dans
    l'`ExchangeSim` réellement utilisé par le cycle de production (pas seulement disponible en
    config sans être branché)."""
    import bot.runner as runner
    from bot import config

    exchange = runner._exchange_for_wallet(config.wallet_config("equilibre"))
    assert exchange.step_for("SPY") == pytest.approx(config.QTY_STEP_EQUITY_ETF)
    assert exchange.step_for("AAPL") == pytest.approx(config.QTY_STEP_EQUITY_ETF)
    # la crypto n'est pas affectée par ce câblage (steps crypto par défaut inchangés)


def test_runner_exchange_for_wallet_wires_equity_max_quote_age_override():
    """Correctif incident production 2026-07-23T18/T19 : `bot.runner._exchange_for_wallet`
    doit câbler `config.MAX_QUOTE_AGE_SECONDS_EQUITY` (25 min) pour TOUT `config.SYMBOLS_EQUITY`
    dans l'`ExchangeSim` réellement utilisé en production — sans ce câblage, une quote
    actions/ETF acceptée côté feeds (`bot.feeds.equities`, seuil également élargi) serait quand
    même rejetée ICI, à l'exécution, par le seuil crypto générique (120s), rendant le correctif
    inopérant de bout en bout. Le seuil crypto par défaut reste, lui, inchangé."""
    import bot.runner as runner
    from bot import config

    exchange = runner._exchange_for_wallet(config.wallet_config("equilibre"))

    for sym in ("AAPL", "SPY", "BRK.B"):
        assert exchange.max_quote_age_seconds_for(sym) == pytest.approx(
            config.MAX_QUOTE_AGE_SECONDS_EQUITY
        )
    # la crypto n'est pas affectée par ce câblage (seuil par défaut 120s, INCHANGÉ).
    assert exchange.max_quote_age_seconds_for("BTC") == pytest.approx(config.MAX_QUOTE_AGE_SECONDS)
    assert config.MAX_QUOTE_AGE_SECONDS_EQUITY > config.MAX_QUOTE_AGE_SECONDS
    assert exchange.step_for("BTC") == pytest.approx(0.00001)


# ---------------------------------------------------------------------------
# Paliers de coûts par symbole (majors/mids/smalls) — multi-wallets, wallet agressif
# ---------------------------------------------------------------------------

def test_per_symbol_fee_and_slippage_override_applies_only_to_matching_symbol():
    sim = ExchangeSim(
        fee_taker_bps=10, slippage_penalty_bps=5,
        fee_taker_bps_by_symbol={"XLM": 25}, slippage_penalty_bps_by_symbol={"XLM": 20},
    )
    assert sim.fee_taker_bps_for("XLM") == 25
    assert sim.slippage_penalty_bps_for("XLM") == 20
    # Symbole absent du dict de palier : retombe sur les valeurs de base inchangées.
    assert sim.fee_taker_bps_for("BTC") == 10
    assert sim.slippage_penalty_bps_for("BTC") == 5


# ---------------------------------------------------------------------------
# Seuil de fraîcheur À L'EXÉCUTION par symbole — correctif incident production
# 2026-07-23T18/T19 (actions/ETF Yahoo gratuit structurellement différées ~15-20 min).
# ---------------------------------------------------------------------------

def test_default_120s_threshold_still_rejects_a_900s_quote_for_unconfigured_symbol():
    """Reproduit le second verrou (exécution) qui bloquerait encore les actions/ETF même
    après un correctif purement côté feeds : sans override par symbole, une quote de 900s
    (15 min, délai Yahoo typique) reste rejetée par le seuil crypto générique (120s)."""
    sim = make_sim()  # pas de max_quote_age_seconds_by_symbol -> défaut 120s pour tous
    old_ts = (NOW - timedelta(seconds=900)).isoformat()
    quote = make_quote(bid=100.0, ask=100.2, ts=old_ts, source="yahoo", delayed=True)

    result = sim.execute_order("BUY", "AAPL", 1.0, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(result, Reject)
    assert "périmée" in result.reason


def test_per_symbol_max_quote_age_override_accepts_delayed_equity_quote():
    """Avec l'override actions/ETF (25 min), la même quote de 900s passe désormais — le seuil
    crypto par défaut (120s, `max_quote_age_seconds`) reste inchangé pour les symboles non
    listés dans l'override."""
    sim = ExchangeSim(
        fee_taker_bps=10, slippage_penalty_bps=5,
        max_quote_age_seconds=120.0,
        max_quote_age_seconds_by_symbol={"AAPL": 1500.0},
    )
    old_ts = (NOW - timedelta(seconds=900)).isoformat()
    quote = make_quote(bid=100.0, ask=100.2, ts=old_ts, source="yahoo", delayed=True)

    result = sim.execute_order("BUY", "AAPL", 1.0, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(result, Fill)
    # Le symbole crypto non listé dans l'override garde le seuil par défaut (120s) inchangé.
    assert sim.max_quote_age_seconds_for("BTC") == pytest.approx(120.0)
    assert sim.max_quote_age_seconds_for("AAPL") == pytest.approx(1500.0)


def test_per_symbol_override_still_rejects_beyond_its_own_threshold():
    sim = ExchangeSim(
        fee_taker_bps=10, slippage_penalty_bps=5,
        max_quote_age_seconds_by_symbol={"AAPL": 1500.0},
    )
    too_old_ts = (NOW - timedelta(seconds=1600)).isoformat()
    quote = make_quote(bid=100.0, ask=100.2, ts=too_old_ts, source="yahoo", delayed=True)

    result = sim.execute_order("BUY", "AAPL", 1.0, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(result, Reject)
    assert "périmée" in result.reason


def test_fill_propagates_quote_delayed_flag():
    sim = ExchangeSim(
        fee_taker_bps=10, slippage_penalty_bps=5,
        max_quote_age_seconds_by_symbol={"AAPL": 1500.0},
    )
    old_ts = (NOW - timedelta(seconds=900)).isoformat()
    quote = make_quote(bid=100.0, ask=100.2, ts=old_ts, source="yahoo", delayed=True)

    result = sim.execute_order("BUY", "AAPL", 1.0, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(result, Fill)
    assert result.quote_delayed is True


def test_fill_quote_delayed_false_by_default():
    sim = make_sim()
    quote = make_quote(bid=100.0, ask=100.2)  # delayed=False par défaut, fraîche

    result = sim.execute_order("BUY", "BTC", 1.0, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(result, Fill)
    assert result.quote_delayed is False


def test_reject_propagates_quote_delayed_flag():
    sim = make_sim()  # seuil par défaut 120s, symbole non couvert par un override
    old_ts = (NOW - timedelta(seconds=900)).isoformat()
    quote = make_quote(bid=100.0, ask=100.2, ts=old_ts, source="yahoo", delayed=True)

    result = sim.execute_order("BUY", "AAPL", 1.0, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(result, Reject)
    assert result.quote_delayed is True


def test_per_symbol_fee_override_produces_higher_fees_than_base_rate():
    base_sim = make_sim()
    tiered_sim = ExchangeSim(
        fee_taker_bps=10, slippage_penalty_bps=5,
        fee_taker_bps_by_symbol={"XLM": 25}, slippage_penalty_bps_by_symbol={"XLM": 20},
    )
    quote = make_quote(bid=0.10, ask=0.1002)

    base_fill = base_sim.execute_order("BUY", "XLM", 1000.0, quote, "s", "2026-07-22T14", now=NOW)
    tiered_fill = tiered_sim.execute_order("BUY", "XLM", 1000.0, quote, "s", "2026-07-22T14", now=NOW)

    assert isinstance(base_fill, Fill) and isinstance(tiered_fill, Fill)
    assert tiered_fill.fees_usd > base_fill.fees_usd
    assert tiered_fill.price_fill > base_fill.price_fill  # slippage plus pénalisant
