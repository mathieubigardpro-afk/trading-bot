"""Tests de `tools/migrate_to_wallets.py` : archivage de l'ancien portefeuille unique 100k$
+ création des 3 wallets non initialisés, de façon idempotente."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bot import config
from bot.persist.audit import verify_chain
from bot.persist.state import compute_state_hash, init_state, load_state, save_state, validate_schema
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


# --- Non-régression : finding MAJEUR de l'audit multi-wallets --------------------------
#
# Le migrateur déplace l'ancien state.json (schéma legacy, sans wallet_id/initial_eur/fx) tel
# quel. Ni `test_migrate_archives_legacy_state_unchanged` (n'appelle jamais `load_state()` sur
# l'archive) ni les autres tests ci-dessus (ne portent que sur les 3 wallets neufs) ne
# vérifiaient que l'archive produite reste effectivement chargeable ET auditable après coup —
# c'est précisément ce que couvre le test suivant, en reproduisant un état legacy COMPLET
# (tous les champs du schéma pré-wallets, comme le vrai state.json de production) sur deux
# cycles réels avant migration.


def _make_full_legacy_repo(tmp_path) -> Path:
    """Dépôt avec un historique legacy complet et VALIDE (2 cycles réels, schéma pré-wallets
    complet — pas le fixture minimal de `_make_legacy_repo`, qui n'a jamais eu vocation à
    passer `validate_schema`/`verify_chain`)."""
    repo = tmp_path / "repo_full_legacy"
    repo.mkdir()
    assert _git(repo, "init", "-b", "main").returncode == 0
    state_dir = repo / "state"
    state_dir.mkdir()

    s0 = init_state()
    del s0["wallet_id"]
    del s0["initial_eur"]
    del s0["fx"]
    s0["cash_usd"] = 100000.0
    s0["equity_peak_usd"] = 100000.0
    save_state(s0, str(state_dir / "state.json"))
    (state_dir / "trades.jsonl").write_text("", encoding="utf-8")
    (state_dir / "equity.jsonl").write_text("", encoding="utf-8")
    (state_dir / "decisions.jsonl").write_text("", encoding="utf-8")
    _git(repo, "add", "state")
    _git(repo, "commit", "-m", "Cycle T0")
    hash0 = compute_state_hash(s0)

    # Cycle T1 : aucun trade (cash/positions inchangés, cohérent avec trades.jsonl vide) —
    # seul `last_run_id`/`state_hash_prev` avancent, comme un cycle horaire "0 trade(s)" réel.
    s1 = json.loads(json.dumps(s0))
    s1["last_run_id"] = "2026-07-23T07"
    s1["state_hash_prev"] = hash0
    save_state(s1, str(state_dir / "state.json"))
    _git(repo, "add", "state")
    _git(repo, "commit", "-m", "Cycle T1")

    return repo


def test_migrated_archive_stays_loadable_and_auditable(tmp_path):
    """Reproduction directe du finding MAJEUR : après `migrate()` (git mv réel, contenu
    inchangé), `state/archive-100k/state.json` doit rester chargeable par `load_state()`
    (schéma legacy accepté sans wallet_id/initial_eur/fx) ET auditable de bout en bout par
    `verify_chain()` (chemin suivi à travers le renommage, chaîne de hash cohérente malgré la
    version identique introduite par le mv)."""
    repo = _make_full_legacy_repo(tmp_path)

    report = migrate(str(repo))
    assert report.archived is True
    # migrate() ne committe pas lui-même (§10.8 : c'est un simple `git mv` staged) — on
    # committe ici pour reproduire fidèlement le commit réel de migration du dépôt.
    _git(repo, "commit", "-m", "Migration multi-wallets : archive l'ancien portefeuille")

    archived_path = repo / "state" / "archive-100k" / "state.json"
    state = load_state(str(archived_path))  # ne doit pas lever StateValidationError
    validate_schema(state)
    assert "wallet_id" not in state
    assert state["cash_usd"] == 100000.0
    assert state["last_run_id"] == "2026-07-23T07"

    result = verify_chain(str(repo), path="state/archive-100k/state.json")
    assert result.ok is True, result.errors
    assert result.n_versions_checked == 3
