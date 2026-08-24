"""Tests de l'extension funding rate / klines perpétuelles (futures USDT-M) de
`tools/fetch_data.py` -- idée backlog P0#1 (`docs/RESEARCH-BACKLOG.md`, "funding carry sur
perpétuels simulés"). Intégralement OFFLINE : le réseau réel est bloqué dans cet
environnement de développement (proxy 403, cf. mission), aucun test ici ne fait de vraie
requête HTTP -- tout est mocké (fixtures d'archives zip construites en mémoire, `session.get`/
`fetch_with_retries` monkeypatchés).

Ce fichier ne teste QUE la collecte de donnée (parsing, coverage, dédup, écriture, manifeste)
-- aucune logique de stratégie/backtest, hors du périmètre de cette mission.
"""

from __future__ import annotations

import gzip
import io
import zipfile
from datetime import datetime, timezone

import pandas as pd
import pytest

import tools.fetch_data as fd


# --------------------------------------------------------------------------------------
# Fixtures utilitaires
# --------------------------------------------------------------------------------------


def _zip_bytes(filename: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(filename, content)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", json_data=None, headers=None):
        self.status_code = status_code
        self.content = content
        self._json = json_data
        self.headers = headers or {}
        self.text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else ""

    def json(self):
        if self._json is None:
            raise ValueError("pas de JSON dans cette FakeResponse")
        return self._json


def _ms(iso: str) -> int:
    return int(pd.Timestamp(iso, tz="UTC").timestamp() * 1000)


def _kline_row(open_iso: str, close_iso: str, o=100.0, h=101.0, l=99.0, c=100.5, v=10.0) -> str:
    return (
        f"{_ms(open_iso)},{o},{h},{l},{c},{v},{_ms(close_iso)},1000,5,1,1,0"
    )


# ========================================================================================
# 1. Parseur funding -- tolérance aux variantes de schéma CSV
# ========================================================================================


def test_parse_funding_csv_schema_calc_time():
    csv_bytes = (
        b"calc_time,funding_interval_hours,last_funding_rate\n"
        b"1650000000000,8,0.00010000\n"
        b"1650028800000,8,-0.00050000\n"
    )
    df = fd._parse_binance_funding_csv_bytes(csv_bytes)
    assert list(df.columns) == ["timestamp", "funding_rate", "funding_interval_hours"]
    assert len(df) == 2
    assert df.iloc[0]["funding_rate"] == pytest.approx(0.0001)
    assert df.iloc[1]["funding_rate"] == pytest.approx(-0.0005)
    assert (df["funding_interval_hours"] == 8.0).all()


def test_parse_funding_csv_schema_symbol_funding_time():
    csv_bytes = (
        b"symbol,fundingTime,fundingRate\n"
        b"BTCUSDT,1650000000000,0.00030000\n"
        b"BTCUSDT,1650028800000,0.00015000\n"
    )
    df = fd._parse_binance_funding_csv_bytes(csv_bytes)
    # Pas de funding_interval_hours dans ce schéma -> colonne absente, pas juste vide.
    assert list(df.columns) == ["timestamp", "funding_rate"]
    assert len(df) == 2
    assert df.iloc[0]["funding_rate"] == pytest.approx(0.0003)


def test_parse_funding_csv_schema_case_and_underscore_insensitive():
    # Variante de casse/underscore -- doit toujours être reconnue.
    csv_bytes = b"CalcTime,LastFundingRate\n1650000000000,0.0002\n"
    df = fd._parse_binance_funding_csv_bytes(csv_bytes)
    assert len(df) == 1
    assert df.iloc[0]["funding_rate"] == pytest.approx(0.0002)


def test_parse_funding_csv_unknown_schema_returns_empty_df():
    csv_bytes = b"foo,bar,baz\n1,2,3\n"
    df = fd._parse_binance_funding_csv_bytes(csv_bytes)
    assert df.empty
    assert "timestamp" in df.columns and "funding_rate" in df.columns


def test_parse_funding_csv_malformed_row_skipped_others_kept():
    csv_bytes = (
        b"calc_time,funding_interval_hours,last_funding_rate\n"
        b"1650000000000,8,0.0001\n"
        b"NOT_A_TIMESTAMP,8,0.0002\n"
        b"1650028800000,8,NOT_A_NUMBER\n"
        b"1650057600000,8,-0.0003\n"
    )
    df = fd._parse_binance_funding_csv_bytes(csv_bytes)
    assert len(df) == 2
    assert set(df["funding_rate"].round(4)) == {0.0001, -0.0003}


def test_parse_funding_csv_empty_bytes():
    df = fd._parse_binance_funding_csv_bytes(b"")
    assert df.empty


# ========================================================================================
# 2. Parseur klines perp (réutilise _parse_binance_csv_bytes, même format que le spot)
# ========================================================================================


def test_parse_perp_klines_csv_no_header():
    row1 = _kline_row("2022-01-01T00:00:00Z", "2022-01-01T00:59:59Z")
    row2 = _kline_row("2022-01-01T01:00:00Z", "2022-01-01T01:59:59Z", c=101.0)
    csv_bytes = (row1 + "\n" + row2 + "\n").encode()
    df = fd._parse_binance_csv_bytes(csv_bytes)
    assert len(df) == 2
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert df.iloc[1]["close"] == pytest.approx(101.0)


# ========================================================================================
# 3. download_binance_funding_month / download_binance_perp_month -- URL + extraction zip
# ========================================================================================


def test_download_binance_funding_month_builds_correct_url_and_parses(monkeypatch):
    captured = {}
    csv_bytes = b"calc_time,funding_interval_hours,last_funding_rate\n1650000000000,8,0.0001\n"
    zbytes = _zip_bytes("BTCUSDT-fundingRate-2022-04.csv", csv_bytes)

    def fake_fetch(session, url, **kwargs):
        captured["url"] = url
        return fd.FetchResult(status="OK", response=FakeResponse(200, content=zbytes))

    monkeypatch.setattr(fd, "fetch_with_retries", fake_fetch)
    status, df = fd.download_binance_funding_month(object(), "BTCUSDT", 2022, 4, max_attempts=1)

    assert status == "OK"
    assert captured["url"] == (
        "https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT/"
        "BTCUSDT-fundingRate-2022-04.zip"
    )
    assert len(df) == 1
    assert df.iloc[0]["funding_rate"] == pytest.approx(0.0001)


def test_download_binance_perp_month_builds_correct_url_and_parses(monkeypatch):
    captured = {}
    row = _kline_row("2022-04-01T00:00:00Z", "2022-04-01T00:59:59Z")
    zbytes = _zip_bytes("BTCUSDT-1h-2022-04.csv", (row + "\n").encode())

    def fake_fetch(session, url, **kwargs):
        captured["url"] = url
        return fd.FetchResult(status="OK", response=FakeResponse(200, content=zbytes))

    monkeypatch.setattr(fd, "fetch_with_retries", fake_fetch)
    status, df = fd.download_binance_perp_month(object(), "BTCUSDT", 2022, 4, max_attempts=1)

    assert status == "OK"
    assert captured["url"] == (
        "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1h/"
        "BTCUSDT-1h-2022-04.zip"
    )
    assert len(df) == 1


def test_download_binance_funding_month_propagates_not_found(monkeypatch):
    monkeypatch.setattr(
        fd, "fetch_with_retries", lambda *a, **k: fd.FetchResult(status="NOT_FOUND")
    )
    status, df = fd.download_binance_funding_month(object(), "BTCUSDT", 2019, 1, max_attempts=1)
    assert status == "NOT_FOUND"
    assert df is None


def test_download_binance_funding_month_bad_zip_is_error(monkeypatch):
    monkeypatch.setattr(
        fd, "fetch_with_retries",
        lambda *a, **k: fd.FetchResult(status="OK", response=FakeResponse(200, content=b"not a zip")),
    )
    status, df = fd.download_binance_funding_month(object(), "BTCUSDT", 2022, 4, max_attempts=1)
    assert status == "ERROR"
    assert df is None


# ========================================================================================
# 4. _classify_monthly_coverage -- skip normal (avant 1re archive) vs anomalie (trou milieu)
# ========================================================================================


def test_coverage_leading_not_found_before_listing_is_normal_not_anomaly():
    statuses = [
        ("2022-01", "NOT_FOUND"), ("2022-02", "NOT_FOUND"), ("2022-03", "NOT_FOUND"),
        ("2022-04", "OK"), ("2022-05", "OK"), ("2022-06", "OK"),
    ]
    cov = fd._classify_monthly_coverage(statuses)
    assert cov.leading_gap_months == ["2022-01", "2022-02", "2022-03"]
    assert cov.mid_gap_months == []
    assert cov.first_ok_month == "2022-04"
    assert cov.last_ok_month == "2022-06"


def test_coverage_gap_after_first_ok_is_mid_anomaly():
    statuses = [
        ("2022-01", "OK"), ("2022-02", "NOT_FOUND"), ("2022-03", "OK"),
    ]
    cov = fd._classify_monthly_coverage(statuses)
    assert cov.leading_gap_months == []
    assert cov.mid_gap_months == ["2022-02"]


def test_coverage_never_found_anywhere_all_leading_no_exclusion_signal_confused():
    statuses = [("2022-01", "NOT_FOUND"), ("2022-02", "NOT_FOUND")]
    cov = fd._classify_monthly_coverage(statuses)
    assert cov.leading_gap_months == ["2022-01", "2022-02"]
    assert cov.mid_gap_months == []
    assert cov.first_ok_month is None
    assert cov.months_ok == []


def test_coverage_error_months_tracked_separately_from_not_found():
    statuses = [("2022-01", "OK"), ("2022-02", "ERROR"), ("2022-03", "OK")]
    cov = fd._classify_monthly_coverage(statuses)
    assert cov.months_error == ["2022-02"]
    assert cov.mid_gap_months == []  # ERROR n'est pas classé NOT_FOUND


# ========================================================================================
# 5. _validate_strictly_increasing_timestamps -- validation défensive
# ========================================================================================


def test_validate_strictly_increasing_logs_error_on_violation(caplog):
    ts = pd.Series(pd.to_datetime(["2022-01-01", "2022-01-01", "2022-01-02"], utc=True))
    with caplog.at_level("ERROR"):
        fd._validate_strictly_increasing_timestamps(ts, context="test-ctx")
    assert any("ANOMALIE" in r.message for r in caplog.records)


def test_validate_strictly_increasing_silent_when_ok(caplog):
    ts = pd.Series(pd.to_datetime(["2022-01-01", "2022-01-02", "2022-01-03"], utc=True))
    with caplog.at_level("ERROR"):
        fd._validate_strictly_increasing_timestamps(ts, context="test-ctx")
    assert not any("ANOMALIE" in r.message for r in caplog.records)


# ========================================================================================
# 6. process_funding_symbol / process_perp_symbol -- assemblage archive + complément API,
#    dédup/tri, flag |rate|>3%
# ========================================================================================


def _funding_df(rows):
    """rows: list of (iso_ts, rate) -> DataFrame timestamp,funding_rate."""
    return pd.DataFrame({
        "timestamp": [pd.Timestamp(t, tz="UTC") for t, _ in rows],
        "funding_rate": [r for _, r in rows],
    })


def test_process_funding_symbol_dedup_sort_and_flags_extreme_rate(monkeypatch):
    archive_months = [(2022, 1), (2022, 2)]

    month1 = _funding_df([("2022-01-01T00:00:00Z", 0.0001), ("2022-01-01T08:00:00Z", 0.0002)])
    # Le 2e mois RE-fournit le dernier timestamp du mois 1 (chevauchement attendu entre
    # archives consécutives) avec un taux différent -- keep="last" doit gagner, plus un taux
    # extrême (>3%) à flagger.
    month2 = _funding_df([
        ("2022-01-01T08:00:00Z", 0.00025),
        ("2022-02-01T00:00:00Z", 0.05),  # extrême, doit être flaggé mais PAS supprimé
    ])

    def fake_download(session, pair, year, month, max_attempts):
        if (year, month) == (2022, 1):
            return "OK", month1
        if (year, month) == (2022, 2):
            return "OK", month2
        raise AssertionError("mois inattendu")

    monkeypatch.setattr(fd, "download_binance_funding_month", fake_download)
    monkeypatch.setattr(
        fd, "fetch_binance_funding_current_month_completion",
        lambda session, pair, month_start, max_attempts: pd.DataFrame(columns=["timestamp", "funding_rate"]),
    )

    res = fd.process_funding_symbol(object(), "BTC", archive_months, max_attempts=1)

    assert res.included is True
    assert res.pair == "BTCUSDT"
    # Dédoublonné : 3 lignes uniques (2022-01-01T00:00, 08:00 [dernière valeur gagne], 02-01)
    assert len(res.df) == 3
    assert list(res.df["timestamp"]) == sorted(res.df["timestamp"])
    row_0800 = res.df[res.df["timestamp"] == pd.Timestamp("2022-01-01T08:00:00Z")]
    assert row_0800.iloc[0]["funding_rate"] == pytest.approx(0.00025)  # keep="last"

    # Flag extrême : présent, valeur conservée dans le df (pas supprimée).
    assert len(res.flagged_extreme_rates) == 1
    assert res.flagged_extreme_rates[0]["funding_rate"] == pytest.approx(0.05)
    assert (res.df["funding_rate"] == 0.05).any()


def test_process_funding_symbol_leading_gap_then_included_normally(monkeypatch):
    archive_months = [(2022, 1), (2022, 2), (2022, 3)]

    def fake_download(session, pair, year, month, max_attempts):
        if (year, month) == (2022, 1):
            return "NOT_FOUND", None  # perp/funding pas encore listé -- normal
        return "OK", _funding_df([(f"2022-{month:02d}-01T00:00:00Z", 0.0001)])

    monkeypatch.setattr(fd, "download_binance_funding_month", fake_download)
    monkeypatch.setattr(
        fd, "fetch_binance_funding_current_month_completion",
        lambda *a, **k: pd.DataFrame(columns=["timestamp", "funding_rate"]),
    )

    res = fd.process_funding_symbol(object(), "SOME", archive_months, max_attempts=1)

    assert res.included is True
    assert res.coverage.leading_gap_months == ["2022-01"]
    assert res.coverage.mid_gap_months == []
    assert "inclus" in res.reason
    assert "anomalie" not in res.reason


def test_process_funding_symbol_mid_gap_flagged_as_anomaly_but_still_included(monkeypatch):
    archive_months = [(2022, 1), (2022, 2), (2022, 3)]

    def fake_download(session, pair, year, month, max_attempts):
        if (year, month) == (2022, 2):
            return "NOT_FOUND", None  # trou AU MILIEU -- anomalie
        return "OK", _funding_df([(f"2022-{month:02d}-01T00:00:00Z", 0.0001)])

    monkeypatch.setattr(fd, "download_binance_funding_month", fake_download)
    monkeypatch.setattr(
        fd, "fetch_binance_funding_current_month_completion",
        lambda *a, **k: pd.DataFrame(columns=["timestamp", "funding_rate"]),
    )

    res = fd.process_funding_symbol(object(), "SOME", archive_months, max_attempts=1)

    assert res.included is True  # une anomalie n'exclut PAS le symbole
    assert res.coverage.mid_gap_months == ["2022-02"]
    assert "anomalie" in res.reason


def test_process_funding_symbol_excluded_when_never_found_anywhere(monkeypatch):
    archive_months = [(2022, 1), (2022, 2)]
    monkeypatch.setattr(fd, "download_binance_funding_month", lambda *a, **k: ("NOT_FOUND", None))
    monkeypatch.setattr(
        fd, "fetch_binance_funding_current_month_completion",
        lambda *a, **k: pd.DataFrame(columns=["timestamp", "funding_rate"]),
    )

    res = fd.process_funding_symbol(object(), "NEVER", archive_months, max_attempts=1)

    assert res.included is False
    assert res.df is None
    assert "exclu" in res.reason.lower()


def test_process_funding_symbol_includes_current_month_completion(monkeypatch):
    archive_months = [(2022, 1)]
    monkeypatch.setattr(
        fd, "download_binance_funding_month",
        lambda *a, **k: ("OK", _funding_df([("2022-01-01T00:00:00Z", 0.0001)])),
    )
    completion_df = _funding_df([("2099-01-01T00:00:00Z", 0.0003)])
    monkeypatch.setattr(
        fd, "fetch_binance_funding_current_month_completion", lambda *a, **k: completion_df,
    )

    res = fd.process_funding_symbol(object(), "BTC", archive_months, max_attempts=1)

    assert len(res.df) == 2
    assert res.df["timestamp"].iloc[-1] == pd.Timestamp("2099-01-01T00:00:00Z")


def _perp_ohlcv_df(rows):
    """rows: list of (iso_ts, close) -> DataFrame OHLCV minimal."""
    return pd.DataFrame({
        "timestamp": [pd.Timestamp(t, tz="UTC") for t, _ in rows],
        "open": [100.0] * len(rows), "high": [101.0] * len(rows), "low": [99.0] * len(rows),
        "close": [c for _, c in rows], "volume": [10.0] * len(rows),
    })


def test_process_perp_symbol_dedup_sort_and_completion(monkeypatch):
    archive_months = [(2022, 1)]
    monkeypatch.setattr(
        fd, "download_binance_perp_month",
        lambda *a, **k: ("OK", _perp_ohlcv_df([
            ("2022-01-01T00:00:00Z", 100.0), ("2022-01-01T01:00:00Z", 101.0),
            ("2022-01-01T01:00:00Z", 999.0),  # doublon -- keep="last" doit gagner
        ])),
    )
    monkeypatch.setattr(
        fd, "fetch_binance_perp_current_month_completion",
        lambda *a, **k: _perp_ohlcv_df([("2099-01-01T00:00:00Z", 200.0)]),
    )

    res = fd.process_perp_symbol(object(), "ETH", archive_months, max_attempts=1)

    assert res.included is True
    assert res.pair == "ETHUSDT"
    assert len(res.df) == 3  # 2 uniques du mois d'archive + 1 du complément
    dup_row = res.df[res.df["timestamp"] == pd.Timestamp("2022-01-01T01:00:00Z")]
    assert dup_row.iloc[0]["close"] == pytest.approx(999.0)
    assert list(res.df["timestamp"]) == sorted(res.df["timestamp"])


def test_process_perp_symbol_excluded_when_perp_never_listed(monkeypatch):
    archive_months = [(2022, 1), (2022, 2)]
    monkeypatch.setattr(fd, "download_binance_perp_month", lambda *a, **k: ("NOT_FOUND", None))
    monkeypatch.setattr(
        fd, "fetch_binance_perp_current_month_completion",
        lambda *a, **k: pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"]),
    )

    res = fd.process_perp_symbol(object(), "NEVERLISTED", archive_months, max_attempts=1)

    assert res.included is False
    assert res.df is None


def test_process_symbol_exception_isolation_does_not_propagate_via_run_funding(monkeypatch, tmp_path):
    # `run_funding` doit isoler les exceptions par symbole (même contrat que `run_crypto`) --
    # un symbole qui explose ne doit pas faire planter tout le run.
    def flaky_download(session, pair, year, month, max_attempts):
        if pair == "BTCUSDT":
            raise RuntimeError("boom")
        return "OK", _funding_df([("2022-01-01T00:00:00Z", 0.0001)])

    monkeypatch.setattr(fd, "CRYPTO_SYMBOLS", ["BTC", "ETH"])
    monkeypatch.setattr(fd, "download_binance_funding_month", flaky_download)
    monkeypatch.setattr(
        fd, "fetch_binance_funding_current_month_completion",
        lambda *a, **k: pd.DataFrame(columns=["timestamp", "funding_rate"]),
    )
    # Une seule archive mois pour aller vite.
    monkeypatch.setattr(fd, "last_complete_month", lambda now=None: (2022, 1))
    monkeypatch.setattr(fd, "CRYPTO_ARCHIVE_START", (2022, 1))

    report = fd.run_funding(fd.build_session(), str(tmp_path), max_attempts=1, workers=2)

    assert "BTC" in report["excluded"]
    assert "exception interne" in report["excluded"]["BTC"]["reason"]
    assert "ETH" in report["included"]


# ========================================================================================
# 7. Écriture .csv.gz et relecture
# ========================================================================================


def test_write_gz_csv_funding_with_interval_column_roundtrip(tmp_path):
    df = pd.DataFrame({
        "timestamp": [pd.Timestamp("2022-01-01T00:00:00Z"), pd.Timestamp("2022-01-01T08:00:00Z")],
        "funding_rate": [0.0001, -0.0002],
        "funding_interval_hours": [8.0, 8.0],
    })
    path = str(tmp_path / "BTC.csv.gz")
    fd.write_gz_csv_funding(df, path)

    with gzip.open(path, "rt", encoding="utf-8") as f:
        reloaded = pd.read_csv(f)

    assert list(reloaded.columns) == ["timestamp", "funding_rate", "funding_interval_hours"]
    assert len(reloaded) == 2
    assert reloaded["funding_rate"].iloc[0] == pytest.approx(0.0001)
    assert reloaded["timestamp"].iloc[0] == "2022-01-01T00:00:00+00:00"


def test_write_gz_csv_funding_omits_interval_column_when_absent(tmp_path):
    df = pd.DataFrame({
        "timestamp": [pd.Timestamp("2022-01-01T00:00:00Z")],
        "funding_rate": [0.0001],
    })
    path = str(tmp_path / "BTC.csv.gz")
    fd.write_gz_csv_funding(df, path)

    with gzip.open(path, "rt", encoding="utf-8") as f:
        reloaded = pd.read_csv(f)

    assert list(reloaded.columns) == ["timestamp", "funding_rate"]


def test_write_gz_csv_funding_omits_interval_column_when_all_nan(tmp_path):
    df = pd.DataFrame({
        "timestamp": [pd.Timestamp("2022-01-01T00:00:00Z"), pd.Timestamp("2099-01-01T00:00:00Z")],
        "funding_rate": [0.0001, 0.0002],
        "funding_interval_hours": [float("nan"), float("nan")],
    })
    path = str(tmp_path / "BTC.csv.gz")
    fd.write_gz_csv_funding(df, path)

    with gzip.open(path, "rt", encoding="utf-8") as f:
        reloaded = pd.read_csv(f)

    assert list(reloaded.columns) == ["timestamp", "funding_rate"]


def test_write_gz_csv_perp_reuses_standard_ohlcv_writer(tmp_path):
    # `data/perp/{SYMBOL}.csv.gz` réutilise `write_gz_csv` (même schéma OHLCV que le spot) --
    # verrouille cette réutilisation plutôt qu'un nouveau writer dupliqué.
    df = _perp_ohlcv_df([("2022-01-01T00:00:00Z", 100.5)])
    path = str(tmp_path / "BTC.csv.gz")
    fd.write_gz_csv(df, path)

    with gzip.open(path, "rt", encoding="utf-8") as f:
        reloaded = pd.read_csv(f)
    assert list(reloaded.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert reloaded["close"].iloc[0] == pytest.approx(100.5)


# ========================================================================================
# 8. run_funding / run_perp -- orchestration bout-en-bout (staging, pas de git)
# ========================================================================================


def test_run_funding_writes_csv_and_manifest_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(fd, "CRYPTO_SYMBOLS", ["BTC"])
    monkeypatch.setattr(fd, "last_complete_month", lambda now=None: (2022, 1))
    monkeypatch.setattr(fd, "CRYPTO_ARCHIVE_START", (2022, 1))
    monkeypatch.setattr(
        fd, "download_binance_funding_month",
        lambda *a, **k: ("OK", _funding_df([("2022-01-01T00:00:00Z", 0.0001)])),
    )
    monkeypatch.setattr(
        fd, "fetch_binance_funding_current_month_completion",
        lambda *a, **k: pd.DataFrame(columns=["timestamp", "funding_rate"]),
    )

    report = fd.run_funding(fd.build_session(), str(tmp_path), max_attempts=1, workers=1)

    assert report["included"]["BTC"]["rows"] == 1
    assert report["included"]["BTC"]["pair"] == "BTCUSDT"
    assert (tmp_path / "data" / "funding" / "BTC.csv.gz").is_file()
    assert report["flag_threshold_abs_rate"] == fd.FUNDING_RATE_ABS_FLAG_THRESHOLD


def test_run_perp_writes_csv_and_manifest_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(fd, "CRYPTO_SYMBOLS", ["BTC"])
    monkeypatch.setattr(fd, "last_complete_month", lambda now=None: (2022, 1))
    monkeypatch.setattr(fd, "CRYPTO_ARCHIVE_START", (2022, 1))
    monkeypatch.setattr(
        fd, "download_binance_perp_month",
        lambda *a, **k: ("OK", _perp_ohlcv_df([("2022-01-01T00:00:00Z", 100.0)])),
    )
    monkeypatch.setattr(
        fd, "fetch_binance_perp_current_month_completion",
        lambda *a, **k: pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"]),
    )

    report = fd.run_perp(fd.build_session(), str(tmp_path), max_attempts=1, workers=1)

    assert report["included"]["BTC"]["rows"] == 1
    assert (tmp_path / "data" / "perp" / "BTC.csv.gz").is_file()


# ========================================================================================
# 9. Manifest / rapport -- sections funding/perp
# ========================================================================================


def _fake_funding_report():
    return {
        "archive_from": "2022-01", "archive_to": "2026-07",
        "flag_threshold_abs_rate": 0.03,
        "included": {
            "BTC": {
                "pair": "BTCUSDT", "rows": 100, "first_ts": "2022-01-01T00:00:00+00:00",
                "last_ts": "2026-07-01T00:00:00+00:00", "reason": "inclus",
                "months_missing_leading_normal": [], "months_missing_mid_anomaly": [],
                "months_error": [], "flagged_extreme_rates": [
                    {"timestamp": "2022-05-01T00:00:00+00:00", "funding_rate": 0.045},
                ],
            },
            "ETH": {
                "pair": "ETHUSDT", "rows": 50, "first_ts": "2023-01-01T00:00:00+00:00",
                "last_ts": "2026-07-01T00:00:00+00:00", "reason": "inclus avec anomalie journalisée",
                "months_missing_leading_normal": ["2022-01"], "months_missing_mid_anomaly": ["2023-06"],
                "months_error": [], "flagged_extreme_rates": [],
            },
        },
        "excluded": {
            "NEVERLISTED": {"pair": "NEVERLISTEDUSDT", "reason": "aucune archive funding trouvée sur toute la fenêtre — exclu"},
        },
    }


def _fake_perp_report():
    return {
        "archive_from": "2022-01", "archive_to": "2026-07",
        "included": {
            "BTC": {
                "pair": "BTCUSDT", "rows": 40000, "first_ts": "2022-01-01T00:00:00+00:00",
                "last_ts": "2026-07-31T23:00:00+00:00", "reason": "inclus",
                "months_missing_leading_normal": [], "months_missing_mid_anomaly": [], "months_error": [],
            },
        },
        "excluded": {},
    }


def test_build_manifest_includes_funding_and_perp_sections():
    funding_report = _fake_funding_report()
    perp_report = _fake_perp_report()
    started = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 8, 24, 10, 5, tzinfo=timezone.utc)

    manifest = fd.build_manifest({}, {}, {}, started, ended, funding_report=funding_report, perp_report=perp_report)

    assert manifest["funding"] == funding_report
    assert manifest["perp"] == perp_report
    assert manifest["counts"]["funding_included"] == 2
    assert manifest["counts"]["funding_excluded"] == 1
    assert manifest["counts"]["perp_included"] == 1
    assert manifest["counts"]["perp_excluded"] == 0
    assert "funding_bulk_archive" in manifest["sources"]
    assert "perp_bulk_archive" in manifest["sources"]


def test_build_manifest_defaults_funding_perp_when_omitted():
    started = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 8, 24, 10, 5, tzinfo=timezone.utc)
    manifest = fd.build_manifest({}, {}, {}, started, ended)
    assert manifest["funding"] == {"included": {}, "excluded": {}}
    assert manifest["perp"] == {"included": {}, "excluded": {}}
    assert manifest["counts"]["funding_included"] == 0
    assert manifest["counts"]["perp_included"] == 0


