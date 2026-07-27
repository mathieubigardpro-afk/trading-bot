"""Tests du détecteur de trous (correctif planification 2026-07-27, cf. bandeau
`_detect_equity_gaps` dans `bot/runner.py` et `.github/workflows/bot.yml`) : le cron passe de
3 à 6 tentatives/heure (idempotence par run_id déjà en place) ; ce détecteur, lu au début de
chaque cycle, repère les heures des dernières 24h SANS AUCUNE entrée `equity.jsonl` (tous
wallets confondus) -- sans jamais tenter de rattrapage rétroactif."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bot.runner as runner
from bot import config


def _write_equity_line(path: Path, run_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"run_id": run_id, "ts": "2026-07-27T00:00:00+00:00"}) + "\n")


def test_detect_equity_gaps_no_files_returns_full_window(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WALLETS_DIR", str(tmp_path / "wallets"))
    monkeypatch.setattr(config, "WALLET_IDS", ["prudent"])

    now = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
    missing = runner._detect_equity_gaps(now, window_hours=24)

    assert len(missing) == 24
    # borne : l'heure courante elle-même n'est jamais listée (cycle pas encore terminé).
    assert now.strftime("%Y-%m-%dT%H") not in missing
    # l'heure juste précédente doit apparaître comme manquante.
    assert (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H") in missing


def test_detect_equity_gaps_all_hours_present_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WALLETS_DIR", str(tmp_path / "wallets"))
    monkeypatch.setattr(config, "WALLET_IDS", ["prudent", "agressif"])

    now = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
    path = tmp_path / "wallets" / "prudent" / "equity.jsonl"
    for h in range(1, 25):
        hour = (now - timedelta(hours=h)).strftime("%Y-%m-%dT%H")
        _write_equity_line(path, hour)

    missing = runner._detect_equity_gaps(now, window_hours=24)
    assert missing == []


def test_detect_equity_gaps_union_across_wallets(tmp_path, monkeypatch):
    """Une heure n'est PAS un trou si AU MOINS UN wallet a une entrée (union, pas
    intersection) -- un wallet isolé qui saute son équity un cycle donné (raison propre à ce
    wallet) ne doit pas déclencher un faux positif "aucun cycle n'a tourné"."""
    monkeypatch.setattr(config, "WALLETS_DIR", str(tmp_path / "wallets"))
    monkeypatch.setattr(config, "WALLET_IDS", ["prudent", "agressif"])

    now = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
    hour_08 = "2026-07-27T08"
    hour_09 = "2026-07-27T09"
    _write_equity_line(tmp_path / "wallets" / "prudent" / "equity.jsonl", hour_08)
    _write_equity_line(tmp_path / "wallets" / "agressif" / "equity.jsonl", hour_09)

    missing = runner._detect_equity_gaps(now, window_hours=24)
    assert hour_08 not in missing
    assert hour_09 not in missing


def test_detect_equity_gaps_some_missing_hours(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WALLETS_DIR", str(tmp_path / "wallets"))
    monkeypatch.setattr(config, "WALLET_IDS", ["prudent"])

    now = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
    path = tmp_path / "wallets" / "prudent" / "equity.jsonl"
    for h in range(1, 25):
        if h in (3, 4):  # 2 heures volontairement absentes
            continue
        hour = (now - timedelta(hours=h)).strftime("%Y-%m-%dT%H")
        _write_equity_line(path, hour)

    missing = runner._detect_equity_gaps(now, window_hours=24)
    assert missing == sorted([
        (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H"),
        (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H"),
    ])


def test_detect_equity_gaps_malformed_lines_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WALLETS_DIR", str(tmp_path / "wallets"))
    monkeypatch.setattr(config, "WALLET_IDS", ["prudent"])

    now = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
    path = tmp_path / "wallets" / "prudent" / "equity.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("not json at all\n")
        f.write("\n")
        f.write(json.dumps({"no_run_id_field": True}) + "\n")

    # Ne lève jamais, se contente d'ignorer les lignes inexploitables.
    missing = runner._detect_equity_gaps(now, window_hours=24)
    assert len(missing) == 24


def test_build_gap_detected_record_shape():
    now = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
    record = runner._build_gap_detected_record("prudent", "2026-07-27T10", now, ["2026-07-27T03"])

    assert record["type"] == "gap_detected"
    assert record["wallet_id"] == "prudent"
    assert record["run_id"] == "2026-07-27T10"
    assert record["missing_hours"] == ["2026-07-27T03"]
    assert record["window_hours"] == runner.GAP_DETECTION_WINDOW_HOURS
    assert "rattrapage" in record["reason"]
