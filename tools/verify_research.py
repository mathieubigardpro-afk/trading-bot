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

# Convention de fidélité de la bande de non-négociation (docs/BAND-FIDELITY-SPEC.md §3.6,
# session #10) : toute NOUVELLE entrée du registre (date_test >= cette date) doit porter un
# bloc `band_fidelity` (dict de l'annexe S1/S2 + exposition CB, clés minimales ci-dessous) ou
# une exemption explicite (chaîne SUBSTANTIELLE documentant pourquoi la bande est sans objet
# pour cette entrée — audit adversarial 2026-10-12, M4 : une chaîne courte type "n/a" ne
# passe pas). Les entrées antérieures sont exemptées (registre append-only, jamais réécrit).
BAND_FIDELITY_REQUIRED_FROM = "2026-10-12"
BAND_FIDELITY_REQUIRED_KEYS = ("a", "sharpe_s1", "sharpe_s2", "band_fragile", "cb")
BAND_FIDELITY_EXEMPTION_MIN_CHARS = 30

# Audit adversarial 2026-10-12 (M4) : les comparaisons de dates de ce script sont
# lexicographiques — elles ne sont correctes QUE si date_test est au format ISO strict
# YYYY-MM-DD. Toute entrée dont la date ne matche pas ce format est une violation (une date
# "15/10/2026" passerait sinon silencieusement pour antérieure à toutes les bornes).
import re as _re

_ISO_DATE_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    """Retourne la liste des violations (`[]` si le registre est conforme). Deux familles de
    vérifications : (1) borne minimale de `k_total` (§1.3) — une entrée qui NE déclare PAS de
    champ `k_total` (antécédent documenté `docs/PROMOTION-RULES.md` §5) n'est pas une violation
    de CETTE famille, seule une entrée qui le DÉCLARE est vérifiée ; (2) garde `band_fidelity`
    (docs/BAND-FIDELITY-SPEC.md §3.6, session #10) — s'applique à TOUTE entrée datée >=
    2026-10-12, et impose au passage un `date_test` ISO présent pour toutes les entrées (une
    entrée sans date exploitable ne peut prouver son antériorité à la convention)."""
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
    issues.extend(_verify_band_fidelity(entries))
    return issues


def _verify_band_fidelity(entries: List[dict]) -> List[str]:
    """Garde mécanique de docs/BAND-FIDELITY-SPEC.md §3.6 : toute entrée dont `date_test` est
    >= `BAND_FIDELITY_REQUIRED_FROM` doit porter un champ `band_fidelity` soit sous forme de
    dict contenant les clés minimales `BAND_FIDELITY_REQUIRED_KEYS` non nulles (avec `a`/
    `sharpe_s1`/`sharpe_s2` numériques, `band_fragile` booléen, `cb` dict non vide — contre-
    audit 2026-10-12), soit sous forme de chaîne d'au moins `BAND_FIDELITY_EXEMPTION_MIN_CHARS`
    caractères (exemption explicite, justifiée sur le fond dans la fiche — l'audit §1.4 la
    vérifie, cette garde ne jauge que la forme). Toute autre forme = violation. LIMITE
    DOCUMENTÉE : une entrée ANTIDATÉE (date_test < borne, choisie après coup) passe — la garde
    ne consulte pas l'historique git ; c'est l'audit de la session qui attrape ce cas. Une entrée sans `date_test` exploitable n'est pas
    évaluable ici — elle est déjà refusée par la vérification k_total si elle déclare k_total ;
    par prudence, une entrée SANS date_test du tout est signalée (impossible de prouver son
    antériorité à la convention)."""
    issues: List[str] = []
    for entry in entries:
        strategy_id = entry.get("id", "?")
        date_test = str(entry.get("date_test", "") or "")
        if not date_test:
            issues.append(
                f"{strategy_id!r} : date_test manquant/vide — impossible de prouver l'antériorité "
                f"à la convention band_fidelity ({BAND_FIDELITY_REQUIRED_FROM}), refus par prudence"
            )
            continue
        if not _ISO_DATE_RE.match(date_test):
            issues.append(
                f"{strategy_id!r} : date_test={date_test!r} hors format ISO YYYY-MM-DD — les "
                "comparaisons de dates de ce script sont lexicographiques, un format non-ISO "
                "contournerait silencieusement les bornes (k_total ET band_fidelity), refus"
            )
            continue
        if date_test < BAND_FIDELITY_REQUIRED_FROM:
            continue  # antériorité : registre append-only, jamais réécrit
        bf = entry.get("band_fidelity")
        if isinstance(bf, str):
            if len(bf.strip()) >= BAND_FIDELITY_EXEMPTION_MIN_CHARS:
                continue  # exemption explicite substantielle — sa justification est auditée (§1.4)
            issues.append(
                f"{strategy_id!r} (date_test={date_test}) : exemption band_fidelity trop courte "
                f"({len(bf.strip())} caractères < {BAND_FIDELITY_EXEMPTION_MIN_CHARS}) — une "
                "exemption doit documenter POURQUOI la bande est sans objet, cf. "
                "docs/BAND-FIDELITY-SPEC.md §3.6"
            )
            continue
        if isinstance(bf, dict) and bf:
            missing = [k for k in BAND_FIDELITY_REQUIRED_KEYS if bf.get(k) is None]
            # Contre-audit 2026-10-12 : forme minimale des valeurs, pas seulement leur présence
            # (un bloc {"a": "", ..., "cb": 0} ne doit pas passer). bool est un int en Python :
            # exclu explicitement des champs numériques.
            bad_types = [
                k for k in ("a", "sharpe_s1", "sharpe_s2")
                if k not in missing and (isinstance(bf.get(k), bool) or not isinstance(bf.get(k), (int, float)))
            ]
            if "band_fragile" not in missing and not isinstance(bf.get("band_fragile"), bool):
                bad_types.append("band_fragile")
            if "cb" not in missing and not (isinstance(bf.get("cb"), dict) and bf.get("cb")):
                bad_types.append("cb")
            if not missing and not bad_types:
                continue
            issues.append(
                f"{strategy_id!r} (date_test={date_test}) : bloc band_fidelity incomplet — clés "
                f"manquantes ou nulles : {missing}, clés de type invalide : {bad_types} "
                f"(minimum requis : {list(BAND_FIDELITY_REQUIRED_KEYS)}, a/sharpe numériques, "
                "band_fragile booléen, cb dict non vide), cf. docs/BAND-FIDELITY-SPEC.md §3.2"
            )
            continue
        issues.append(
            f"{strategy_id!r} (date_test={date_test}) : champ band_fidelity manquant ou vide — "
            "toute entrée à partir du "
            f"{BAND_FIDELITY_REQUIRED_FROM} doit porter le bloc d'annexe S1/S2 + exposition CB "
            f"(dict avec au minimum {list(BAND_FIDELITY_REQUIRED_KEYS)}) ou une exemption "
            "explicite justifiée (chaîne substantielle), cf. docs/BAND-FIDELITY-SPEC.md §3.6"
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
        print(f"OK : {n} entrée(s) dans {args.registry}, aucune violation détectée (k_total §1.3, band_fidelity BAND-FIDELITY-SPEC §3.6)")
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
