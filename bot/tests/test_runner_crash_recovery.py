"""Tests d'intégration de `bot/runner.py` : scénarios de reprise après crash, correspondant
aux findings MAJEUR n°2 et n°3 de l'audit adversarial.

Ces tests exécutent `bot.runner.main()` pour de vrai (pas de mock du runner lui-même), contre
un dépôt git jetable (bare `origin` + clone), avec `bot.feeds.get_prices`/`is_us_market_open`
substitués par des doublures déterministes (aucun appel réseau réel : le proxy du bac à sable
bloque de toute façon l'accès public Binance/Yahoo, et ces tests doivent rester rapides et
déterministes indépendamment de la disponibilité réseau).
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import bot.runner as runner
from bot.persist.journal import append_journal
from bot.persist.state import init_state, save_state

FIXED_NOW = datetime(2026, 7, 22, 14, 3, 0, tzinfo=timezone.utc)


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
    """Bare `origin` + clone avec l'état initial (100 000$, aucune position) déjà committé et
    poussé — reproduit fidèlement le point de départ d'un run réel."""
    origin = tmp_path / "origin.git"
    _git_ok(tmp_path, "init", "--bare", "-b", "main", str(origin))

    clone = tmp_path / "clone"
    _git_ok(tmp_path, "clone", str(origin), str(clone))
    _write_state_files(clone, init_state())
    _git_ok(clone, "add", "state")
    _git_ok(clone, "commit", "-m", "Etat initial")
    _git_ok(clone, "push", "origin", "main")

    return origin, clone


@pytest.fixture(autouse=True)
def _no_network_and_repo_override(monkeypatch, origin_and_clone):
    """Neutralise tout appel réseau réel (get_prices -> None partout, aucun trade possible,
    ce qui suffit à exercer les garde-fous de reprise ciblés par ces tests) et fait pointer
    `bot.runner.repo_dir()` vers le clone jetable plutôt que le vrai dépôt du projet."""
    _origin, clone = origin_and_clone

    monkeypatch.setattr(runner, "repo_dir", lambda: str(clone))
    monkeypatch.setattr(runner, "get_prices", lambda symbols: {sym: None for sym in symbols})
    monkeypatch.setattr(runner, "is_us_market_open", lambda now: False)
    monkeypatch.chdir(clone)


def test_runner_aborts_on_orphaned_run_records_instead_of_duplicating_fills(origin_and_clone):
    """Non-régression, finding MAJEUR n°2 : si `trades.jsonl` porte déjà des enregistrements
    pour le `run_id` qui va être traité (signe d'un cycle précédent interrompu entre deux
    écritures de journal, AVANT que `state.json` n'ait jamais été sauvegardé), le runner doit
    refuser de rejouer le cycle plutôt que de produire un fill en double — code de sortie non
    nul, aucune écriture supplémentaire, aucun commit/push."""
    origin, clone = origin_and_clone
    run_id = runner.compute_run_id(FIXED_NOW)

    # Simule le fill fantôme laissé par un `kill -9` en cours de journalisation d'un cycle
    # PRÉCÉDENT pour cette même heure : la ligne existe sur disque, mais state.json (jamais
    # sauvegardé avant le crash) ignore toujours tout de ce run_id.
    orphan_fill = {
        "run_id": run_id, "ts": FIXED_NOW.isoformat(), "symbol": "BTC", "strategy": "ensemble",
        "side": "BUY", "qty": 0.1, "notional_usd": 1990.0, "price_fill": 19900.0,
        "price_mid_ideal": 19895.0, "fees_usd": 10.0, "slippage_usd": 0.5,
        "quote_source": "binance", "quote_ts": FIXED_NOW.isoformat(), "cash_after_usd": 98000.0,
    }
    append_journal(str(clone / "state" / "trades.jsonl"), orphan_fill)

    state_before = json.loads((clone / "state" / "state.json").read_text(encoding="utf-8"))
    assert state_before["last_run_id"] is None  # confirmé : state.json ignore ce run_id

    exit_code = runner.main(now=FIXED_NOW)

    assert exit_code == 1  # échec explicite, alerting externe attendu (pas un code 0 masqué)

    # state.json n'a SUBI AUCUNE modification (aucune écriture, aucun cycle exécuté).
    state_after = json.loads((clone / "state" / "state.json").read_text(encoding="utf-8"))
    assert state_after == state_before

    # trades.jsonl contient TOUJOURS exactement une ligne pour ce run_id : pas de doublon.
    lines = (clone / "state" / "trades.jsonl").read_text(encoding="utf-8").splitlines()
    matching = [json.loads(line) for line in lines if json.loads(line).get("run_id") == run_id]
    assert len(matching) == 1

    # Rien n'a été poussé sur le remote : aucun nouveau commit.
    remote_state = json.loads(_git_ok(origin, "show", "main:state/state.json"))
    assert remote_state["last_run_id"] is None