def test_build_report_md_includes_funding_and_perp_sections_with_anomalies():
    started = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 8, 24, 10, 5, tzinfo=timezone.utc)
    manifest = fd.build_manifest(
        {"archive_from": "2022-01", "archive_to": "2026-07", "full_coverage_required_from": "2023-07",
         "included": {}, "excluded": {}},
        {}, {}, started, ended,
        funding_report=_fake_funding_report(), perp_report=_fake_perp_report(),
    )

    report = fd.build_report_md(manifest)

    assert "## Funding rate (perpétuels USDT-M)" in report
    assert "## Klines perpétuelles (futures USDT-M, horaire)" in report
    assert "NEVERLISTED" in report  # exclusion journalisée
    assert "2023-06" in report  # anomalie trou au milieu journalisée
    assert "0.045" in report or "0.0450" in report  # funding extrême journalisé
    assert "BTCUSDT" in report


def test_build_report_md_omits_funding_perp_sections_when_skipped():
    started = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 8, 24, 10, 5, tzinfo=timezone.utc)
    manifest = fd.build_manifest(
        {"archive_from": "2022-01", "archive_to": "2026-07", "full_coverage_required_from": "2023-07",
         "included": {}, "excluded": {}},
        {}, {}, started, ended,
    )
    report = fd.build_report_md(manifest)
    assert "## Funding rate" not in report
    assert "## Klines perpétuelles" not in report


