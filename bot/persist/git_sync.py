"""bot/persist/git_sync.py — synchronisation git de l'état (ARCHITECTURE.md §4.5).

SÉCURITÉ : ce module ne touche JAMAIS aux credentials. Le remote est déjà authentifié dans
l'URL du clone fourni à l'environnement d'exécution (token dans l'URL HTTPS du remote `origin`).
On n'exécute jamais `git remote -v` ni on n'affiche/loggue son contenu ici ; on ne le modifie
jamais (`git remote set-url` interdit) ; on ne le copie jamais dans un fichier du dépôt.

Deux fonctions publiques :
  - `pull_rebase(repo_dir)` : à appeler en tout DÉBUT de cycle, avant la moindre lecture de
    `state.json`, pour repartir de l'état le plus récent poussé par un run précédent.
  - `git_sync(repo_dir, message, ...)` : à appeler en TOUTE FIN de cycle (après toutes les
    écritures de journaux + `save_state`), commit + push, avec gestion du conflit de push
    concurrent et re-vérification d'idempotence via `last_run_id` distant.
"""

from __future__ import annotations

import json
import subprocess

STATE_FILES = [
    "state/state.json",
    "state/trades.jsonl",
    "state/equity.jsonl",
    "state/decisions.jsonl",
]

DEFAULT_STATE_PATH = "state/state.json"


def _run(repo_dir: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo_dir, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def pull_rebase(repo_dir: str, branch: str = "main") -> str:
    """`git pull --rebase origin <branch>` en tout début de cycle.

    Retourne 'SUCCESS' ou 'FAILED'. Ne lève jamais d'exception : un run qui ne peut pas puller
    au tout début (réseau indisponible, remote injoignable) n'est pas nécessairement fatal —
    c'est au runner de décider s'il continue avec l'état local du clone tel quel ou s'arrête ;
    ce module se contente de rapporter le résultat et de nettoyer tout rebase laissé en
    suspens pour ne jamais laisser le dépôt local dans un état intermédiaire ambigu.
    """
    result = _run(repo_dir, "pull", "--rebase", "origin", branch)
    if result.returncode == 0:
        return "SUCCESS"
    # Nettoyage défensif : si le pull a laissé un rebase en cours (conflit), on l'annule pour
    # repartir d'un working tree propre plutôt que de continuer un cycle sur un état ambigu.
    _run(repo_dir, "rebase", "--abort")
    return "FAILED"


def _remote_last_run_id(repo_dir: str, state_path: str, branch: str = "main") -> str | None:
    """Lit `last_run_id` du `state.json` tel qu'il existe sur `origin/<branch>`, sans toucher
    au working tree local. Retourne None si indisponible/illisible (traité prudemment comme
    "impossible de confirmer un doublon" par l'appelant)."""
    result = _run(repo_dir, "show", f"origin/{branch}:{state_path}")
    if result.returncode != 0:
        return None
    try:
        remote_state = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(remote_state, dict):
        return None
    return remote_state.get("last_run_id")


def git_sync(
    repo_dir: str,
    message: str,
    max_retries: int = 3,
    run_id: str | None = None,
    state_path: str = DEFAULT_STATE_PATH,
    branch: str = "main",
) -> str:
    """Commit + push des fichiers d'état. Retourne 'SUCCESS' | 'ABORTED_DUPLICATE' | 'FAILED'.

    Séquence (ARCHITECTURE.md §4.5) :
      1. `git add` des 4 fichiers d'état.
      2. `git commit -m message`.
      3. `git push` — succès direct -> 'SUCCESS'.
      4. Échec (non-fast-forward) : `git pull --rebase origin <branch>`.
         - Conflit sur `state/state.json` -> signe qu'un autre run a déjà traité (ou traite)
           le même `run_id` : `git rebase --abort`, relit `last_run_id` distant. S'il est >=
           au `run_id` local fourni -> 'ABORTED_DUPLICATE' (course perdue, pas une erreur).
           Sinon, retente jusqu'à `max_retries`.
         - Rebase réussi sans conflit -> retente le push.
      5. Après `max_retries` échecs -> 'FAILED'.

    `run_id` doit être fourni par l'appelant pour permettre la ré-vérification d'idempotence
    en cas de conflit concurrent. Sans lui, un conflit est traité prudemment comme 'FAILED'
    plutôt que de risquer de masquer un double-run.
    """
    add = _run(repo_dir, "add", *STATE_FILES)
    if add.returncode != 0:
        return "FAILED"

    commit = _run(repo_dir, "commit", "-m", message)
    if commit.returncode != 0:
        # Rien à committer (ou échec de commit) : ne devrait pas arriver dans un cycle qui a
        # produit des écritures d'état ; traité comme échec explicite plutôt que masqué.
        return "FAILED"

    attempt = 0
    while attempt <= max_retries:
        push = _run(repo_dir, "push", "origin", branch)
        if push.returncode == 0:
            return "SUCCESS"

        rebase = _run(repo_dir, "pull", "--rebase", "origin", branch)
        if rebase.returncode != 0:
            _run(repo_dir, "rebase", "--abort")
            if run_id is not None:
                remote_run_id = _remote_last_run_id(repo_dir, state_path, branch)
                if remote_run_id is not None and remote_run_id >= run_id:
                    return "ABORTED_DUPLICATE"
            attempt += 1
            continue

        attempt += 1

    return "FAILED"
