"""bot/persist/cycle.py — idempotence GLOBALE du cycle multi-wallets (`state/cycle.json`).

Évolution multi-wallets (docs/ARCHITECTURE.md §9) : un cycle horaire traite désormais TROIS
wallets (`bot.config.WALLET_IDS`) sous un même `run_id`, en UN SEUL commit, tout-ou-rien. Le
schéma d'idempotence "un `run_id` ne peut produire des effets qu'une seule fois"
(ARCHITECTURE.md §4.2) porte donc sur ce petit fichier séparé plutôt que sur le `state.json`
d'un wallet individuel — chaque wallet garde EN PLUS sa propre chaîne d'intégrité
(`state_hash_prev`) totalement indépendante des deux autres (voir `bot.persist.state`).

`state/cycle.json` :
    {"schema_version": 1, "last_run_id": "2026-07-22T14",
     "last_run_completed_at": "2026-07-22T14:03:41+00:00",
     "wallet_ids": ["prudent", "equilibre", "agressif"]}
"""

from __future__ import annotations

import json
import os
from typing import Any

SCHEMA_VERSION = 1


class CycleStateValidationError(Exception):
    """`cycle.json` existe mais est corrompu/incomplet — jamais de repli silencieux."""


def init_cycle_state(wallet_ids: list[str]) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "last_run_id": None,
        "last_run_completed_at": None,
        "wallet_ids": list(wallet_ids),
    }


def validate_cycle_schema(state: Any) -> None:
    if not isinstance(state, dict):
        raise CycleStateValidationError(
            f"cycle.json doit contenir un objet JSON (dict), reçu {type(state).__name__}"
        )
    if not isinstance(state.get("schema_version"), int) or isinstance(state.get("schema_version"), bool):
        raise CycleStateValidationError("champ 'schema_version' manquant ou invalide")
    if state["schema_version"] != SCHEMA_VERSION:
        raise CycleStateValidationError(
            f"schema_version non supporté : {state['schema_version']!r} (attendu {SCHEMA_VERSION})"
        )
    if "last_run_id" not in state or not isinstance(state["last_run_id"], (str, type(None))):
        raise CycleStateValidationError("champ 'last_run_id' manquant ou invalide")
    if "last_run_completed_at" not in state or not isinstance(state["last_run_completed_at"], (str, type(None))):
        raise CycleStateValidationError("champ 'last_run_completed_at' manquant ou invalide")
    wallet_ids = state.get("wallet_ids")
    if not isinstance(wallet_ids, list) or not all(isinstance(w, str) and w for w in wallet_ids):
        raise CycleStateValidationError("champ 'wallet_ids' : doit être une liste de chaînes non vides")


def load_cycle_state(path: str, wallet_ids: list[str]) -> dict:
    """Fichier absent (tout premier cycle de l'histoire du dépôt) -> état initial (SEUL cas de
    valeur par défaut silencieuse). Fichier présent mais invalide -> lève
    `CycleStateValidationError`, jamais de repli silencieux (même contrat que
    `bot.persist.state.load_state`)."""
    if not os.path.exists(path):
        return init_cycle_state(wallet_ids)
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CycleStateValidationError(f"cycle.json corrompu : JSON invalide à {path} ({exc})") from exc
    validate_cycle_schema(state)
    return state


def save_cycle_state(state: dict, path: str) -> None:
    validate_cycle_schema(state)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    canonical = json.dumps(state, sort_keys=True, indent=2, ensure_ascii=False)
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(canonical)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def is_cycle_already_done(cycle_state: dict, run_id: str) -> bool:
    return cycle_state.get("last_run_id") == run_id
