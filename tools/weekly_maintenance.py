#!/usr/bin/env python3
"""tools/weekly_maintenance.py — boucle MÉCANIQUE d'auto-amélioration, gratuite, sur GitHub
Actions (mission "auto-amélioration continue" — cf. `docs/PROMOTION-RULES.md`,
`docs/RECALIBRATION-SPEC.md`).

Ce script ne prend JAMAIS de décision de promotion/rétrogradation/mort — il SIGNALE (§ moniteur
de dérive) et applique, DANS UNE GRILLE PRÉ-ENREGISTRÉE ÉTROITE, un recalibrage borné d'un seul
paramètre de signal (§ recalibrage encadré). Les décisions de gouvernance restent la propriété
exclusive d'une session de recherche hebdomadaire humaine (`docs/PROMOTION-RULES.md`, `docs/
RESEARCH-LOG.md`).

Ennemi désigné (rappel mission) : le SUR-APPRENTISSAGE. Deux garde-fous structurels dans ce
fichier le combattent directement :
  1. le moniteur de dérive ne fait QUE comparer des métriques déjà calculées à des seuils déjà
     écrits dans `docs/PROMOTION-RULES.md` — il ne cherche, ne teste, ni ne sélectionne rien ;
  2. le recalibrage n'explore JAMAIS une valeur hors de la grille pré-enregistrée dans
     `docs/RECALIBRATION-SPEC.md` (vérifié structurellement par `validate_param_in_grid()`), ne
     touche JAMAIS au cadre de risque (`bot/config.py:WALLETS[*]["risque"]`, hors de portée de la
     recherche par `docs/PROMOTION-RULES.md` §4.3), et n'applique un changement que si
     l'amélioration OOS relative dépasse 10 % — sinon il ne touche à rien.

Deux responsabilités :

  a) MONITEUR DE DÉRIVE — pour chaque stratégie active (wallets réels) et chaque candidate en
     incubation (wallet labo), compare les métriques VÉCUES (journaux `state/wallets/*/`,
     agrégées ici en un proxy de série de rendements quotidiens — cf. `build_strategy_cycle_
     returns`/`aggregate_daily_returns`) aux métriques OOS de référence (`docs/RESEARCH-
     REGISTRY.json`, elles-mêmes extraites des `results.json` de chaque backtest audité) et
     classe le résultat OK / SURVEILLER / ALERTE selon les seuils chiffrés des RÈGLES DE MORT de
     `docs/PROMOTION-RULES.md` (§2.1/§2.2 Porte 2 pour l'incubation, §3.1/§3.2 pour les
     stratégies actives). Écrit `docs/DRIFT-REPORT.md`.

     Note d'honnêteté (cf. `docs/PROMOTION-RULES.md` §5) : `xs_momentum_sp100`, `dual_momentum_
     multiclasse_etf` et `quasi_passif_crypto` sont un antécédent explicitement HORS du cadre
     formel §3 (déployées avant l'existence de ce protocole). Leur verdict ci-dessous reste
     informatif ("si cette règle s'appliquait") et ne déclenche jamais d'action automatique.

  b) RECALIBRAGE ENCADRÉ — pour `quasi_passif_crypto` UNIQUEMENT (les stratégies mensuelles,
     actions/ETF, n'ont pas besoin de ce rythme). Re-walk-forward de `REGIME_SMA_DAYS` (le seul
     paramètre de signal de cette stratégie qui ne soit PAS un paramètre du cadre de risque, cf.
     `docs/RECALIBRATION-SPEC.md` pour la justification complète de ce choix), strictement DANS
     la grille pré-enregistrée `[150, 175, 200, 225, 250]`, sur des données fraîchement
     téléchargées (réutilise `tools/fetch_data.py`). Si la valeur optimale a changé ET que
     l'amélioration OOS relative dépasse 10 %, applique le changement dans `bot/config.py` ET
     `bot/strategies/quasi_passif_crypto.py` (les deux constantes doivent rester synchronisées,
     cf. `apply_recalibration_to_files`) avec un commit dédié documenté. Sinon, ne touche à rien.

  c) Rafraîchit les données de backtest en réutilisant `tools/fetch_data.py` (`--only crypto`,
     `--skip-git` — la publication sur la branche `market-data` reste la responsabilité du
     workflow dédié `fetch-data.yml`, ce script se contente de disposer de données fraîches
     LOCALEMENT pour son propre recalibrage).

Posture pessimiste habituelle du projet : toute donnée manquante/insuffisante (réseau
indisponible, historique trop court, registre introuvable) fait SAUTER l'étape concernée
(journalisé explicitement dans le rapport) plutôt que de produire un résultat inventé ou de faire
planter tout le job.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import statistics
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import bot.config as config  # noqa: E402
from bot.persist.git_sync import git_sync, has_uncommitted_state_changes, pull_rebase  # noqa: E402
from bot.strategies.quasi_passif_crypto import (  # noqa: E402
    SPEC_UNIVERSE_BY_WALLET,
    _daily_closes,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("tools.weekly_maintenance")

DRIFT_REPORT_RELPATH = "docs/DRIFT-REPORT.md"
REGISTRY_RELPATH = "docs/RESEARCH-REGISTRY.json"

# ==========================================================================================
# --- Seuils du moniteur de dérive (cf. docs/PROMOTION-RULES.md — cités section par section) ---
# ==========================================================================================
INCUBATION_MIN_OBSERVATION_DAYS = 28   # §2.1 Porte 2 : durée minimale avant tout diagnostic
DEATH_DD_MULTIPLE_ACTIVE = 2.0         # §3.1 : drawdown vécu > 2x attendu -> rétrogradation
WATCH_DD_MULTIPLE_ACTIVE = 1.5         # vigilance précoce -- réutilise le seuil Porte 2 §2.2,
                                        # PAS un seuil de mort en tant que tel pour une stratégie
                                        # déjà active, mais un signal d'approche raisonnable.
ROLLING_SHARPE_WINDOW_DAYS = 60        # §3.1
ROLLING_SHARPE_NEGATIVE_ALERT_DAYS = 30  # §3.1 : 30j consécutifs de Sharpe roulant 60j < 0
PORTE2_MIN_SHARPE_RATIO = 0.5          # §2.2 : Sharpe vécu >= 50% du Sharpe OOS annoncé
PORTE2_MAX_DD_MULTIPLE = 1.5           # §2.2 : DD vécu <= 1.5x le DD OOS attendu
INCUBATION_MAX_DAYS = 56               # §3.2 : mort automatique labo au-delà de 56j
INCUBATION_WATCH_DAYS = 42             # heuristique INTERNE (PAS un seuil PROMOTION-RULES) :
                                        # 75% de 56j, pour alerter la session hebdo AVANT le
                                        # couperet -- jamais utilisé pour déclencher une action.

VERDICT_ORDER = {"OK": 0, "SURVEILLER": 1, "ALERTE": 2}

# Correspondance wallet -> variante SPEC du backtest non audité de quasi_passif_crypto
# (docs/RESEARCH-REGISTRY.json:strategies[].sharpe_backtest_non_audite / max_drawdown_pct_backtest)
QUASI_PASSIF_WALLET_VARIANT = {
    "prudent": "prudent_btc_eth",
    "equilibre": "equilibre_6majors",
    "agressif": "agressif_12diversifie",
}

# Antécédent (docs/PROMOTION-RULES.md §5) : ces 3 stratégies ont été déployées AVANT
# l'existence du protocole formel -- le moniteur les évalue quand même (signal informatif utile),
# mais ne prétend jamais qu'un verdict ALERTE ici déclenche une rétrogradation automatique.
STRATEGIES_ANTECEDENT_HORS_PROMOTION_RULES = {
    "xs_momentum_sp100",
    "dual_momentum_multiclasse_etf",
    "quasi_passif_crypto",
}


def _escalate(current: str, candidate: str) -> str:
    """Retourne le plus sévère de `current`/`candidate` (OK < SURVEILLER < ALERTE)."""
    return candidate if VERDICT_ORDER[candidate] > VERDICT_ORDER[current] else current


# ==========================================================================================
# --- Chargement des journaux / du registre ---
# ==========================================================================================


def _read_jsonl(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    records: List[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def load_wallet_journals(repo_dir: str, wallet_id: str) -> Dict[str, List[dict]]:
    """Charge `decisions.jsonl`/`trades.jsonl`/`equity.jsonl` d'un wallet. Un fichier absent
    donne une liste vide (jamais une exception) -- posture pessimiste habituelle : "pas de
    données" plutôt qu'un plantage."""
    return {
        "decisions": _read_jsonl(os.path.join(repo_dir, config.wallet_decisions_jsonl(wallet_id))),
        "trades": _read_jsonl(os.path.join(repo_dir, config.wallet_trades_jsonl(wallet_id))),
        "equity": _read_jsonl(os.path.join(repo_dir, config.wallet_equity_jsonl(wallet_id))),
    }


