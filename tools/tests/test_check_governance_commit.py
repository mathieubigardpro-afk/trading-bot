"""tools/tests/test_check_governance_commit.py — F7 (MAJEUR, audit adversarial) :
`tools/check_governance_commit.py` fait respecter `docs/PROMOTION-RULES.md` §0 ("gouvernance et
jugement jamais dans le même commit"). Utilise un dépôt git TEMPORAIRE (jamais le dépôt réel :
on ne doit ni committer dans le dépôt réel, ni dépendre de son historique)."""

from __future__ import annotations

import os
import subprocess

import pytest

import tools.check_governance_commit as check_governance_commit


def _run_git(repo_dir: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )


@pytest.fixture
def temp_repo(tmp_path):
    repo_dir = str(tmp_path / "repo")
    os.makedirs(repo_dir)
    _run_git(repo_dir, "init", "-q", "-b", "main")
    _run_git(repo_dir, "config", "commit.gpgsign", "false")
    return repo_dir


def _write(repo_dir: str, relpath: str, content: str) -> None:
    full = os.path.join(repo_dir, relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)


def _commit(repo_dir: str, message: str, paths: list) -> str:
    _run_git(repo_dir, "add", *paths)
    _run_git(repo_dir, "commit", "-q", "-m", message)
    return _run_git(repo_dir, "rev-parse", "HEAD").stdout.strip()


# ------------------------------------------------------------------------------------------
# changed_files
# ------------------------------------------------------------------------------------------


def test_changed_files_lists_paths_touched_by_commit(temp_repo):
    _write(temp_repo, "docs/PROMOTION-RULES.md", "v1")
    _write(temp_repo, "README.md", "v1")
    sha = _commit(temp_repo, "init", ["docs/PROMOTION-RULES.md", "README.md"])

    files = check_governance_commit.changed_files(sha, repo_dir=temp_repo)
    assert set(files) == {"docs/PROMOTION-RULES.md", "README.md"}


# ------------------------------------------------------------------------------------------
# check_commit / main() — scénarios conformes
# ------------------------------------------------------------------------------------------


def test_commit_touching_only_promotion_rules_is_ok(temp_repo):
    _write(temp_repo, "docs/PROMOTION-RULES.md", "v1")
    sha = _commit(temp_repo, "gouvernance seule", ["docs/PROMOTION-RULES.md"])
    assert check_governance_commit.check_commit(sha, repo_dir=temp_repo) == []
    assert check_governance_commit.main([sha, "--repo-dir", temp_repo]) == 0


def test_commit_touching_only_config_is_ok(temp_repo):
    _write(temp_repo, "bot/config.py", "X = 1\n")
    sha = _commit(temp_repo, "jugement seul", ["bot/config.py"])
    assert check_governance_commit.check_commit(sha, repo_dir=temp_repo) == []
    assert check_governance_commit.main([sha, "--repo-dir", temp_repo]) == 0


def test_commit_touching_config_and_registry_but_not_promotion_rules_is_ok(temp_repo):
    _write(temp_repo, "bot/config.py", "X = 1\n")
    _write(temp_repo, "docs/RESEARCH-REGISTRY.json", "{}")
    sha = _commit(temp_repo, "promotion", ["bot/config.py", "docs/RESEARCH-REGISTRY.json"])
    assert check_governance_commit.check_commit(sha, repo_dir=temp_repo) == []


# ------------------------------------------------------------------------------------------
# check_commit / main() — scénarios EN VIOLATION
# ------------------------------------------------------------------------------------------


def test_commit_touching_promotion_rules_and_config_is_a_violation(temp_repo):
    _write(temp_repo, "docs/PROMOTION-RULES.md", "v1")
    _write(temp_repo, "bot/config.py", "X = 1\n")
    sha = _commit(temp_repo, "mélange interdit", ["docs/PROMOTION-RULES.md", "bot/config.py"])

    violations = check_governance_commit.check_commit(sha, repo_dir=temp_repo)
    assert len(violations) == 1
    assert "bot/config.py" in violations[0]

    rc = check_governance_commit.main([sha, "--repo-dir", temp_repo])
    assert rc == 1


def test_commit_touching_promotion_rules_and_registry_is_a_violation(temp_repo):
    _write(temp_repo, "docs/PROMOTION-RULES.md", "v1")
    _write(temp_repo, "docs/RESEARCH-REGISTRY.json", "{}")
    sha = _commit(
        temp_repo, "mélange interdit registre",
        ["docs/PROMOTION-RULES.md", "docs/RESEARCH-REGISTRY.json"],
    )

    violations = check_governance_commit.check_commit(sha, repo_dir=temp_repo)
    assert len(violations) == 1
    assert "docs/RESEARCH-REGISTRY.json" in violations[0]
    assert check_governance_commit.main([sha, "--repo-dir", temp_repo]) == 1


def test_commit_touching_all_three_is_two_violations(temp_repo):
    _write(temp_repo, "docs/PROMOTION-RULES.md", "v1")
    _write(temp_repo, "bot/config.py", "X = 1\n")
    _write(temp_repo, "docs/RESEARCH-REGISTRY.json", "{}")
    sha = _commit(
        temp_repo, "mélange total interdit",
        ["docs/PROMOTION-RULES.md", "bot/config.py", "docs/RESEARCH-REGISTRY.json"],
    )

    violations = check_governance_commit.check_commit(sha, repo_dir=temp_repo)
    assert len(violations) == 2


def test_main_defaults_sha_to_head(temp_repo):
    _write(temp_repo, "docs/PROMOTION-RULES.md", "v1")
    _write(temp_repo, "bot/config.py", "X = 1\n")
    _commit(temp_repo, "mélange interdit (HEAD)", ["docs/PROMOTION-RULES.md", "bot/config.py"])

    assert check_governance_commit.main(["--repo-dir", temp_repo]) == 1


def test_main_returns_1_on_unresolvable_sha(temp_repo):
    _write(temp_repo, "README.md", "v1")
    _commit(temp_repo, "init", ["README.md"])
    assert check_governance_commit.main(["deadbeefdeadbeef", "--repo-dir", temp_repo]) == 1
