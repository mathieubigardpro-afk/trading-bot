"""Tests de `bot.feeds.crypto` — parsing de réponses réelles enregistrées en
fixtures (Binance bookTicker/klines, Coinbase ticker, capturés en direct
depuis ce projet) + simulation d'échecs réseau (mock, jamais d'appel réseau
réel dans les tests)."""

import datetime as _dt
import json
from pathlib import Path

import pytest
import requests

import bot.feeds.crypto as crypto_mod
from bot.feeds.types import HistoryUnavailableError

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json_data


def load_fixture(name):
    with open(FIXTURES / name) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# bookTicker (Binance) — parsing de fixture réelle
# ---------------------------------------------------------------------------


def test_binance_bookticker_parses_real_fixture(monkeypatch):
    data = load_fixture("binance_bookticker_btcusdt.json")
    monkeypatch.setattr(crypto_mod._session, "get", lambda *a, **k: FakeResponse(data))

    q = crypto_mod._fetch_binance_bookticker("BTCUSDT")

    assert q is not None
    assert q.source == "binance"
    assert q.bid == pytest.approx(73474.58)
    assert q.ask == pytest.approx(73474.59)
    assert q.bid < q.ask
    assert q.mid == pytest.approx((73474.58 + 73474.59) / 2)


def test_binance_bookticker_invalid_bid_ask_returns_none(monkeypatch):
    bad = {"symbol": "BTCUSDT", "bidPrice": "100.0", "askPrice": "90.0"}  # bid > ask
    monkeypatch.setattr(crypto_mod._session, "get", lambda *a, **k: FakeResponse(bad))

    assert crypto_mod._fetch_binance_bookticker("BTCUSDT") is None


def test_binance_bookticker_zero_price_returns_none(monkeypatch):
    bad = {"symbol": "BTCUSDT", "bidPrice": "0", "askPrice": "0"}
    monkeypatch.setattr(crypto_mod._session, "get", lambda *a, **k: FakeResponse(bad))

    assert crypto_mod._fetch_binance_bookticker("BTCUSDT") is None


def test_binance_bookticker_malformed_json_returns_none(monkeypatch):
    monkeypatch.setattr(crypto_mod._session, "get", lambda *a, **k: FakeResponse({"unexpected": "shape"}))

    assert crypto_mod._fetch_binance_bookticker("BTCUSDT") is None


def test_binance_bookticker_network_failure_returns_none(monkeypatch):
    def raise_err(*a, **k):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(crypto_mod._session, "get", raise_err)

    assert crypto_mod._fetch_binance_bookticker("BTCUSDT") is None


def test_binance_bookticker_http_error_returns_none(monkeypatch):
    monkeypatch.setattr(crypto_mod._session, "get", lambda *a, **k: FakeResponse({}, status_code=503))

    assert crypto_mod._fetch_binance_bookticker("BTCUSDT") is None


# ---------------------------------------------------------------------------
# ticker (Coinbase) — parsing de fixture réelle
# ---------------------------------------------------------------------------


def test_coinbase_ticker_parses_real_fixture(monkeypatch):
    data = load_fixture("coinbase_ticker_btcusd.json")
    monkeypatch.setattr(crypto_mod._session, "get", lambda *a, **k: FakeResponse(data))

    q = crypto_mod._fetch_coinbase_ticker("BTC-USD")

    assert q is not None
    assert q.source == "coinbase"
    assert q.bid == pytest.approx(78571.17)
    assert q.ask == pytest.approx(78571.18)
    assert q.ts.startswith("2026-05-16T07:03:03")


def test_coinbase_ticker_network_failure_returns_none(monkeypatch):
    def raise_err(*a, **k):
        raise requests.Timeout("timeout")

    monkeypatch.setattr(crypto_mod._session, "get", raise_err)

    assert crypto_mod._fetch_coinbase_ticker("BTC-USD") is None


# ---------------------------------------------------------------------------
# get_prices_crypto — orchestration primaire/fallback/fraîcheur
# ---------------------------------------------------------------------------


def _fresh_coinbase_payload(bid="100.0", ask="100.5"):
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return {"bid": bid, "ask": ask, "time": now_iso}


def test_get_prices_crypto_uses_binance_when_available(monkeypatch):
    data = load_fixture("binance_bookticker_btcusdt.json")
    monkeypatch.setattr(crypto_mod._session, "get", lambda *a, **k: FakeResponse(data))

    result = crypto_mod.get_prices_crypto(["BTC"])

    assert result["BTC"] is not None
    assert result["BTC"].source == "binance"


def test_get_prices_crypto_falls_back_to_coinbase_when_binance_fails(monkeypatch):
    def fake_get(url, *a, **k):
        if "bookTicker" in url:
            raise requests.ConnectionError("binance down")
        return FakeResponse(_fresh_coinbase_payload())

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    result = crypto_mod.get_prices_crypto(["BTC"])

    assert result["BTC"] is not None
    assert result["BTC"].source == "coinbase"
    assert result["BTC"].bid == pytest.approx(100.0)


