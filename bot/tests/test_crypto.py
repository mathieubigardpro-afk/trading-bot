"""Tests de `bot.feeds.crypto` — parsing de réponses réelles enregistrées en
fixtures (Binance bookTicker/klines, Coinbase ticker, capturés en direct
depuis ce projet) + simulation d'échecs réseau (mock, jamais d'appel réseau
réel dans les tests)."""

import datetime as _dt
import json
from pathlib import Path

import pandas as pd
import pytest
import requests

import bot.feeds.crypto as crypto_mod
import bot.feeds.daily as daily_mod
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
# ticker (Kraken) — parsing de fixture réelle (correctif 2026-07-27, repli intermédiaire)
# ---------------------------------------------------------------------------


def test_kraken_ticker_parses_real_fixture(monkeypatch):
    data = load_fixture("kraken_ticker_xbtusd.json")
    monkeypatch.setattr(crypto_mod._session, "get", lambda *a, **k: FakeResponse(data))

    q = crypto_mod._fetch_kraken_ticker("XBTUSD")

    assert q is not None
    assert q.source == "kraken"
    assert q.bid == pytest.approx(73479.90)
    assert q.ask == pytest.approx(73480.20)
    assert q.bid < q.ask
    assert q.mid == pytest.approx((73479.90 + 73480.20) / 2)


def test_kraken_ticker_api_error_field_returns_none(monkeypatch):
    # Kraken signale une erreur applicative (ex. paire inconnue) via le champ "error" du corps
    # JSON, pas via un status HTTP -- ne doit jamais lever, ni être confondu avec un succès.
    bad = {"error": ["EQuery:Unknown asset pair"], "result": {}}
    monkeypatch.setattr(crypto_mod._session, "get", lambda *a, **k: FakeResponse(bad))

    assert crypto_mod._fetch_kraken_ticker("NOPEUSD") is None


def test_kraken_ticker_empty_result_returns_none(monkeypatch):
    monkeypatch.setattr(crypto_mod._session, "get", lambda *a, **k: FakeResponse({"error": [], "result": {}}))

    assert crypto_mod._fetch_kraken_ticker("XBTUSD") is None


def test_kraken_ticker_invalid_bid_ask_returns_none(monkeypatch):
    bad = {"error": [], "result": {"XXBTZUSD": {"a": ["10.0"], "b": ["20.0"]}}}  # bid > ask
    monkeypatch.setattr(crypto_mod._session, "get", lambda *a, **k: FakeResponse(bad))

    assert crypto_mod._fetch_kraken_ticker("XBTUSD") is None


