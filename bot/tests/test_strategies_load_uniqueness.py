"""bot/tests/test_strategies_load_uniqueness.py — F1 (CRITIQUE, audit adversarial) : deux
stratégies concrètes ne peuvent jamais partager le même `StrategyBase.name`.

Avant correctif, `bot/strategies/__init__.py:load_strategies()` laissait silencieusement la
dernière classe rencontrée (ordre non garanti de `pkgutil.iter_modules`) écraser toute
précédente avec le même `name` -- vecteur de hijack démontré : un module labo (candidate
d'incubation) déclarant `name = "quasi_passif_crypto"` (ou toute autre stratégie de
production) aurait pu se substituer à elle dans `strategies_by_name` (`bot/runner.py`), sans
aucune erreur ni trace. Le correctif fait lever `ValueError` explicitement sur toute collision.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

import bot.strategies as strategies_pkg
from bot.strategies import load_strategies


def test_load_strategies_names_are_unique_on_real_package():
    """Sur le paquet RÉEL (3 stratégies de production) : `load_strategies()` réel, aucun mock
    -- vérifie qu'aucune collision de `name` n'existe aujourd'hui dans le dépôt."""
    loaded = load_strategies()
    names = [s.name for s in loaded]
    assert len(names) == len(set(names)), f"names dupliqués détectés : {names}"
    assert set(names) == {"quasi_passif_crypto", "xs_momentum_sp100", "dual_momentum_etf"}


def test_load_strategies_raises_on_name_collision():
    """Injecte TEMPORAIREMENT un module dont la classe concrète déclare le même `name` qu'une
    stratégie de production déjà présente (`dual_momentum_etf`) directement dans le répertoire
    RÉEL de `bot/strategies/` -- `pkgutil.iter_modules(package.__path__)` a besoin d'un chemin
    réellement scanné par le paquet, un répertoire externe (tmp_path) ne serait jamais visité.
    Le fichier injecté est supprimé dans un `finally` : aucune trace laissée dans le dépôt une
    fois le test terminé, qu'il réussisse ou échoue."""
    pkg_dir = os.path.dirname(strategies_pkg.__file__)
    evil_mod_name = "zzz_test_evil_hijack_candidate"
    evil_path = os.path.join(pkg_dir, f"{evil_mod_name}.py")
    evil_qualified_name = f"{strategies_pkg.__name__}.{evil_mod_name}"

    source = (
        "from bot.strategies import StrategyBase\n\n\n"
        "class EvilHijack(StrategyBase):\n"
        "    \"\"\"Module labo factice (test) qui usurpe le name d'une stratégie de production.\"\"\"\n\n"
        "    name = \"dual_momentum_etf\"\n\n"
        "    def target_weights(self, history, state, profile=None):\n"
        "        return {}\n"
    )

    assert not os.path.exists(evil_path), (
        "un fichier de test précédent n'a pas été nettoyé -- abandon par prudence, ne rien écraser"
    )

    try:
        with open(evil_path, "w", encoding="utf-8") as f:
            f.write(source)
        importlib.invalidate_caches()

        with pytest.raises(ValueError, match="collision de name"):
            load_strategies()
    finally:
        if os.path.exists(evil_path):
            os.remove(evil_path)
        sys.modules.pop(evil_qualified_name, None)
        importlib.invalidate_caches()

    # Nettoyage effectif : le paquet retrouve son état normal (3 stratégies, aucune collision).
    loaded = load_strategies()
    assert len(loaded) == len({s.name for s in loaded}) == 3
