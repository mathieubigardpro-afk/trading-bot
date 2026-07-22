"""bot.risk — gestionnaire de risque du bot (calibrage AGRESSIF, breakers non négociables).

Voir `docs/rapport-recherche.md` §3 (framework de risque) et `docs/ARCHITECTURE.md` (schéma
`state.json`, contrat d'interface `bot.risk`) pour le contexte complet. Le détail du calibrage
et des choix documentés vit dans `config_fallback.py`, `vol_targeting.py`,
`circuit_breakers.py` et `manager.py` (docstrings de module).

Interface contractuelle exposée ici :

    from bot.risk import apply
    cibles_finales, reasons = apply(cibles_brutes, state, prices, history)

équivalente à :

    from bot.risk import RiskManager
    cibles_finales, reasons = RiskManager().apply(cibles_brutes, state, prices, history)

`RiskManager(...)` accepte tous les seuils en paramètres du constructeur pour les tests et un
calibrage explicite ; `apply()` module-level utilise toujours les défauts AGRESSIFS du projet.
"""

from __future__ import annotations

from . import circuit_breakers, vol_targeting
from .circuit_breakers import DEFAULT_CB_CONFIG, default_breaker_state, evaluate_breakers
from .config_fallback import cfg as DEFAULT_RISK_CONFIG
from .manager import RiskManager, apply
from .vol_targeting import PERIODS_PER_YEAR, compute_vol_scalar, portfolio_vol_annualized

__all__ = [
    "RiskManager",
    "apply",
    "DEFAULT_CB_CONFIG",
    "DEFAULT_RISK_CONFIG",
    "PERIODS_PER_YEAR",
    "default_breaker_state",
    "evaluate_breakers",
    "compute_vol_scalar",
    "portfolio_vol_annualized",
    "circuit_breakers",
    "vol_targeting",
]
