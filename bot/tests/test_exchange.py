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


def make_quote(bid, ask, ts=None, source="binance"):
    ts = ts or NOW.isoformat()
    mid = (bid + ask) / 2
    return Quote(bid=bid, ask=ask, mid=mid, ts=ts, source=source)


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
