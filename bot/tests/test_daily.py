"""Tests de `bot.feeds.daily` — historique JOURNALIER (bougies clôturées) pour les
stratégies bas-fréquence (filtre SMA200 crypto, momentum cross-sectionnel S&P100,
dual-momentum ETF). Parsing de structures de réponse réalistes enregistrées en fixtures
(Binance klines 1d, Coinbase candles 86400s, CSV stooq), exclusion systématique de la bougie
du jour en cours, gestion d'échec (source unique / toutes sources), et cache mémoire par
processus. Jamais d'appel réseau réel dans ces tests (mock intégral)."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests

import bot.feeds.daily as daily_mod
from bot.feeds.types import HistoryUnavailableError

FIXTURES = Path(__file__).parent / "fixtures"
NY_TZ = ZoneInfo("America/New_York")


class FakeResponse:
    """Double minimal de `requests.Response` — sert à la fois les endpoints JSON
    (Binance/Coinbase) et texte (stooq)."""

    def __init__(self, json_data=None, text_data=None, status_code=200):
        self._json_data = json_data
        self.text = text_data if text_data is not None else (json.dumps(json_data) if json_data is not None else "")
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json_data


def load_fixture_json(name):
    with open(FIXTURES / name) as f:
        return json.load(f)


def load_fixture_text(name):
    with open(FIXTURES / name) as f:
        return f.read()


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    daily_mod.clear_daily_cache()
    yield
    daily_mod.clear_daily_cache()


# ---------------------------------------------------------------------------
# asset_class — normalisation
# ---------------------------------------------------------------------------


def test_normalize_asset_class_accepts_known_aliases():
    assert daily_mod._normalize_asset_class("crypto") == "crypto"
    assert daily_mod._normalize_asset_class("equity") == "equity"
    assert daily_mod._normalize_asset_class("equities") == "equity"
    assert daily_mod._normalize_asset_class("ETF") == "etf"
    assert daily_mod._normalize_asset_class("etfs") == "etf"


def test_get_daily_history_unknown_asset_class_raises():
    with pytest.raises(ValueError):
        daily_mod.get_daily_history("BTC", 10, "bogus")


def test_get_daily_history_non_positive_n_days_raises():
    with pytest.raises(ValueError):
        daily_mod.get_daily_history("BTC", 0, "crypto")
    with pytest.raises(ValueError):
        daily_mod.get_daily_history("BTC", -5, "crypto")


# ---------------------------------------------------------------------------
# Binance klines 1d — parsing de fixture réaliste
# ---------------------------------------------------------------------------


def test_fetch_binance_daily_klines_parses_real_fixture(monkeypatch):
    rows = load_fixture_json("binance_klines_1d_btcusdt.json")
    monkeypatch.setattr(daily_mod._session, "get", lambda *a, **k: FakeResponse(json_data=rows))

    df = daily_mod._fetch_binance_daily_klines("BTCUSDT", n_days=5)

    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 5
    assert df.index.is_monotonic_increasing
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.iloc[0]["open"] == pytest.approx(42283.58)
    assert df.iloc[-1]["close"] == pytest.approx(40966.83)


def _make_binance_daily_rows(n_closed, now=None):
    """`n_closed` bougies quotidiennes clôturées se terminant hier (UTC), PLUS une bougie
    supplémentaire du jour UTC courant, encore en formation (close_time futur) — cette
    dernière ne doit JAMAIS apparaître dans le résultat."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    for i in range(n_closed, 0, -1):
        open_time = today_start - _dt.timedelta(days=i)
        close_time = open_time + _dt.timedelta(days=1) - _dt.timedelta(milliseconds=1)
        rows.append([
            int(open_time.timestamp() * 1000), "100.0", "101.0", "99.0", "100.5", "10.0",
            int(close_time.timestamp() * 1000), "1000.0", "5", "5.0", "500.0", "0",
        ])
    open_time = today_start
    close_time = open_time + _dt.timedelta(days=1) - _dt.timedelta(milliseconds=1)
    rows.append([
        int(open_time.timestamp() * 1000), "999", "999", "999", "999", "999",
        int(close_time.timestamp() * 1000), "1", "1", "1", "1", "0",
    ])
    return rows


