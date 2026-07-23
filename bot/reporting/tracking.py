"""bot/reporting/tracking.py — suivi de confrontation backtest-vs-vécu (§ Labo, mission
auto-amélioration, docs/ARCHITECTURE.md § Labo).

Rôle : donner à CHAQUE stratégie (incubée dans le labo OU active dans un wallet réel) un jeu de
métriques VÉCUES (PnL cumulé net, nombre de trades, exposition moyenne), calculées à partir des
journaux réellement committés (`trades.jsonl`/`decisions.jsonl`/`equity.jsonl`), jamais du
backtest qui a justifié son incubation/sa sélection. C'est la pièce centrale de la lutte contre
le SUR-APPRENTISSAGE : un backtest qui "a l'air bon" ne suffit jamais, seul le comportement
vécu, mesuré ici, compte pour une décision de promotion/retrait future.

`compute_live_metrics()` est une fonction PURE (aucun appel réseau, aucune lecture disque —
l'appelant lui fournit les journaux déjà chargés en mémoire) et RÉUTILISABLE tel quel par deux
consommateurs futurs distincts, explicitement visés par la mission :
  - un "moniteur de dérive" (compare les métriques vécues au comportement backtesté attendu,
    alerte en cas d'écart significatif) ;
  - les "sessions de recherche" (évaluent une candidate en incubation avant une éventuelle
    décision de promotion).

Décision assumée, documentée (pas un oubli) : ce module n'est PAS câblé dans le cycle horaire de
production (`bot/runner.py:process_wallet`), qui reste une fonction quasi pure sans lecture
disque au-delà de ce qui lui est explicitement transmis par `bot/runner.py:main()` (contrat
documenté en tête de `bot/runner.py`). Calculer des métriques vécues exige de relire l'historique
COMPLET des journaux (potentiellement des milliers de lignes après plusieurs mois de cycles
horaires), ce qui romprait ce contrat de "pas de disque à l'intérieur du cycle par-wallet" pour
un besoin qui n'est PAS celui du cycle horaire lui-même (trader), mais celui d'un outillage
d'observabilité tourné en dehors du chemin critique. `update_strategy_state_live_metrics()`
reste néanmoins fourni ci-dessous pour qu'un futur script d'observabilité puisse persister ces
métriques dans `state["strategy_state"][strategy_id]["live_metrics"]`, au même endroit et avec
la même convention que le reste du `strategy_state` (docs/ARCHITECTURE.md §11.5), sans avoir à
réinventer cette convention.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

__all__ = ["compute_live_metrics", "update_strategy_state_live_metrics"]


def _as_records(value: Any) -> List[dict]:
    if not value:
        return []
    return [r for r in value if isinstance(r, dict)]


def compute_live_metrics(
    wallet_state: Optional[dict],
    journaux: Optional[Dict[str, Iterable[dict]]],
    strategy_id: str,
) -> Dict[str, Any]:
    """Métriques VÉCUES d'une stratégie (`strategy_id`) pour UN wallet, à partir de ses
    journaux déjà chargés.

    Paramètres :
      - `wallet_state` : `state/wallets/<id>/state.json` déjà chargé (ou `None`) — utilisé
        uniquement pour renseigner `wallet_id` dans le résultat (traçabilité), jamais pour
        recalculer une métrique à sa place (les journaux restent l'unique source de vérité des
        métriques vécues, cf. docstring de module).
      - `journaux` : dict optionnel avec jusqu'à 3 clés, chacune une liste de dicts au format
        JSONL réel du bot (docs/ARCHITECTURE.md §3.2-3.3, §10.4) :
          - `"trades"` : lignes de `trades.jsonl` (fills réellement exécutés) ;
          - `"decisions"` : lignes de `decisions.jsonl` (une ligne par run_id x symbole) ;
          - `"equity"` : lignes de `equity.jsonl` (une ligne par run_id, `exposures` incluses).
        Une clé absente ou vide est traitée comme "aucune donnée" (jamais une erreur) —
        posture pessimiste habituelle de ce dépôt (§0) : mieux vaut des métriques partielles
        honnêtement signalées (`n_cycles_observed` bas) qu'une exception qui casse un moniteur
        appelant.
      - `strategy_id` : `StrategyBase.name` de la stratégie (production OU candidate incubée).

    Méthode — AUCUNE hypothèse d'univers statique par stratégie n'est faite ici (une candidate
    en incubation peut voir son univers changer d'un cycle à l'autre en théorie) : l'attribution
    "ce symbole, ce cycle, appartient à `strategy_id`" est relue directement dans
    `decisions.jsonl["strategy_signals"]` (rempli par `bot/runner.py:_combine_pockets()` — la clé
    `strategy_id` n'y figure QUE pour les symboles où cette stratégie a effectivement émis un
    poids ce cycle-là), puis jointe à `trades.jsonl`/`equity.jsonl` par `(run_id, symbole)`.

    Retourne un dict avec (entre autres) `n_trades`, `realized_pnl_cumulative_usd`,
    `avg_exposure_pct` — les 3 métriques demandées par la mission — plus quelques champs de
    contexte utiles à un moniteur (`n_cycles_observed`, `first_run_id`, `last_run_id`).
    """
    journaux = journaux or {}
    decisions = _as_records(journaux.get("decisions"))
    trades = _as_records(journaux.get("trades"))
    equity = _as_records(journaux.get("equity"))

    # --- symboles attribués à strategy_id, PAR run_id (jamais supposé statique) ---
    symbols_by_run: Dict[str, set] = {}
    for d in decisions:
        signals = d.get("strategy_signals") or {}
        if not isinstance(signals, dict) or strategy_id not in signals:
            continue
        run_id = d.get("run_id")
        symbol = d.get("symbol")
        if run_id is None or symbol is None:
            continue
        symbols_by_run.setdefault(run_id, set()).add(symbol)

    run_ids_observed = sorted(symbols_by_run.keys())

    # --- trades imputables à strategy_id (même run_id, symbole attribué CE run) ---
    matched_trades = [
        t for t in trades
        if t.get("symbol") in symbols_by_run.get(t.get("run_id"), ())
    ]
    n_trades = len(matched_trades)
    realized_pnl_cumulative_usd = sum(
        float(t.get("realized_pnl_usd") or 0.0)
        for t in matched_trades
        if t.get("side") == "SELL"
    )

    # --- exposition moyenne : pour chaque run où le signal est présent ET l'equity disponible,
    # somme des expositions des symboles attribués ce run-là ---
    equity_by_run = {e.get("run_id"): e for e in equity}
    exposures_per_run: List[float] = []
    for run_id, symbols in symbols_by_run.items():
        rec = equity_by_run.get(run_id)
        if rec is None:
            continue
        exp_map = rec.get("exposures") or {}
        if not isinstance(exp_map, dict):
            continue
        exposures_per_run.append(sum(float(exp_map.get(s, 0.0) or 0.0) for s in symbols))

    avg_exposure_pct = (
        sum(exposures_per_run) / len(exposures_per_run) if exposures_per_run else 0.0
    )

    return {
        "strategy_id": strategy_id,
        "wallet_id": (wallet_state or {}).get("wallet_id"),
        "n_trades": n_trades,
        "realized_pnl_cumulative_usd": realized_pnl_cumulative_usd,
        "avg_exposure_pct": avg_exposure_pct,
        "n_cycles_observed": len(run_ids_observed),
        "n_cycles_with_exposure": len(exposures_per_run),
        "first_run_id": run_ids_observed[0] if run_ids_observed else None,
        "last_run_id": run_ids_observed[-1] if run_ids_observed else None,
    }


def update_strategy_state_live_metrics(
    state: dict,
    journaux: Optional[Dict[str, Iterable[dict]]],
    strategy_id: str,
) -> Dict[str, Any]:
    """Calcule `compute_live_metrics(state, journaux, strategy_id)` et le persiste EN PLACE dans
    `state["strategy_state"][strategy_id]["live_metrics"]` (même convention que les autres
    champs de `strategy_state`, cf. docs/ARCHITECTURE.md §11.5/§12.4) — puis retourne ce même
    dict de métriques.

    Mutation EN PLACE de `state` (comme `bot.strategies.apply_missing_data_policy`), jamais
    d'écriture disque ici : c'est à l'appelant de persister `state` via
    `bot.persist.save_state()` s'il souhaite conserver ce calcul (ex. un futur script de
    moniteur de dérive tournant hors du cycle horaire, cf. docstring de module)."""
    metrics = compute_live_metrics(state, journaux, strategy_id)

    if not isinstance(state, dict):
        return metrics

    strategy_state_all = state.get("strategy_state")
    if not isinstance(strategy_state_all, dict):
        strategy_state_all = {}
        state["strategy_state"] = strategy_state_all

    own_state = strategy_state_all.get(strategy_id)
    if not isinstance(own_state, dict):
        own_state = {}
        strategy_state_all[strategy_id] = own_state

    own_state["live_metrics"] = metrics
    return metrics
