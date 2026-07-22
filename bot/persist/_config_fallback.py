"""Repli local des constantes de `bot/config.py` nécessaires à `bot.persist`.

`bot/config.py` (hors périmètre de ce module, construit potentiellement en parallèle par un
autre agent) est la SOURCE DE VÉRITÉ unique documentée dans `docs/ARCHITECTURE.md` §2. Ce
fichier ne fait que permettre à `bot.persist` de fonctionner et d'être testé de façon autonome
si `bot/config.py` n'est pas encore présent dans l'arbre — dès que `bot/config.py` existe, ses
valeurs sont utilisées en priorité absolue et ce module ne sert plus que de garde-fou silencieux.
Même pattern que `bot/feeds/_config_fallback.py` et `bot/risk/config_fallback.py`.

Aucune logique métier ici : uniquement des constantes, recopiées à l'identique de
`docs/ARCHITECTURE.md` §2 pour rester cohérentes avec la source de vérité.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

_DEFAULTS = {
    "INITIAL_CASH_USD": 100_000.0,
    "STATE_DIR": "state",
    "STATE_JSON": "state/state.json",
    "TRADES_JSONL": "state/trades.jsonl",
    "EQUITY_JSONL": "state/equity.jsonl",
    "DECISIONS_JSONL": "state/decisions.jsonl",
}


def _load() -> SimpleNamespace:
    try:
        real_config = importlib.import_module("bot.config")
    except ImportError:
        real_config = None

    values = dict(_DEFAULTS)
    if real_config is not None:
        for key in _DEFAULTS:
            if hasattr(real_config, key):
                values[key] = getattr(real_config, key)
    return SimpleNamespace(**values)


# Résolu une fois à l'import. Les tests qui veulent forcer le repli peuvent recharger ce module
# (`importlib.reload`) après avoir masqué `bot.config`.
cfg = _load()
