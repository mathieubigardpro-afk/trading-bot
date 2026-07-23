"""bot/reporting/ — outillage de suivi/observabilité, PAS le cœur transactionnel du bot.

Ce paquet est délibérément à part de `bot/runner.py` (le cycle horaire de production, qui
committe l'état toutes les heures) : rien ici n'est appelé par le cycle horaire actuel, pour ne
jamais faire dépendre le chemin de production (qui doit rester simple, testé, stable) d'un
outillage encore en construction (moniteur de dérive, sessions de recherche — cf. `tracking.py`
et docs/ARCHITECTURE.md § Labo, "Décision assumée : pas de câblage dans le cycle horaire").
"""

from __future__ import annotations

from .tracking import compute_live_metrics, update_strategy_state_live_metrics

__all__ = ["compute_live_metrics", "update_strategy_state_live_metrics"]