def load_registry(repo_dir: str) -> dict:
    path = os.path.join(repo_dir, REGISTRY_RELPATH)
    if not os.path.exists(path):
        return {"strategies": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def registry_entry(registry: dict, strategy_id: str) -> Optional[dict]:
    for entry in registry.get("strategies", []):
        if entry.get("id") == strategy_id:
            return entry
    return None


def reference_metrics_for(strategy_id: str, wallet_id: str, entry: Optional[dict]) -> Dict[str, Any]:
    """Retourne `{"sharpe_ref": float|None, "dd_ref_pct": float|None, "source": str}` -- les
    métriques OOS de référence pour `strategy_id` dans `wallet_id`, extraites de l'entrée du
    registre (`docs/RESEARCH-REGISTRY.json`, elle-même dérivée de `results.json`).

    `quasi_passif_crypto` est un cas particulier : son backtest non audité expose une
    référence PAR VARIANTE (une par wallet), pas une valeur unique -- cf.
    `QUASI_PASSIF_WALLET_VARIANT`.
    """
    if entry is None:
        return {"sharpe_ref": None, "dd_ref_pct": None, "source": "introuvable dans le registre"}

    if strategy_id == "quasi_passif_crypto":
        variant = QUASI_PASSIF_WALLET_VARIANT.get(wallet_id)
        sharpe_ref = (entry.get("sharpe_backtest_non_audite") or {}).get(variant)
        dd_ref = (entry.get("max_drawdown_pct_backtest") or {}).get(variant)
        return {
            "sharpe_ref": sharpe_ref,
            "dd_ref_pct": dd_ref,
            "source": f"backtest NON AUDITÉ (variante {variant or '?'})",
        }

    return {
        "sharpe_ref": entry.get("sharpe_oos"),
        "dd_ref_pct": entry.get("max_drawdown_oos_pct"),
        "source": "walk-forward OOS audité",
    }


# ==========================================================================================
# --- Construction d'une série de rendements quotidiens VÉCUS attribués à une stratégie ---
# ==========================================================================================


def build_strategy_cycle_returns(
    decisions: Iterable[dict], equity: Iterable[dict], strategy_id: str
) -> List[Tuple[str, float]]:
    """Retourne `[(run_id, rendement_de_contribution), ...]`, triée par `run_id` croissant.

    Approximation d'attribution documentée (le bot ne tient pas de sous-ledger par stratégie,
    cf. `bot/reporting/tracking.py`) : au run `R`, `rendement_de_contribution(R) = poids_de_la_
    stratégie(R-1) * rendement_du_wallet(R-1 -> R)`, où `poids_de_la_stratégie(R-1)` est la
    somme des `exposures` (fraction de l'équity du wallet) des symboles attribués à
    `strategy_id` au run `R-1` (même clé de jointure `(run_id, symbole) -> strategy_signals`
    que `bot.reporting.tracking.compute_live_metrics`). Ce n'est PAS une vérité comptable
    exacte (le wallet peut porter plusieurs stratégies simultanément dans des poches
    différentes ; la fraction non attribuée du rendement du wallet est ignorée ici) mais un
    proxy raisonnable et déterministe pour un moniteur de SIGNAL, pas pour un ledger réel.
    """
    equity_by_run = {e["run_id"]: e for e in equity if isinstance(e, dict) and "run_id" in e}
    run_ids_sorted = sorted(equity_by_run.keys())

    symbols_by_run: Dict[str, set] = {}
    for d in decisions:
        if not isinstance(d, dict):
            continue
        signals = d.get("strategy_signals") or {}
        if not isinstance(signals, dict) or strategy_id not in signals:
            continue
        run_id = d.get("run_id")
        symbol = d.get("symbol")
        if run_id is None or symbol is None:
            continue
        symbols_by_run.setdefault(run_id, set()).add(symbol)

    returns: List[Tuple[str, float]] = []
    for prev_run, cur_run in zip(run_ids_sorted, run_ids_sorted[1:]):
        if prev_run not in symbols_by_run:
            # La stratégie n'a produit AUCUN signal ce cycle (jamais évaluée -- pas la même
            # chose qu'un poids nul obtenu après évaluation) : cycle exclu de la série plutôt
            # que compté comme un rendement de contribution de 0.0, pour ne jamais gonfler
            # artificiellement `n_days_observed` d'une stratégie qui n'a en réalité jamais
            # tradé sur ce wallet.
            continue
        prev_eq = equity_by_run[prev_run]
        cur_eq = equity_by_run[cur_run]
        prev_equity_usd = prev_eq.get("equity_usd")
        cur_equity_usd = cur_eq.get("equity_usd")
        if not prev_equity_usd or prev_equity_usd <= 0 or cur_equity_usd is None:
            continue
        wallet_return = (float(cur_equity_usd) - float(prev_equity_usd)) / float(prev_equity_usd)
        exposures = prev_eq.get("exposures") or {}
        weight = sum(float(exposures.get(s, 0.0) or 0.0) for s in symbols_by_run.get(prev_run, ()))
        returns.append((cur_run, weight * wallet_return))
    return returns


def aggregate_daily_returns(cycle_returns: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
    """Agrège une série `[(run_id, rendement), ...]` (`run_id` au format `YYYY-MM-DDTHH`) en
    rendements quotidiens (composition des rendements intra-journaliers), triés
    chronologiquement. Un jour partiellement observé (bot lancé en cours de journée) reste
    inclus tel quel -- pas de règle de complétude ici (à la différence de `_daily_closes`, qui
    porte sur des prix, pas sur des rendements de stratégie déjà réalisés)."""
    by_day: Dict[str, float] = {}
    order: List[str] = []
    for run_id, r in cycle_returns:
        day = str(run_id)[:10]
        if day not in by_day:
            by_day[day] = 1.0
            order.append(day)
        by_day[day] *= (1.0 + r)
    return [(day, by_day[day] - 1.0) for day in order]


def sharpe_from_daily_returns(daily_returns: Sequence[float]) -> Optional[float]:
    """Sharpe annualisé (racine de 365) d'une série de rendements QUOTIDIENS. `None` si moins
    de 2 observations ou variance nulle (non calculable de façon fiable)."""
    values = list(daily_returns)
    if len(values) < 2:
        return None
    mean = statistics.fmean(values)
    try:
        stdev = statistics.stdev(values)
    except statistics.StatisticsError:
        return None
    if stdev == 0:
        return None
    return (mean / stdev) * math.sqrt(365.0)


def max_drawdown_from_daily_returns(daily_returns: Sequence[float]) -> Optional[float]:
    """Max drawdown (en %, positif) d'une courbe d'équity reconstruite par composition des
    rendements quotidiens fournis, base 1.0. `None` si la série est vide."""
    values = list(daily_returns)
    if not values:
        return None
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in values:
        equity *= (1.0 + r)
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return max_dd * 100.0


def rolling_sharpe_negative_streak(daily_returns: Sequence[float], window: int) -> Optional[int]:
    """Nombre de jours consécutifs (en partant de la fin de la série) où le Sharpe roulant sur
    `window` jours est strictement négatif. `None` si moins de `window` observations sont
    disponibles (Sharpe roulant non calculable du tout sur cette fenêtre)."""
    values = list(daily_returns)
    n = len(values)
    if n < window:
        return None
    rolling: List[Optional[float]] = []
    for end in range(window, n + 1):
        segment = values[end - window:end]
        rolling.append(sharpe_from_daily_returns(segment))
    streak = 0
    for s in reversed(rolling):
        if s is not None and s < 0:
            streak += 1
        else:
            break
    return streak


def compute_live_return_stats(journaux: dict, strategy_id: str) -> Dict[str, Any]:
    decisions = journaux.get("decisions") or []
    equity = journaux.get("equity") or []
    cycle_returns = build_strategy_cycle_returns(decisions, equity, strategy_id)
    daily = aggregate_daily_returns(cycle_returns)
    daily_values = [r for _, r in daily]
    return {
        "n_days_observed": len(daily_values),
        "sharpe_live": sharpe_from_daily_returns(daily_values),
        "dd_live_pct": max_drawdown_from_daily_returns(daily_values),
        "negative_rolling_streak_days": rolling_sharpe_negative_streak(
            daily_values, ROLLING_SHARPE_WINDOW_DAYS
        ),
    }


# ==========================================================================================
# --- Classification du verdict de dérive (OK / SURVEILLER / ALERTE) ---
# ==========================================================================================


def classify_active_strategy_drift(
    *,
    sharpe_live: Optional[float],
    dd_live_pct: Optional[float],
    sharpe_ref: Optional[float],
    dd_ref_pct: Optional[float],
    n_days_observed: int,
    negative_rolling_streak_days: Optional[int],
) -> Dict[str, Any]:
    """Verdict pour une stratégie ACTIVE (portée par un wallet réel), selon les RÈGLES DE MORT
    §3.1 de `docs/PROMOTION-RULES.md`. Ne prend aucune décision -- signale seulement."""
    if n_days_observed < INCUBATION_MIN_OBSERVATION_DAYS:
        return {
            "verdict": "SURVEILLER",
            "reasons": [
                f"historique vécu {n_days_observed}j < {INCUBATION_MIN_OBSERVATION_DAYS}j "
                "(§2.1) — trop tôt pour un diagnostic fiable"
            ],
        }

    verdict = "OK"
    reasons: List[str] = []

    if sharpe_live is None:
        verdict = _escalate(verdict, "SURVEILLER")
        reasons.append("Sharpe vécu non calculable (variance nulle observée sur la fenêtre)")

    if dd_ref_pct is not None and dd_ref_pct > 0 and dd_live_pct is not None:
        dd_ratio = dd_live_pct / dd_ref_pct
        if dd_ratio > DEATH_DD_MULTIPLE_ACTIVE:
            verdict = _escalate(verdict, "ALERTE")
            reasons.append(
                f"drawdown vécu {dd_live_pct:.1f}% > {DEATH_DD_MULTIPLE_ACTIVE:.1f}x le DD "
                f"attendu ({dd_ref_pct:.1f}%) — seuil de rétrogradation §3.1 dépassé"
            )
        elif dd_ratio > WATCH_DD_MULTIPLE_ACTIVE:
            verdict = _escalate(verdict, "SURVEILLER")
            reasons.append(
                f"drawdown vécu {dd_live_pct:.1f}% > {WATCH_DD_MULTIPLE_ACTIVE:.1f}x le DD "
                f"attendu ({dd_ref_pct:.1f}%) — approche du seuil §3.1 ({DEATH_DD_MULTIPLE_ACTIVE:.1f}x)"
            )
    else:
        verdict = _escalate(verdict, "SURVEILLER")
        reasons.append("DD de référence indisponible dans le registre — comparaison impossible")

    if negative_rolling_streak_days is None:
        verdict = _escalate(verdict, "SURVEILLER")
        reasons.append(
            f"Sharpe roulant {ROLLING_SHARPE_WINDOW_DAYS}j non calculable "
            f"(historique < {ROLLING_SHARPE_WINDOW_DAYS}j)"
        )
    elif negative_rolling_streak_days >= ROLLING_SHARPE_NEGATIVE_ALERT_DAYS:
        verdict = _escalate(verdict, "ALERTE")
        reasons.append(
            f"Sharpe roulant {ROLLING_SHARPE_WINDOW_DAYS}j négatif depuis "
            f"{negative_rolling_streak_days}j consécutifs (seuil §3.1 : "
            f"{ROLLING_SHARPE_NEGATIVE_ALERT_DAYS}j) — seuil de rétrogradation dépassé"
        )
    elif negative_rolling_streak_days > 0:
        verdict = _escalate(verdict, "SURVEILLER")
        reasons.append(
            f"Sharpe roulant {ROLLING_SHARPE_WINDOW_DAYS}j négatif depuis "
            f"{negative_rolling_streak_days}j (sous le seuil d'alerte "
            f"{ROLLING_SHARPE_NEGATIVE_ALERT_DAYS}j)"
        )

    if not reasons:
        reasons.append("aucun signal de dérive détecté")
    return {"verdict": verdict, "reasons": reasons}


def _age_days(entered_at: Optional[str], now: datetime) -> Optional[int]:
    if not entered_at:
        return None
    try:
        dt = datetime.fromisoformat(str(entered_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now.date() - dt.date()).days


def classify_incubating_drift(
    *,
    age_days: Optional[int],
    sharpe_live: Optional[float],
    dd_live_pct: Optional[float],
    sharpe_ref: Optional[float],
    dd_ref_pct: Optional[float],
    n_days_observed: int,
) -> Dict[str, Any]:
    """Verdict pour une candidate EN INCUBATION (wallet labo), selon §2.1/§2.2 (Porte 2) et
    §3.2 (mort automatique à 56j) de `docs/PROMOTION-RULES.md`."""
    if age_days is None:
        return {
            "verdict": "ALERTE",
            "reasons": [
                "entered_at manquant ou invalide dans INCUBATING_STRATEGIES — traçabilité de "
                "la fenêtre d'incubation rompue (bot/config.py, bandeau INCUBATING_STRATEGIES) "
                "— à corriger avant toute décision de promotion"
            ],
        }

    verdict = "OK"
    reasons: List[str] = []

    if age_days > INCUBATION_MAX_DAYS:
        verdict = _escalate(verdict, "ALERTE")
        reasons.append(
            f"incubation depuis {age_days}j > {INCUBATION_MAX_DAYS}j — dépasse la fenêtre "
            "maximale §3.2, candidate à tuer selon la prochaine session de recherche"
        )
    elif age_days > INCUBATION_WATCH_DAYS:
        verdict = _escalate(verdict, "SURVEILLER")
        reasons.append(
            f"incubation depuis {age_days}j, approche de la limite {INCUBATION_MAX_DAYS}j "
            f"(§3.2) — seuil de vigilance interne {INCUBATION_WATCH_DAYS}j (heuristique, PAS "
            "un seuil PROMOTION-RULES)"
        )

    if n_days_observed < INCUBATION_MIN_OBSERVATION_DAYS:
        verdict = _escalate(verdict, "SURVEILLER")
        reasons.append(
            f"historique vécu {n_days_observed}j < {INCUBATION_MIN_OBSERVATION_DAYS}j (§2.1) "
            "— critères de cohérence Porte 2 pas encore évaluables"
        )
    else:
        if sharpe_ref is not None and sharpe_ref > 0 and sharpe_live is not None:
            ratio = sharpe_live / sharpe_ref
            if ratio < PORTE2_MIN_SHARPE_RATIO:
                verdict = _escalate(verdict, "ALERTE")
                reasons.append(
                    f"Sharpe vécu {sharpe_live:.2f} < {PORTE2_MIN_SHARPE_RATIO:.0%} du Sharpe "
                    f"OOS annoncé ({sharpe_ref:.2f}) — cohérence Porte 2 §2.2 non satisfaite"
                )
        if dd_ref_pct is not None and dd_ref_pct > 0 and dd_live_pct is not None:
            ratio = dd_live_pct / dd_ref_pct
            if ratio > PORTE2_MAX_DD_MULTIPLE:
                verdict = _escalate(verdict, "ALERTE")
                reasons.append(
                    f"drawdown vécu {dd_live_pct:.1f}% > {PORTE2_MAX_DD_MULTIPLE:.1f}x le DD "
                    f"attendu ({dd_ref_pct:.1f}%) — cohérence Porte 2 §2.2 non satisfaite"
                )

    if not reasons:
        reasons.append("aucun signal de dérive détecté")
    return {"verdict": verdict, "reasons": reasons}


# ==========================================================================================
# --- Assemblage des lignes du rapport ---
# ==========================================================================================


def iter_active_strategy_targets() -> List[Tuple[str, str]]:
    """`[(strategy_id, wallet_id), ...]` pour chaque poche non-cash des 3 wallets réels."""
    targets: List[Tuple[str, str]] = []
    for wallet_id in config.PRODUCTION_WALLET_IDS:
        wallet = config.wallet_config(wallet_id)
        for pocket in wallet["pockets"]:
            ref = pocket.get("strategy_ref")
            if ref:
                targets.append((ref, wallet_id))
    return targets


def build_drift_rows(repo_dir: str, registry: dict, now: datetime) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    for strategy_id, wallet_id in iter_active_strategy_targets():
        journaux = load_wallet_journals(repo_dir, wallet_id)
        live = compute_live_return_stats(journaux, strategy_id)
        entry = registry_entry(registry, strategy_id)
        ref = reference_metrics_for(strategy_id, wallet_id, entry)
        verdict = classify_active_strategy_drift(
            sharpe_live=live["sharpe_live"],
            dd_live_pct=live["dd_live_pct"],
            sharpe_ref=ref["sharpe_ref"],
            dd_ref_pct=ref["dd_ref_pct"],
            n_days_observed=live["n_days_observed"],
            negative_rolling_streak_days=live["negative_rolling_streak_days"],
        )
        rows.append(
            {
                "categorie": "active",
                "strategy_id": strategy_id,
                "wallet_id": wallet_id,
                "antecedent_hors_promotion_rules": strategy_id in STRATEGIES_ANTECEDENT_HORS_PROMOTION_RULES,
                **live,
                **ref,
                **verdict,
            }
        )

    for candidate in config.INCUBATING_STRATEGIES:
        strategy_id = candidate.get("id", "?")
        wallet_id = config.LABO_WALLET_ID
        journaux = load_wallet_journals(repo_dir, wallet_id)
        live = compute_live_return_stats(journaux, strategy_id)
        entry = registry_entry(registry, strategy_id)
        ref = reference_metrics_for(strategy_id, wallet_id, entry)
        age_days = _age_days(candidate.get("entered_at"), now)
        verdict = classify_incubating_drift(
            age_days=age_days,
            sharpe_live=live["sharpe_live"],
            dd_live_pct=live["dd_live_pct"],
            sharpe_ref=ref["sharpe_ref"],
            dd_ref_pct=ref["dd_ref_pct"],
            n_days_observed=live["n_days_observed"],
        )
        rows.append(
            {
                "categorie": "incubation",
                "strategy_id": strategy_id,
                "wallet_id": wallet_id,
                "age_days": age_days,
                "antecedent_hors_promotion_rules": False,
                **live,
                **ref,
                **verdict,
            }
        )

    return rows


# ==========================================================================================
# --- Recalibrage encadré : quasi_passif_crypto / REGIME_SMA_DAYS ---
# ==========================================================================================

# Grille pré-enregistrée (docs/RECALIBRATION-SPEC.md) -- écrite AVANT toute exécution réelle
# contre des données rafraîchies. JAMAIS élargie/resserrée après coup sans une session dédiée.
RECALIBRATION_GRIDS: Dict[str, Dict[str, List[int]]] = {
    "quasi_passif_crypto": {"regime_sma_days": [150, 175, 200, 225, 250]},
}
MIN_RELATIVE_IMPROVEMENT = 0.10  # mission point b) -- amélioration OOS relative > 10%
RECAL_IS_DAYS = 270   # ~9 mois -- convention crypto déjà utilisée par le projet (§1.1)
RECAL_OOS_DAYS = 90   # ~3 mois
RECAL_WALLET_ID = "equilibre"  # univers de référence (6 majors) pour le recalibrage


def validate_param_in_grid(strategy_id: str, param_name: str, value: Any) -> None:
    """Lève `ValueError` si `value` n'appartient pas à la grille pré-enregistrée -- jamais de
    valeur hors-grille, structurellement (docs/PROMOTION-RULES.md §1.1)."""
    grid = RECALIBRATION_GRIDS.get(strategy_id, {}).get(param_name)
    if grid is None:
        raise ValueError(f"aucune grille pré-enregistrée pour {strategy_id}.{param_name}")
    if value not in grid:
        raise ValueError(
            f"valeur {value!r} hors de la grille pré-enregistrée {grid} pour "
            f"{strategy_id}.{param_name} — refusé (docs/RECALIBRATION-SPEC.md)"
        )


def decide_recalibration(
    strategy_id: str,
    param_name: str,
    current_value: Any,
    oos_sharpe_by_value: Dict[Any, float],
) -> Dict[str, Any]:
    """Décide s'il faut changer `current_value` en la meilleure valeur de la grille, selon le
    seuil `MIN_RELATIVE_IMPROVEMENT`. Fonction PURE (aucune écriture) -- `apply_recalibration_
    to_files` applique le changement séparément si `result["changed"]` est vrai."""
    validate_param_in_grid(strategy_id, param_name, current_value)
    for v in oos_sharpe_by_value:
        validate_param_in_grid(strategy_id, param_name, v)
    if current_value not in oos_sharpe_by_value:
        raise ValueError(
            "Sharpe OOS de la valeur actuellement en production manquant du résultat du "
            "walk-forward — impossible de comparer, refus par prudence"
        )

    current_sharpe = oos_sharpe_by_value[current_value]
    best_value = max(oos_sharpe_by_value, key=lambda v: oos_sharpe_by_value[v])
    best_sharpe = oos_sharpe_by_value[best_value]

    result: Dict[str, Any] = {
        "strategy_id": strategy_id,
        "param_name": param_name,
        "current_value": current_value,
        "current_oos_sharpe": current_sharpe,
        "best_value": best_value,
        "best_oos_sharpe": best_sharpe,
        "changed": False,
        "relative_improvement": None,
        "reason": "",
    }

    if best_value == current_value:
        result["reason"] = "la valeur actuellement en production reste optimale sur la grille — aucun changement"
        return result

    if current_sharpe <= 0:
        result["reason"] = (
            f"Sharpe OOS actuel non positif ({current_sharpe:.3f}) — amélioration relative non "
            "mesurable de façon fiable, refus automatique par prudence (revue humaine requise)"
        )
        return result

    relative_improvement = (best_sharpe - current_sharpe) / abs(current_sharpe)
    result["relative_improvement"] = relative_improvement
    if relative_improvement > MIN_RELATIVE_IMPROVEMENT:
        result["changed"] = True
        result["reason"] = (
            f"amélioration OOS relative {relative_improvement:.1%} > seuil "
            f"{MIN_RELATIVE_IMPROVEMENT:.0%} — changement appliqué"
        )
    else:
        result["reason"] = (
            f"amélioration OOS relative {relative_improvement:.1%} <= seuil "
            f"{MIN_RELATIVE_IMPROVEMENT:.0%} — pas assez significatif, aucun changement"
        )
    return result


def simulate_daily_returns(
    universe: Sequence[str],
    history: Dict[str, pd.DataFrame],
    sma_days: int,
    risque: dict,
    fee_slippage_bps_by_symbol: Dict[str, float],
) -> pd.Series:
    """Simule les rendements quotidiens NETS d'un portefeuille "quasi-passif" (long/flat,
    filtre de tendance SMA `sma_days` + vol-targeting), pour UNE valeur de `sma_days`.

    Réutilise `bot.strategies.quasi_passif_crypto._daily_closes()` (même définition exacte de
    "jour calendaire complet" que la production) pour le filtre de tendance. SIMPLIFIE en
    revanche le calcul de la vol réalisée par rapport à `_basket_vol_annualized` (production) :
    EWM sur le rendement quotidien MOYEN de tout l'univers (pas seulement les actifs "on" ce
    jour-là), pour rester vectorisable/rapide sur plusieurs années de données x 5 valeurs de
    grille x plusieurs fenêtres walk-forward. Cette simplification est appliquée IDENTIQUEMENT
    à chaque valeur de la grille testée : elle n'invalide donc pas la comparaison RELATIVE entre
    elles, seul usage fait de ce simulateur (jamais un chiffre de performance affiché comme
    définitif -- cf. `docs/RECALIBRATION-SPEC.md` §2).

    Ne touche à AUCUN paramètre du cadre de risque (`vol_target_annualized`, `gross_exposure_
    max`, `cap_per_asset`, `vol_ewma_halflife_hours`) -- lus tels quels depuis `risque`, jamais
    optimisés (docs/PROMOTION-RULES.md §4.3).
    """
    closes: Dict[str, pd.Series] = {}
    for s in universe:
        dc = _daily_closes(history.get(s))
        if not dc.empty:
            closes[s] = dc
    if not closes:
        return pd.Series(dtype=float)

    frame = pd.DataFrame(closes).sort_index()
    sma = frame.rolling(window=int(sma_days), min_periods=int(sma_days)).mean()
    trend_on = (frame > sma).fillna(False)

    asset_returns = frame.pct_change()
    asset_returns_filled = asset_returns.fillna(0.0)

    vol_target = float(risque["vol_target_annualized"])
    gross_exposure_max = float(risque["gross_exposure_max"])
    cap_per_asset = float(risque["cap_per_asset"])
    halflife_days = max(1.0, float(risque["vol_ewma_halflife_hours"]) / 24.0)

    universe_mean_return = asset_returns.mean(axis=1)
    vol_ewm_daily = universe_mean_return.ewm(halflife=halflife_days, adjust=False).std(bias=False)
    vol_annualized = (vol_ewm_daily * math.sqrt(365.0)).shift(1)  # connue à J-1, pas de look-ahead

    # `fill_value=False` (plutôt que `.shift(1).fillna(False)`) évite que pandas ne promeuve les
    # colonnes booléennes en dtype `object` à cause du NaN introduit par un `.shift()` classique
    # sur un DataFrame bool -- sans ce détail, les opérations arithmétiques en aval (`.where()`,
    # `.sum()`) produisent silencieusement un dtype `object` au lieu de `float64`.
    eligible_yesterday = trend_on.shift(1, fill_value=False)
    n_eligible_yesterday = eligible_yesterday.sum(axis=1).astype(float)

    poids_brut = (vol_target / vol_annualized).clip(upper=gross_exposure_max)
    poids_brut = poids_brut.clip(lower=0.0).fillna(0.0)

    n_eligible_safe = n_eligible_yesterday.where(n_eligible_yesterday > 0, 1.0)
    per_asset_all = poids_brut.div(n_eligible_safe)
    per_asset_all = per_asset_all.where(n_eligible_yesterday > 0, 0.0)

    weights = pd.DataFrame(0.0, index=frame.index, columns=frame.columns)
    for sym in frame.columns:
        weights[sym] = per_asset_all.where(eligible_yesterday[sym], 0.0).clip(upper=cap_per_asset)

    gross_return = (weights * asset_returns_filled).sum(axis=1)

    turnover = weights.diff().abs().fillna(0.0)
    cost_bps_series = pd.Series({s: float(fee_slippage_bps_by_symbol.get(s, 25.0)) for s in frame.columns})
    cost = turnover.mul(cost_bps_series, axis=1).sum(axis=1) / 10_000.0

    net_return = (gross_return - cost).dropna()
    return net_return


def walk_forward_windows(
    index: pd.Index, is_days: int, oos_days: int
) -> List[Tuple[pd.Index, pd.Index]]:
    """Fenêtres glissantes NON chevauchantes (bloc IS puis bloc OOS consécutifs, avancées d'un
    bloc OOS à chaque itération) -- même convention que les 8 backtests walk-forward déjà menés
    sur ce projet ("9m IS / 3m OOS, N fenêtres", `docs/RESEARCH-REGISTRY.json`)."""
    windows: List[Tuple[pd.Index, pd.Index]] = []
    n = len(index)
    start = 0
    while start + is_days + oos_days <= n:
        is_idx = index[start:start + is_days]
        oos_idx = index[start + is_days:start + is_days + oos_days]
        windows.append((is_idx, oos_idx))
        start += oos_days
    return windows


def walk_forward_select_and_compare(
    returns_by_value: Dict[Any, pd.Series],
    current_value: Any,
    is_days: int = RECAL_IS_DAYS,
    oos_days: int = RECAL_OOS_DAYS,
) -> Dict[str, Any]:
    """Walk-forward IS/OOS glissant sur `returns_by_value` (une série de rendements quotidiens
    par valeur de grille, déjà simulée par `simulate_daily_returns`). Pour chaque fenêtre,
    sélectionne en IS la valeur au meilleur Sharpe (vote), puis concatène les rendements OOS de
    CHAQUE valeur sur toute la période pour calculer son Sharpe OOS -- c'est ce Sharpe OOS
    concaténé par valeur qui alimente `decide_recalibration` (pas seulement la valeur la plus
    souvent votée en IS, qui reste journalisée à titre informatif : `modal_value`)."""
    common_index: Optional[pd.Index] = None
    for series in returns_by_value.values():
        idx = series.dropna().index
        common_index = idx if common_index is None else common_index.intersection(idx)

    if common_index is None or len(common_index) == 0:
        return {"status": "DONNEES_INSUFFISANTES", "windows": 0, "n_days_available": 0}

    common_index = common_index.sort_values()
    windows = walk_forward_windows(common_index, is_days, oos_days)
    if not windows:
        return {
            "status": "DONNEES_INSUFFISANTES",
            "windows": 0,
            "n_days_available": len(common_index),
        }

    votes: Dict[Any, int] = {v: 0 for v in returns_by_value}
    oos_returns_by_value: Dict[Any, List[float]] = {v: [] for v in returns_by_value}

    for is_idx, oos_idx in windows:
        is_sharpes = {
            v: sharpe_from_daily_returns(list(returns_by_value[v].reindex(is_idx).dropna()))
            for v in returns_by_value
        }
        is_sharpes = {v: s for v, s in is_sharpes.items() if s is not None}
        if is_sharpes:
            winner = max(is_sharpes, key=lambda v: is_sharpes[v])
            votes[winner] += 1
        for v in returns_by_value:
            oos_returns_by_value[v].extend(list(returns_by_value[v].reindex(oos_idx).dropna()))

    oos_sharpe_by_value = {
        v: sharpe_from_daily_returns(rs) for v, rs in oos_returns_by_value.items()
    }
    oos_sharpe_by_value = {v: s for v, s in oos_sharpe_by_value.items() if s is not None}

    if current_value not in oos_sharpe_by_value:
        return {"status": "DONNEES_INSUFFISANTES", "windows": len(windows)}

    modal_value = max(votes, key=lambda v: votes[v]) if any(votes.values()) else current_value

    return {
        "status": "OK",
        "windows": len(windows),
        "votes": votes,
        "oos_sharpe_by_value": oos_sharpe_by_value,
        "modal_value": modal_value,
    }


def load_hourly_history_from_staging(staging_dir: str, symbol: str) -> Optional[pd.DataFrame]:
    """Charge l'historique horaire d'un symbole crypto tel qu'écrit par `tools/fetch_data.py`
    (`data/crypto/{SYMBOLE}.csv.gz`, colonnes `timestamp,open,high,low,close,volume`)."""
    path = os.path.join(staging_dir, "data", "crypto", f"{symbol}.csv.gz")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    if "timestamp" not in df.columns:
        return None
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            return None
    return df[["open", "high", "low", "close", "volume"]]


def refresh_backtest_data(repo_dir: str, staging_dir: str) -> str:
    """Rafraîchit les données crypto locales en réutilisant `tools/fetch_data.py`
    (`--only crypto --skip-git` : la publication sur la branche `market-data` reste la
    responsabilité du workflow dédié `fetch-data.yml`). Retourne `'OK'` | `'ERROR'` -- ne lève
    jamais d'exception (posture pessimiste : un échec de rafraîchissement fait sauter le
    recalibrage de ce cycle, jamais planter tout le job)."""
    try:
        import tools.fetch_data as fetch_data
    except Exception as exc:  # noqa: BLE001
        logger.warning("refresh_backtest_data: import de tools.fetch_data impossible (%s)", exc)
        return "ERROR"
    try:
        rc = fetch_data.main(
            [
                "--only", "crypto",
                "--skip-git",
                "--staging-dir", staging_dir,
                "--repo-dir", repo_dir,
            ]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("refresh_backtest_data: échec du rafraîchissement (%s)", exc)
        return "ERROR"
    return "OK" if rc == 0 else "ERROR"


def run_recalibration(repo_dir: str, staging_dir: str) -> Dict[str, Any]:
    """Orchestre le recalibrage encadré de `quasi_passif_crypto.REGIME_SMA_DAYS`. Retourne un
    dict avec au moins `status` in {"OK", "DONNEES_INSUFFISANTES", "ERREUR"}."""
    strategy_id = "quasi_passif_crypto"
    param_name = "regime_sma_days"
    grid = RECALIBRATION_GRIDS[strategy_id][param_name]
    current_value = config.REGIME_SMA_DAYS

    universe = SPEC_UNIVERSE_BY_WALLET[RECAL_WALLET_ID]
    risque = config.wallet_config(RECAL_WALLET_ID)["risque"]

    history: Dict[str, pd.DataFrame] = {}
    for sym in universe:
        h = load_hourly_history_from_staging(staging_dir, sym)
        if h is not None:
            history[sym] = h

    missing = sorted(set(universe) - set(history))
    if missing:
        logger.warning("run_recalibration: données manquantes pour %s — recalibrage sauté", missing)
        return {"status": "DONNEES_INSUFFISANTES", "missing_symbols": missing}

    fee_slippage_bps = {
        sym: (
            config.COST_TIER_FEE_TAKER_BPS[config.cost_tier_of(sym)]
            + config.COST_TIER_SLIPPAGE_PENALTY_BPS[config.cost_tier_of(sym)]
        )
        for sym in universe
    }

    returns_by_value = {
        v: simulate_daily_returns(universe, history, v, risque, fee_slippage_bps) for v in grid
    }

    wf = walk_forward_select_and_compare(returns_by_value, current_value)
    if wf["status"] != "OK":
        return wf

    decision = decide_recalibration(strategy_id, param_name, current_value, wf["oos_sharpe_by_value"])
    decision.update(
        {
            "status": "OK",
            "windows": wf["windows"],
            "votes": wf["votes"],
            "modal_value": wf["modal_value"],
        }
    )
    return decision


def apply_recalibration_to_files(repo_dir: str, old_value: int, new_value: int) -> List[str]:
    """Applique le changement de `REGIME_SMA_DAYS` dans `bot/config.py` ET `bot/strategies/
    quasi_passif_crypto.py` (les deux constantes doivent rester synchronisées -- la stratégie
    lit sa PROPRE copie locale, pas celle de `bot/config.py`, cf. `docs/RECALIBRATION-SPEC.md`
    §1). Refuse (lève `ValueError`) si le motif attendu n'est pas trouvé EXACTEMENT une fois
    dans un fichier -- jamais d'écrasement à l'aveugle si le code source a dérivé de ce qui
    était attendu."""
    changed_files: List[str] = []
    targets = [
        "bot/config.py",
        "bot/strategies/quasi_passif_crypto.py",
    ]
    pattern = re.compile(r"(?m)^REGIME_SMA_DAYS = %d\s*$" % int(old_value))
    replacement = f"REGIME_SMA_DAYS = {int(new_value)}"

    for relpath in targets:
        full = os.path.join(repo_dir, relpath)
        with open(full, "r", encoding="utf-8") as f:
            text = f.read()
        matches = pattern.findall(text)
        if len(matches) != 1:
            raise ValueError(
                f"apply_recalibration_to_files: motif REGIME_SMA_DAYS = {old_value} trouvé "
                f"{len(matches)} fois dans {relpath} (attendu exactement 1) — refus d'appliquer "
                "un changement ambigu"
            )
        new_text = pattern.sub(replacement, text, count=1)
        with open(full, "w", encoding="utf-8") as f:
            f.write(new_text)
        changed_files.append(relpath)
    return changed_files


# ==========================================================================================
# --- Rendu du rapport ---
# ==========================================================================================


def _fmt_num(x: Optional[float], digits: int = 2, suffix: str = "") -> str:
    if x is None:
        return "n/d"
    return f"{x:.{digits}f}{suffix}"


def _render_row(row: Dict[str, Any]) -> str:
    return (
        f"| {row['strategy_id']} | {row['wallet_id']} | {row['categorie']} | "
        f"{row['n_days_observed']} | {_fmt_num(row.get('sharpe_live'))} | "
        f"{_fmt_num(row.get('sharpe_ref'))} | {_fmt_num(row.get('dd_live_pct'), 1, '%')} | "
        f"{_fmt_num(row.get('dd_ref_pct'), 1, '%')} | **{row['verdict']}** |"
    )


def render_drift_report(
    rows: List[Dict[str, Any]],
    generated_at: datetime,
    recalibration: Optional[Dict[str, Any]],
    data_refresh_status: str,
) -> str:
    lines: List[str] = []
    lines.append("# DRIFT-REPORT.md — Moniteur de dérive (backtest vs vécu)")
    lines.append("")
    lines.append(
        f"*Généré automatiquement par `tools/weekly_maintenance.py` le "
        f"{generated_at.isoformat()}. Ce document NE PREND AUCUNE DÉCISION — il signale. Les "
        "décisions de promotion, rétrogradation ou mort appartiennent exclusivement à une "
        "session de recherche hebdomadaire humaine, suivant `docs/PROMOTION-RULES.md`.*"
    )
    lines.append("")
    lines.append("## 1. Moniteur de dérive par stratégie")
    lines.append("")
    lines.append(
        "Compare les métriques VÉCUES (journaux `state/wallets/*/`) aux métriques OOS de "
        "référence (`docs/RESEARCH-REGISTRY.json`, elles-mêmes issues des `results.json` des "
        "backtests audités). Verdict classé selon les seuils chiffrés des RÈGLES DE MORT de "
        "`docs/PROMOTION-RULES.md` (§2.1/§2.2 pour les candidates en incubation, §3.1/§3.2 pour "
        "les stratégies actives)."
    )
    lines.append("")

    if not rows:
        lines.append("_Aucune stratégie active ni candidate en incubation à surveiller._")
    else:
        lines.append(
            "| Stratégie | Wallet | Statut | Jours observés | Sharpe vécu | Sharpe attendu | "
            "DD vécu | DD attendu | Verdict |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for row in rows:
            lines.append(_render_row(row))
        lines.append("")
        lines.append("### Détail des raisons")
        lines.append("")
        for row in rows:
            footnote = " _(antécédent hors §3, cf. PROMOTION-RULES.md §5 — informatif)_" if row.get(
                "antecedent_hors_promotion_rules"
            ) else ""
            lines.append(f"- **{row['strategy_id']}** ({row['wallet_id']}, {row['categorie']}) — **{row['verdict']}**{footnote}")
            for reason in row.get("reasons", []):
                lines.append(f"  - {reason}")

    lines.append("")
    lines.append(
        "*Note : `xs_momentum_sp100`, `dual_momentum_multiclasse_etf` et `quasi_passif_crypto` "
        "sont un antécédent explicitement HORS du cadre formel §3 de `PROMOTION-RULES.md` "
        "(cf. §5) — leur verdict ci-dessus reste informatif (« si cette règle s'appliquait ») "
        "et ne déclenche aucune rétrogradation automatique.*"
    )
    lines.append("")
    lines.append("## 2. Recalibrage encadré — quasi-passif crypto")
    lines.append("")
    lines.append(
        f"Rafraîchissement des données de marché (`tools/fetch_data.py --only crypto`) : "
        f"**{data_refresh_status}**."
    )
    lines.append("")
    lines.append(
        "Grille pré-enregistrée (`docs/RECALIBRATION-SPEC.md`) : "
        f"`REGIME_SMA_DAYS ∈ {RECALIBRATION_GRIDS['quasi_passif_crypto']['regime_sma_days']}` "
        f"(seuil de changement : amélioration OOS relative > {MIN_RELATIVE_IMPROVEMENT:.0%})."
    )
    lines.append("")

    if recalibration is None:
        lines.append("_Recalibrage non exécuté ce cycle (`--skip-recalibration`)._")
    elif recalibration.get("status") == "DONNEES_INSUFFISANTES":
        lines.append(
            "_Recalibrage SAUTÉ : données insuffisantes ou indisponibles ce cycle "
            f"({recalibration.get('missing_symbols') or recalibration.get('detail') or 'historique trop court'})._"
        )
    elif recalibration.get("status") == "ERREUR":
        lines.append(f"_Recalibrage SAUTÉ suite à une erreur : {recalibration.get('detail')}._")
    else:
        lines.append(
            f"- Fenêtres walk-forward (9m IS / 3m OOS) : **{recalibration.get('windows')}**"
        )
        lines.append(
            f"- Valeur en production : `REGIME_SMA_DAYS = {recalibration.get('current_value')}` "
            f"(Sharpe OOS concaténé : {_fmt_num(recalibration.get('current_oos_sharpe'), 3)})"
        )
        lines.append(
            f"- Meilleure valeur de la grille : `REGIME_SMA_DAYS = {recalibration.get('best_value')}` "
            f"(Sharpe OOS concaténé : {_fmt_num(recalibration.get('best_oos_sharpe'), 3)})"
        )
        lines.append(f"- Valeur la plus souvent sélectionnée en IS (informatif) : `{recalibration.get('modal_value')}`")
        rel = recalibration.get("relative_improvement")
        lines.append(f"- Amélioration relative : {_fmt_num(rel * 100 if rel is not None else None, 1, '%')}")
        lines.append(f"- **Décision : {'CHANGEMENT APPLIQUÉ' if recalibration.get('changed') else 'aucun changement'}**")
        lines.append(f"  - {recalibration.get('reason')}")

    lines.append("")
    return "\n".join(lines)


# ==========================================================================================
# --- main() ---
# ==========================================================================================


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", default=_REPO_ROOT, help="racine du dépôt git (défaut : parent de tools/)")
    parser.add_argument("--skip-pull", action="store_true", help="ne fait pas de git pull --rebase avant de commencer")
    parser.add_argument("--skip-push", action="store_true", help="n'effectue aucun commit/push (dry-run local)")
    parser.add_argument("--skip-recalibration", action="store_true", help="désactive le recalibrage encadré (moniteur de dérive seul)")
    parser.add_argument("--skip-data-refresh", action="store_true", help="ne rafraîchit pas les données de marché (le recalibrage sera alors sauté faute de données)")
    parser.add_argument("--staging-dir", default=None, help="répertoire de staging pour les données de marché (défaut : dossier temporaire)")
    args = parser.parse_args(argv)

    repo_dir = args.repo_dir
    now = datetime.now(timezone.utc)

    if not args.skip_pull:
        pull_result = pull_rebase(repo_dir)
        logger.info("pull_rebase: %s", pull_result)

    registry = load_registry(repo_dir)
    rows = build_drift_rows(repo_dir, registry, now)

    recalibration: Optional[Dict[str, Any]] = None
    data_refresh_status = "sauté (--skip-recalibration)"
    changed_recal_files: List[str] = []

    if not args.skip_recalibration:
        staging_dir = args.staging_dir or tempfile.mkdtemp(prefix="weekly_maintenance_staging_")
        if args.skip_data_refresh:
            data_refresh_status = "sauté (--skip-data-refresh)"
        else:
            data_refresh_status = refresh_backtest_data(repo_dir, staging_dir)

        if data_refresh_status == "OK":
            try:
                recalibration = run_recalibration(repo_dir, staging_dir)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "run_recalibration: échec inattendu (%s) — aucun changement appliqué "
                    "(posture pessimiste)", exc,
                )
                recalibration = {"status": "ERREUR", "detail": str(exc)}
        else:
            recalibration = {
                "status": "DONNEES_INSUFFISANTES",
                "detail": f"rafraîchissement des données : {data_refresh_status}",
            }

        if recalibration.get("status") == "OK" and recalibration.get("changed"):
            try:
                changed_recal_files = apply_recalibration_to_files(
                    repo_dir, recalibration["current_value"], recalibration["best_value"]
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("apply_recalibration_to_files: échec (%s) — recalibrage NON appliqué", exc)
                recalibration["changed"] = False
                recalibration["reason"] = f"{recalibration.get('reason', '')} [ÉCHEC APPLICATION: {exc}]"
                changed_recal_files = []

    report_md = render_drift_report(rows, now, recalibration, data_refresh_status)
    report_path = os.path.join(repo_dir, DRIFT_REPORT_RELPATH)
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_md)
    logger.info("rapport écrit dans %s", report_path)

    if args.skip_push:
        logger.info("--skip-push : aucun commit/push (dry-run)")
        return 0

    # Commit 1 (DÉDIÉ) : recalibrage, uniquement s'il y a eu un changement réel.
    if changed_recal_files:
        rc = recalibration
        message = (
            f"Recalibrage automatique quasi_passif_crypto : REGIME_SMA_DAYS "
            f"{rc['current_value']} -> {rc['best_value']} "
            f"(Sharpe OOS {_fmt_num(rc['current_oos_sharpe'], 3)} -> {_fmt_num(rc['best_oos_sharpe'], 3)}, "
            f"amélioration relative {_fmt_num(rc['relative_improvement'] * 100 if rc.get('relative_improvement') is not None else None, 1)}%, "
            f"{rc['windows']} fenêtre(s) walk-forward, semaine du {now.strftime('%Y-%m-%d')})"
        )
        status = git_sync(repo_dir, message, paths=changed_recal_files, branch="main")
        logger.info("git_sync (recalibrage): %s", status)
        if status == "FAILED":
            logger.error("échec du commit/push du recalibrage — abandon avant le rapport de dérive")
            return 1
        if status != "ABORTED_DUPLICATE" and not args.skip_pull:
            pull_rebase(repo_dir)  # avant le second commit, cf. mission "pull --rebase avant chaque push"

    # Commit 2 : rapport de dérive (toujours produit, sauf si rien n'a changé dans le fichier).
    if has_uncommitted_state_changes(repo_dir, paths=[DRIFT_REPORT_RELPATH]):
        message = (
            f"Maintenance hebdomadaire {now.strftime('%Y-%m-%d')} : rapport de dérive"
            + (" + recalibrage quasi_passif_crypto" if changed_recal_files else "")
        )
        status = git_sync(repo_dir, message, paths=[DRIFT_REPORT_RELPATH], branch="main")
        logger.info("git_sync (rapport de dérive): %s", status)
        return 0 if status in ("SUCCESS", "ABORTED_DUPLICATE") else 1

    logger.info("aucun changement dans %s — rien à committer", DRIFT_REPORT_RELPATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
