"""Tests de bot/persist/git_sync.py : commit+push, conflit de push concurrent, gestion de
l'idempotence via last_run_id distant (ABORTED_DUPLICATE)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bot.persist.git_sync import git_sync, has_uncommitted_state_changes, pull_rebase
from bot.persist.state import init_state, save_state


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def _git_ok(repo: Path, *args: str) -> str:
    result = _git(repo, *args)
    assert result.returncode == 0, f"git {args} a échoué : {result.stderr}"
    return result.stdout


def _write_state_files(repo: Path, state: dict) -> None:
    state_dir = repo / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    save_state(state, str(state_dir / "state.json"))
    for name in ("trades.jsonl", "equity.jsonl", "decisions.jsonl"):
        p = state_dir / name
        if not p.exists():
            p.write_text("", encoding="utf-8")


@pytest.fixture
def origin_and_clone(tmp_path):
    """Un dépôt bare `origin` + un clone local avec un commit initial contenant
    state/state.json (état initial) déjà présent, comme le premier run committé du projet."""
    origin = tmp_path / "origin.git"
    _git_ok(tmp_path, "init", "--bare", "-b", "main", str(origin))

    clone = tmp_path / "clone"
    _git_ok(tmp_path, "clone", str(origin), str(clone))

    _write_state_files(clone, init_state())
    _git_ok(clone, "add", "state")
    _git_ok(clone, "commit", "-m", "Etat initial")
    _git_ok(clone, "push", "origin", "main")

    return origin, clone


def test_git_sync_success_simple_commit(origin_and_clone, tmp_path):
    origin, clone = origin_and_clone

    state = init_state()
    state["last_run_id"] = "2026-07-22T14"
    state["cash_usd"] = 95000.0
    _write_state_files(clone, state)

    result = git_sync(str(clone), "Run 2026-07-22T14 : 1 trade, equity 100482$", run_id="2026-07-22T14")
    assert result == "SUCCESS"

    # L'origin distant reflète bien le nouveau commit.
    log = _git_ok(origin, "log", "--format=%s", "-n1")
    assert "2026-07-22T14" in log


def test_git_sync_fails_cleanly_without_remote(tmp_path):
    """Un dépôt sans remote configuré (ou injoignable) doit retourner FAILED proprement, sans
    lever d'exception, et sans laisser le working tree dans un état incohérent."""
    repo = tmp_path / "lonely_repo"
    repo.mkdir()
    _git_ok(repo, "init", "-b", "main")
    _write_state_files(repo, init_state())

    result = git_sync(str(repo), "Run isolé", run_id="2026-07-22T14", max_retries=1)
    assert result == "FAILED"


def test_git_sync_aborted_duplicate_on_concurrent_same_run_id(tmp_path):
    """Deux clones indépendants (deux 'runs' concurrents) partent du même commit distant.
    Le premier pousse un run_id X avec succès. Le second, qui a calculé le MÊME run_id X sans
    connaître le push du premier, doit détecter le doublon via le last_run_id distant et
    retourner ABORTED_DUPLICATE plutôt que d'écraser/committer une deuxième fois."""
    origin = tmp_path / "origin.git"
    _git_ok(tmp_path, "init", "--bare", "-b", "main", str(origin))

    base_clone = tmp_path / "base"
    _git_ok(tmp_path, "clone", str(origin), str(base_clone))
    base_state = init_state()
    base_state["last_run_id"] = "2026-07-22T13"
    _write_state_files(base_clone, base_state)
    _git_ok(base_clone, "add", "state")
    _git_ok(base_clone, "commit", "-m", "Run 2026-07-22T13")
    _git_ok(base_clone, "push", "origin", "main")

    clone_a = tmp_path / "clone_a"
    clone_b = tmp_path / "clone_b"
    _git_ok(tmp_path, "clone", str(origin), str(clone_a))
    _git_ok(tmp_path, "clone", str(origin), str(clone_b))

    run_id = "2026-07-22T14"

    # Run A calcule et pousse le run_id "2026-07-22T14" en premier.
    state_a = init_state()
    state_a["last_run_id"] = run_id
    state_a["cash_usd"] = 90000.0
    _write_state_files(clone_a, state_a)
    result_a = git_sync(str(clone_a), f"Run {run_id} (A)", run_id=run_id)
    assert result_a == "SUCCESS"

    # Run B, parti du même commit de base, calcule INDÉPENDAMMENT le même run_id (même heure)
    # et modifie lui aussi state.json avant de tenter de pousser -> conflit garanti sur
    # state/state.json lors du rebase.
    state_b = init_state()
    state_b["last_run_id"] = run_id
    state_b["cash_usd"] = 91000.0
    _write_state_files(clone_b, state_b)
    result_b = git_sync(str(clone_b), f"Run {run_id} (B)", run_id=run_id)

    assert result_b == "ABORTED_DUPLICATE"

    # L'état distant reste celui poussé par A, jamais écrasé par B.
    remote_state = json.loads(_git_ok(origin, "show", "main:state/state.json"))
    assert remote_state["cash_usd"] == 90000.0


