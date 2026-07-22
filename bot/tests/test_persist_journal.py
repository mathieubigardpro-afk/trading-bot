"""Tests de bot/persist/journal.py : append_journal (append-only, jamais de réécriture)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from bot.persist.journal import append_journal, append_journal_many, records_for_run


def test_append_journal_creates_file_and_writes_one_line(tmp_path):
    path = tmp_path / "trades.jsonl"
    append_journal(str(path), {"run_id": "2026-07-22T14", "symbol": "BTC"})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"run_id": "2026-07-22T14", "symbol": "BTC"}


def test_append_journal_appends_without_touching_previous_lines(tmp_path):
    path = tmp_path / "equity.jsonl"
    append_journal(str(path), {"run_id": "2026-07-22T13", "equity_usd": 100000.0})
    append_journal(str(path), {"run_id": "2026-07-22T14", "equity_usd": 100482.10})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["run_id"] == "2026-07-22T13"
    assert json.loads(lines[1])["run_id"] == "2026-07-22T14"


def test_append_journal_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "decisions.jsonl"
    append_journal(str(path), {"symbol": "ETH"})
    assert path.exists()


def test_append_journal_rejects_non_dict_record(tmp_path):
    path = tmp_path / "trades.jsonl"
    with pytest.raises(TypeError):
        append_journal(str(path), ["not", "a", "dict"])  # type: ignore[arg-type]
    assert not path.exists()


def test_append_journal_many_lines_preserve_order(tmp_path):
    path = tmp_path / "decisions.jsonl"
    for i in range(50):
        append_journal(str(path), {"i": i})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 50
    for i, line in enumerate(lines):
        assert json.loads(line)["i"] == i


# --- append_journal_many() : écriture groupée en un seul write()+fsync() -------------------


def test_append_journal_many_writes_all_records_in_one_call(tmp_path):
    path = tmp_path / "trades.jsonl"
    records = [{"run_id": "2026-07-22T14", "symbol": sym} for sym in ("BTC", "ETH", "SOL")]

    append_journal_many(str(path), records)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["symbol"] for line in lines] == ["BTC", "ETH", "SOL"]


def test_append_journal_many_appends_after_existing_content(tmp_path):
    path = tmp_path / "trades.jsonl"
    append_journal(str(path), {"i": 0})
    append_journal_many(str(path), [{"i": 1}, {"i": 2}])

    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["i"] for line in lines] == [0, 1, 2]


def test_append_journal_many_empty_list_creates_file_without_writing(tmp_path):
    path = tmp_path / "trades.jsonl"
    append_journal_many(str(path), [])

    assert path.exists()
    assert path.read_text(encoding="utf-8") == ""


def test_append_journal_many_rejects_non_dict_record(tmp_path):
    path = tmp_path / "trades.jsonl"
    with pytest.raises(TypeError):
        append_journal_many(str(path), [{"ok": 1}, ["not", "a", "dict"]])  # type: ignore[list-item]
    # Aucune écriture partielle : la validation a lieu avant toute ouverture de fichier.
    assert not path.exists()


# --- Non-régression : finding MAJEUR n°2 de l'audit adversarial ----------------------------
#
# "Un crash (kill -9) entre deux écritures individuelles dans trades.jsonl [...] produit un
# enregistrement fantôme permanent" : reproduit ici en tuant un sous-processus au milieu d'une
# boucle de `append_journal()` individuels (ancien comportement), puis on démontre que
# `append_journal_many()` — désormais utilisé par bot/runner.py — ferme cette fenêtre : soit
# aucun des enregistrements du cycle n'atteint le disque, soit ils y sont tous.


def test_kill_mid_loop_of_individual_append_journal_leaves_a_phantom_record(tmp_path):
    """Reproduit exactement le mécanisme incriminé par l'audit : une boucle de deux
    `append_journal()` séparés, tuée (SIGKILL) entre les deux appels, laisse UNE SEULE ligne
    (la première) sur disque — c'est cette asymétrie qui permettait au rejeu du cycle suivant
    de fabriquer un doublon puisque rien ne signalait cet état intermédiaire."""
    path = tmp_path / "trades.jsonl"
    script = textwrap.dedent(
        f"""
        import os
        from bot.persist.journal import append_journal
        append_journal({str(path)!r}, {{"run_id": "R", "symbol": "BTC"}})
        os._exit(137)  # équivalent de kill -9 sur soi-même, juste après le 1er append
        append_journal({str(path)!r}, {{"run_id": "R", "symbol": "ETH"}})
        """
    )
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 137

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # la ligne ETH n'a jamais été écrite : fantôme évité, pas de doublon
    assert json.loads(lines[0])["symbol"] == "BTC"


def test_append_journal_many_never_leaves_a_subset_on_disk(tmp_path):
    """Le correctif : en remplaçant la boucle d'`append_journal()` individuels par UN SEUL
    `append_journal_many()`, un crash ne peut plus survenir "entre deux fills" du même cycle —
    soit le processus meurt AVANT l'appel (aucune ligne), soit APRÈS (toutes les lignes)."""
    path = tmp_path / "trades.jsonl"
    script = textwrap.dedent(
        f"""
        import os, sys
        from bot.persist.journal import append_journal_many
        append_journal_many({str(path)!r}, [
            {{"run_id": "R", "symbol": "BTC"}},
            {{"run_id": "R", "symbol": "ETH"}},
        ])
        os._exit(137)
        """
    )
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 137

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # jamais un sous-ensemble : les deux fills sont présents ensemble
    assert [json.loads(line)["symbol"] for line in lines] == ["BTC", "ETH"]


# --- records_for_run() : détection d'enregistrements orphelins d'un run_id ------------------


def test_records_for_run_filters_by_run_id(tmp_path):
    path = tmp_path / "trades.jsonl"
    append_journal_many(str(path), [
        {"run_id": "2026-07-22T13", "symbol": "BTC"},
        {"run_id": "2026-07-22T14", "symbol": "ETH"},
        {"run_id": "2026-07-22T14", "symbol": "SOL"},
    ])

    matches = records_for_run(str(path), "2026-07-22T14")
    assert [m["symbol"] for m in matches] == ["ETH", "SOL"]


def test_records_for_run_missing_file_returns_empty_list(tmp_path):
    path = tmp_path / "does_not_exist.jsonl"
    assert records_for_run(str(path), "2026-07-22T14") == []


def test_records_for_run_ignores_malformed_lines(tmp_path):
    path = tmp_path / "trades.jsonl"
    path.write_text(
        '{"run_id": "2026-07-22T14", "symbol": "BTC"}\n'
        "{ ceci n'est pas du json valide\n"
        '{"run_id": "2026-07-22T14", "symbol": "ETH"}\n',
        encoding="utf-8",
    )

    matches = records_for_run(str(path), "2026-07-22T14")
    assert [m["symbol"] for m in matches] == ["BTC", "ETH"]
