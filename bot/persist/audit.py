"""bot/persist/audit.py — `verify_chain()` : audit rétroactif de la chaîne d'intégrité.

Chaque `state.json` porte, dans `state_hash_prev`, le sha256 (clés triées, canonique) de
l'état tel qu'il existait au tout début du run qui l'a produit (voir `state.compute_state_hash`
et ARCHITECTURE.md §4.3). `verify_chain()` remonte l'historique git de `state/state.json`
commit par commit et vérifie que chaque maillon est cohérent avec le précédent — une
falsification manuelle d'un commit intermédiaire (sans recalcul de toute la chaîne en aval,
ce qui nécessiterait de réécrire l'historique git lui-même) est détectée ici.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

from .state import StateValidationError, compute_state_hash, validate_schema


@dataclass
class ChainAuditResult:
    ok: bool
    n_versions_checked: int
    errors: list[str] = field(default_factory=list)


def _run(repo_dir: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo_dir, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def verify_chain(
    repo_dir: str,
    path: str = "state/state.json",
    max_commits: int | None = None,
) -> ChainAuditResult:
    """Vérifie la chaîne de hash de `path` à travers l'historique git de `repo_dir`.

    Ne lève pas d'exception pour un maillon cassé ou illisible : les problèmes sont accumulés
    dans `errors` (avec le commit fautif identifié) et `ok=False`, pour permettre un audit
    complet même en présence de plusieurs anomalies plutôt que de s'arrêter à la première.
    """
    log_args = ["log", "--follow", "--format=%H"]
    if max_commits:
        log_args += [f"-n{max_commits}"]
    log_args += ["--", path]

    log = _run(repo_dir, *log_args)
    if log.returncode != 0:
        return ChainAuditResult(
            ok=False,
            n_versions_checked=0,
            errors=[f"impossible de lister l'historique git de {path} : {log.stderr.strip()}"],
        )

    commit_hashes = [h for h in log.stdout.splitlines() if h.strip()]
    commit_hashes.reverse()  # ordre chronologique, du plus ancien au plus récent

    errors: list[str] = []
    states: list[dict | None] = []

    for commit in commit_hashes:
        show = _run(repo_dir, "show", f"{commit}:{path}")
        if show.returncode != 0:
            errors.append(f"commit {commit} : impossible de lire {path} ({show.stderr.strip()})")
            states.append(None)
            continue
        try:
            parsed = json.loads(show.stdout)
            validate_schema(parsed)
        except (json.JSONDecodeError, StateValidationError) as exc:
            errors.append(f"commit {commit} : {path} invalide ({exc})")
            states.append(None)
            continue
        states.append(parsed)

    for i in range(1, len(states)):
        prev_state, cur_state = states[i - 1], states[i]
        if prev_state is None or cur_state is None:
            # Maillon déjà signalé illisible/invalide ci-dessus : on ne peut pas comparer un
            # hash contre un état qui n'a pas pu être chargé, mais l'anomalie est déjà tracée.
            continue
        expected_hash = compute_state_hash(prev_state)
        actual_hash = cur_state.get("state_hash_prev")
        if actual_hash != expected_hash:
            errors.append(
                f"commit {commit_hashes[i]} : state_hash_prev={actual_hash!r} ne correspond "
                f"pas au sha256 attendu de la version précédente ({expected_hash!r}) — "
                f"falsification possible entre {commit_hashes[i - 1]} et {commit_hashes[i]}"
            )

    return ChainAuditResult(ok=not errors, n_versions_checked=len(states), errors=errors)
