"""Tests de `tools/check_data_anomalies.py` — détecteur d'anomalies de corporate actions
(`docs/RESEARCH-BACKLOG.md` idée P0#11, cas DHR/Fortive). Entièrement offline : aucune fixture
ne touche le réseau, tout est construit synthétiquement en mémoire ou sur `tmp_path`."""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

import tools.check_data_anomalies as cda

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_frame(dates, closes, opens=None, highs=None, lows=None, volume=1_000_000.0) -> pd.DataFrame:
    """Construit une série OHLCV synthétique au même contrat que `backtest/data.py:
    load_raw_series` (index = date normalisée, colonnes open/high/low/close/volume). Par défaut
    open=close, high=close*1.01, low=close*0.99 (barre "plate", sans incohérence OHLC propre)."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates], name="date")
    n = len(dates)
    opens = opens if opens is not None else list(closes)
    highs = highs if highs is not None else [c * 1.01 for c in closes]
    lows = lows if lows is not None else [c * 0.99 for c in closes]
    return pd.DataFrame(
        {
            "open": [float(x) for x in opens],
            "high": [float(x) for x in highs],
            "low": [float(x) for x in lows],
            "close": [float(x) for x in closes],
            "volume": [float(volume)] * n,
        },
        index=idx,
    )


def _healthy_series(n=30, start="2024-01-02", start_price=100.0):
    """Série saine : jours ouvrés consécutifs (lun-ven), rendements quotidiens petits (< 2%),
    OHLC toujours cohérent -> AUCUNE anomalie attendue."""
    dates = pd.bdate_range(start, periods=n)
    closes = []
    price = start_price
    for i in range(n):
        # petite oscillation déterministe, jamais > 2%
        price *= 1.0 + (0.01 if i % 2 == 0 else -0.008)
        closes.append(price)
    return _make_frame(dates, closes)


def _write_csv_gz(path: Path, df: pd.DataFrame) -> None:
    """Écrit `df` (index=date, colonnes OHLCV) au format exact attendu par
    `backtest/data.py:load_raw_series` : CSV gz, colonne `timestamp` + open/high/low/close/volume."""
    out = df.reset_index().rename(columns={"index": "timestamp", "date": "timestamp"})
    out["timestamp"] = pd.DatetimeIndex(out["timestamp"]).tz_localize("UTC").map(lambda ts: ts.isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        out.to_csv(f, index=False, columns=["timestamp", "open", "high", "low", "close", "volume"])


# ---------------------------------------------------------------------------
# scan_frame — série saine
# ---------------------------------------------------------------------------


def test_healthy_series_has_zero_anomalies():
    df = _healthy_series()
    anomalies = cda.scan_frame(df, "AAPL")
    assert anomalies == []


def test_healthy_series_with_weekend_and_single_holiday_gap_not_flagged():
    # Lundi -> vendredi -> lundi normal (3 jours calendaires) : PAS un trou anormal.
    dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
    closes = [100.0, 100.5, 101.0, 100.8, 101.2]
    df = _make_frame(dates, closes)
    anomalies = cda.scan_frame(df, "AAPL")
    assert anomalies == []
    gaps = [a for a in anomalies if a["type"] == "calendar_gap"]
    assert gaps == []


def test_three_day_weekend_gap_not_flagged_directly():
    """Un trou de 3 jours calendaires (week-end simple) ne doit JAMAIS être signalé, seuil
    par défaut = 10 jours."""
    dates = pd.bdate_range("2024-01-02", periods=10)  # week-ends naturellement absents
    closes = [100.0 + i * 0.1 for i in range(10)]
    df = _make_frame(dates, closes)
    anomalies = cda.scan_frame(df, "AAPL")
    assert [a for a in anomalies if a["type"] == "calendar_gap"] == []


# ---------------------------------------------------------------------------
# scan_frame — cas DHR/Fortive (spike de rendement positif ET négatif)
# ---------------------------------------------------------------------------


def test_dhr_style_plus_62_percent_spike_detected():
    """Reproduit le motif exact de l'incident documenté : un titre "plat" qui saute de +62% en
    une séance (spin-off mal ajusté) doit être détecté comme return_spike."""
    dates = pd.bdate_range("2016-07-01", periods=5)
    closes = [80.0, 80.2, 80.1, 129.76, 130.0]  # +61.9% le 4e jour, motif DHR
    df = _make_frame(dates, closes)
    anomalies = cda.scan_frame(df, "DHR")
    spikes = [a for a in anomalies if a["type"] == "return_spike"]
    assert len(spikes) == 1
    spike = spikes[0]
    assert spike["symbol"] == "DHR"
    assert spike["date"] == dates[3].date().isoformat()
    assert spike["valeur"] == pytest.approx(0.62, abs=0.01)
    assert spike["valeur"] > 0


def test_minus_45_percent_true_crash_also_detected_as_expected_false_positive():
    """Un vrai -45% (profit warning idiosyncratique, pas une anomalie de donnée) DOIT quand
    même apparaître dans le rapport — c'est un faux positif ASSUMÉ (cf. docstring module) :
    ce script ne peut pas distinguer, sans calendrier de corporate actions externe, un vrai
    krach d'un artefact d'ajustement. L'humain tranche, pas le script."""
    dates = pd.bdate_range("2024-03-01", periods=5)
    closes = [50.0, 50.5, 49.8, 27.4, 27.6]  # -45% le 4e jour
    df = _make_frame(dates, closes)
    anomalies = cda.scan_frame(df, "XYZ")
    spikes = [a for a in anomalies if a["type"] == "return_spike"]
    assert len(spikes) == 1
    assert spikes[0]["valeur"] == pytest.approx(-0.45, abs=0.01)
    assert spikes[0]["valeur"] < 0


