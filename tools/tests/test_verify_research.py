"""tools/tests/test_verify_research.py — F6 (CRITIQUE, audit adversarial) :
`tools/verify_research.py` (K_total imposé par docs/PROMOTION-RULES.md §1.3)."""

from __future__ import annotations

import json
import os

import pytest

import tools.verify_research as verify_research


def _registry(*entries: dict) -> dict:
    return {"schema_version": 1, "strategies": list(entries)}


def _entry(id_: str, date_test: str, k_total=None) -> dict:
    e = {"id": id_, "date_test": date_test}
    if k_total is not None:
        e["k_total"] = k_total
    return e


# ------------------------------------------------------------------------------------------
# count_strictly_earlier / verify_registry — logique pure
# ------------------------------------------------------------------------------------------


def test_count_strictly_earlier_excludes_same_date_and_later():
    entries = [
        _entry("a", "2026-07-01"),
        _entry("b", "2026-07-10"),
        _entry("c", "2026-07-10"),
        _entry("d", "2026-07-20"),
    ]
    assert verify_research.count_strictly_earlier(entries, "2026-07-10") == 1
    assert verify_research.count_strictly_earlier(entries, "2026-07-01") == 0
    assert verify_research.count_strictly_earlier(entries, "2026-08-01") == 4


def test_verify_registry_ok_on_real_repo_registry_which_has_no_k_total_declared():
    """Le vrai `docs/RESEARCH-REGISTRY.json` n'a AUCUNE entrée avec `k_total` déclaré
    aujourd'hui (antécédent, cf. `docs/PROMOTION-RULES.md` §5) -- rien à valider, donc conforme
    par construction (pas une violation de ne rien déclarer)."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    registry = verify_research.load_registry(os.path.join(repo_root, "docs/RESEARCH-REGISTRY.json"))
    assert verify_research.verify_registry(registry) == []


def test_verify_registry_accepts_k_total_covering_the_full_prior_registry():
    entries = [
        _entry("a", "2026-07-01"),
        _entry("b", "2026-07-10"),
        _entry("c", "2026-07-20", k_total=2),  # exactement les 2 lignes antérieures -- conforme
    ]
    assert verify_research.verify_registry(_registry(*entries)) == []


def test_verify_registry_accepts_k_total_strictly_above_the_minimum():
    entries = [
        _entry("a", "2026-07-01"),
        _entry("b", "2026-07-10"),
        _entry("c", "2026-07-20", k_total=50),  # bien au-dessus du minimum (2) -- conforme aussi
    ]
    assert verify_research.verify_registry(_registry(*entries)) == []


def test_verify_registry_rejects_undeclared_k_total_below_registry_size():
    """Cas central F6 : une candidate déclare un `k_total` INFÉRIEUR au nombre de lignes déjà
    présentes dans le registre à sa date de test -- sous-déclaration, doit être détectée."""
    entries = [
        _entry("a", "2026-07-01"),
        _entry("b", "2026-07-10"),
        _entry("c", "2026-07-20", k_total=1),  # 1 < 2 lignes antérieures -- violation
    ]
    issues = verify_research.verify_registry(_registry(*entries))
    assert len(issues) == 1
    assert "'c'" in issues[0]
    assert "k_total=1" in issues[0]


def test_verify_registry_rejects_missing_date_test_when_k_total_declared():
    entries = [_entry("a", "", k_total=5)]
    entries[0].pop("date_test")
    issues = verify_research.verify_registry(_registry(*entries))
    assert len(issues) == 1
    assert "date_test manquant" in issues[0]


def test_compute_k_total_adds_grid_size_to_registry_length():
    registry = _registry(_entry("a", "2026-07-01"), _entry("b", "2026-07-10"))
    assert verify_research.compute_k_total(registry, grid_size=216) == 2 + 216


def test_compute_k_total_rejects_non_positive_grid_size():
    registry = _registry()
    with pytest.raises(ValueError):
        verify_research.compute_k_total(registry, grid_size=0)
    with pytest.raises(ValueError):
        verify_research.compute_k_total(registry, grid_size=-3)


# ------------------------------------------------------------------------------------------
# main() — modes CLI --check / --compute
# ------------------------------------------------------------------------------------------


def test_main_check_mode_exits_0_on_real_registry():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    rc = verify_research.main(["--check", "--registry", os.path.join(repo_root, "docs/RESEARCH-REGISTRY.json")])
    assert rc == 0


def test_main_check_mode_exits_1_on_violation(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(_registry(
            _entry("a", "2026-07-01"),
            _entry("b", "2026-07-10"),
            _entry("c", "2026-07-20", k_total=1),
        )),
        encoding="utf-8",
    )
    rc = verify_research.main(["--check", "--registry", str(registry_path)])
    assert rc == 1


def test_main_check_mode_exits_1_on_missing_registry(tmp_path):
    rc = verify_research.main(["--check", "--registry", str(tmp_path / "does_not_exist.json")])
    assert rc == 1


def test_main_compute_mode_prints_k_total(tmp_path, capsys):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(_registry(_entry("a", "2026-07-01"), _entry("b", "2026-07-10"))),
        encoding="utf-8",
    )
    rc = verify_research.main(["--compute", "--grid-size", "216", "--registry", str(registry_path)])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "218"


def test_main_compute_mode_requires_grid_size(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(_registry()), encoding="utf-8")
    with pytest.raises(SystemExit):
        verify_research.main(["--compute", "--registry", str(registry_path)])
