"""bot/persist/ — persistance et intégrité de l'état du bot.

Façade publique du module (ARCHITECTURE.md §5.4). Voir chaque sous-module pour le détail :
  - `state.py` : `init_state`, `load_state`, `save_state`, `compute_state_hash`,
    `is_run_already_done`, `validate_schema`, `StateValidationError`.
  - `journal.py` : `append_journal`.
  - `git_sync.py` : `git_sync`, `pull_rebase`.
  - `audit.py` : `verify_chain` (audit rétroactif de la chaîne de hash via l'historique git).
"""

from .audit import ChainAuditResult, verify_chain
from .git_sync import git_sync, pull_rebase
from .journal import append_journal
from .state import (
    GENESIS_HASH,
    StateValidationError,
    compute_state_hash,
    init_state,
    is_run_already_done,
    load_state,
    save_state,
    validate_schema,
)

__all__ = [
    "ChainAuditResult",
    "verify_chain",
    "git_sync",
    "pull_rebase",
    "append_journal",
    "GENESIS_HASH",
    "StateValidationError",
    "compute_state_hash",
    "init_state",
    "is_run_already_done",
    "load_state",
    "save_state",
    "validate_schema",
]
