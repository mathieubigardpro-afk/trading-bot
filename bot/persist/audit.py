"""bot/persist/audit.py — `verify_chain()` : audit rétroactif de la chaîne d'intégrité.

Chaque `state.json` porte, dans `state_hash_prev`, le sha256 (clés triées, canonique) de
l'état tel qu'il existait au tout début du run qui l'a produit (voir `state.compute_state_hash`
et ARCHITECTURE.md §4.3). `verify_chain()` remonte l'historique git de `state/state.json`
commit par commit et vérifie que chaque maillon est cohérent avec le précédent.

Limite structurelle assumée (finding CRITIQUE de l'audit adversarial) : `state_hash_prev` est
un sha256 PUBLIC, pas un HMAC/signature adossé à un secret — n'importe qui peut donc fabriquer
un nouveau commit dont l'état est arbitraire (ex. `cash_usd` gonflé sans aucun trade) tout en
recopiant correctement le hash de l'état précédent réel, ce qui rend la chaîne de hash SEULE
incapable de détecter ce type de falsification (elle ne détecte que la réécriture rétroactive
d'un commit déjà existant, immuable par nature en git une fois publié). Pour fermer cette
faille sans dépendre d'un secret que ce dépôt public ne peut pas conserver, `verify_chain()`
vérifie en plus un INVARIANT DE CONSERVATION indépendant du hash à chaque transition de
commit : la variation de `cash_usd` et de chaque `positions[symbole].qty` entre deux versions
successives de `state.json` doit être exactement justifiée par les fills journalisés dans
`state/trades.jsonl` entre ces deux mêmes commits (aucun fill => aucune variation de cash/
position tolérée), et `trades.jsonl` doit être un simple ajout (append-only strict, jamais
réécrit) d'un commit à l'autre. Un attaquant devrait donc désormais fabriquer EN PLUS des
lignes de `trades.jsonl` cohérentes avec la falsification — un signal bien plus difficile à
dissimuler et strictement plus coûteux que l'attaque triviale démontrée par l'audit.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any

from .state import StateValidationError, compute_state_hash, validate_schema

# Tolérances numériques (résidus flottants attendus après arrondis qty_step / centimes).
_EPSILON_CASH_USD = 0.01
_EPSILON_QTY = 1e-6


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


def _sibling_trades_path(state_path: str) -> str:
    if "/" in state_path:
        return state_path.rsplit("/", 1)[0] + "/trades.jsonl"
    return "trades.jsonl"


def _commit_paths(repo_dir: str, path: str, max_commits: int | None) -> list[tuple[str, str]] | None:
    """Liste, dans l'ordre chronologique (plus ancien -> plus récent), les commits qui ont
    touché `path` (en suivant les renommages comme `git log --follow`), chacun associé au
    chemin QU'AVAIT LE FICHIER À CE COMMIT PRÉCIS.

    Nécessaire car un renommage (ex. migration multi-wallets, `git mv state/state.json
    state/archive-100k/state.json`) fait que les commits antérieurs au renommage ne
    contiennent le fichier qu'à l'ANCIEN chemin — `git show <commit>:<chemin_actuel>` échoue
    pour eux (« exists on disk, but not in <commit> »). `git log --follow --name-status`
    donne, pour chaque commit, le statut ET le chemin du fichier tel qu'il existait dans ce
    commit (le nouveau chemin pour une ligne de renommage `R<score>\\told\\tnew`, l'unique
    chemin pour une ligne `M`/`A`/`D`) — on l'utilise directement plutôt que de supposer que le
    chemin demandé par l'appelant est valable pour toute l'histoire.

    Retourne `None` si `git log` échoue (ex. chemin jamais suivi par git).
    """
    log_args = ["log", "--follow", "--format=%H", "--name-status"]
    if max_commits:
        log_args += [f"-n{max_commits}"]
    log_args += ["--", path]

    log = _run(repo_dir, *log_args)
    if log.returncode != 0:
        return None

    commit_paths: list[tuple[str, str]] = []
    current_commit: str | None = None
    for line in log.stdout.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        if "\t" not in line:
            # Ligne de hash de commit (40 chars hex, %H) : pas de tabulation dedans.
            current_commit = line.strip()
            continue
        # Ligne de statut ("M\tpath", "A\tpath", "D\tpath", "R100\told\tnew", "C100\told\tnew").
        fields = line.split("\t")
        path_at_commit = fields[-1]
        if current_commit is not None:
            commit_paths.append((current_commit, path_at_commit))
            current_commit = None  # une seule ligne de statut attendue par commit pour ce pathspec

    commit_paths.reverse()  # chronologique, du plus ancien au plus récent
    return commit_paths


def _read_jsonl_at_commit(
    repo_dir: str, commit: str, path: str
) -> tuple[list[dict] | None, str | None]:
    """Lit `path` (fichier `.jsonl`) tel qu'il existait à `commit`.

    Retourne `([], None)` si le fichier n'existe pas encore à ce commit (traité comme "vide",
    cas légitime des tout premiers commits avant la création de `trades.jsonl`), `(None, err)`
    si le contenu existe mais est illisible (JSON invalide sur une ligne), ou `(records, None)`
    sinon.
    """
    show = _run(repo_dir, "show", f"{commit}:{path}")
    if show.returncode != 0:
        return [], None
    records: list[dict] = []
    for lineno, raw_line in enumerate(show.stdout.splitlines(), start=1):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            parsed: Any = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            return None, f"{path} illisible au commit {commit} (ligne {lineno}) : {exc}"
        if not isinstance(parsed, dict):
            return None, f"{path} illisible au commit {commit} (ligne {lineno}) : objet JSON attendu"
        records.append(parsed)
    return records, None


def _check_conservation_invariant(
    prev_state: dict, cur_state: dict, prev_trades: list[dict], cur_trades: list[dict], commit: str
) -> list[str]:
    """Vérifie que la variation de cash/positions entre deux versions successives de
    `state.json` est intégralement justifiée par les fills apparus dans `trades.jsonl` entre
    ces deux mêmes commits (voir docstring du module — ferme la faille du hash public seul)."""
    errors: list[str] = []

    if len(cur_trades) < len(prev_trades) or cur_trades[: len(prev_trades)] != prev_trades:
        errors.append(
            f"commit {commit} : trades.jsonl n'est pas un simple ajout par rapport à la "
            "version précédente (des lignes existantes ont été modifiées, réordonnées ou "
            "supprimées) — violation de l'invariant append-only, falsification probable du "
            "grand livre d'audit"
        )
        # La base de comparaison elle-même n'est plus fiable : impossible de calculer un delta
        # de fills significatif, mais l'anomalie est déjà tracée ci-dessus.
        return errors

    new_records = cur_trades[len(prev_trades):]

    expected_cash_delta = 0.0
    expected_qty_delta: dict[str, float] = {}
    for rec in new_records:
        try:
            side = str(rec.get("side", "")).upper()
            qty = float(rec["qty"])
            notional = float(rec["notional_usd"])
            fees = float(rec["fees_usd"])
            symbol = str(rec["symbol"])
        except (KeyError, TypeError, ValueError):
            errors.append(
                f"commit {commit} : enregistrement de trades.jsonl malformé "
                f"({rec!r}) — invariant de conservation non vérifiable pour ce fill"
            )
            continue
        if side == "BUY":
            expected_cash_delta -= notional + fees
            expected_qty_delta[symbol] = expected_qty_delta.get(symbol, 0.0) + qty
        elif side == "SELL":
            expected_cash_delta += notional - fees
            expected_qty_delta[symbol] = expected_qty_delta.get(symbol, 0.0) - qty
        else:
            errors.append(f"commit {commit} : fill trades.jsonl avec side invalide ({rec.get('side')!r})")

    actual_cash_delta = float(cur_state.get("cash_usd", 0.0)) - float(prev_state.get("cash_usd", 0.0))
    if abs(actual_cash_delta - expected_cash_delta) > _EPSILON_CASH_USD:
        errors.append(
            f"commit {commit} : variation de cash_usd ({actual_cash_delta:+.2f}$) incohérente "
            f"avec les fills journalisés dans trades.jsonl pour ce cycle (attendu "
            f"{expected_cash_delta:+.2f}$ d'après {len(new_records)} fill(s)) — falsification "
            "possible de state.json sans trade justificatif"
        )

    prev_positions = prev_state.get("positions", {}) or {}
    cur_positions = cur_state.get("positions", {}) or {}
    symbols = set(expected_qty_delta) | set(prev_positions) | set(cur_positions)
    for symbol in symbols:
        prev_qty = float((prev_positions.get(symbol) or {}).get("qty", 0.0) or 0.0)
        cur_qty = float((cur_positions.get(symbol) or {}).get("qty", 0.0) or 0.0)
        actual_delta = cur_qty - prev_qty
        expected_delta = expected_qty_delta.get(symbol, 0.0)
        if abs(actual_delta - expected_delta) > _EPSILON_QTY:
            errors.append(
                f"commit {commit} : variation de la position {symbol} ({actual_delta:+.8f}) "
                f"incohérente avec les fills journalisés dans trades.jsonl (attendu "
                f"{expected_delta:+.8f}) — falsification possible de state.json"
            )

    return errors


def verify_chain(
    repo_dir: str,
    path: str = "state/state.json",
    max_commits: int | None = None,
    trades_path: str | None = None,
) -> ChainAuditResult:
    """Vérifie la chaîne de hash de `path` à travers l'historique git de `repo_dir`, ET
    l'invariant de conservation cash/positions vs `trades_path` (par défaut `trades.jsonl`
    dans le même répertoire que `path`) — voir docstring du module.

    Ne lève pas d'exception pour un maillon cassé ou illisible : les problèmes sont accumulés
    dans `errors` (avec le commit fautif identifié) et `ok=False`, pour permettre un audit
    complet même en présence de plusieurs anomalies plutôt que de s'arrêter à la première.
    """
    trades_path_arg = trades_path

    commit_paths = _commit_paths(repo_dir, path, max_commits)
    if commit_paths is None:
        log = _run(repo_dir, "log", "--follow", "--format=%H", "--", path)
        return ChainAuditResult(
            ok=False,
            n_versions_checked=0,
            errors=[f"impossible de lister l'historique git de {path} : {log.stderr.strip()}"],
        )

    commit_hashes = [c for c, _p in commit_paths]

    errors: list[str] = []
    states: list[dict | None] = []
    trades_by_commit: list[list[dict] | None] = []

    for commit, path_at_commit in commit_paths:
        # `trades_path` par défaut est calculé À PARTIR du chemin du fichier state.json TEL
        # QU'IL ÉTAIT À CE COMMIT (pas le chemin actuel/final) : avant un renommage
        # (`git mv state/state.json state/archive-100k/state.json`), state.json ET
        # trades.jsonl vivaient tous les deux sous l'ancien répertoire.
        trades_path = trades_path_arg or _sibling_trades_path(path_at_commit)

        show = _run(repo_dir, "show", f"{commit}:{path_at_commit}")
        if show.returncode != 0:
            errors.append(f"commit {commit} : impossible de lire {path_at_commit} ({show.stderr.strip()})")
            states.append(None)
            trades_by_commit.append(None)
            continue
        try:
            parsed = json.loads(show.stdout)
            validate_schema(parsed)
        except (json.JSONDecodeError, StateValidationError) as exc:
            errors.append(f"commit {commit} : {path_at_commit} invalide ({exc})")
            states.append(None)
            trades_by_commit.append(None)
            continue
        states.append(parsed)

        trades, trades_err = _read_jsonl_at_commit(repo_dir, commit, trades_path)
        if trades_err is not None:
            errors.append(f"commit {commit} : {trades_err}")
        trades_by_commit.append(trades)

    for i in range(1, len(states)):
        prev_state, cur_state = states[i - 1], states[i]
        if prev_state is None or cur_state is None:
            # Maillon déjà signalé illisible/invalide ci-dessus : on ne peut pas comparer un
            # hash contre un état qui n'a pas pu être chargé, mais l'anomalie est déjà tracée.
            continue
        if prev_state == cur_state:
            # Version strictement IDENTIQUE à la précédente (même contenu JSON complet, y
            # compris son propre `state_hash_prev`) : ce n'est pas un nouveau run (aucun champ
            # n'a changé, donc `state_hash_prev` n'avait aucune raison d'être recalculé) mais
            # une simple entrée d'historique git sans changement de contenu — typiquement un
            # `git mv` préservant le blob (ex. migration §10.8 de l'archive 100k$ vers
            # `state/archive-100k/`, cf. `tools/migrate_to_wallets.py`). Exiger que
            # `state_hash_prev` pointe vers le hash de CETTE version-ci (qui est par
            # construction identique à la précédente) contredirait la définition même du
            # champ (hash de l'état tel qu'il existait AVANT les modifications d'un run réel) —
            # et ne réduit pas la protection : un contenu strictement identique ne peut par
            # définition receler aucune falsification de cash/positions (voir invariant de
            # conservation ci-dessous, qui continue de s'appliquer normalement).
            pass
        else:
            expected_hash = compute_state_hash(prev_state)
            actual_hash = cur_state.get("state_hash_prev")
            if actual_hash != expected_hash:
                errors.append(
                    f"commit {commit_hashes[i]} : state_hash_prev={actual_hash!r} ne correspond "
                    f"pas au sha256 attendu de la version précédente ({expected_hash!r}) — "
                    f"falsification possible entre {commit_hashes[i - 1]} et {commit_hashes[i]}"
                )

        prev_trades, cur_trades = trades_by_commit[i - 1], trades_by_commit[i]
        if prev_trades is None or cur_trades is None:
            # trades.jsonl illisible à l'un des deux commits : déjà signalé ci-dessus, on ne
            # peut pas vérifier l'invariant de conservation pour cette transition.
            continue
        errors.extend(
            _check_conservation_invariant(
                prev_state, cur_state, prev_trades, cur_trades, commit_hashes[i]
            )
        )

    return ChainAuditResult(ok=not errors, n_versions_checked=len(states), errors=errors)
