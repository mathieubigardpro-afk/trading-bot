#!/usr/bin/env python3
"""tools/build_daily_cache.py — régénère le cache disque quotidien `data-cache/` consommé par
`bot/feeds/daily.py:prefetch_daily_history()` (cf. bandeau "INCIDENT 2026-07-24" en tête de ce
module). Exécuté quotidiennement à 21h30 UTC (après la clôture NYSE 20:00 UTC) par
`.github/workflows/daily-data-cache.yml`, ainsi que sur `workflow_dispatch` et sur push touchant
ce script ou ce workflow (run de validation immédiat, même principe que
`.github/workflows/fetch-data.yml` / `tools/fetch_data.py`).

--------------------------------------------------------------------------------------------
Pourquoi ce script existe (incident 2026-07-24, cycles horaires T13/T14 manquants)
--------------------------------------------------------------------------------------------
Le cycle horaire (`bot/runner.py`, `.github/workflows/bot.yml`, timeout 30 min) a besoin, à
CHAQUE cycle, de l'historique JOURNALIER clôturé de ~112 tickers actions/ETF (103 S&P100 + SPY
+ 8 ETF risqués + IEF) pour ses filtres SMA200/momentum. Télécharger ces 112 historiques EN
DIRECT depuis `yfinance` (par lots, avec repli séquentiel par ticker en cas d'échec de lot) a
fait dépasser le timeout du job le premier jour où la charge Yahoo à l'ouverture du marché US
(13:30 UTC) a fait échouer suffisamment de lots pour faire basculer un grand nombre de tickers
sur ce repli séquentiel — sans qu'AUCUNE exception ne remonte jamais (donc aucune trace).

Un signal SMA200/momentum MENSUEL n'a strictement aucun besoin de données `yfinance` fraîches
à la minute : un historique vieux d'un jour (voire de quelques jours, cf.
`bot.feeds.daily.CACHE_MAX_AGE_DAYS`) est PARFAIT. Ce script sort donc ce téléchargement lourd
du cycle horaire : il tourne une fois par jour, HORS du chemin critique, avec un budget de
temps généreux (`timeout-minutes: 45` côté workflow), et publie son résultat sur la branche
git orpheline `data-cache` — que `.github/workflows/bot.yml` checkoute ensuite localement
(répertoire `data-cache/` à la racine du workspace) avant de lancer le cycle horaire.
`bot/feeds/daily.py:prefetch_daily_history()` lit ce cache EN PREMIER, et ne retombe sur un
téléchargement réseau direct (désormais plafonné à 6 min, cf. `EQUITY_ETF_FETCH_DEADLINE_
SECONDS`) que pour les symboles manquants du cache ou si le cache est absent/périmé.

--------------------------------------------------------------------------------------------
Réutilisation délibérée (aucun nouveau mécanisme réseau inventé ici)
--------------------------------------------------------------------------------------------
  - Téléchargement actions/ETF : `bot.feeds.daily.prefetch_daily_history()` /
    `get_daily_history()` — MÊME pipeline (lots yfinance + repli stooq) que celui déjà câblé
    en production dans `bot.runner`, avec `deadline_seconds=None` (ce script n'est PAS soumis
    au plafond de 6 min du cycle horaire, il dispose du budget complet du job).
  - Téléchargement crypto journalier : idem, `asset_class="crypto"` (Binance klines 1d, repli
    Coinbase candles 86400s) — pas strictement consommé par le cycle horaire aujourd'hui (la
    SMA200 crypto est dérivée des bougies HORAIRES par `bot.strategies.quasi_passif_crypto`),
    mais peu coûteux à maintenir et robustifie un futur usage direct de `bot.feeds.daily` côté
    crypto (cf. mission).
  - Publication git (branche orpheline, force-push) : `tools.fetch_data.publish_to_orphan_
    branch()`, réutilisée telle quelle (même mécanisme déjà éprouvé pour la branche
    `market-data`), ciblant la branche `data-cache`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Dict, List

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from bot import config  # noqa: E402
from bot import runner as runner_mod  # noqa: E402
from bot.feeds import daily as daily_mod  # noqa: E402
from bot.feeds.types import HistoryUnavailableError  # noqa: E402
from tools.fetch_data import publish_to_orphan_branch  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tools.build_daily_cache")

# Marge au-delà de `MIN_WARMUP_DAYS` (400) : le nombre de bougies HISTORIQUES ne diminue pas
# avec le temps qui passe (un cache généré aujourd'hui contient toujours >= 400 bougies dans
# `CACHE_MAX_AGE_DAYS` jours), cette marge protège seulement des trous ponctuels côté source
# (jour férié non couvert par une source, ticker temporairement en échec côté yfinance).
_N_DAYS_MARGIN = 20
N_DAYS_EQUITY_ETF = daily_mod.MIN_WARMUP_DAYS + _N_DAYS_MARGIN
N_DAYS_CRYPTO = daily_mod.MIN_WARMUP_DAYS + _N_DAYS_MARGIN

DEFAULT_BRANCH = "data-cache"


def equity_etf_universe() -> List[str]:
    """Univers actions/ETF EXACT consommé par le cycle horaire — importé de `bot.runner` (pas
    recopié) pour qu'un ajout/retrait de ticker dans `bot.config` ne puisse JAMAIS désynchroniser
    ce cache de ce que `bot.runner._gather_daily_history()` demande réellement en production."""
    return sorted(set(runner_mod.EQUITIES_DATA_SYMBOLS) | set(runner_mod.ETF_DATA_SYMBOLS))


def crypto_universe() -> List[str]:
    """Union des univers crypto de tous les wallets (`bot.config.WALLETS[*]["univers_crypto"]`)
    — même dérivation que `bot.runner.main()` pour `crypto_symbols_all`."""
    return sorted({sym for w in config.WALLETS for sym in w.get("univers_crypto", [])})


def build_equity_etf_cache(cache_dir: str, symbols: List[str], n_days: int) -> Dict[str, dict]:
    """Télécharge (réseau, sans plafond de temps) et écrit sur disque l'historique journalier de
    `symbols` sous la classe normalisée `"equity"` — EXACTEMENT la clé que `bot.runner` utilise
    en production (`DAILY_HISTORY_ASSET_CLASS = "equities"` -> normalisée `"equity"`), quel que
    soit le rôle réel du ticker (action S&P100 ou membre de l'univers ETF risqué)."""
    daily_mod.clear_daily_cache()
    daily_mod.prefetch_daily_history(symbols, "equity", n_days=n_days, deadline_seconds=None)

    report: Dict[str, dict] = {}
    for symbol in symbols:
        try:
            df = daily_mod.get_daily_history(symbol, n_days, "equity")
        except HistoryUnavailableError as exc:
            logger.warning("equity/etf %s: indisponible, non inclus dans le cache disque (%s)", symbol, exc)
            report[symbol] = {"ok": False, "rows": 0, "reason": str(exc)}
            continue
        daily_mod.write_cache_symbol_csv(cache_dir, "equity", symbol, df)
        report[symbol] = {"ok": True, "rows": len(df), "last_ts": df.index[-1].isoformat()}

    return report


def build_crypto_cache(cache_dir: str, symbols: List[str], n_days: int) -> Dict[str, dict]:
    """Télécharge et écrit sur disque l'historique journalier crypto sous la classe normalisée
    `"crypto"` — pas de préchargement par lots pour la crypto (chaque paire est déjà une requête
    indépendante peu coûteuse, cf. `bot.feeds.daily.prefetch_daily_history` docstring)."""
    daily_mod.clear_daily_cache()
    report: Dict[str, dict] = {}
    for symbol in symbols:
        try:
            df = daily_mod.get_daily_history(symbol, n_days, "crypto")
        except HistoryUnavailableError as exc:
            logger.warning("crypto %s: indisponible, non inclus dans le cache disque (%s)", symbol, exc)
            report[symbol] = {"ok": False, "rows": 0, "reason": str(exc)}
            continue
        daily_mod.write_cache_symbol_csv(cache_dir, "crypto", symbol, df)
        report[symbol] = {"ok": True, "rows": len(df), "last_ts": df.index[-1].isoformat()}

    return report


def build_cache(staging_dir: str) -> dict:
    started_at = datetime.now(timezone.utc)

    eq_symbols = equity_etf_universe()
    logger.info("=== actions/ETF : %d ticker(s) (n_days=%d) ===", len(eq_symbols), N_DAYS_EQUITY_ETF)
    equity_report = build_equity_etf_cache(staging_dir, eq_symbols, N_DAYS_EQUITY_ETF)

    crypto_symbols = crypto_universe()
    logger.info("=== crypto : %d ticker(s) (n_days=%d) ===", len(crypto_symbols), N_DAYS_CRYPTO)
    crypto_report = build_crypto_cache(staging_dir, crypto_symbols, N_DAYS_CRYPTO)

    ended_at = datetime.now(timezone.utc)

    equity_ok = sum(1 for r in equity_report.values() if r["ok"])
    crypto_ok = sum(1 for r in crypto_report.values() if r["ok"])
    logger.info(
        "Résumé : actions/ETF ok=%d/%d | crypto ok=%d/%d | durée=%.0fs",
        equity_ok, len(eq_symbols), crypto_ok, len(crypto_symbols),
        (ended_at - started_at).total_seconds(),
    )

    manifest_extra = {
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "duration_seconds": (ended_at - started_at).total_seconds(),
        "n_days_equity_etf": N_DAYS_EQUITY_ETF,
        "n_days_crypto": N_DAYS_CRYPTO,
        "counts": {
            "equity_etf_total": len(eq_symbols),
            "equity_etf_ok": equity_ok,
            "equity_etf_failed": len(eq_symbols) - equity_ok,
            "crypto_total": len(crypto_symbols),
            "crypto_ok": crypto_ok,
            "crypto_failed": len(crypto_symbols) - crypto_ok,
        },
        "equity_etf": equity_report,
        "crypto": crypto_report,
    }
    daily_mod.write_cache_manifest(staging_dir, ended_at, extra=manifest_extra)

    with open(os.path.join(staging_dir, "MANIFEST.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)
    return manifest


def parse_args(argv=None) -> argparse.Namespace:
    default_repo_dir = _REPO_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", default=default_repo_dir, help="racine du dépôt git (défaut : parent de tools/)")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help=f"branche orpheline de publication (défaut: {DEFAULT_BRANCH})")
    parser.add_argument("--skip-push", action="store_true", help="ne pousse pas sur origin (commit local uniquement, pour tests)")
    parser.add_argument("--skip-git", action="store_true", help="ne touche pas du tout au dépôt git (écrit seulement dans --staging-dir)")
    parser.add_argument("--staging-dir", default=None, help="répertoire de staging (défaut : dossier temporaire)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    staging_dir = args.staging_dir or tempfile.mkdtemp(prefix="daily_cache_staging_")
    os.makedirs(staging_dir, exist_ok=True)
    logger.info("répertoire de staging : %s", staging_dir)

    manifest = build_cache(staging_dir)
    counts = manifest.get("counts", {})
    if counts.get("equity_etf_ok", 0) == 0:
        # Aucun ticker actions/ETF résolu : publier ce cache serait pire que ne rien publier
        # (un cache "frais mais vide" empêcherait le repli réseau direct de se déclencher côté
        # cycle horaire, cf. `_manifest_is_fresh` — mieux vaut échouer bruyamment ici).
        logger.error("AUCUN ticker actions/ETF résolu — publication annulée (cache inutilisable)")
        return 1

    if args.skip_git:
        logger.info("--skip-git : pas de publication, fichiers laissés dans %s", staging_dir)
        return 0

    try:
        publish_to_orphan_branch(args.repo_dir, staging_dir, args.branch, push=not args.skip_push)
    except RuntimeError as exc:
        logger.error("échec de la publication git : %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