def test_get_prices_crypto_both_sources_fail_returns_none(monkeypatch):
    def raise_err(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(crypto_mod._session, "get", raise_err)

    result = crypto_mod.get_prices_crypto(["BTC"])

    assert result["BTC"] is None


def test_get_prices_crypto_stale_coinbase_fallback_rejected(monkeypatch):
    # La fixture réelle Coinbase a un horodatage de mai 2026, largement
    # périmé par rapport à "maintenant" -> doit être rejetée (jamais un prix
    # périmé n'est retourné, même en dernier recours).
    stale_data = load_fixture("coinbase_ticker_btcusd.json")

    def fake_get(url, *a, **k):
        if "bookTicker" in url:
            raise requests.ConnectionError("binance down")
        return FakeResponse(stale_data)

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    result = crypto_mod.get_prices_crypto(["BTC"])

    assert result["BTC"] is None


def test_get_prices_crypto_multi_symbol_independent_results(monkeypatch):
    btc_data = load_fixture("binance_bookticker_btcusdt.json")
    eth_data = load_fixture("binance_bookticker_ethusdt.json")

    def fake_get(url, params=None, timeout=None):
        symbol = (params or {}).get("symbol")
        if symbol == "BTCUSDT":
            return FakeResponse(btc_data)
        if symbol == "ETHUSDT":
            return FakeResponse(eth_data)
        raise requests.ConnectionError("unknown pair, binance down")

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    result = crypto_mod.get_prices_crypto(["BTC", "ETH"])

    assert result["BTC"].source == "binance"
    assert result["ETH"].source == "binance"
    assert result["BTC"].bid != result["ETH"].bid


def test_quote_is_fresh_boundaries():
    # Marges volontaires (pas de pile-poil sur la limite à 300s) pour ne
    # jamais rendre ce test flaky à cause du temps réel écoulé entre la
    # construction de `now` ici et l'appel à `_now_utc()` dans la fonction.
    now = _dt.datetime.now(_dt.timezone.utc)
    assert crypto_mod._quote_is_fresh(now, 300) is True
    assert crypto_mod._quote_is_fresh(now - _dt.timedelta(seconds=310), 300) is False
    assert crypto_mod._quote_is_fresh(now - _dt.timedelta(seconds=290), 300) is True
    assert crypto_mod._quote_is_fresh(now + _dt.timedelta(seconds=2), 300) is True  # léger skew toléré
    assert crypto_mod._quote_is_fresh(now + _dt.timedelta(seconds=60), 300) is False  # horodatage futur suspect


# ---------------------------------------------------------------------------
# get_history_crypto — klines Binance, exclusion de la bougie en formation
# ---------------------------------------------------------------------------


def _make_binance_klines_rows(n_closed, now=None):
    """n_closed bougies clôturées se terminant juste avant l'heure en cours
    en plus d'UNE bougie supplémentaire encore en formation (close_time dans
    le futur) — cette dernière ne doit JAMAIS apparaître dans le résultat."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    current_hour_start = now.replace(minute=0, second=0, microsecond=0)
    rows = []
    for i in range(n_closed, 0, -1):
        open_time = current_hour_start - _dt.timedelta(hours=i)
        close_time = open_time + _dt.timedelta(hours=1) - _dt.timedelta(milliseconds=1)
        rows.append(
            [
                int(open_time.timestamp() * 1000), "100.0", "101.0", "99.0", "100.5", "10.0",
                int(close_time.timestamp() * 1000), "1000.0", "5", "5.0", "500.0", "0",
            ]
        )
    # bougie en cours de formation (open_time = heure courante)
    open_time = current_hour_start
    close_time = open_time + _dt.timedelta(hours=1) - _dt.timedelta(milliseconds=1)
    rows.append(
        [
            int(open_time.timestamp() * 1000), "999", "999", "999", "999", "999",
            int(close_time.timestamp() * 1000), "1", "1", "1", "1", "0",
        ]
    )
    return rows


def test_get_history_crypto_excludes_forming_candle(monkeypatch):
    rows = _make_binance_klines_rows(10)

    def fake_get(url, *a, **k):
        assert "klines" in url
        return FakeResponse(rows)

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    df = crypto_mod.get_history_crypto("BTC", 10)

    assert len(df) == 10
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert (df["close"] != 999.0).all()
    assert df.index.is_monotonic_increasing


def test_get_history_crypto_raises_when_insufficient_everywhere(monkeypatch):
    rows = _make_binance_klines_rows(5)

    def fake_get(url, *a, **k):
        if "klines" in url:
            return FakeResponse(rows)
        if "candles" in url:
            return FakeResponse([])
        raise AssertionError(f"URL inattendue: {url}")

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    with pytest.raises(HistoryUnavailableError):
        crypto_mod.get_history_crypto("BTC", 10)


def test_get_history_crypto_falls_back_to_coinbase_candles(monkeypatch):
    few_rows = _make_binance_klines_rows(2)
    now = _dt.datetime.now(_dt.timezone.utc)
    current_hour_start = now.replace(minute=0, second=0, microsecond=0)

    def coinbase_candles(n):
        return [
            [int((current_hour_start - _dt.timedelta(hours=i)).timestamp()), 99.0, 101.0, 100.0, 100.5, 10.0]
            for i in range(1, n + 1)
        ]

    def fake_get(url, *a, **k):
        if "klines" in url:
            return FakeResponse(few_rows)
        if "candles" in url:
            return FakeResponse(coinbase_candles(300))
        raise AssertionError(f"URL inattendue: {url}")

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    df = crypto_mod.get_history_crypto("BTC", 50)

    assert len(df) == 50
    assert df.index.is_monotonic_increasing


def test_get_history_crypto_network_failure_both_sources_raises(monkeypatch):
    def raise_err(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(crypto_mod._session, "get", raise_err)

    with pytest.raises(HistoryUnavailableError):
        crypto_mod.get_history_crypto("BTC", 10)
