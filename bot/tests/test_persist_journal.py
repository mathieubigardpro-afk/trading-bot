"""Tests de bot/persist/journal.py : append_journal (append-only, jamais de réécriture)."""

from __future__ import annotations

import json

import pytest

from bot.persist.journal import append_journal


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