def test_kraken_ticker_network_failure_returns_none(monkeypatch):
    def raise_err(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(crypto_mod._session, "get", raise_err)

    assert crypto_mod._fetch_kraken_ticker("XBTUSD") is None


def test_kraken_ticker_geo_blocked_451_returns_none_without_exception(monkeypatch):
    monkeypatch.setattr(crypto_mod._session, "get", lambda *a, **k: FakeResponse({}, status_code=451))

    assert crypto_mod._fetch_kraken_ticker("XBTUSD") is None


# ---------------------------------------------------------------------------
# get_prices_crypto — orchestration Coinbase (primaire) -> Kraken -> Binance (dernier recours)
# correctif 2026-07-27 : Binance renvoie HTTP 451 sur 100% des cycles depuis les runners
# GitHub Actions (géo-blocage) -- rétrogradé en dernier recours documenté.
# ---------------------------------------------------------------------------


def _fresh_coinbase_payload(bid="100.0", ask="100.5"):
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return {"bid": bid, "ask": ask, "time": now_iso}


def _kraken_payload(bid="200.0", ask="200.5"):
    return {"error": [], "result": {"XXBTZUSD": {"a": [ask], "b": [bid]}}}


def test_get_prices_crypto_uses_coinbase_when_available(monkeypatch):
    def fake_get(url, *a, **k):
        assert "ticker" in url and "kraken" not in url.lower() and "binance" not in url.lower()
        return FakeResponse(_fresh_coinbase_payload())

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    result = crypto_mod.get_prices_crypto(["BTC"])

    assert result["BTC"] is not None
    assert result["BTC"].source == "coinbase"
    assert result["BTC"].bid == pytest.approx(100.0)


def test_get_prices_crypto_falls_back_to_kraken_when_coinbase_fails(monkeypatch):
    def fake_get(url, *a, **k):
        if "exchange.coinbase.com" in url:
            raise requests.HTTPError("status 429")
        if "kraken.com" in url:
            return FakeResponse(_kraken_payload())
        raise AssertionError(f"binance ne devrait pas être appelé ici: {url}")

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    result = crypto_mod.get_prices_crypto(["BTC"])

    assert result["BTC"] is not None
    assert result["BTC"].source == "kraken"
    assert result["BTC"].bid == pytest.approx(200.0)


def test_get_prices_crypto_falls_back_to_binance_as_last_resort(monkeypatch):
    binance_data = load_fixture("binance_bookticker_btcusdt.json")

    def fake_get(url, *a, **k):
        if "exchange.coinbase.com" in url:
            raise requests.ConnectionError("coinbase down")
        if "kraken.com" in url:
            raise requests.HTTPError("status 451")  # géo-blocage éventuel côté Kraken aussi
        if "api.binance.com" in url:
            return FakeResponse(binance_data)
        raise AssertionError(f"URL inattendue: {url}")

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    result = crypto_mod.get_prices_crypto(["BTC"])

    assert result["BTC"] is not None
    assert result["BTC"].source == "binance"


def test_get_prices_crypto_binance_451_geo_block_treated_as_ordinary_failure(monkeypatch):
    """Diagnostic 2026-07-27 : Binance renvoie HTTP 451 sur 100% des cycles depuis les runners
    -- ce n'est qu'un échec de source parmi d'autres, jamais une exception qui remonte."""
    def fake_get(url, *a, **k):
        if "api.binance.com" in url:
            return FakeResponse({}, status_code=451)
        raise requests.ConnectionError("down")

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    result = crypto_mod.get_prices_crypto(["BTC"])

    assert result["BTC"] is None


def test_get_prices_crypto_all_three_sources_fail_returns_none(monkeypatch):
    def raise_err(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(crypto_mod._session, "get", raise_err)

    result = crypto_mod.get_prices_crypto(["BTC"])

    assert result["BTC"] is None


def test_get_prices_crypto_stale_coinbase_rejected_falls_through(monkeypatch):
    # La fixture réelle Coinbase a un horodatage de mai 2026, largement périmé par rapport à
    # "maintenant" -> doit être rejetée même en source primaire, puis le repli Kraken utilisé.
    stale_data = load_fixture("coinbase_ticker_btcusd.json")

    def fake_get(url, *a, **k):
        if "exchange.coinbase.com" in url:
            return FakeResponse(stale_data)
        if "kraken.com" in url:
            return FakeResponse(_kraken_payload())
        raise AssertionError(f"binance ne devrait pas être appelé ici: {url}")

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    result = crypto_mod.get_prices_crypto(["BTC"])

    assert result["BTC"] is not None
    assert result["BTC"].source == "kraken"


def test_get_prices_crypto_symbol_without_kraken_mapping_skips_to_binance(monkeypatch):
    # BNB n'est intentionnellement pas dans bot.config.CRYPTO_PAIR_KRAKEN (absent chez Kraken,
    # cf. bandeau bot/config.py) -- si Coinbase échoue, le repli saute directement à Binance.
    monkeypatch.setattr(crypto_mod.cfg, "CRYPTO_PAIR_COINBASE", {"BNB": "BNB-USD"})
    monkeypatch.setattr(crypto_mod.cfg, "CRYPTO_PAIR_KRAKEN", {})  # pas d'entrée BNB
    monkeypatch.setattr(crypto_mod.cfg, "CRYPTO_PAIR_BINANCE", {"BNB": "BNBUSDT"})

    def fake_get(url, *a, **k):
        if "exchange.coinbase.com" in url:
            raise requests.ConnectionError("coinbase down")
        if "kraken.com" in url:
            raise AssertionError("kraken ne devrait jamais être appelé pour un symbole non couvert")
        if "api.binance.com" in url:
            return FakeResponse(load_fixture("binance_bookticker_btcusdt.json"))
        raise AssertionError(f"URL inattendue: {url}")

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    result = crypto_mod.get_prices_crypto(["BNB"])

    assert result["BNB"] is not None
    assert result["BNB"].source == "binance"


def test_get_prices_crypto_multi_symbol_independent_results(monkeypatch):
    def fake_get(url, *a, **k):
        if "BTC-USD" in url:
            return FakeResponse(_fresh_coinbase_payload(bid="100.0", ask="100.5"))
        if "ETH-USD" in url:
            return FakeResponse(_fresh_coinbase_payload(bid="50.0", ask="50.5"))
        raise AssertionError(f"URL inattendue: {url}")

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    result = crypto_mod.get_prices_crypto(["BTC", "ETH"])

    assert result["BTC"].source == "coinbase"
    assert result["ETH"].source == "coinbase"
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


# ---------------------------------------------------------------------------
# get_history_crypto — correctif 2026-07-27 (feed crypto aveugle) : la portion réseau
# "vivante" est plafonnée à `_RECENT_HOURLY_WINDOW_HOURS`, la portion plus ancienne complétée
# depuis le cache disque JOURNALIER (jamais par une pagination réseau profonde).
# ---------------------------------------------------------------------------

_FIXED_NOW = _dt.datetime(2026, 7, 27, 10, 30, tzinfo=_dt.timezone.utc)


def _write_daily_cache(cache_dir, symbol, n_days, generated_at, start=1000.0):
    idx = pd.date_range(
        end=(generated_at - _dt.timedelta(days=1)).date().isoformat(), periods=n_days, freq="D", tz="UTC"
    )
    closes = [start + i for i in range(n_days)]
    df = pd.DataFrame(
        {
            "open": closes, "high": [c * 1.001 for c in closes], "low": [c * 0.999 for c in closes],
            "close": closes, "volume": [1.0] * n_days,
        },
        index=pd.Index(idx, name="ts"),
    )
    daily_mod.write_cache_symbol_csv(str(cache_dir), "crypto", symbol, df)
    daily_mod.write_cache_manifest(str(cache_dir), generated_at)
    return df


def _coinbase_live_window_rows(now, n_hours=300):
    current_hour_start = now.replace(minute=0, second=0, microsecond=0)
    return [
        [int((current_hour_start - _dt.timedelta(hours=i)).timestamp()), 99.0, 101.0, 100.0, 5000.0 + i, 10.0]
        for i in range(1, n_hours + 1)
    ]


def test_get_history_crypto_small_request_never_touches_cache(tmp_path, monkeypatch):
    """Pour n_hours <= la fenêtre vivante (300h), le comportement est STRICTEMENT INCHANGÉ :
    le cache disque n'est jamais consulté (pas d'accès disque, aucun `BOT_DATA_CACHE_DIR`
    requis)."""
    monkeypatch.setattr(crypto_mod, "_now_utc", lambda: _FIXED_NOW)
    monkeypatch.setenv("BOT_DATA_CACHE_DIR", str(tmp_path / "does-not-exist"))
    rows = _make_binance_klines_rows(10, now=_FIXED_NOW)
    monkeypatch.setattr(crypto_mod._session, "get", lambda url, *a, **k: FakeResponse(rows))

    df = crypto_mod.get_history_crypto("BTC", 10)

    assert len(df) == 10


def test_get_history_crypto_large_request_capped_network_plus_daily_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto_mod, "_now_utc", lambda: _FIXED_NOW)
    cache_dir = tmp_path / "data-cache"
    monkeypatch.setenv("BOT_DATA_CACHE_DIR", str(cache_dir))
    _write_daily_cache(cache_dir, "BTC", n_days=250, generated_at=_FIXED_NOW)

    call_count = {"n": 0}

    def fake_get(url, *a, **k):
        call_count["n"] += 1
        if "api.binance.com" in url:
            return FakeResponse({}, status_code=451)  # géo-blocage systématique (diagnostic 2026-07-27)
        if "exchange.coinbase.com" in url:
            return FakeResponse(_coinbase_live_window_rows(_FIXED_NOW))
        raise AssertionError(f"URL inattendue: {url}")

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    n_hours = 4848  # bot.config.HISTORY_N_HOURS
    df = crypto_mod.get_history_crypto("BTC", n_hours)

    assert len(df) == n_hours
    assert df.index.is_monotonic_increasing
    assert not df.index.duplicated().any()
    # Plafond de requêtes strict (objectif mesuré ~290/jour au lieu de ~4 900, cf. bandeau
    # module) : 1 échec Binance (451) + 1 seule page Coinbase, QUEL QUE SOIT n_hours demandé.
    assert call_count["n"] == 2

    # La portion ancienne provient bien du cache : les ~24 dernières heures de la fenêtre
    # RÉCENTE gardent les clôtures réseau (5000+), la portion plus ancienne les clôtures
    # cache (1000+).
    assert df["close"].iloc[-1] >= 5000.0
    assert df["close"].iloc[0] < 2000.0


def test_get_history_crypto_request_count_capped_even_when_cache_missing(tmp_path, monkeypatch):
    """Sans cache disque disponible, AUCUNE pagination profonde n'est tentée en repli -- le
    symbole est simplement marqué indisponible ce cycle (no-trade strict), jamais un orage de
    requêtes comme l'incident 429 du 2026-07-27."""
    monkeypatch.setattr(crypto_mod, "_now_utc", lambda: _FIXED_NOW)
    monkeypatch.setenv("BOT_DATA_CACHE_DIR", str(tmp_path / "does-not-exist"))

    call_count = {"n": 0}

    def fake_get(url, *a, **k):
        call_count["n"] += 1
        if "api.binance.com" in url:
            return FakeResponse({}, status_code=451)
        if "exchange.coinbase.com" in url:
            return FakeResponse(_coinbase_live_window_rows(_FIXED_NOW))
        raise AssertionError(f"URL inattendue: {url}")

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    with pytest.raises(HistoryUnavailableError):
        crypto_mod.get_history_crypto("BTC", 4848)

    assert call_count["n"] == 2  # jamais de pagination profonde de secours


def test_get_history_crypto_stale_daily_cache_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto_mod, "_now_utc", lambda: _FIXED_NOW)
    cache_dir = tmp_path / "data-cache"
    monkeypatch.setenv("BOT_DATA_CACHE_DIR", str(cache_dir))
    stale_generated_at = _FIXED_NOW - _dt.timedelta(days=daily_mod.CACHE_MAX_AGE_DAYS + 1)
    _write_daily_cache(cache_dir, "BTC", n_days=250, generated_at=stale_generated_at)

    def fake_get(url, *a, **k):
        if "api.binance.com" in url:
            return FakeResponse({}, status_code=451)
        if "exchange.coinbase.com" in url:
            return FakeResponse(_coinbase_live_window_rows(_FIXED_NOW))
        raise AssertionError(f"URL inattendue: {url}")

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    with pytest.raises(HistoryUnavailableError):
        crypto_mod.get_history_crypto("BTC", 4848)


