#!/usr/bin/env python3
"""tools/verify_research.py — F6 (CRITIQUE, audit adversarial) : vérifie/calcule `K_total`,
le nombre TOTAL de combinaisons/stratégies déflatées par le Deflated Sharpe Ratio (DSR) imposé
par `docs/PROMOTION-RULES.md` §1.3 :

    K_total = (nombre de lignes dans docs/RESEARCH-REGISTRY.json à la date du test)
            + (nombre de combinaisons de la grille walk-forward interne à CETTE candidate)

Ce script ne recalcule AUCUN Sharpe/DSR lui-même (hors périmètre — c'est le travail d'une
session de recherche) : il sert à (a) VALIDER que tout `k_total` déjà écrit dans le registre est
au moins aussi grand que ce que §1.3 impose structurellement (`--check`), et (b) CALCULER le
`K_total` à utiliser AUJOURD'HUI pour une nouvelle candidate, avant qu'elle ne regarde le
moindre résultat OOS (`--compute --grid-size N`) — pré-enregistrement au sens de
`docs/PROMOTION-RULES.md` §0.

Pourquoi ce garde-fou : un `k_total` sous-déclaré (candidate qui prétend déflater son DSR sur
moins de combinaisons que le registre n'en contenait réellement à sa date de test) gonfle
artificiellement son DSR affiché — exactement la rationalisation que §0/§1.3 visent à rendre
impossible. `--check` rend cette sous-déclaration détectable mécaniquement.

Modes :
  --check                    : valide `docs/RESEARCH-REGISTRY.json` (exit 0 si conforme, 1 sinon).
  --compute --grid-size N    : affiche le K_total à utiliser AUJOURD'HUI (registre entier + N).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("tools.verify_research")

REGISTRY_RELPATH = "docs/RESEARCH-REGISTRY.json"


def load_registry(path: str) -> Dict[str, Any]:
    """Charge le registre JSON. Lève `FileNotFoundError`/`json.JSONDecodeError` telles quelles
    (posture pessimiste : un registre introuvable ou corrompu ne doit JAMAIS être traité comme
    un registre vide par ce script de VALIDATION — contrairement à `tools/weekly_maintenance.py:
    load_registry()`, qui lui a une posture "signale, ne bloque jamais")."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def registry_entries(registry: Dict[str, Any]) -> List[dict]:
    return list(registry.get("strategies", []) or [])


def count_strictly_earlier(entries: List[dict], date_test: str) -> int:
    """Nombre d'entrées dont `date_test` est STRICTEMENT antérieur (comparaison lexicographique
    de chaînes ISO8601 `YYYY-MM-DD`, valide tant que le format reste celui du registre — vérifié
    par le registre lui-même, `docs/RESEARCH-REGISTRY.json` n'utilise que ce format)."""
    return sum(1 for e in entries if str(e.get("date_test", "")) < date_test)


def compute_k_total(registry: Dict[str, Any], grid_size: int) -> int:
    """K_total à utiliser AUJOURD'HUI pour une NOUVELLE candidate (docs/PROMOTION-RULES.md
    §1.3) : nombre de lignes déjà dans le registre + taille de la grille interne à la nouvelle
    candidate. `grid_size` doit être STRICTEMENT positif (une grille walk-forward a toujours au
    moins une combinaison — la combinaison unique des paramètres retenus, au minimum)."""
    if grid_size <= 0:
        raise ValueError(f"--grid-size doit être strictement positif (reçu {grid_size!r})")
    return len(registry_entries(registry)) + int(grid_size)


def verify_registry(registry: Dict[str, Any]) -> List[str]:
    """Retourne la liste des violations (`[]` si le registre est conforme). Une entrée qui NE
    déclare PAS de champ `k_total` (cas actuel : `dsr` calculé sans K_total inter-stratégies,
    antécédent documenté `docs/PROMOTION-RULES.md` §5) n'est PAS une violation — seule une
    entrée qui DÉCLARE `k_total` est vérifiée, cf. mission §1.3 ("pour toute entrée du registre
    ayant un champ k_total déclaré")."""
    entries = registry_entries(registry)
    issues: List[str] = []
    for entry in entries:
        k_total = entry.get("k_total")
        if k_total is None:
            continue
        strategy_id = entry.get("id", "?")
        date_test = entry.get("date_test")
        if not date_test:
            issues.append(
                f"{strategy_id!r} : k_total={k_total!r} déclaré mais date_test manquant/vide — "
                "impossible de vérifier la borne minimale imposée par §1.3, refus par prudence"
            )
            continue
        try:
            k_total_int = int(k_total)
        except (TypeError, ValueError):
            issues.append(f"{strategy_id!r} : k_total={k_total!r} n'est pas un entier exploitable")
            continue
        min_required = count_strictly_earlier(entries, str(date_test))
        if k_total_int < min_required:
            issues.append(
                f"{strategy_id!r} (date_test={date_test}) : k_total={k_total_int} < "
                f"{min_required} lignes du registre strictement antérieures — sous-déclaration "
                "structurelle, viole docs/PROMOTION-RULES.md §1.3 (K_total DOIT au moins couvrir "
                "tout le registre à la date du test, avant même d'ajouter la grille interne de "
                "la candidate)"
            )
    return issues


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--registry", default=os.path.join(_REPO_ROOT, REGISTRY_RELPATH),
        help=f"chemin du registre JSON (défaut : {REGISTRY_RELPATH} à la racine du dépôt)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="valide le registre existant (exit 1 si non conforme)")
    mode.add_argument("--compute", action="store_true", help="calcule le K_total à utiliser aujourd'hui")
    parser.add_argument(
        "--grid-size", type=int, default=None,
        help="taille de la grille walk-forward interne à la nouvelle candidate (requis avec --compute)",
    )
    args = parser.parse_args(argv)

    try:
        registry = load_registry(args.registry)
    except FileNotFoundError:
        logger.error("registre introuvable : %s", args.registry)
        return 1
    except json.JSONDecodeError as exc:
        logger.error("registre JSON invalide (%s) : %s", args.registry, exc)
        return 1

    if args.check:
        issues = verify_registry(registry)
        if issues:
            for issue in issues:
                logger.error("verify_research --check : %s", issue)
            print(f"ÉCHEC : {len(issues)} violation(s) détectée(s) dans {args.registry}", file=sys.stderr)
            return 1
        n = len(registry_entries(registry))
        print(f"OK : {n} entrée(s) dans {args.registry}, aucune violation de k_total détectée")
        return 0

    # --compute
    if args.grid_size is None:
        parser.error("--compute requiert --grid-size N")
    try:
        k_total = compute_k_total(registry, args.grid_size)
    except ValueError as exc:
        logger.error("verify_research --compute : %s", exc)
        return 1
    print(k_total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