def test_git_sync_retries_and_succeeds_on_non_conflicting_concurrent_push(tmp_path):
    """Si un autre run a poussé un run_id DIFFÉRENT (plus ancien que le run_id courant), le
    rebase doit réussir sans conflit et le push retenté doit aboutir en SUCCESS (pas de faux
    positif de doublon)."""
    origin = tmp_path / "origin.git"
    _git_ok(tmp_path, "init", "--bare", "-b", "main", str(origin))

    base_clone = tmp_path / "base"
    _git_ok(tmp_path, "clone", str(origin), str(base_clone))
    _write_state_files(base_clone, init_state())
    _git_ok(base_clone, "add", "state")
    _git_ok(base_clone, "commit", "-m", "Etat initial")
    _git_ok(base_clone, "push", "origin", "main")

    clone_a = tmp_path / "clone_a"
    clone_b = tmp_path / "clone_b"
    _git_ok(tmp_path, "clone", str(origin), str(clone_a))
    _git_ok(tmp_path, "clone", str(origin), str(clone_b))

    # A pousse un fichier totalement indépendant (pas dans state/) pour avancer origin/main
    # SANS toucher à state/state.json -> pas de conflit possible pour B ensuite.
    (clone_a / "README_A.md").write_text("run A\n", encoding="utf-8")
    _git_ok(clone_a, "add", "README_A.md")
    _git_ok(clone_a, "commit", "-m", "Ajout non lié à state/")
    _git_ok(clone_a, "push", "origin", "main")

    state_b = init_state()
    state_b["last_run_id"] = "2026-07-22T14"
    _write_state_files(clone_b, state_b)
    result_b = git_sync(str(clone_b), "Run 2026-07-22T14", run_id="2026-07-22T14")

    assert result_b == "SUCCESS"
    remote_state = json.loads(_git_ok(origin, "show", "main:state/state.json"))
    assert remote_state["last_run_id"] == "2026-07-22T14"


def test_pull_rebase_success_when_up_to_date(origin_and_clone):
    origin, clone = origin_and_clone
    assert pull_rebase(str(clone)) == "SUCCESS"


def test_pull_rebase_fetches_new_remote_commits(tmp_path):
    origin = tmp_path / "origin.git"
    _git_ok(tmp_path, "init", "--bare", "-b", "main", str(origin))

    writer = tmp_path / "writer"
    _git_ok(tmp_path, "clone", str(origin), str(writer))
    _write_state_files(writer, init_state())
    _git_ok(writer, "add", "state")
    _git_ok(writer, "commit", "-m", "Etat initial")
    _git_ok(writer, "push", "origin", "main")

    reader = tmp_path / "reader"
    _git_ok(tmp_path, "clone", str(origin), str(reader))

    # Le writer avance origin/main après le clone du reader.
    other_state = init_state()
    other_state["last_run_id"] = "2026-07-22T14"
    _write_state_files(writer, other_state)
    _git_ok(writer, "add", "state")
    _git_ok(writer, "commit", "-m", "Run 2026-07-22T14")
    _git_ok(writer, "push", "origin", "main")

    assert pull_rebase(str(reader)) == "SUCCESS"
    reloaded = json.loads((reader / "state" / "state.json").read_text(encoding="utf-8"))
    assert reloaded["last_run_id"] == "2026-07-22T14"


def test_pull_rebase_fails_without_remote(tmp_path):
    repo = tmp_path / "no_remote"
    repo.mkdir()
    _git_ok(repo, "init", "-b", "main")
    _write_state_files(repo, init_state())
    _git_ok(repo, "add", "state")
    _git_ok(repo, "commit", "-m", "Etat initial")

    assert pull_rebase(str(repo)) == "FAILED"
    # Le working tree doit rester propre (pas de rebase laissé en suspens).
    status = _git_ok(repo, "status", "--porcelain")
    assert status.strip() == ""


# --- has_uncommitted_state_changes() : défense en profondeur, finding MAJEUR n°3 -----------


def test_has_uncommitted_state_changes_false_on_clean_tree(tmp_path):
    repo = tmp_path / "clean"
    repo.mkdir()
    _git_ok(repo, "init", "-b", "main")
    _write_state_files(repo, init_state())
    _git_ok(repo, "add", "state")
    _git_ok(repo, "commit", "-m", "Etat initial")

    assert has_uncommitted_state_changes(str(repo)) is False


def test_has_uncommitted_state_changes_true_after_save_state_without_commit(tmp_path):
    """Reproduit le scénario du finding MAJEUR n°3 : `save_state()` a réécrit `state.json`
    localement (le cycle a réussi), mais un crash survient AVANT `git_sync()` — le working
    tree porte donc des changements non commités sur `state/state.json`."""
    repo = tmp_path / "crashed"
    repo.mkdir()
    _git_ok(repo, "init", "-b", "main")
    _write_state_files(repo, init_state())
    _git_ok(repo, "add", "state")
    _git_ok(repo, "commit", "-m", "Etat initial")

    state = init_state()
    state["last_run_id"] = "2026-07-22T14"
    state["cash_usd"] = 95000.0
    _write_state_files(repo, state)  # save_state() a eu lieu, git commit/push jamais tenté

    assert has_uncommitted_state_changes(str(repo)) is True


def test_has_uncommitted_state_changes_ignores_files_outside_state_dir(tmp_path):
    repo = tmp_path / "unrelated_change"
    repo.mkdir()
    _git_ok(repo, "init", "-b", "main")
    _write_state_files(repo, init_state())
    _git_ok(repo, "add", "state")
    _git_ok(repo, "commit", "-m", "Etat initial")

    (repo / "README.md").write_text("changement hors state/\n", encoding="utf-8")

    assert has_uncommitted_state_changes(str(repo)) is False