def test_fetch_binance_daily_klines_excludes_forming_candle(monkeypatch):
    rows = _make_binance_daily_rows(10)
    monkeypatch.setattr(daily_mod._session, "get", lambda *a, **k: FakeResponse(json_data=rows))

    df = daily_mod._fetch_binance_daily_klines("BTCUSDT", n_days=10)

    assert len(df) == 10
    assert (df["close"] != 999.0).all()
    assert df.index.is_monotonic_increasing


def test_fetch_binance_daily_klines_network_failure_returns_empty(monkeypatch):
    def raise_err(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(daily_mod._session, "get", raise_err)

    df = daily_mod._fetch_binance_daily_klines("BTCUSDT", n_days=10)

    assert df.empty


# ---------------------------------------------------------------------------
# Coinbase candles 86400s — parsing de fixture réaliste + exclusion du jour courant
# ---------------------------------------------------------------------------


def test_fetch_coinbase_daily_candles_parses_real_fixture(monkeypatch):
    rows = load_fixture_json("coinbase_candles_1d_btcusd.json")
    # Les timestamps de la fixture sont fixes (2024-01) : toujours dans le passé par rapport
    # à "maintenant", donc aucune exclusion "jour courant" ne doit intervenir ici.
    monkeypatch.setattr(daily_mod._session, "get", lambda *a, **k: FakeResponse(json_data=rows))

    df = daily_mod._fetch_coinbase_daily_candles("BTC-USD", n_days=5)

    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 5
    assert df.index.is_monotonic_increasing
    assert df.iloc[0]["close"] == pytest.approx(44179.55)


def _make_coinbase_daily_rows(n, now=None):
    now = now or _dt.datetime.now(_dt.timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return [
        [int((today_start - _dt.timedelta(days=i)).timestamp()), 99.0, 101.0, 100.0, 100.5, 10.0]
        for i in range(1, n + 1)
    ]


def test_fetch_coinbase_daily_candles_excludes_current_utc_day(monkeypatch):
    now = _dt.datetime.now(_dt.timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = _make_coinbase_daily_rows(10, now=now)
    # Ajoute la bougie du jour UTC courant (encore en formation) — ne doit jamais apparaître.
    rows.append([int(today_start.timestamp()), 999.0, 999.0, 999.0, 999.0, 999.0])

    monkeypatch.setattr(daily_mod._session, "get", lambda *a, **k: FakeResponse(json_data=rows))

    df = daily_mod._fetch_coinbase_daily_candles("BTC-USD", n_days=10)

    assert len(df) == 10
    assert (df["close"] != 999.0).all()


# ---------------------------------------------------------------------------
# get_daily_history — crypto : orchestration primaire/fallback, échec, cache
# ---------------------------------------------------------------------------


def test_get_daily_history_crypto_uses_binance_when_sufficient(monkeypatch):
    rows = _make_binance_daily_rows(30)
    monkeypatch.setattr(daily_mod._session, "get", lambda *a, **k: FakeResponse(json_data=rows))

    df = daily_mod.get_daily_history("BTC", 30, "crypto")

    assert len(df) == 30
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.is_monotonic_increasing


def test_get_daily_history_crypto_falls_back_to_coinbase(monkeypatch):
    few_binance_rows = _make_binance_daily_rows(3)

    def fake_get(url, *a, **k):
        if "klines" in url:
            return FakeResponse(json_data=few_binance_rows)
        if "candles" in url:
            return FakeResponse(json_data=_make_coinbase_daily_rows(50))
        raise AssertionError(f"URL inattendue: {url}")

    monkeypatch.setattr(daily_mod._session, "get", fake_get)

    df = daily_mod.get_daily_history("BTC", 40, "crypto")

    assert len(df) == 40
    assert df.index.is_monotonic_increasing


def test_get_daily_history_crypto_raises_when_insufficient_everywhere(monkeypatch):
    few_rows = _make_binance_daily_rows(3)

    def fake_get(url, *a, **k):
        if "klines" in url:
            return FakeResponse(json_data=few_rows)
        if "candles" in url:
            return FakeResponse(json_data=[])
        raise AssertionError(f"URL inattendue: {url}")

    monkeypatch.setattr(daily_mod._session, "get", fake_get)

    with pytest.raises(HistoryUnavailableError):
        daily_mod.get_daily_history("BTC", 400, "crypto")


def test_get_daily_history_crypto_unknown_symbol_raises_unavailable(monkeypatch):
    # Aucune paire résolue pour ce symbole -> les deux sources sont sautées -> vide -> échec,
    # jamais de prix/historique inventé.
    def fail_if_called(*a, **k):
        raise AssertionError("aucun appel réseau attendu pour un symbole hors univers connu")

    monkeypatch.setattr(daily_mod._session, "get", fail_if_called)

    with pytest.raises(HistoryUnavailableError):
        daily_mod.get_daily_history("NOPE_UNKNOWN_SYMBOL", 10, "crypto")


def test_get_daily_history_crypto_cache_avoids_second_network_call(monkeypatch):
    rows = _make_binance_daily_rows(30)
    call_count = {"n": 0}

    def fake_get(*a, **k):
        call_count["n"] += 1
        return FakeResponse(json_data=rows)

    monkeypatch.setattr(daily_mod._session, "get", fake_get)

    df1 = daily_mod.get_daily_history("BTC", 30, "crypto")
    calls_after_first = call_count["n"]
    df2 = daily_mod.get_daily_history("BTC", 30, "crypto")

    assert call_count["n"] == calls_after_first  # aucun appel réseau supplémentaire
    pd.testing.assert_frame_equal(df1, df2)


def test_get_daily_history_crypto_cache_serves_smaller_n_days_from_larger_fetch(monkeypatch):
    rows = _make_binance_daily_rows(30)
    call_count = {"n": 0}

    def fake_get(*a, **k):
        call_count["n"] += 1
        return FakeResponse(json_data=rows)

    monkeypatch.setattr(daily_mod._session, "get", fake_get)

    daily_mod.get_daily_history("BTC", 30, "crypto")
    calls_after_first = call_count["n"]
    df_small = daily_mod.get_daily_history("BTC", 10, "crypto")

    assert call_count["n"] == calls_after_first
    assert len(df_small) == 10


def test_get_daily_history_crypto_cached_failure_not_retried_for_equal_or_smaller_request(monkeypatch):
    few_rows = _make_binance_daily_rows(3)
    call_count = {"n": 0}

    def fake_get(url, *a, **k):
        call_count["n"] += 1
        if "klines" in url:
            return FakeResponse(json_data=few_rows)
        return FakeResponse(json_data=[])

    monkeypatch.setattr(daily_mod._session, "get", fake_get)

    with pytest.raises(HistoryUnavailableError):
        daily_mod.get_daily_history("BTC", 20, "crypto")
    calls_after_first_failure = call_count["n"]

    with pytest.raises(HistoryUnavailableError):
        daily_mod.get_daily_history("BTC", 20, "crypto")  # même n_days -> pas de nouvel appel

    assert call_count["n"] == calls_after_first_failure


def test_get_daily_history_crypto_larger_request_after_failure_retries(monkeypatch):
    few_rows = _make_binance_daily_rows(3)
    call_count = {"n": 0}

    def fake_get(url, *a, **k):
        call_count["n"] += 1
        if "klines" in url:
            return FakeResponse(json_data=few_rows)
        return FakeResponse(json_data=[])

    monkeypatch.setattr(daily_mod._session, "get", fake_get)

    with pytest.raises(HistoryUnavailableError):
        daily_mod.get_daily_history("BTC", 5, "crypto")
    calls_after_first_failure = call_count["n"]

    with pytest.raises(HistoryUnavailableError):
        daily_mod.get_daily_history("BTC", 500, "crypto")  # demande plus large -> nouvel essai

    assert call_count["n"] > calls_after_first_failure


def test_clear_daily_cache_forces_new_network_call(monkeypatch):
    rows = _make_binance_daily_rows(30)
    call_count = {"n": 0}

    def fake_get(*a, **k):
        call_count["n"] += 1
        return FakeResponse(json_data=rows)

    monkeypatch.setattr(daily_mod._session, "get", fake_get)

    daily_mod.get_daily_history("BTC", 30, "crypto")
    calls_after_first = call_count["n"]
    daily_mod.clear_daily_cache()
    daily_mod.get_daily_history("BTC", 30, "crypto")

    assert call_count["n"] > calls_after_first


def test_is_daily_history_available_true_and_false(monkeypatch):
    rows = _make_binance_daily_rows(30)
    monkeypatch.setattr(daily_mod._session, "get", lambda *a, **k: FakeResponse(json_data=rows))

    assert daily_mod.is_daily_history_available("BTC", 30, "crypto") is True
    assert daily_mod.is_daily_history_available("BTC", 400, "crypto") is False


# ---------------------------------------------------------------------------
# Validation OHLCV — jamais de donnée inventée/interpolée, timestamps propres
# ---------------------------------------------------------------------------


def test_validate_ohlcv_drops_non_positive_close_and_dedupes():
    idx = pd.to_datetime(
        ["2024-01-01", "2024-01-02", "2024-01-02", "2024-01-03"], utc=True
    )
    df = pd.DataFrame(
        {
            "open": [1.0, 2.0, 2.1, -3.0],
            "high": [1.5, 2.5, 2.6, 3.5],
            "low": [0.5, 1.5, 1.6, 2.5],
            "close": [1.2, 0.0, 2.2, 3.2],  # ligne 2 close=0 invalide, ligne 4 open négatif invalide
            "volume": [10.0, 20.0, 21.0, 30.0],
        },
        index=idx,
    )
    df.index.name = "ts"

    out = daily_mod._validate_ohlcv(df, context="test")

    assert (out["close"] > 0).all()
    assert (out["open"] > 0).all()
    assert not out.index.duplicated().any()
    assert out.index.is_monotonic_increasing


def test_validate_ohlcv_empty_input_returns_empty_frame():
    out = daily_mod._validate_ohlcv(None, context="test")
    assert out.empty
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# yfinance (equities/ETF) — normalisation, exclusion de la séance du jour, repli stooq
# ---------------------------------------------------------------------------


def _make_yf_daily_df(n_closed, now=None, tz_aware=False):
    """DataFrame façon `yfinance` : index de dates (America/New_York), `n_closed` séances
    clôturées PLUS la séance du jour NY courant (encore "en cours" au sens de ce module,
    jamais retenue)."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    today_ny = now.astimezone(NY_TZ).date()
    dates = [today_ny - _dt.timedelta(days=i) for i in range(n_closed, 0, -1)]
    dates.append(today_ny)

    opens = [100.0] * n_closed + [999.0]
    highs = [101.0] * n_closed + [999.0]
    lows = [99.0] * n_closed + [999.0]
    closes = [100.5] * n_closed + [999.0]
    volumes = [1_000_000.0] * n_closed + [999.0]

    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    if tz_aware:
        idx = idx.tz_localize(NY_TZ)
    df = pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes}, index=idx
    )
    return df


def test_normalize_yf_daily_localizes_tz_naive_index_to_new_york():
    raw = _make_yf_daily_df(5, tz_aware=False)

    out = daily_mod._normalize_yf_daily(raw)

    assert str(out["ts_ny"].dt.tz) == str(NY_TZ)
    assert len(out) == 6  # 5 clôturées + 1 séance du jour (pas encore exclue à ce stade)


def test_finalize_intermediate_daily_excludes_today_ny_session():
    raw = _make_yf_daily_df(10, tz_aware=False)
    intermediate = daily_mod._normalize_yf_daily(raw)

    out = daily_mod._finalize_intermediate_daily(intermediate, context="test")

    assert len(out) == 10
    assert (out["close"] != 999.0).all()
    assert isinstance(out.index, pd.DatetimeIndex)
    assert str(out.index.tz) == "UTC"
    assert out.index.is_monotonic_increasing


class _FakeYfTicker:
    def __init__(self, symbol, df_to_return):
        self.symbol = symbol
        self._df = df_to_return

    def history(self, period=None, interval=None, auto_adjust=None):
        return self._df


def test_fetch_yfinance_single_returns_normalized_frame(monkeypatch):
    fake_df = _make_yf_daily_df(20)
    monkeypatch.setattr(daily_mod.yf, "Ticker", lambda sym: _FakeYfTicker(sym, fake_df))

    raw = daily_mod._fetch_yfinance_single("AAPL", period="2y")

    assert raw is not None
    assert not raw.empty


def test_get_daily_history_equity_uses_yfinance_when_available(monkeypatch):
    fake_df = _make_yf_daily_df(30)
    monkeypatch.setattr(daily_mod.yf, "Ticker", lambda sym: _FakeYfTicker(sym, fake_df))

    def fail_if_stooq_called(*a, **k):
        raise AssertionError("stooq ne devrait pas être appelé si yfinance suffit")

    monkeypatch.setattr(daily_mod._stooq_session, "get", fail_if_stooq_called)

    df = daily_mod.get_daily_history("AAPL", 30, "equities")

    assert len(df) == 30
    assert (df["close"] != 999.0).all()


def test_get_daily_history_equity_falls_back_to_stooq_when_yfinance_unavailable(monkeypatch):
    monkeypatch.setattr(daily_mod, "_YFINANCE_AVAILABLE", False)
    text = load_fixture_text("stooq_daily_aapl.csv")
    monkeypatch.setattr(daily_mod._stooq_session, "get", lambda *a, **k: FakeResponse(text_data=text))

    df = daily_mod.get_daily_history("AAPL", 5, "equity")

    assert len(df) == 5
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.is_monotonic_increasing
    assert df.iloc[0]["close"] == pytest.approx(185.64)


def test_get_daily_history_equity_falls_back_to_stooq_when_yfinance_insufficient(monkeypatch):
    scarce_df = _make_yf_daily_df(2)  # bien en dessous des 5 jours requis
    monkeypatch.setattr(daily_mod.yf, "Ticker", lambda sym: _FakeYfTicker(sym, scarce_df))
    text = load_fixture_text("stooq_daily_aapl.csv")
    monkeypatch.setattr(daily_mod._stooq_session, "get", lambda *a, **k: FakeResponse(text_data=text))

    df = daily_mod.get_daily_history("AAPL", 5, "equity")

    assert len(df) == 5


def test_get_daily_history_equity_raises_when_all_sources_fail(monkeypatch):
    monkeypatch.setattr(daily_mod, "_YFINANCE_AVAILABLE", False)

    def raise_err(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(daily_mod._stooq_session, "get", raise_err)

    with pytest.raises(HistoryUnavailableError):
        daily_mod.get_daily_history("AAPL", 5, "equity")


def test_fetch_stooq_daily_malformed_csv_returns_none(monkeypatch):
    monkeypatch.setattr(
        daily_mod._stooq_session, "get", lambda *a, **k: FakeResponse(text_data="not,the,expected,columns\n1,2,3,4")
    )

    assert daily_mod._fetch_stooq_daily("AAPL") is None


def test_fetch_stooq_daily_no_data_response_returns_none(monkeypatch):
    monkeypatch.setattr(daily_mod._stooq_session, "get", lambda *a, **k: FakeResponse(text_data="No data"))

    assert daily_mod._fetch_stooq_daily("UNKNOWNTICKER") is None


# ---------------------------------------------------------------------------
# prefetch_daily_history — téléchargement par lots, cache partagé entre wallets
# ---------------------------------------------------------------------------


def test_prefetch_daily_history_rejects_crypto():
    with pytest.raises(ValueError):
        daily_mod.prefetch_daily_history(["BTC"], "crypto", 30)


def _make_yf_batch_multiindex(tickers, n_closed, now=None):
    now = now or _dt.datetime.now(_dt.timezone.utc)
    today_ny = now.astimezone(NY_TZ).date()
    dates = [today_ny - _dt.timedelta(days=i) for i in range(n_closed, 0, -1)]
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])

    frames = {}
    for ticker in tickers:
        frames[(ticker, "Open")] = [100.0] * n_closed
        frames[(ticker, "High")] = [101.0] * n_closed
        frames[(ticker, "Low")] = [99.0] * n_closed
        frames[(ticker, "Close")] = [100.5] * n_closed
        frames[(ticker, "Volume")] = [1_000_000.0] * n_closed
    df = pd.DataFrame(frames, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


def test_prefetch_daily_history_batch_populates_cache_for_get_daily_history(monkeypatch):
    tickers = ["AAPL", "MSFT"]
    batch_df = _make_yf_batch_multiindex(tickers, 30)

    call_count = {"n": 0}

    def fake_download(**kwargs):
        call_count["n"] += 1
        return batch_df

    monkeypatch.setattr(daily_mod.yf, "download", fake_download)

    def fail_if_stooq_called(*a, **k):
        raise AssertionError("stooq ne devrait pas être nécessaire, le lot yfinance a suffi")

    monkeypatch.setattr(daily_mod._stooq_session, "get", fail_if_stooq_called)

    statuses = daily_mod.prefetch_daily_history(tickers, "equities", 30)

    assert statuses == {"AAPL": "ok", "MSFT": "ok"}
    assert call_count["n"] == 1  # un seul lot pour les deux tickers

    # get_daily_history ne doit déclencher AUCUN appel réseau supplémentaire (cache rempli).
    def fail_if_called_again(**kwargs):
        raise AssertionError("aucun appel réseau supplémentaire attendu après prefetch")

    monkeypatch.setattr(daily_mod.yf, "download", fail_if_called_again)

    df_aapl = daily_mod.get_daily_history("AAPL", 30, "equities")
    df_msft = daily_mod.get_daily_history("MSFT", 30, "equities")

    assert len(df_aapl) == 30
    assert len(df_msft) == 30


def test_prefetch_daily_history_falls_back_per_ticker_when_missing_from_batch(monkeypatch):
    # Le lot ne renvoie AUCUNE donnée pour "MSFT" (absent du MultiIndex) -> repli individuel
    # yfinance puis stooq, exactement comme documenté.
    batch_df = _make_yf_batch_multiindex(["AAPL"], 30)
    monkeypatch.setattr(daily_mod.yf, "download", lambda **kwargs: batch_df)

    fake_single_df = _make_yf_daily_df(30)
    monkeypatch.setattr(daily_mod.yf, "Ticker", lambda sym: _FakeYfTicker(sym, fake_single_df))

    statuses = daily_mod.prefetch_daily_history(["AAPL", "MSFT"], "equities", 30)

    assert statuses["AAPL"] == "ok"
    assert statuses["MSFT"] == "ok"

    df_msft = daily_mod.get_daily_history("MSFT", 30, "equities")
    assert len(df_msft) == 30


def test_prefetch_daily_history_skips_symbols_already_cached():
    df = daily_mod._validate_ohlcv(
        pd.DataFrame(
            {"open": [1.0] * 30, "high": [1.0] * 30, "low": [1.0] * 30, "close": [1.0] * 30, "volume": [1.0] * 30},
            index=pd.date_range("2024-01-01", periods=30, freq="D", tz="UTC"),
        ),
        context="seed",
    )
    daily_mod._store_cache("equity", "AAPL", df, 30, None)

    statuses = daily_mod.prefetch_daily_history(["AAPL"], "equities", 30)

    assert statuses == {"AAPL": "cache"}


# ---------------------------------------------------------------------------
# _yf_period_for — dimensionnement de la fenêtre de téléchargement
# ---------------------------------------------------------------------------


def test_yf_period_for_scales_with_n_days():
    assert daily_mod._yf_period_for(100) == "2y"
    assert daily_mod._yf_period_for(400) == "2y"
    assert daily_mod._yf_period_for(1000) in ("2y", "5y")
    assert daily_mod._yf_period_for(3000) in ("5y", "10y", "max")
