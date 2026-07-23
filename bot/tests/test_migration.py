"""Tests de `tools/migrate_to_wallets.py` : archivage de l'ancien portefeuille unique 100k$
+ création des 3 wallets non initialisés, de façon idempotente."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bot import config
from bot.persist.state import load_state, validate_schema
from tools.migrate_to_wallets import migrate


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


def _make_legacy_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-b", "main").returncode == 0
    state_dir = repo / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"schema_version": 1, "cash_usd": 87654.32, "positions": {}}), encoding="utf-8"
    )
    (state_dir / "trades.jsonl").write_text('{"run_id": "2026-07-22T14"}\n', encoding="utf-8")
    (state_dir / "equity.jsonl").write_text('{"run_id": "2026-07-22T14", "equity_usd": 87654.32}\n', encoding="utf-8")
    (state_dir / "decisions.jsonl").write_text("", encoding="utf-8")
    _git(repo, "add", "state")
    _git(repo, "commit", "-m", "Etat legacy 100k$")
    return repo


def test_migrate_archives_legacy_state_unchanged(tmp_path):
    repo = _make_legacy_repo(tmp_path)
    original_content = (repo / "state" / "state.json").read_text(encoding="utf-8")

    report = migrate(str(repo))

    assert report.archived is True
    assert set(report.archived_files) == {"state.json", "trades.jsonl", "equity.jsonl", "decisions.jsonl"}

    archive_dir = repo / "state" / "archive-100k"
    assert (archive_dir / "state.json").read_text(encoding="utf-8") == original_content
    assert (archive_dir / "trades.jsonl").read_text(encoding="utf-8") == '{"run_id": "2026-07-22T14"}\n'
    # L'ancien emplacement n'existe plus (déplacé, pas dupliqué).
    assert not (repo / "state" / "state.json").exists()
    assert not (repo / "state" / "trades.jsonl").exists()


def test_migrate_creates_three_uninitialized_wallets(tmp_path):
    repo = _make_legacy_repo(tmp_path)
    report = migrate(str(repo))

    assert set(report.wallets_created) == set(config.WALLET_IDS)
    for wallet_cfg in config.WALLETS:
        wallet_id = wallet_cfg["id"]
        state_path = repo / config.wallet_state_json(wallet_id)
        assert state_path.exists()
        state = load_state(str(state_path))
        validate_schema(state)
        assert state["wallet_id"] == wallet_id
        assert state["initial_eur"] == pytest.approx(wallet_cfg["capital_initial_eur"])
        assert state["cash_usd"] == 0.0
        assert state["positions"] == {}
        assert state["fx"]["initial_rate"] is None
        assert state["last_run_id"] is None

        for name in ("trades.jsonl", "equity.jsonl", "decisions.jsonl"):
            assert (repo / config.wallet_state_dir(wallet_id) / name).exists()

    cycle_path = repo / config.CYCLE_JSON
    assert cycle_path.exists()
    cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
    assert cycle["last_run_id"] is None
    assert set(cycle["wallet_ids"]) == set(config.WALLET_IDS)


def test_migrate_is_idempotent(tmp_path):
    repo = _make_legacy_repo(tmp_path)
    migrate(str(repo))

    archive_snapshot = (repo / "state" / "archive-100k" / "state.json").read_text(encoding="utf-8")
    wallet_snapshot = (repo / config.wallet_state_json("prudent")).read_text(encoding="utf-8")

    second_report = migrate(str(repo))

    assert second_report.archived is False  # déjà archivé, rien de plus à faire
    assert set(second_report.wallets_already_present) == set(config.WALLET_IDS)
    assert second_report.wallets_created == []
    assert second_report.cycle_json_created is False

    assert (repo / "state" / "archive-100k" / "state.json").read_text(encoding="utf-8") == archive_snapshot
    assert (repo / config.wallet_state_json("prudent")).read_text(encoding="utf-8") == wallet_snapshot


def test_migrate_does_not_reinitialize_an_already_initialized_wallet(tmp_path):
    """Garde-fou critique : relancer le script de migration par erreur après qu'un cycle réel
    a déjà initialisé un wallet (capital converti, positions ouvertes) ne doit JAMAIS écraser
    son état — l'idempotence porte sur la présence du fichier, pas sur son contenu."""
    repo = _make_legacy_repo(tmp_path)
    migrate(str(repo))

    state_path = repo / config.wallet_state_json("agressif")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["cash_usd"] = 1234.56
    state["fx"]["initial_rate"] = 1.09
    state_path.write_text(json.dumps(state), encoding="utf-8")

    migrate(str(repo))

    reloaded = json.loads(state_path.read_text(encoding="utf-8"))
    assert reloaded["cash_usd"] == 1234.56
    assert reloaded["fx"]["initial_rate"] == 1.09


def test_migrate_without_legacy_state_only_creates_wallets(tmp_path):
    repo = tmp_path / "repo_no_legacy"
    repo.mkdir()
    (repo / "state").mkdir()

    report = migrate(str(repo))

    assert report.archived is False
    assert not (repo / "state" / "archive-100k").exists()
    assert set(report.wallets_created) == set(config.WALLET_IDS)
