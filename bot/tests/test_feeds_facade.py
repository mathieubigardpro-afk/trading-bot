"""Tests de la façade `bot.feeds` — routage crypto/actions, fusion des
résultats, propagation des erreurs. Les fonctions de bas niveau
(get_prices_crypto/equity, get_history_crypto/equity) sont monkeypatchées :
ce module teste uniquement le ROUTAGE, pas le parsing réseau (couvert par
test_crypto.py / test_equities.py)."""

import pandas as pd
import pytest

import bot.feeds as feeds
from bot.feeds.types import Quote


def _quote(source):
    return Quote(bid=1.0, ask=1.1, mid=1.05, ts="2026-07-22T00:00:00+00:00", source=source)


def test_get_prices_routes_and_merges_crypto_and_equity(monkeypatch):
    monkeypatch.setattr(feeds, "get_prices_crypto", lambda syms: {s: _quote("binance") for s in syms})
    monkeypatch.setattr(feeds, "get_prices_equity", lambda syms: {s: _quote("yahoo") for s in syms})

    result = feeds.get_prices(["BTC", "AAPL", "ETH"])

    assert list(result.keys()) == ["BTC", "AAPL", "ETH"]  # ordre d'entrée préservé
    assert result["BTC"].source == "binance"
    assert result["ETH"].source == "binance"
    assert result["AAPL"].source == "yahoo"


def test_get_prices_unknown_symbol_returns_none(monkeypatch):
    monkeypatch.setattr(feeds, "get_prices_crypto", lambda syms: {s: _quote("binance") for s in syms})
    monkeypatch.setattr(feeds, "get_prices_equity", lambda syms: {s: _quote("yahoo") for s in syms})

    result = feeds.get_prices(["BTC", "NOTASYMBOL"])

    assert result["BTC"] is not None
    assert result["NOTASYMBOL"] is None


def test_get_prices_only_calls_relevant_adapter(monkeypatch):
    calls = {"crypto": None, "equity": None}

    def fake_crypto(syms):
        calls["crypto"] = list(syms)
        return {s: _quote("binance") for s in syms}

    def fake_equity(syms):
        calls["equity"] = list(syms)
        return {s: _quote("yahoo") for s in syms}

    monkeypatch.setattr(feeds, "get_prices_crypto", fake_crypto)
    monkeypatch.setattr(feeds, "get_prices_equity", fake_equity)

    feeds.get_prices(["BTC"])

    assert calls["crypto"] == ["BTC"]
    assert calls["equity"] is None


def test_get_prices_empty_list_returns_empty_dict():
    assert feeds.get_prices([]) == {}


def test_get_history_routes_to_crypto(monkeypatch):
    sentinel = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]})
    monkeypatch.setattr(feeds, "get_history_crypto", lambda sym, n: sentinel)

    df = feeds.get_history("BTC", 5)

    assert df is sentinel


def test_get_history_routes_to_equity(monkeypatch):
    sentinel = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]})
    monkeypatch.setattr(feeds, "get_history_equity", lambda sym, n: sentinel)

    df = feeds.get_history("AAPL", 5)

    assert df is sentinel


def test_get_history_unknown_symbol_raises_value_error():
    with pytest.raises(ValueError):
        feeds.get_history("NOTASYMBOL", 5)