def test_get_history_crypto_binance_451_coinbase_429_no_exception_leaks(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto_mod, "_now_utc", lambda: _FIXED_NOW)
    monkeypatch.setenv("BOT_DATA_CACHE_DIR", str(tmp_path / "does-not-exist"))

    def fake_get(url, *a, **k):
        if "api.binance.com" in url:
            return FakeResponse({}, status_code=451)
        if "exchange.coinbase.com" in url:
            return FakeResponse({}, status_code=429)
        raise AssertionError(f"URL inattendue: {url}")

    monkeypatch.setattr(crypto_mod._session, "get", fake_get)

    # Ni 451 (Binance) ni 429 (Coinbase) ne doivent jamais fuiter comme exception brute --
    # seule HistoryUnavailableError (échec métier documenté) est attendue.
    with pytest.raises(HistoryUnavailableError):
        crypto_mod.get_history_crypto("BTC", 4848)


def test_synthesize_hourly_from_daily_cache_replicates_real_close_24x_per_day(tmp_path, monkeypatch):
    # _now_utc DOIT être figé comme dans les tests voisins : la fraîcheur du manifest
    # (CACHE_MAX_AGE_DAYS=5) est comparée à "maintenant" -- sans ce patch, le test pourrit
    # mécaniquement 5 jours après _FIXED_NOW (constaté le 2026-08-03, session hebdo #2).
    monkeypatch.setattr(crypto_mod, "_now_utc", lambda: _FIXED_NOW)
    cache_dir = tmp_path / "data-cache"
    monkeypatch.setenv("BOT_DATA_CACHE_DIR", str(cache_dir))
    _write_daily_cache(cache_dir, "BTC", n_days=10, generated_at=_FIXED_NOW)

    before_ts = _FIXED_NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    synth = crypto_mod._synthesize_hourly_from_daily_cache("BTC", before_ts, n_hours_needed=48)

    assert not synth.empty
    assert list(synth.columns) == ["open", "high", "low", "close", "volume"]
    # Chaque jour synthétique porte EXACTEMENT 24 heures distinctes, toutes à la même clôture
    # réelle (aucune donnée inventée : réplication de la clôture du cache, jamais une
    # interpolation) -- condition requise par
    # `bot.strategies.quasi_passif_crypto._daily_closes()`.
    by_date = synth.groupby(synth.index.date)
    for date, group in by_date:
        assert len(group) == 24
        assert group["close"].nunique() == 1


def test_synthesize_hourly_from_daily_cache_no_cache_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DATA_CACHE_DIR", str(tmp_path / "does-not-exist"))

    synth = crypto_mod._synthesize_hourly_from_daily_cache("BTC", _FIXED_NOW, n_hours_needed=48)

    assert synth.empty
    assert list(synth.columns) == ["open", "high", "low", "close", "volume"]
