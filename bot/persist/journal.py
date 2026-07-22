"""bot/persist/journal.py — `append_journal()` : écriture append-only des journaux `.jsonl`.

ARCHITECTURE.md §4.4 : `trades.jsonl`, `equity.jsonl`, `decisions.jsonl` ne sont JAMAIS
réécrits ni tronqués — uniquement des ajouts, une ligne JSON par appel, flush + fsync avant
retour pour garantir qu'un crash juste après l'appel ne perd pas la ligne déjà visible par le
prochain `open()`.

Correctif post-audit (finding MAJEUR n°2) : un cycle qui exécute plusieurs fills journalisait
historiquement CHAQUE fill via un appel `append_journal()` séparé, dans une boucle. Un
`kill -9` entre deux de ces appels laisse un fill "fantôme" déjà fsync sur disque alors que
`state.json` (écrit en tout dernier) ne l'a pas encore intégré : le run suivant, ne voyant
aucun changement de `last_run_id`, rejoue tout le cycle depuis zéro et RE-produit ce même fill
en plus de l'orphelin déjà présent — désynchronisation permanente entre `trades.jsonl` (grand
livre d'audit) et `state.json` (source de vérité des positions). `append_journal_many()`
regroupe tous les fills d'un même cycle en un seul appel `write()` + `fsync()`, réduisant la
fenêtre de crash à "aucun fill du cycle n'est écrit" ou "tous le sont" — jamais un sous-
ensemble. Combiné à `records_for_run()` (garde-fou de reprise dans `bot/runner.py`), qui
détecte toute trace orpheline d'un `run_id` déjà partiellement journalisé pour refuser de
rejouer aveuglément le cycle, cela ferme la fenêtre de désynchronisation observée par l'audit.
"""

from __future__ import annotations

import json
import os
from typing import Iterable, List


def _validate_record(record: dict) -> None:
    if not isinstance(record, dict):
        raise TypeError(f"append_journal: 'record' doit être un dict, reçu {type(record).__name__}")


def append_journal(path: str, record: dict) -> None:
    """Ajoute `record` en une ligne JSON (+ '\\n') à la fin du fichier `path`.

    Ouverture stricte en mode append (`"a"`) : ce module ne lit jamais le contenu existant et
    ne le modifie jamais. Crée le répertoire parent si besoin (premier run, fichier `.jsonl`
    encore inexistant). `flush()` + `os.fsync()` avant de rendre la main, pour que la ligne
    survive à un crash immédiatement après l'appel.
    """
    append_journal_many(path, [record])


def append_journal_many(path: str, records: Iterable[dict]) -> None:
    """Ajoute plusieurs enregistrements en UN SEUL appel `write()` + `fsync()`.

    À utiliser à la place d'une boucle de `append_journal()` individuels quand plusieurs
    enregistrements appartiennent au même cycle logique (ex. tous les fills d'un run) : cela
    élimine la fenêtre de crash entre deux écritures individuelles qui pouvait laisser un
    sous-ensemble orphelin sur disque (voir docstring du module, finding MAJEUR n°2 de
    l'audit). Si `records` est vide, le fichier est tout de même créé (comportement identique
    à l'ancien appel en boucle sur une liste vide) mais aucun `write()` n'est effectué.
    """
    records = list(records)
    for record in records:
        _validate_record(record)

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    if not records:
        # Garantit l'existence du fichier (comportement historique), sans écriture inutile.
        open(path, "a", encoding="utf-8").close()
        return

    payload = "".join(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n" for record in records)
    with open(path, "a", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())


def records_for_run(path: str, run_id: str) -> List[dict]:
    """Retourne les enregistrements de `path` (fichier `.jsonl`) dont le champ `run_id`
    correspond exactement à `run_id`.

    Utilisé comme garde-fou de reprise (`bot/runner.py`) : si des enregistrements existent
    déjà pour le `run_id` que l'on s'apprête à traiter alors que `state.json` ne le connaît
    pas encore comme `last_run_id`, c'est le signe d'un cycle précédent interrompu en cours de
    journalisation (crash entre écritures) — rejouer le cycle produirait des doublons. Ne lève
    jamais d'exception : un fichier absent ou une ligne malformée sont traités comme
    "aucun enregistrement trouvé pour cette ligne" (le fichier peut ne pas encore exister au
    tout premier run), la responsabilité de décider quoi faire d'une anomalie de contenu
    incombe à l'appelant.
    """
    if not os.path.exists(path):
        return []
    matches: List[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("run_id") == run_id:
                matches.append(record)
    return matches
