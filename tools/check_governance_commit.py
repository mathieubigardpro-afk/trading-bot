#!/usr/bin/env python3
"""tools/check_governance_commit.py — F7 (MAJEUR, audit adversarial) : fait respecter
`docs/PROMOTION-RULES.md` §0 mécaniquement — "gouvernance et jugement sont deux actes séparés,
toujours". Concrètement : "Aucun agent de recherche, aucune session incubant/évaluant une
candidate ne peut modifier [`docs/PROMOTION-RULES.md`] dans le même commit qu'une action de
promotion/rétrogradation/mort."

Un commit qui touche À LA FOIS `docs/PROMOTION-RULES.md` (la RÈGLE) ET `bot/config.py`
(`INCUBATING_STRATEGIES`/`WALLETS[*]["pockets"]`, où vivent les actions de promotion/
rétrogradation/mort) OU `docs/RESEARCH-REGISTRY.json` (le JUGEMENT, verdict d'une candidate)
viole structurellement cette règle — ce script le détecte et échoue bruyamment (exit 1) plutôt
que de laisser passer silencieusement un commit qui mélangerait les deux actes.

Usage :
    python tools/check_governance_commit.py [SHA]   # défaut : HEAD
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from typing import List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("tools.check_governance_commit")

PROMOTION_RULES_PATH = "docs/PROMOTION-RULES.md"
GOVERNED_PATHS = ("bot/config.py", "docs/RESEARCH-REGISTRY.json")


def changed_files(sha: str, repo_dir: Optional[str] = None) -> List[str]:
    """`git show --name-only --format=` (mission) : liste des chemins touchés par `sha`, un par
    ligne, sans aucun autre bruit (format vide -- pas de message de commit dans la sortie)."""
    result = subprocess.run(
        ["git", "show", "--name-only", "--format=", sha],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def check_commit(sha: str, repo_dir: Optional[str] = None) -> List[str]:
    """Retourne la liste des violations (`[]` si le commit `sha` est conforme à §0)."""
    files = set(changed_files(sha, repo_dir=repo_dir))
    if PROMOTION_RULES_PATH not in files:
        return []
    violations = [p for p in GOVERNED_PATHS if p in files]
    if not violations:
        return []
    return [
        f"le commit {sha} touche à la fois {PROMOTION_RULES_PATH!r} (gouvernance) ET "
        f"{path!r} (jugement/action de promotion) — interdit par docs/PROMOTION-RULES.md §0 "
        "(gouvernance et jugement jamais dans le même commit)"
        for path in violations
    ]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sha", nargs="?", default="HEAD", help="SHA (ou révision git) du commit à vérifier (défaut : HEAD)")
    parser.add_argument("--repo-dir", default=None, help="racine du dépôt git (défaut : répertoire courant)")
    args = parser.parse_args(argv)

    repo_dir = args.repo_dir or os.getcwd()

    try:
        violations = check_commit(args.sha, repo_dir=repo_dir)
    except subprocess.CalledProcessError as exc:
        logger.error(
            "check_governance_commit : impossible de lire le commit %r (%s) : %s",
            args.sha, exc, (exc.stderr or "").strip(),
        )
        return 1

    if violations:
        for v in violations:
            logger.error("check_governance_commit : %s", v)
        print(f"ÉCHEC : commit {args.sha} viole docs/PROMOTION-RULES.md §0", file=sys.stderr)
        return 1

    print(f"OK : commit {args.sha} respecte docs/PROMOTION-RULES.md §0 (gouvernance/jugement séparés)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
