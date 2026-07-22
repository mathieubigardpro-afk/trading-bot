"""bot/persist/journal.py — `append_journal()` : écriture append-only des journaux `.jsonl`.

ARCHITECTURE.md §4.4 : `trades.jsonl`, `equity.jsonl`, `decisions.jsonl` ne sont JAMAIS
réécrits ni tronqués — uniquement des ajouts, une ligne JSON par appel, flush + fsync avant
retour pour garantir qu'un crash juste après l'appel ne perd pas la ligne déjà visible par le
prochain `open()`.
"""

from __future__ import annotations

import json
import os


def append_journal(path: str, record: dict) -> None:
    """Ajoute `record` en une ligne JSON (+ '\\n') à la fin du fichier `path`.

    Ouverture stricte en mode append (`"a"`) : ce module ne lit jamais le contenu existant et
    ne le modifie jamais. Crée le répertoire parent si besoin (premier run, fichier `.jsonl`
    encore inexistant). `flush()` + `os.fsync()` avant de rendre la main, pour que la ligne
    survive à un crash immédiatement après l'appel.
    """
    if not isinstance(record, dict):
        raise TypeError(f"append_journal: 'record' doit être un dict, reçu {type(record).__name__}")

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    line = json.dumps(record, sort_keys=True, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
