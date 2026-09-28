"""backtest/tests/test_sizing_fidelity_lint.py — garde MÉCANIQUE de la convention
`docs/SIZING-FIDELITY-SPEC.md` (backlog #21, session #9, 2026-09-28).

Le défaut `apply_vol_targeting=True` de `backtest/engine.py:simulate_segment` n'est PAS le
chemin de production réel d'une candidate (`bot/runner.py:_risk_manager_for_wallet` neutralise
le vol-targeting portefeuille pour tous les wallets — finding F1 CRITIQUE de l'audit de
`pairs_ethbtc_ratio_rotation`, session #8). Un futur script de backtest qui omettrait le
paramètre obtiendrait SILENCIEUSEMENT l'overlay protecteur et produirait des chiffres Porte 1
sous une hypothèse de sizing infidèle, sans aucune erreur.

Ce lint statique (AST, aucun changement de comportement runtime, aucune archive re-exécutée)
exige que TOUT appel à `simulate_segment(` dans un script `backtest/run_*.py` passe
`apply_vol_targeting=` EXPLICITEMENT — la décision doit être écrite dans le script (et
justifiée dans sa SPEC), jamais héritée du défaut.

Exemption : les scripts ANTÉRIEURS à la convention, archivés tels quels pour la
reproductibilité de leurs results.json (registre append-only). Liste FERMÉE — ne JAMAIS y
ajouter un nouveau script (ce serait contourner la convention) ; elle ne peut que rétrécir si
un script archivé est un jour mis à niveau.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKTEST_DIR = Path(__file__).resolve().parent.parent

# Scripts d'AVANT la convention 2026-09-28, conservés bit-à-bit pour la reproductibilité de
# leurs archives (`backtest/results/*/results.json`). Liste fermée, cf. docstring.
PRE_CONVENTION_SCRIPTS = {
    "run_xsmom_invvol.py",  # session #1 (2026-07-27) : seul script à s'appuyer sur le défaut.
}


def _simulate_segment_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name == "simulate_segment":
            yield node


def find_violations(directory: Path, exempt: frozenset = frozenset(PRE_CONVENTION_SCRIPTS)):
    """Retourne la liste `script.py:ligne` des appels à simulate_segment() sans
    `apply_vol_targeting=` explicite, sur tous les `run_*.py` du répertoire donné."""
    violations = []
    for script in sorted(directory.glob("run_*.py")):
        if script.name in exempt:
            continue
        source = script.read_text(encoding="utf-8")
        for call in _simulate_segment_calls(ast.parse(source)):
            kwarg_names = {kw.arg for kw in call.keywords}  # kw.arg est None pour **kwargs
            if "apply_vol_targeting" in kwarg_names:
                continue
            if None in kwarg_names and "apply_vol_targeting" in source:
                # Un **kwargs peut transporter le paramètre : toléré uniquement si le nom
                # apparaît textuellement dans le script (décision écrite quelque part).
                continue
            violations.append(f"{script.name}:{call.lineno}")
    return violations


def test_every_run_script_sets_apply_vol_targeting_explicitly():
    scripts = sorted(BACKTEST_DIR.glob("run_*.py"))
    assert scripts, "aucun script run_*.py trouvé — le lint ne lint rien (chemin cassé ?)"
    violations = find_violations(BACKTEST_DIR)
    assert not violations, (
        "appel(s) à simulate_segment() sans `apply_vol_targeting=` explicite : "
        f"{violations} — interdit par docs/SIZING-FIDELITY-SPEC.md §3 (le défaut True du "
        "moteur n'est pas le chemin de production réel d'une candidate)"
    )


def test_pre_convention_exemption_list_is_closed():
    """La liste d'exemption ne référence que des scripts qui existent réellement (une entrée
    fantôme signalerait un renommage ou une tentative de contournement par renommage)."""
    for name in PRE_CONVENTION_SCRIPTS:
        assert (BACKTEST_DIR / name).exists(), (
            f"{name} listé en exemption pré-convention mais absent de backtest/ — "
            "mettre la liste à jour (elle ne peut que rétrécir)"
        )


def test_lint_actually_fires_on_an_omitted_parameter(tmp_path):
    """Garde non tautologique : la MÊME fonction de lint que le test principal, exécutée sur
    des scripts synthétiques, détecte réellement l'omission (rouge) et accepte l'appel
    explicite (vert) — y compris via un positionnel, un alias de module, et un **kwargs."""
    (tmp_path / "run_bad.py").write_text(
        "import backtest.engine as eng\nseg = eng.simulate_segment(cal, w, o, c, 0, 1, 5.0)\n",
        encoding="utf-8",
    )
    assert find_violations(tmp_path, exempt=frozenset()) == ["run_bad.py:2"]

    (tmp_path / "run_bad.py").unlink()
    (tmp_path / "run_good.py").write_text(
        "from backtest.engine import simulate_segment\n"
        "seg = simulate_segment(cal, w, o, c, 0, 1, 5.0, apply_vol_targeting=False)\n",
        encoding="utf-8",
    )
    assert find_violations(tmp_path, exempt=frozenset()) == []

    # **kwargs sans mention textuelle du paramètre = violation (le défaut s'appliquerait).
    (tmp_path / "run_good.py").unlink()
    (tmp_path / "run_kwargs.py").write_text(
        "from backtest.engine import simulate_segment\nseg = simulate_segment(cal, **opts)\n",
        encoding="utf-8",
    )
    assert find_violations(tmp_path, exempt=frozenset()) == ["run_kwargs.py:2"]

    # Un script exempté n'est pas linté (rétro-compat des archives).
    assert find_violations(tmp_path, exempt=frozenset({"run_kwargs.py"})) == []