def test_runner_resumes_git_sync_instead_of_silently_aborting_as_duplicate(origin_and_clone):
    """Non-régression, finding MAJEUR n°3 : si `state.json` local a déjà été mis à jour avec
    `last_run_id = run_id` (le cycle a réellement eu lieu) mais qu'un crash survient AVANT
    `git_sync()`, une invocation suivante pour le MÊME `run_id` ne doit PAS se contenter de
    sortir en silence (code 0) sans jamais pousser — elle doit reprendre `git_sync()`."""
    origin, clone = origin_and_clone
    run_id = runner.compute_run_id(FIXED_NOW)

    # Simule un cycle qui a réellement complété toutes ses écritures locales (save_state
    # compris) mais dont le crash a empêché tout commit/push : le working tree est "sale".
    completed_state = init_state()
    completed_state["last_run_id"] = run_id
    completed_state["last_run_completed_at"] = FIXED_NOW.isoformat()
    completed_state["cash_usd"] = 95000.0
    save_state(completed_state, str(clone / "state" / "state.json"))

    status_before = _git_ok(clone, "status", "--porcelain", "--", "state/state.json")
    assert status_before.strip() != ""  # bien "sale" : jamais commité

    exit_code = runner.main(now=FIXED_NOW)

    assert exit_code == 0  # la reprise de git_sync a réussi (pas un doublon détecté à tort)

    # Le remote reflète maintenant bien ce cycle : la reprise a poussé, pas abandonné en silence.
    remote_state = json.loads(_git_ok(origin, "show", "main:state/state.json"))
    assert remote_state["last_run_id"] == run_id
    assert remote_state["cash_usd"] == 95000.0

    # Le working tree local est de nouveau propre (le commit a bien eu lieu).
    status_after = _git_ok(clone, "status", "--porcelain", "--", "state/state.json")
    assert status_after.strip() == ""


def test_runner_real_cycle_no_strategies_no_trades_commits_and_pushes(origin_and_clone):
    """Cycle réel de bout en bout (sans stratégie concrète déposée dans bot/strategies/, comme
    l'état actuel du dépôt) : 0 trade, mais le cycle doit tout de même journaliser et pousser
    un nouvel état, pour que le run SUIVANT (heure différente) ne soit jamais bloqué."""
    origin, clone = origin_and_clone
    run_id = runner.compute_run_id(FIXED_NOW)

    exit_code = runner.main(now=FIXED_NOW)
    assert exit_code == 0

    remote_state = json.loads(_git_ok(origin, "show", "main:state/state.json"))
    assert remote_state["last_run_id"] == run_id
    assert remote_state["cash_usd"] == pytest.approx(100000.0)
    assert remote_state["positions"] == {}

    remote_equity_lines = _git_ok(origin, "show", "main:state/equity.jsonl").splitlines()
    assert len(remote_equity_lines) == 1
    assert json.loads(remote_equity_lines[0])["equity_usd"] == pytest.approx(100000.0)


def test_runner_idempotent_second_call_same_hour_is_clean_noop(origin_and_clone):
    """Deuxième invocation pour le MÊME run_id APRÈS un cycle correctement poussé : sortie
    silencieuse propre (code 0), aucun nouveau commit (comportement d'idempotence attendu,
    cf. ARCHITECTURE.md §4.2 — c'est le scénario explicitement demandé par la mission)."""
    origin, clone = origin_and_clone

    assert runner.main(now=FIXED_NOW) == 0
    log_after_first = _git_ok(origin, "log", "--format=%H")

    assert runner.main(now=FIXED_NOW) == 0
    log_after_second = _git_ok(origin, "log", "--format=%H")

    assert log_after_first == log_after_second  # aucun commit supplémentaire
