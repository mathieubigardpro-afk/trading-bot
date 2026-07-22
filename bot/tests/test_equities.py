"""Tests de `bot.feeds.equities` — parsing du schéma Yahoo Finance v7/v8
(structure figée et largement documentée ; confirmée empiriquement via la
page web finance.yahoo.com/quote/AAPL pendant le développement de ce module,
les endpoints JSON bruts étant bloqués par robots.txt pour l'outil de fetch
disponible dans cet environnement de build — voir note de livraison) +
simulation d'échecs réseau (mock, jamais d'appel réseau réel dans les
tests)."""

import datetime as _dt

import pandas as pd
import pytest
import requests

import bot.feeds.equities as equities_mod
from bot.feeds.types import HistoryUnavailableError


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json_data


def _quote_payload(symbol="AAPL", bid=325.02, ask=329.97, price=326.59, market_time=None, market_state="REGULAR"):
    market_time = market_time or int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    return {
        "quoteResponse": {
            "result": [
                {
                    "symbol": symbol,
                    "bid": bid,
                    "ask": ask,
                    "bidSize": 400,
                    "askSize": 400,
                    "regularMarketPrice": price,
                    "regularMarketTime": market_time,
                    "marketState": market_state,
                }
            ],
            "error": None,
        }
    }


# ---------------------------------------------------------------------------
# get_prices_equity
# ---------------------------------------------------------------------------


def test_get_prices_equity_parses_valid_quote(monkeypatch):
    payload = _quote_payload()
    monkeypatch.setattr(equities_mod._session, "get", lambda *a, **k: FakeResponse(payload))

    result = equities_mod.get_prices_equity(["AAPL"])

    q = result["AAPL"]
    assert q is not None
    assert q.source == "yahoo"
    assert q.bid == pytest.approx(325.02)
    assert q.ask == pytest.approx(329.97)
    assert q.bid < q.ask


def test_get_prices_equity_missing_bid_ask_returns_none_by_default(monkeypatch):
    payload = _quote_payload()
    payload["quoteResponse"]["result"][0]["bid"] = None
    payload["quoteResponse"]["result"][0]["ask"] = None
    monkeypatch.setattr(equities_mod._session, "get", lambda *a, **k: FakeResponse(payload))

    result = equities_mod.get_prices_equity(["AAPL"])

    assert result["AAPL"] is None


def test_get_prices_equity_synthetic_spread_when_explicitly_enabled(monkeypatch):
    payload = _quote_payload()
    payload["quoteResponse"]["result"][0]["bid"] = None
    payload["quoteResponse"]["result"][0]["ask"] = None
    monkeypatch.setattr(equities_mod._session, "get", lambda *a, **k: FakeResponse(payload))
    monkeypatch.setattr(equities_mod.cfg, "EQUITY_SYNTHETIC_SPREAD_ENABLED", True)

    result = equities_mod.get_prices_equity(["AAPL"])

    q = result["AAPL"]
    assert q is not None
    assert q.source == "yahoo_synthetic_spread"
    assert q.bid < q.mid < q.ask
    assert q.mid == pytest.approx(326.59)


def test_get_prices_equity_invalid_bid_ask_crossed_returns_none(monkeypatch):
    payload = _quote_payload(bid=330.0, ask=325.0)  # bid > ask, incohérent
    monkeypatch.setattr(equities_mod._session, "get", lambda *a, **k: FakeResponse(payload))

    result = equities_mod.get_prices_equity(["AAPL"])

    assert result["AAPL"] is None


def test_get_prices_equity_stale_quote_returns_none(monkeypatch):
    old_time = int((_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=5)).timestamp())
    payload = _quote_payload(market_time=old_time)
    monkeypatch.setattr(equities_mod._session, "get", lambda *a, **k: FakeResponse(payload))

    result = equities_mod.get_prices_equity(["AAPL"])

    assert result["AAPL"] is None


