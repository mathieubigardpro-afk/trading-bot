#!/usr/bin/env python3
"""tools/migrate_to_wallets.py — migration ponctuelle : portefeuille unique 100 000$ ->
trois wallets indépendants de 1 000€ (docs/ARCHITECTURE.md §9).

Deux effets, tous deux IDEMPOTENTS (sûr de relancer ce script plusieurs fois) :

  1. Archive `state/state.json`, `trades.jsonl`, `equity.jsonl`, `decisions.jsonl` (l'ancien
     portefeuille unique) tels quels dans `state/archive-100k/` — RIEN n'est détruit, aucune
     transformation de contenu, un simple déplacement (`git mv` si possible, sinon copie +
     suppression). Ne fait rien si `state/state.json` n'existe pas (déjà migré, ou dépôt qui
     n'a jamais eu de portefeuille unique).
  2. Crée `state/wallets/<id>/{state.json,trades.jsonl,equity.jsonl,decisions.jsonl}` pour
     chaque wallet de `bot.config.WALLETS`, NON INITIALISÉ (`fx.initial_rate=None`,
     `cash_usd=0.0` — jamais de taux EUR/USD inventé ici), et `state/cycle.json` initial. Ne
     touche jamais un wallet déjà présent (idempotence : un wallet déjà initialisé par un
     cycle réel ne doit jamais être réinitialisé par une relance accidentelle de ce script).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from bot import config
from bot.persist.cycle import init_cycle_state, save_cycle_state
from bot.persist.state import init_state, save_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tools.migrate_to_wallets")

LEGACY_FILES = ["state.json", "trades.jsonl", "equity.jsonl", "decisions.jsonl"]


@dataclass
class MigrationReport:
    archived: bool = False
    archived_files: List[str] = field(default_factory=list)
    wallets_created: List[str] = field(default_factory=list)
    wallets_already_present: List[str] = field(default_factory=list)
    cycle_json_created: bool = False


def _git_mv_or_copy(repo_dir: str, src: str, dst: str) -> None:
    """`git mv` si `repo_dir` est un dépôt git suivant déjà `src` ; sinon repli sur une copie
    + suppression classique (tests unitaires sur un répertoire jetable sans git)."""
    result = subprocess.run(
        ["git", "-C", repo_dir, "mv", src, dst],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0:
        return
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.copy2(src, dst)
    os.remove(src)


def migrate(repo_dir: str) -> MigrationReport:
    """Exécute la migration dans `repo_dir` (racine du dépôt, contenant `state/`).

    Fonction pure du point de vue de son résultat (idempotente) : peut être appelée
    plusieurs fois sans effet destructif après la première exécution réussie.
    """
    report = MigrationReport()
    state_dir = os.path.join(repo_dir, "state")
    legacy_state_json = os.path.join(state_dir, "state.json")
    archive_dir = os.path.join(state_dir, "archive-100k")

    # --- 1) archivage de l'ancien portefeuille unique ---
    if os.path.exists(legacy_state_json) and not os.path.exists(archive_dir):
        os.makedirs(archive_dir, exist_ok=True)
        for name in LEGACY_FILES:
            src = os.path.join(state_dir, name)
            if not os.path.exists(src):
                continue
            dst = os.path.join(archive_dir, name)
            _git_mv_or_copy(repo_dir, src, dst)
            report.archived_files.append(name)
        report.archived = True
        logger.info("Ancien portefeuille unique archivé dans %s : %s", archive_dir, report.archived_files)
    elif os.path.exists(archive_dir):
        logger.info("Archive déjà présente (%s) — migration §1 déjà effectuée, ignorée.", archive_dir)
    else:
        logger.info("Aucun state/state.json legacy trouvé — rien à archiver.")

    # --- 2) création des 3 wallets (non initialisés) + cycle.json ---
    for wallet_cfg in config.WALLETS:
        wallet_id = wallet_cfg["id"]
        wallet_dir = os.path.join(repo_dir, config.wallet_state_dir(wallet_id))
        wallet_state_path = os.path.join(repo_dir, config.wallet_state_json(wallet_id))
        if os.path.exists(wallet_state_path):
            report.wallets_already_present.append(wallet_id)
            continue
        os.makedirs(wallet_dir, exist_ok=True)
        save_state(init_state(wallet_id, wallet_cfg["capital_initial_eur"]), wallet_state_path)
        for name in ("trades.jsonl", "equity.jsonl", "decisions.jsonl"):
            p = os.path.join(wallet_dir, name)
            if not os.path.exists(p):
                with open(p, "a", encoding="utf-8"):
                    pass
        report.wallets_created.append(wallet_id)
        logger.info("Wallet %r créé (non initialisé) dans %s.", wallet_id, wallet_dir)

    cycle_json_path = os.path.join(repo_dir, config.CYCLE_JSON)
    if not os.path.exists(cycle_json_path):
        save_cycle_state(init_cycle_state(config.WALLET_IDS), cycle_json_path)
        report.cycle_json_created = True
        logger.info("state/cycle.json initialisé.")
    else:
        logger.info("state/cycle.json déjà présent — inchangé.")

    return report


def main() -> int:
    repo_dir = _REPO_ROOT
    report = migrate(repo_dir)
    logger.info("Migration terminée : %s", report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