def test_spike_exactly_at_threshold_not_flagged_strictly_greater_required():
    dates = pd.bdate_range("2024-01-02", periods=2)
    closes = [100.0, 140.0]  # exactement +40%
    df = _make_frame(dates, closes)
    anomalies = cda.scan_frame(df, "ABC", threshold=0.40)
    assert [a for a in anomalies if a["type"] == "return_spike"] == []


def test_threshold_is_configurable():
    dates = pd.bdate_range("2024-01-02", periods=2)
    closes = [100.0, 115.0]  # +15%
    df = _make_frame(dates, closes)
    # seuil par défaut (40%) : rien détecté
    assert cda.scan_frame(df, "ABC") == []
    # seuil abaissé à 10% : détecté
    anomalies = cda.scan_frame(df, "ABC", threshold=0.10)
    spikes = [a for a in anomalies if a["type"] == "return_spike"]
    assert len(spikes) == 1
    assert spikes[0]["valeur"] == pytest.approx(0.15, abs=1e-9)


# ---------------------------------------------------------------------------
# scan_frame — incohérences OHLC
# ---------------------------------------------------------------------------


def test_low_greater_than_high_detected():
    dates = pd.bdate_range("2024-01-02", periods=3)
    closes = [100.0, 100.5, 101.0]
    highs = [101.0, 99.0, 102.0]  # jour 2 : high < low (corrompu)
    lows = [99.0, 100.0, 100.0]
    df = _make_frame(dates, closes, highs=highs, lows=lows)
    anomalies = cda.scan_frame(df, "ABC")
    bad = [a for a in anomalies if a["type"] == "ohlc_inconsistency" and "low" in a["detail"] and "high" in a["detail"]]
    assert len(bad) == 1
    assert bad[0]["date"] == dates[1].date().isoformat()


def test_negative_or_zero_price_detected():
    dates = pd.bdate_range("2024-01-02", periods=3)
    closes = [100.0, -5.0, 101.0]  # close négatif au jour 2
    highs = [101.0, 1.0, 102.0]
    lows = [99.0, -10.0, 100.0]
    opens = [100.0, -6.0, 100.5]
    df = _make_frame(dates, closes, opens=opens, highs=highs, lows=lows)
    anomalies = cda.scan_frame(df, "ABC")
    neg = [a for a in anomalies if a["type"] == "ohlc_inconsistency" and "<= 0" in a["detail"]]
    # close, low et open sont tous <= 0 au jour 2 -> au moins ces 3 signalements
    dates_flagged = {a["date"] for a in neg}
    assert dates[1].date().isoformat() in dates_flagged
    assert len(neg) >= 3


def test_float_noise_at_boundary_not_flagged():
    """Bruit de flottant réel constaté sur les prix ajustés multi-décennies (ex. AAPL 1981) :
    close et low représentent la même valeur "réelle" mais diffèrent à ~1e-16 près à cause de
    deux arrondis de calcul différents côté fournisseur. Ceci ne doit PAS être signalé — ce
    n'est pas une incohérence de donnée, juste du bruit numérique. Cf. `_OHLC_REL_TOL`."""
    dates = pd.bdate_range("2024-01-02", periods=2)
    close_val = 0.06105915457010269
    low_val = 0.0610591545701027  # même valeur, dernier bit différent
    closes = [100.0, close_val]
    lows = [99.0, low_val]
    highs = [101.0, 0.0615]
    opens = [100.0, close_val]
    df = _make_frame(dates, closes, opens=opens, highs=highs, lows=lows)
    anomalies = cda.scan_frame(df, "AAPL")
    assert [a for a in anomalies if a["type"] == "ohlc_inconsistency"] == []