def test_get_prices_equity_network_failure_returns_none_for_all(monkeypatch):
    def raise_err(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(equities_mod._session, "get", raise_err)

    result = equities_mod.get_prices_equity(["AAPL", "MSFT"])

    assert result == {"AAPL": None, "MSFT": None}


def test_get_prices_equity_symbol_missing_from_response_returns_none(monkeypatch):
    payload = _quote_payload(symbol="AAPL")  # ne contient pas MSFT
    monkeypatch.setattr(equities_mod._session, "get", lambda *a, **k: FakeResponse(payload))

    result = equities_mod.get_prices_equity(["AAPL", "MSFT"])

    assert result["AAPL"] is not None
    assert result["MSFT"] is None


def test_get_prices_equity_empty_symbol_list_returns_empty_dict():
    assert equities_mod.get_prices_equity([]) == {}


# ---------------------------------------------------------------------------
# get_history_equity
# ---------------------------------------------------------------------------


def _chart_payload(n_closed, now=None, with_null_gap=False):
    now = now or _dt.datetime.now(_dt.timezone.utc)
    current_hour_start = now.replace(minute=0, second=0, microsecond=0)

    timestamps = []
    opens, highs, lows, closes, volumes = [], [], [], [], []

    for i in range(n_closed, 0, -1):
        t = current_hour_start - _dt.timedelta(hours=i)
        timestamps.append(int(t.timestamp()))
        opens.append(100.0)
        highs.append(101.0)
        lows.append(99.0)
        closes.append(100.5)
        volumes.append(1000.0)

    if with_null_gap:
        # Un trou de données (hors séance) au milieu -> doit être filtré.
        idx = len(timestamps) // 2
        closes[idx] = None

    # Bougie en cours de formation : ne doit jamais apparaître dans le résultat.
    timestamps.append(int(current_hour_start.timestamp()))
    opens.append(999.0)
    highs.append(999.0)
    lows.append(999.0)
    closes.append(999.0)
    volumes.append(999.0)

    return {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": "AAPL", "currency": "USD"},
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": opens,
                                "high": highs,
                                "low": lows,
                                "close": closes,
                                "volume": volumes,
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def test_get_history_equity_parses_valid_chart_and_excludes_forming_candle(monkeypatch):
    payload = _chart_payload(10)
    monkeypatch.setattr(equities_mod._session, "get", lambda *a, **k: FakeResponse(payload))

    df = equities_mod.get_history_equity("AAPL", 10)

    assert len(df) == 10
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert (df["close"] != 999.0).all()
    assert df.index.is_monotonic_increasing
    assert isinstance(df.index, pd.DatetimeIndex)


def test_get_history_equity_filters_null_gaps(monkeypatch):
    payload = _chart_payload(11, with_null_gap=True)
    monkeypatch.setattr(equities_mod._session, "get", lambda *a, **k: FakeResponse(payload))

    df = equities_mod.get_history_equity("AAPL", 10)

    # 11 bougies générées, 1 trouée (filtrée) -> il n'en reste que 10 valides.
    assert len(df) == 10
    assert not df["close"].isna().any()


def test_get_history_equity_raises_when_insufficient(monkeypatch):
    payload = _chart_payload(5)
    monkeypatch.setattr(equities_mod._session, "get", lambda *a, **k: FakeResponse(payload))

    with pytest.raises(HistoryUnavailableError):
        equities_mod.get_history_equity("AAPL", 10)


def test_get_history_equity_network_failure_raises(monkeypatch):
    def raise_err(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(equities_mod._session, "get", raise_err)

    with pytest.raises(HistoryUnavailableError):
        equities_mod.get_history_equity("AAPL", 10)


def test_get_history_equity_malformed_payload_raises(monkeypatch):
    monkeypatch.setattr(equities_mod._session, "get", lambda *a, **k: FakeResponse({"chart": {"result": []}}))

    with pytest.raises(HistoryUnavailableError):
        equities_mod.get_history_equity("AAPL", 10)
