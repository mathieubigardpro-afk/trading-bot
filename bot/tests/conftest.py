"""Assure que la racine du dépôt est sur sys.path, pour que `import bot...` fonctionne quel
que soit le répertoire depuis lequel pytest est invoqué (aucun package/pyproject requis)."""

import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture(autouse=True)
def _isolate_disk_data_cache(tmp_path, monkeypatch):
    """Isole TOUS les tests de `bot/feeds/daily.py:_cache_dir()` (cache disque `data-cache/`,
    cf. `.github/workflows/daily-data-cache.yml`) : par défaut, pointe vers un répertoire vide
    qui n'existe pas encore, pour qu'aucun test (y compris ceux qui exercent le cycle complet
    via `bot.runner`) ne dépende accidentellement d'un `data-cache/` réel laissé sur disque par
    `tools/build_daily_cache.py` exécuté localement. Les tests qui veulent exercer le chemin
    cache-disque positionnent `BOT_DATA_CACHE_DIR` eux-mêmes (écrase cette valeur par défaut)."""
    monkeypatch.setenv("BOT_DATA_CACHE_DIR", str(tmp_path / "no-such-data-cache"))