def test_close_outside_low_high_range_detected():
    dates = pd.bdate_range("2024-01-02", periods=2)
    closes = [100.0, 105.0]  # jour 2 : close > high
    highs = [101.0, 103.0]
    lows = [99.0, 101.0]
    df = _make_frame(dates, closes, highs=highs, lows=lows)
    anomalies = cda.scan_frame(df, "ABC")
    bad = [a for a in anomalies if a["type"] == "ohlc_inconsistency" and "close" in a["detail"]]
    assert len(bad) == 1
    assert bad[0]["date"] == dates[1].date().isoformat()


# ---------------------------------------------------------------------------
# scan_frame — trous de calendrier
# ---------------------------------------------------------------------------


def test_fifteen_day_gap_detected():
    dates = [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03"), pd.Timestamp("2024-01-18")]
    closes = [100.0, 100.5, 101.0]
    df = _make_frame(dates, closes)
    anomalies = cda.scan_frame(df, "ABC")
    gaps = [a for a in anomalies if a["type"] == "calendar_gap"]
    assert len(gaps) == 1
    assert gaps[0]["date"] == dates[2].date().isoformat()
    assert gaps[0]["valeur"] == 15.0


def test_first_bar_never_flagged_as_gap_even_if_series_starts_late():
    # La toute première barre d'une série (ex. après IPO) n'a pas de "précédente" -> jamais
    # de calendar_gap sur cette barre, quelle que soit la date de départ.
    dates = pd.bdate_range("2024-06-01", periods=3)
    closes = [50.0, 50.2, 50.1]
    df = _make_frame(dates, closes)
    anomalies = cda.scan_frame(df, "NEWIPO")
    assert [a for a in anomalies if a["type"] == "calendar_gap"] == []


def test_max_gap_days_is_configurable():
    dates = [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03"), pd.Timestamp("2024-01-10")]  # 7 jours
    closes = [100.0, 100.5, 101.0]
    df = _make_frame(dates, closes)
    assert cda.scan_frame(df, "ABC", max_gap_days=10) == []
    gaps = cda.scan_frame(df, "ABC", max_gap_days=5)
    assert len(gaps) == 1
    assert gaps[0]["type"] == "calendar_gap"


# ---------------------------------------------------------------------------
# scan_data_dir — vrais fichiers .csv.gz écrits sur tmp_path
# ---------------------------------------------------------------------------


def test_scan_data_dir_finds_anomalies_across_real_gz_files(tmp_path):
    equities_dir = tmp_path / "equities"
    etf_dir = tmp_path / "etf"

    # AAPL : sain.
    _write_csv_gz(equities_dir / "AAPL.csv.gz", _healthy_series(n=20))

    # DHR : contient le spike +62% caractéristique de l'incident documenté.
    dhr_dates = pd.bdate_range("2016-07-01", periods=5)
    dhr_closes = [80.0, 80.2, 80.1, 129.76, 130.0]
    _write_csv_gz(equities_dir / "DHR.csv.gz", _make_frame(dhr_dates, dhr_closes))

    # SPY (ETF) : trou de calendrier de 20 jours.
    spy_dates = [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03"), pd.Timestamp("2024-01-23")]
    spy_closes = [400.0, 401.0, 402.0]
    _write_csv_gz(etf_dir / "SPY.csv.gz", _make_frame(spy_dates, spy_closes))

    result = cda.scan_data_dir(tmp_path, threshold=0.40)

    assert result["n_files_scanned"] == 3
    assert result["params"]["threshold"] == 0.40
    types_found = {a["type"] for a in result["anomalies"]}
    assert "return_spike" in types_found
    assert "calendar_gap" in types_found

    symbols_with_anomalies = {a["symbol"] for a in result["anomalies"]}
    assert "DHR" in symbols_with_anomalies
    assert "SPY" in symbols_with_anomalies
    assert "AAPL" not in symbols_with_anomalies


def test_scan_data_dir_counts_files_with_no_anomalies_too(tmp_path):
    equities_dir = tmp_path / "equities"
    _write_csv_gz(equities_dir / "AAPL.csv.gz", _healthy_series(n=10))
    _write_csv_gz(equities_dir / "MSFT.csv.gz", _healthy_series(n=10, start_price=300.0))

    result = cda.scan_data_dir(tmp_path)
    assert result["n_files_scanned"] == 2
    assert result["anomalies"] == []


def test_scan_data_dir_missing_subdir_does_not_crash(tmp_path):
    # Ni "equities" ni "etf" n'existent sous tmp_path : scan vide, pas d'exception.
    result = cda.scan_data_dir(tmp_path)
    assert result["n_files_scanned"] == 0
    assert result["anomalies"] == []


def test_scan_data_dir_respects_custom_subdirs(tmp_path):
    custom_dir = tmp_path / "crypto"
    _write_csv_gz(custom_dir / "BTC.csv.gz", _healthy_series(n=5))
    result_default = cda.scan_data_dir(tmp_path)  # equities/etf par défaut -> rien
    assert result_default["n_files_scanned"] == 0

    result_custom = cda.scan_data_dir(tmp_path, subdirs=("crypto",))
    assert result_custom["n_files_scanned"] == 1


# ---------------------------------------------------------------------------
# format_report_md
# ---------------------------------------------------------------------------


def test_format_report_md_empty_result():
    result = {"anomalies": [], "n_files_scanned": 5, "params": {"threshold": 0.4, "max_gap_days": 10, "subdirs": ["equities"]}}
    md = cda.format_report_md(result)
    assert "Aucune anomalie détectée" in md
    assert "5" in md


def test_format_report_md_groups_by_type_and_sorts_by_symbol_then_date():
    result = {
        "anomalies": [
            {"symbol": "ZZZ", "date": "2024-01-05", "type": "return_spike", "valeur": 0.5, "detail": "d1"},
            {"symbol": "AAA", "date": "2024-01-10", "type": "return_spike", "valeur": 0.6, "detail": "d2"},
            {"symbol": "AAA", "date": "2024-01-02", "type": "return_spike", "valeur": 0.41, "detail": "d3"},
            {"symbol": "BBB", "date": "2024-01-01", "type": "calendar_gap", "valeur": 15.0, "detail": "d4"},
        ],
        "n_files_scanned": 10,
        "params": {"threshold": 0.4, "max_gap_days": 10, "subdirs": ["equities", "etf"]},
    }
    md = cda.format_report_md(result)
    # AAA (2024-01-02) doit apparaître avant AAA (2024-01-10), qui doit apparaître avant ZZZ.
    idx_aaa_02 = md.index("AAA | 2024-01-02")
    idx_aaa_10 = md.index("AAA | 2024-01-10")
    idx_zzz = md.index("ZZZ | 2024-01-05")
    assert idx_aaa_02 < idx_aaa_10 < idx_zzz
    assert "Sauts de rendement" in md
    assert "Trous de calendrier" in md


# ---------------------------------------------------------------------------
# CLI (main / subprocess)
# ---------------------------------------------------------------------------


def test_cli_main_writes_json_and_md(tmp_path):
    equities_dir = tmp_path / "equities"
    dhr_dates = pd.bdate_range("2016-07-01", periods=5)
    dhr_closes = [80.0, 80.2, 80.1, 129.76, 130.0]
    _write_csv_gz(equities_dir / "DHR.csv.gz", _make_frame(dhr_dates, dhr_closes))

    out_json = tmp_path / "out" / "anomalies.json"
    out_md = tmp_path / "out" / "anomalies.md"

    rc = cda.main([
        "--data-dir", str(tmp_path),
        "--out-json", str(out_json),
        "--out-md", str(out_md),
    ])

    # Code retour 0 MÊME s'il y a des anomalies : c'est un journal, pas un gate (cf. spec).
    assert rc == 0
    assert out_json.is_file()
    assert out_md.is_file()

    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["n_files_scanned"] == 1
    assert len(payload["anomalies"]) >= 1
    assert any(a["type"] == "return_spike" for a in payload["anomalies"])

    md_content = out_md.read_text(encoding="utf-8")
    assert "DHR" in md_content


def test_cli_main_returns_zero_even_when_anomalies_found_no_out_paths(tmp_path, capsys):
    equities_dir = tmp_path / "equities"
    dhr_dates = pd.bdate_range("2016-07-01", periods=5)
    dhr_closes = [80.0, 80.2, 80.1, 129.76, 130.0]
    _write_csv_gz(equities_dir / "DHR.csv.gz", _make_frame(dhr_dates, dhr_closes))

    rc = cda.main(["--data-dir", str(tmp_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "DHR" in captured.out


def test_cli_main_returns_one_on_execution_error(monkeypatch, tmp_path):
    def _boom(*args, **kwargs):
        raise RuntimeError("panne simulée")

    monkeypatch.setattr(cda, "scan_data_dir", _boom)
    rc = cda.main(["--data-dir", str(tmp_path)])
    assert rc == 1


def test_cli_subprocess_end_to_end(tmp_path):
    equities_dir = tmp_path / "equities"
    _write_csv_gz(equities_dir / "AAPL.csv.gz", _healthy_series(n=10))

    out_json = tmp_path / "out.json"
    result = subprocess.run(
        [
            sys.executable, str(_REPO_ROOT / "tools" / "check_data_anomalies.py"),
            "--data-dir", str(tmp_path),
            "--threshold", "0.40",
            "--out-json", str(out_json),
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert out_json.is_file()
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["n_files_scanned"] == 1
    assert payload["anomalies"] == []