# ========================================================================================
# 10. Non-régression : --only crypto,equities,etf (sans funding/perp) ne change rien
# ========================================================================================


def test_only_without_funding_perp_skips_new_sections_entirely(tmp_path, monkeypatch):
    calls = {"crypto": 0, "equities": 0, "etf": 0, "funding": 0, "perp": 0}

    fake_crypto_report = {
        "archive_from": "2022-01", "archive_to": "2026-07", "full_coverage_required_from": "2023-07",
        "included": {"BTC": {"pair": "BTCUSDT", "rows": 1, "first_ts": "x", "last_ts": "y", "reason": "ok",
                              "months_ok": 1, "months_missing": [], "months_error": []}},
        "excluded": {},
    }

    def fake_run_crypto(session, staging_dir, max_attempts, workers):
        calls["crypto"] += 1
        return fake_crypto_report

    def fake_run_daily_universe(session, staging_dir, tickers, subdir, max_attempts, workers, min_years):
        calls[subdir] += 1
        return {t: {"status": "OK", "source": "yfinance", "symbol_used": t, "rows": 1,
                     "first_ts": "x", "last_ts": "y", "span_years": 5.0} for t in tickers}

    def fake_run_funding(*a, **k):
        calls["funding"] += 1
        raise AssertionError("run_funding ne doit PAS être appelé sans 'funding' dans --only")

    def fake_run_perp(*a, **k):
        calls["perp"] += 1
        raise AssertionError("run_perp ne doit PAS être appelé sans 'perp' dans --only")

    monkeypatch.setattr(fd, "run_crypto", fake_run_crypto)
    monkeypatch.setattr(fd, "run_daily_universe", fake_run_daily_universe)
    monkeypatch.setattr(fd, "run_funding", fake_run_funding)
    monkeypatch.setattr(fd, "run_perp", fake_run_perp)
    monkeypatch.setattr(fd, "SP100_TICKERS", ["AAPL"])
    monkeypatch.setattr(fd, "ETF_TICKERS", ["SPY"])

    rc = fd.main(["--skip-git", "--staging-dir", str(tmp_path), "--only", "crypto,equities,etf"])

    assert rc == 0
    assert calls == {"crypto": 1, "equities": 1, "etf": 1, "funding": 0, "perp": 0}
    assert not (tmp_path / "data" / "funding").exists()
    assert not (tmp_path / "data" / "perp").exists()

    import json
    with open(tmp_path / "MANIFEST.json") as f:
        manifest = json.load(f)
    assert manifest["crypto"]["included"] == fake_crypto_report["included"]
    assert manifest["counts"]["funding_included"] == 0
    assert manifest["counts"]["perp_included"] == 0
    assert manifest["funding"]["included"] == {}
    assert manifest["funding"]["excluded"] == {}
    assert manifest["perp"]["included"] == {}
    assert manifest["perp"]["excluded"] == {}

    with open(tmp_path / "DATA_REPORT.md") as f:
        report_text = f.read()
    assert "## Funding rate" not in report_text
    assert "## Klines perpétuelles" not in report_text


def test_default_only_includes_funding_and_perp():
    args = fd.parse_args([])
    only = {s.strip() for s in args.only.split(",")}
    assert only == {"crypto", "equities", "etf", "funding", "perp"}


def test_main_default_only_calls_funding_and_perp_sections(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(fd, "run_crypto", lambda *a, **k: calls.append("crypto") or {
        "archive_from": "x", "archive_to": "y", "full_coverage_required_from": "z", "included": {}, "excluded": {}
    })
    monkeypatch.setattr(fd, "run_daily_universe", lambda *a, **k: calls.append("daily") or {})
    monkeypatch.setattr(fd, "run_funding", lambda *a, **k: calls.append("funding") or {
        "archive_from": "x", "archive_to": "y", "flag_threshold_abs_rate": 0.03, "included": {}, "excluded": {}
    })
    monkeypatch.setattr(fd, "run_perp", lambda *a, **k: calls.append("perp") or {
        "archive_from": "x", "archive_to": "y", "included": {}, "excluded": {}
    })

    rc = fd.main(["--skip-git", "--staging-dir", str(tmp_path)])

    assert rc == 0
    assert "funding" in calls
    assert "perp" in calls
