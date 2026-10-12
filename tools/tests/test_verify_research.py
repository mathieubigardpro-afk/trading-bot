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
    # Depuis la garde band_fidelity (session #10), une entrée sans date_test cumule DEUX
    # violations : k_total invérifiable (§1.3) ET antériorité à la convention non prouvable
    # (BAND-FIDELITY-SPEC §3.6) — durcissement volontaire, les deux doivent se déclencher.
    assert len(issues) == 2
    assert any("date_test manquant" in i for i in issues)
    assert any("antériorité" in i for i in issues)


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


# ------------------------------------------------------------------------------------------
# Garde band_fidelity (docs/BAND-FIDELITY-SPEC.md §3.6, session #10) — les validations RÉELLES
# doivent se déclencher (rouge) et s'éteindre (vert), jamais des vérités Python génériques.
# ------------------------------------------------------------------------------------------


def _entry_bf(id_: str, date_test: str, **extra) -> dict:
    e = {"id": id_, "date_test": date_test}
    e.update(extra)
    return e


def test_band_fidelity_missing_on_new_entry_is_a_violation():
    reg = _registry(_entry_bf("nouvelle", verify_research.BAND_FIDELITY_REQUIRED_FROM))
    issues = verify_research.verify_registry(reg)
    assert len(issues) == 1 and "band_fidelity" in issues[0]


@pytest.mark.parametrize("bad", [None, {}, "", "   "])
def test_band_fidelity_empty_forms_are_violations(bad):
    reg = _registry(_entry_bf("nouvelle", "2026-11-01", band_fidelity=bad))
    issues = verify_research.verify_registry(reg)
    assert len(issues) == 1 and "band_fidelity" in issues[0]


def test_band_fidelity_complete_dict_passes():
    bloc = {"a": 0.25, "sharpe_s1": 0.7, "sharpe_s2": 0.74, "band_fragile": False,
            "cb": {"maxdd_wallet_s2_pct": 6.2, "seuils_labo": [0.15, 0.25], "episodes": 0}}
    reg = _registry(_entry_bf("nouvelle", "2026-11-01", band_fidelity=bloc))
    assert verify_research.verify_registry(reg) == []


def test_band_fidelity_incomplete_dict_is_a_violation():
    # Audit adversarial 2026-10-12 (M4) : un dict non vide mais sans les clés minimales de la
    # convention (ici : cb manquant, sharpe_s2 nul) ne passe PAS.
    bloc = {"a": 0.25, "sharpe_s1": 0.7, "sharpe_s2": None, "band_fragile": False}
    reg = _registry(_entry_bf("nouvelle", "2026-11-01", band_fidelity=bloc))
    issues = verify_research.verify_registry(reg)
    assert len(issues) == 1 and "incomplet" in issues[0]
    assert "'sharpe_s2'" in issues[0] and "'cb'" in issues[0]


def test_band_fidelity_junk_dict_is_a_violation():
    reg = _registry(_entry_bf("nouvelle", "2026-11-01", band_fidelity={"foo": 1}))
    issues = verify_research.verify_registry(reg)
    assert len(issues) == 1 and "incomplet" in issues[0]


def test_band_fidelity_explicit_exemption_string_passes():
    reg = _registry(_entry_bf("retest_pur", "2026-11-01",
                              band_fidelity="exemption : retest de validation, aucune incubation visée"))
    assert verify_research.verify_registry(reg) == []


def test_band_fidelity_short_exemption_is_a_violation():
    # Audit adversarial 2026-10-12 (M4) : "n/a" et consorts ne passent pas.
    reg = _registry(_entry_bf("nouvelle", "2026-11-01", band_fidelity="n/a"))
    issues = verify_research.verify_registry(reg)
    assert len(issues) == 1 and "trop courte" in issues[0]


@pytest.mark.parametrize("bad_date", ["15/10/2026", "2026-9-1", "1 novembre 2026", "2026-10-12T00:00:00"])
def test_non_iso_date_test_is_a_violation(bad_date):
    # Audit adversarial 2026-10-12 (M4) : une date hors ISO contournerait les comparaisons
    # lexicographiques (elle passerait pour antérieure ou postérieure arbitrairement).
    reg = _registry(_entry_bf("louche", bad_date))
    issues = verify_research.verify_registry(reg)
    assert len(issues) == 1 and "hors format ISO" in issues[0]


def test_main_check_mode_exits_1_on_missing_band_fidelity(tmp_path, capsys):
    # La garde doit se déclencher via le POINT D'ENTRÉE réel (main --check), pas seulement
    # via verify_registry (audit adversarial 2026-10-12, m3).
    import json as _json
    p = tmp_path / "reg.json"
    p.write_text(_json.dumps({"schema_version": 1, "strategies": [
        {"id": "nouvelle", "date_test": "2026-12-01"}]}), encoding="utf-8")
    assert verify_research.main(["--registry", str(p), "--check"]) == 1


def test_band_fidelity_not_required_for_entries_before_cutoff():
    reg = _registry(_entry_bf("ancienne", "2026-09-21"))
    assert verify_research.verify_registry(reg) == []


def test_band_fidelity_missing_date_test_is_flagged_even_without_k_total():
    reg = _registry({"id": "sans_date"})
    issues = verify_research.verify_registry(reg)
    assert len(issues) == 1 and "antériorité" in issues[0]


def test_real_repo_registry_still_passes_check_with_band_fidelity_guard():
    path = os.path.join(verify_research._REPO_ROOT, verify_research.REGISTRY_RELPATH)
    reg = verify_research.load_registry(path)
    assert verify_research.verify_registry(reg) == []


def test_band_fidelity_bad_value_types_are_violations():
    # Contre-audit 2026-10-12 : présence des clés ne suffit pas — types minimaux exigés.
    bloc = {"a": "", "sharpe_s1": "0.7", "sharpe_s2": True, "band_fragile": "false", "cb": 0}
    reg = _registry(_entry_bf("nouvelle", "2026-11-01", band_fidelity=bloc))
    issues = verify_research.verify_registry(reg)
    assert len(issues) == 1 and "type invalide" in issues[0]
    for k in ("'a'", "'sharpe_s1'", "'sharpe_s2'", "'band_fragile'", "'cb'"):
        assert k in issues[0]


def test_band_fidelity_numeric_values_and_nonempty_cb_pass():
    bloc = {"a": 0.25, "sharpe_s1": 0.7, "sharpe_s2": 0.74, "band_fragile": True,
            "cb": {"maxdd_wallet_s2_pct": 26.3}}
    reg = _registry(_entry_bf("nouvelle", "2026-11-01", band_fidelity=bloc))
    assert verify_research.verify_registry(reg) == []
