"""bot/persist/ — persistance et intégrité de l'état du bot.

Façade publique du module (ARCHITECTURE.md §5.4). Voir chaque sous-module pour le détail :
  - `state.py` : `init_state`, `load_state`, `save_state`, `compute_state_hash`,
    `is_run_already_done`, `validate_schema`, `StateValidationError`.
  - `journal.py` : `append_journal`, `append_journal_many` (écriture groupée atomique),
    `records_for_run` (garde-fou anti-doublon post-crash).
  - `git_sync.py` : `git_sync`, `pull_rebase`, `has_uncommitted_state_changes` (défense en
    profondeur pour reprendre un `git_sync` interrompu au lieu de conclure à un doublon).
  - `audit.py` : `verify_chain` (audit rétroactif de la chaîne de hash + invariant de
    conservation cash/positions vs `trades.jsonl` via l'historique git).
"""

from .audit import ChainAuditResult, verify_chain
from .cycle import (
    CycleStateValidationError,
    init_cycle_state,
    is_cycle_already_done,
    load_cycle_state,
    save_cycle_state,
    validate_cycle_schema,
)
from .git_sync import git_sync, has_uncommitted_state_changes, pull_rebase
from .journal import append_journal, append_journal_many, records_for_run
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
    "has_uncommitted_state_changes",
    "pull_rebase",
    "append_journal",
    "append_journal_many",
    "records_for_run",
    "GENESIS_HASH",
    "StateValidationError",
    "compute_state_hash",
    "init_state",
    "is_run_already_done",
    "load_state",
    "save_state",
    "validate_schema",
    "CycleStateValidationError",
    "init_cycle_state",
    "is_cycle_already_done",
    "load_cycle_state",
    "save_cycle_state",
    "validate_cycle_schema",
]
