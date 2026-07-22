"""Tests de bot/sim/ledger.py — Ledger.

Couvre : mise à jour cash/position/prix moyen sur achat et vente, PnL réalisé, invariants
(cash >= 0, position >= 0, equity cohérente), et les rejets obligatoires : achat sans cash
suffisant, survente (long-only strict), equity avec prix de marque manquant.
"""

import random

import pytest

from bot.sim import Fill, Ledger


def make_fill(side, symbol, qty, price_fill, mid=None, fees_bps=10, run_id="2026-07-22T14"):
    mid = mid if mid is not None else price_fill
    notional = qty * price_fill
    fees = notional * fees_bps / 1e4
    slippage = abs(price_fill - mid) * qty
    return Fill(
        run_id=run_id,
        ts="2026-07-22T14:00:00+00:00",
        symbol=symbol,
        strategy="test_strat",
        side=side,
        qty=qty,
        notional_usd=notional,
        price_fill=price_fill,
        price_mid_ideal=mid,
        fees_usd=fees,
        slippage_usd=slippage,
        quote_source="binance",
        quote_ts="2026-07-22T14:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Achat
# ---------------------------------------------------------------------------

def test_buy_updates_cash_and_position():
    ledger = Ledger(cash_usd=100_000.0)
    fill = make_fill("BUY", "BTC", qty=1.0, price_fill=60_000.0)

    ledger.apply_fill(fill)

    expected_cash = 100_000.0 - (fill.notional_usd + fill.fees_usd)
    assert ledger.cash_usd == pytest.approx(expected_cash)
    assert ledger.positions["BTC"]["qty"] == pytest.approx(1.0)
    assert ledger.positions["BTC"]["prix_moyen"] == pytest.approx(60_000.0)
    assert fill.realized_pnl_usd is None


def test_buy_computes_weighted_average_price_on_second_buy():
    ledger = Ledger(cash_usd=1_000_000.0)
    fill1 = make_fill("BUY", "ETH", qty=1.0, price_fill=1000.0)
    fill2 = make_fill("BUY", "ETH", qty=1.0, price_fill=3000.0)

    ledger.apply_fill(fill1)
    ledger.apply_fill(fill2)

    # moyenne pondérée par notionnel : (1*1000 + 1*3000) / 2 = 2000
    assert ledger.positions["ETH"]["qty"] == pytest.approx(2.0)
    assert ledger.positions["ETH"]["prix_moyen"] == pytest.approx(2000.0)


def test_cannot_buy_without_enough_cash():
    ledger = Ledger(cash_usd=100.0)
    fill = make_fill("BUY", "BTC", qty=1.0, price_fill=60_000.0)

    with pytest.raises(ValueError):
        ledger.apply_fill(fill)

    # état inchangé après l'échec
    assert ledger.cash_usd == pytest.approx(100.0)
    assert "BTC" not in ledger.positions


# ---------------------------------------------------------------------------
# Vente
# ---------------------------------------------------------------------------

def test_sell_updates_cash_and_realized_pnl():
    ledger = Ledger(cash_usd=0.0, positions={"BTC": {"qty": 1.0, "prix_moyen": 50_000.0}})
    fill = make_fill("SELL", "BTC", qty=1.0, price_fill=55_000.0)

    ledger.apply_fill(fill)

    expected_pnl = (55_000.0 - 50_000.0) * 1.0 - fill.fees_usd
    assert fill.realized_pnl_usd == pytest.approx(expected_pnl)
    assert ledger.cash_usd == pytest.approx(fill.notional_usd - fill.fees_usd)
    assert "BTC" not in ledger.positions  # position soldée, retirée du dict


def test_sell_partial_keeps_average_price_unchanged():
    ledger = Ledger(cash_usd=0.0, positions={"BTC": {"qty": 2.0, "prix_moyen": 50_000.0}})
    fill = make_fill("SELL", "BTC", qty=1.0, price_fill=55_000.0)

    ledger.apply_fill(fill)

    assert ledger.positions["BTC"]["qty"] == pytest.approx(1.0)
    assert ledger.positions["BTC"]["prix_moyen"] == pytest.approx(50_000.0)


def test_cannot_oversell_long_only_strict():
    ledger = Ledger(cash_usd=0.0, positions={"BTC": {"qty": 1.0, "prix_moyen": 50_000.0}})
    fill = make_fill("SELL", "BTC", qty=1.5, price_fill=55_000.0)  # plus que détenu

    with pytest.raises(ValueError):
        ledger.apply_fill(fill)

    # état inchangé après l'échec
    assert ledger.positions["BTC"]["qty"] == pytest.approx(1.0)
    assert ledger.cash_usd == pytest.approx(0.0)


def test_cannot_sell_symbol_not_held():
    ledger = Ledger(cash_usd=0.0)
    fill = make_fill("SELL", "SOL", qty=1.0, price_fill=100.0)

    with pytest.raises(ValueError):
        ledger.apply_fill(fill)


# ---------------------------------------------------------------------------
# Equity mark-to-market
# ---------------------------------------------------------------------------

def test_equity_cash_plus_positions_marked_to_mid():
    ledger = Ledger(
        cash_usd=10_000.0,
        positions={
            "BTC": {"qty": 1.0, "prix_moyen": 50_000.0},
            "ETH": {"qty": 2.0, "prix_moyen": 2_000.0},
        },
    )

    equity = ledger.equity({"BTC": 60_000.0, "ETH": 2_500.0})

    assert equity == pytest.approx(10_000.0 + 60_000.0 + 5_000.0)


def test_equity_raises_if_mark_price_missing_for_held_position():
    ledger = Ledger(cash_usd=0.0, positions={"BTC": {"qty": 1.0, "prix_moyen": 50_000.0}})

    with pytest.raises(ValueError):
        ledger.equity({})  # pas de prix pour BTC alors qu'il est détenu


def test_equity_with_only_cash_no_positions():
    ledger = Ledger(cash_usd=100_000.0)
    assert ledger.equity({}) == pytest.approx(100_000.0)


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------

def test_initial_negative_cash_rejected():
    with pytest.raises(ValueError):
        Ledger(cash_usd=-1.0)


def test_initial_negative_position_rejected():
    with pytest.raises(ValueError):
        Ledger(cash_usd=1000.0, positions={"BTC": {"qty": -1.0, "prix_moyen": 100.0}})


@pytest.mark.parametrize("seed", range(30))
def test_property_cash_never_negative_across_random_buy_sequences(seed):
    """Propriété : une séquence d'achats qui respecte le cash disponible ne doit jamais
    laisser le ledger dans un état cash négatif, quel que soit l'enchaînement de prix."""
    rng = random.Random(seed)
    ledger = Ledger(cash_usd=100_000.0)

    for _ in range(20):
        price = rng.uniform(1, 70_000)
        # on ne dépense jamais plus qu'une fraction du cash restant, pour rester dans le cas
        # nominal (le cas de rejet est couvert par test_cannot_buy_without_enough_cash)
        max_affordable_notional = ledger.cash_usd * 0.5
        if max_affordable_notional < 10:
            break
        notional = rng.uniform(10, max_affordable_notional)
        qty = notional / price
        fill = make_fill("BUY", "BTC", qty=qty, price_fill=price)
        ledger.apply_fill(fill)
        assert ledger.cash_usd >= -1e-6


def test_realized_pnl_negative_on_loss():
    ledger = Ledger(cash_usd=0.0, positions={"SOL": {"qty": 10.0, "prix_moyen": 150.0}})
    fill = make_fill("SELL", "SOL", qty=10.0, price_fill=140.0)  # vendu moins cher qu'acheté

    ledger.apply_fill(fill)

    assert fill.realized_pnl_usd < 0
