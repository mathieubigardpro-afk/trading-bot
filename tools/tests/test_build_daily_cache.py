"""Tests de `tools/build_daily_cache.py` — script du workflow `daily-data-cache.yml` qui
régénère le cache disque `data-cache/` lu par `bot/feeds/daily.py:prefetch_daily_history()`
(cf. bandeau "INCIDENT 2026-07-24" dans ce module). Réseau intégralement mocké."""

from __future__ import annotations

import datetime as _dt
import json
import subprocess

import pandas as pd
import pytest
import requests

import tools.build_daily_cache as bdc
from bot import runner as runner_mod
from bot.feeds import daily as daily_mod
from bot.feeds.types import HistoryUnavailableError
from tools.fetch_data import publish_to_orphan_branch

NY_TZ = daily_mod._NY_TZ


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    daily_mod.clear_daily_cache()
    yield
    daily_mod.clear_daily_cache()


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


def _make_cache_df(n_days, start="2024-01-01"):
    idx = pd.date_range(start, periods=n_days, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0] * n_days, "high": [101.0] * n_days, "low": [99.0] * n_days,
            "close": [100.5] * n_days, "volume": [1_000_000.0] * n_days,
        },
        index=pd.Index(idx, name="ts"),
    )


# ---------------------------------------------------------------------------
# Univers — source de vérité unique = bot.runner / bot.config
# ---------------------------------------------------------------------------


def test_equity_etf_universe_matches_bot_runner_exactly():
    expected = sorted(set(runner_mod.EQUITIES_DATA_SYMBOLS) | set(runner_mod.ETF_DATA_SYMBOLS))
    assert bdc.equity_etf_universe() == expected
    assert len(bdc.equity_etf_universe()) > 100  # 103 S&P100 + SPY + 8 ETF + IEF (dédupliqués)


def test_crypto_universe_is_non_empty_and_deduplicated():
    universe = bdc.crypto_universe()
    assert universe == sorted(set(universe))
    assert len(universe) > 0


# ---------------------------------------------------------------------------
# build_equity_etf_cache / build_crypto_cache — écriture sur disque
# ---------------------------------------------------------------------------


def test_build_equity_etf_cache_writes_csv_and_reports_status(tmp_path, monkeypatch):
    tickers = ["AAPL", "MSFT"]
    batch_df = _make_yf_batch_multiindex(tickers, bdc.N_DAYS_EQUITY_ETF)
    monkeypatch.setattr(daily_mod.yf, "download", lambda **kwargs: batch_df)

    report = bdc.build_equity_etf_cache(str(tmp_path), tickers, bdc.N_DAYS_EQUITY_ETF)

    assert report["AAPL"]["ok"] is True
    assert report["AAPL"]["rows"] == bdc.N_DAYS_EQUITY_ETF
    assert report["MSFT"]["ok"] is True

    loaded = daily_mod._load_disk_cache_symbol(str(tmp_path), "equity", "AAPL")
    assert loaded is not None
    assert len(loaded) == bdc.N_DAYS_EQUITY_ETF


def test_build_equity_etf_cache_marks_unresolved_symbol_as_failed(tmp_path, monkeypatch):
    # Le lot ne renvoie rien pour "MSFT", et le repli individuel échoue aussi (yfinance ET
    # stooq) -> HistoryUnavailableError attrapée, symbole absent du cache disque.
    monkeypatch.setattr(daily_mod, "_MAX_ATTEMPTS_DEFAULT", 1)  # évite les sleeps de backoff réels
    batch_df = _make_yf_batch_multiindex(["AAPL"], bdc.N_DAYS_EQUITY_ETF)
    monkeypatch.setattr(daily_mod.yf, "download", lambda **kwargs: batch_df)

    def fail_single(sym):
        raise RuntimeError("réseau bloqué (simulation test)")

    monkeypatch.setattr(daily_mod.yf, "Ticker", fail_single)

    def fail_stooq(*a, **k):
        raise requests.ConnectionError("réseau bloqué (simulation test)")

    monkeypatch.setattr(daily_mod._stooq_session, "get", fail_stooq)

    report = bdc.build_equity_etf_cache(str(tmp_path), ["AAPL", "MSFT"], bdc.N_DAYS_EQUITY_ETF)

    assert report["AAPL"]["ok"] is True
    assert report["MSFT"]["ok"] is False
    assert daily_mod._load_disk_cache_symbol(str(tmp_path), "equity", "MSFT") is None


def test_build_crypto_cache_writes_csv(tmp_path, monkeypatch):
    def fake_binance_klines(pair, n_days):
        now_ms = int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
        day_ms = 86_400_000
        rows = {}
        for i in range(n_days, 0, -1):
            open_ms = now_ms - i * day_ms
            rows[open_ms] = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0}
        df = pd.DataFrame.from_dict(rows, orient="index")
        df.index = pd.to_datetime(df.index, unit="ms", utc=True)
        df.index.name = "ts"
        return df.sort_index()[["open", "high", "low", "close", "volume"]]

    monkeypatch.setattr(daily_mod, "_fetch_binance_daily_klines", lambda pair, n_days: fake_binance_klines(pair, n_days))

    symbols = ["BTC"]
    report = bdc.build_crypto_cache(str(tmp_path), symbols, bdc.N_DAYS_CRYPTO)

    assert report["BTC"]["ok"] is True
    loaded = daily_mod._load_disk_cache_symbol(str(tmp_path), "crypto", "BTC")
    assert loaded is not None
    assert len(loaded) == bdc.N_DAYS_CRYPTO


