"""Assure que la racine du dépôt est sur sys.path, pour que `import bot...`/`import
tools...` fonctionne quel que soit le répertoire depuis lequel pytest est invoqué (même
convention que `bot/tests/conftest.py`)."""

import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