# ---------------------------------------------------------------------------
# build_cache — bout-en-bout (staging + MANIFEST.json), sans publication git
# ---------------------------------------------------------------------------


def test_build_cache_writes_manifest_with_counts(tmp_path, monkeypatch):
    universe = ["AAPL", "MSFT"]
    monkeypatch.setattr(bdc, "equity_etf_universe", lambda: universe)
    monkeypatch.setattr(bdc, "crypto_universe", lambda: [])

    batch_df = _make_yf_batch_multiindex(universe, bdc.N_DAYS_EQUITY_ETF)
    monkeypatch.setattr(daily_mod.yf, "download", lambda **kwargs: batch_df)

    manifest = bdc.build_cache(str(tmp_path))

    assert manifest["counts"]["equity_etf_total"] == 2
    assert manifest["counts"]["equity_etf_ok"] == 2
    assert manifest["counts"]["crypto_total"] == 0
    assert "generated_at" in manifest

    with open(tmp_path / "MANIFEST.json") as f:
        on_disk = json.load(f)
    assert on_disk == manifest


def test_main_skip_git_leaves_staging_dir_populated(tmp_path, monkeypatch):
    universe = ["AAPL"]
    monkeypatch.setattr(bdc, "equity_etf_universe", lambda: universe)
    monkeypatch.setattr(bdc, "crypto_universe", lambda: [])
    batch_df = _make_yf_batch_multiindex(universe, bdc.N_DAYS_EQUITY_ETF)
    monkeypatch.setattr(daily_mod.yf, "download", lambda **kwargs: batch_df)

    rc = bdc.main(["--skip-git", "--staging-dir", str(tmp_path)])

    assert rc == 0
    assert (tmp_path / "MANIFEST.json").is_file()
    assert (tmp_path / "equity" / "AAPL.csv.gz").is_file()


def test_main_aborts_publication_when_no_equity_symbol_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_mod, "_MAX_ATTEMPTS_DEFAULT", 1)  # évite les sleeps de backoff réels
    universe = ["AAPL"]
    monkeypatch.setattr(bdc, "equity_etf_universe", lambda: universe)
    monkeypatch.setattr(bdc, "crypto_universe", lambda: [])

    def fail_download(**kwargs):
        raise RuntimeError("réseau bloqué (simulation test)")

    def fail_single(sym):
        raise RuntimeError("réseau bloqué (simulation test)")

    def fail_stooq(*a, **k):
        raise requests.ConnectionError("réseau bloqué (simulation test)")

    monkeypatch.setattr(daily_mod.yf, "download", fail_download)
    monkeypatch.setattr(daily_mod.yf, "Ticker", fail_single)
    monkeypatch.setattr(daily_mod._stooq_session, "get", fail_stooq)

    rc = bdc.main(["--skip-git", "--staging-dir", str(tmp_path)])

    assert rc == 1


# ---------------------------------------------------------------------------
# Publication git (branche orpheline `data-cache`) — régression : `publish_to_orphan_branch`
# est réutilisée depuis `tools/fetch_data.py`, dont la mise en page par défaut ("data/",
# "MANIFEST.json", "DATA_REPORT.md") ne correspond PAS à celle de ce script ("equity/",
# "crypto/", "MANIFEST.json"). Sans `entries=bdc.PUBLISH_ENTRIES` explicite, seul MANIFEST.json
# était publié — AUCUNE donnée — bug identifié en validation réelle (première régénération du
# cache sur le dépôt de production). Ce test verrouille que les deux sont bien publiés.
# ---------------------------------------------------------------------------


def _init_git_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test.local"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


def test_publish_to_orphan_branch_includes_data_directories_not_just_manifest(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir)

    staging_dir = tmp_path / "staging"
    daily_mod.write_cache_symbol_csv(str(staging_dir), "equity", "AAPL", _make_cache_df(5))
    daily_mod.write_cache_manifest(str(staging_dir), _dt.datetime.now(_dt.timezone.utc))

    sha = publish_to_orphan_branch(
        str(repo_dir), str(staging_dir), "data-cache", push=False, entries=bdc.PUBLISH_ENTRIES
    )

    assert sha is not None
    tracked = subprocess.run(
        ["git", "-C", str(repo_dir), "ls-tree", "-r", "--name-only", sha],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert "MANIFEST.json" in tracked
    assert "equity/AAPL.csv.gz" in tracked


def test_publish_to_orphan_branch_default_entries_unchanged_for_market_data(tmp_path):
    """Non-régression : les appelants existants (`tools/fetch_data.py` lui-même, branche
    `market-data`) ne passent PAS `entries` -> le défaut doit rester `data/`+MANIFEST.json+
    DATA_REPORT.md, inchangé par l'ajout du paramètre `entries`."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir)

    staging_dir = tmp_path / "staging"
    (staging_dir / "data" / "crypto").mkdir(parents=True)
    (staging_dir / "data" / "crypto" / "BTC.csv.gz").write_bytes(b"fake")
    (staging_dir / "MANIFEST.json").write_text("{}")
    (staging_dir / "DATA_REPORT.md").write_text("# report")

    sha = publish_to_orphan_branch(str(repo_dir), str(staging_dir), "market-data", push=False)

    assert sha is not None
    tracked = subprocess.run(
        ["git", "-C", str(repo_dir), "ls-tree", "-r", "--name-only", sha],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert "MANIFEST.json" in tracked
    assert "DATA_REPORT.md" in tracked
    assert "data/crypto/BTC.csv.gz" in tracked
