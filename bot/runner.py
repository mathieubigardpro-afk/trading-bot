#!/usr/bin/env python3
"""bot/runner.py — point d'entrée du bot, orchestre le cycle horaire complet.

Séquence exacte (voir `docs/ARCHITECTURE.md` §6) :
  1. `pull_rebase` (repartir de l'état le plus récent poussé par un run précédent).
  2. Idempotence : `load_state()` + `compute_run_id()` ; sortie propre (code 0) si déjà traité.
  3. Détermination de l'univers actif (crypto toujours, actions si marché US ouvert).
  4. Récupération des prix réels (`get_prices`) — jamais de prix stocké arrangé.
  5. Récupération de l'historique (`get_history`) pour les symboles à prix disponible.
  6. Signaux bruts (`combine_strategies` / repli "aucune stratégie -> cibles = positions
     actuelles" si `bot/strategies/` ne contient encore aucune stratégie concrète).
  7. `RiskManager.apply()`.
  8. Génération des ordres via `ExchangeSim` + application au `Ledger`.
  9-11. Journalisation (`trades.jsonl`, `equity.jsonl`, `decisions.jsonl`), mise à jour des
     compteurs de circuit breakers stateful, calcul de l'équity de fin de cycle.
  12-14. Construction et écriture du nouveau `state.json` (dernier fichier écrit).
  15. `git_sync` — dernière étape du programme.

Principe cardinal pessimiste (ARCHITECTURE.md §0) : un symbole sans prix frais et valide ce
cycle (échec réseau, quote périmée, bid/ask invalide) NE TRADE JAMAIS ce cycle — son poids est
figé, jamais mis à zéro, jamais estimé depuis une source de repli non autorisée.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from bot import config
from bot.feeds import get_history, get_prices, is_us_market_open
from bot.feeds.types import HistoryUnavailableError, Quote
from bot.persist import (
    append_journal,
    compute_state_hash,
    git_sync,
    is_run_already_done,
    load_state,
    pull_rebase,
    save_state,
)
from bot.risk import RiskManager
from bot.sim.exchange import ExchangeSim
from bot.sim.fills import Fill, Reject
from bot.sim.ledger import Ledger
from bot.strategies import combine_strategies, load_strategies

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot.runner")

EPSILON_WEIGHT = 1e-9


def compute_run_id(now: Optional[datetime] = None) -> str:
    """ARCHITECTURE.md §4.1 — run_id horaire déterministe, ex. "2026-07-22T14"."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H")


def repo_dir() -> str:
    """Racine du dépôt (parent du paquet `bot/`)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _mark_price(symbol: str, quote: Optional[Quote], positions: dict) -> Optional[float]:
    """Prix de marque pour l'équity : mid frais si disponible, sinon dernier prix moyen connu
    (repli documenté, même logique que `bot.risk.RiskManager._mark_price` — jamais 0, jamais
    une supposition implicite). Retourne None si aucune des deux sources n'est exploitable."""
    if quote is not None:
        mid = getattr(quote, "mid", None)
        if mid is not None:
            try:
                mid_f = float(mid)
                if mid_f > 0:
                    return mid_f
            except (TypeError, ValueError):
                pass
    pos = positions.get(symbol)
    if pos is not None:
        prix_moyen = pos.get("prix_moyen")
        if prix_moyen:
            try:
                pm = float(prix_moyen)
                if pm > 0:
                    return pm
            except (TypeError, ValueError):
                return None
    return None


def _estimate_equity_and_weights(
    cash_usd: float, positions: dict, prices: Dict[str, Optional[Quote]]
) -> Tuple[float, Dict[str, float]]:
    """Réplique la logique de `bot.risk.RiskManager._estimate_equity_and_weights` (équity
    mark-to-market + poids actuels par symbole) pour que le runner décide des ordres de façon
    cohérente avec ce que `RiskManager.apply()` a utilisé en interne pour son propre calcul de
    drawdown / circuit breakers (même state, mêmes prices en entrée -> même résultat)."""
    marks: Dict[str, float] = {}
    total = float(cash_usd)
    for symbol, pos in positions.items():
        qty = float(pos.get("qty", 0.0) or 0.0)
        if qty <= 0:
            continue
        mark = _mark_price(symbol, prices.get(symbol), positions)
        if mark is None:
            continue
        marks[symbol] = mark
        total += qty * mark
    equity = max(total, 0.0)
    weights: Dict[str, float] = {}
    if equity > 0:
        for symbol, pos in positions.items():
            qty = float(pos.get("qty", 0.0) or 0.0)
            if qty <= 0 or symbol not in marks:
                continue
            weights[symbol] = (qty * marks[symbol]) / equity
    return equity, weights


def _cb_snapshot(cb_state: dict, now: datetime) -> dict:
    def _is_future(ts_key: str) -> bool:
        dt = _parse_ts(cb_state.get(ts_key))
        return dt is not None and now < dt

    return {
        "flatten_mode": bool(cb_state.get("flatten_mode", False)),
        "daily_loss_freeze": _is_future("daily_loss_freeze_until"),
        "cooldown": _is_future("cooldown_until"),
        "dd_half_size": bool(cb_state.get("dd_half_size_active", False)),
        # Filtre de régime SMA200/ATR14 non implémenté dans bot/risk (voir sa docstring) —
        # toujours False, documenté explicitement pour ne jamais laisser croire à une gate
        # silencieuse.
        "regime_gate_blocked": False,
    }


def main() -> int:
    now = datetime.now(timezone.utc)
    repo = repo_dir()

    # --- 1) pull_rebase : repartir de l'état le plus récent poussé, AVANT toute lecture ---
    pull_result = pull_rebase(repo)
    if pull_result != "SUCCESS":
        logger.warning(
            "pull_rebase a échoué (%s) — poursuite avec l'état local du clone tel quel "
            "(peut être légèrement en retard, jamais corrompu).",
            pull_result,
        )

    # --- 2) idempotence ---
    state = load_state()
    run_id = compute_run_id(now)
    if is_run_already_done(state, run_id):
        logger.info(
            "run déjà traité pour run_id=%s (last_run_id=%s) — abandon silencieux propre.",
            run_id, state.get("last_run_id"),
        )
        return 0

    prev_hash = compute_state_hash(state)
    logger.info("Démarrage du cycle run_id=%s (prev_hash=%s...)", run_id, prev_hash[:12])

    # --- 3) univers actif ce cycle ---
    market_open = is_us_market_open(now)
    crypto_symbols = list(config.SYMBOLS_CRYPTO)
    equity_symbols = list(config.SYMBOLS_EQUITY)
    full_universe = crypto_symbols + equity_symbols
    active_symbols = crypto_symbols + (equity_symbols if market_open else [])
    logger.info(
        "Univers actif ce cycle : %d crypto + %d action(s) (market_open=%s)",
        len(crypto_symbols), len(equity_symbols) if market_open else 0, market_open,
    )

    # --- 4) prix réels ---
    prices: Dict[str, Optional[Quote]] = get_prices(active_symbols) if active_symbols else {}
    for sym in full_universe:
        prices.setdefault(sym, None)

    # --- 5) historique (uniquement pour les symboles à prix disponible) ---
    history: Dict[str, "object"] = {}
    history_failed: set = set()
    for sym in active_symbols:
        if prices.get(sym) is None:
            continue
        try:
            history[sym] = get_history(sym, config.HISTORY_N_HOURS)
        except HistoryUnavailableError as exc:
            logger.warning("historique indisponible pour %s: %s", sym, exc)
            history_failed.add(sym)

    # --- équity / poids AVANT ce cycle (avant tout fill), utilisé pour le sizing des ordres ---
    equity_avant_cycle, poids_actuel = _estimate_equity_and_weights(
        state.get("cash_usd", 0.0), state.get("positions", {}) or {}, prices
    )

    # --- 6) signaux bruts ---
    strategies_actives = load_strategies()
    if strategies_actives:
        cibles_brutes, strategy_signals = combine_strategies(strategies_actives, history, state)
        logger.info("%d stratégie(s) active(s) : %s", len(strategies_actives), [s.name for s in strategies_actives])
    else:
        # Aucune stratégie concrète déposée dans bot/strategies/ (V1) : cibles brutes =
        # positions actuelles -> le bot tourne, évalue, journalise, ne trade jamais.
        cibles_brutes = dict(poids_actuel)
        strategy_signals = {}
        logger.info("Aucune stratégie active — cibles brutes = positions actuelles (no-op).")

    # --- 7) RiskManager (mute `state` en place : equity_peak_*, circuit_breakers) ---
    cibles_finales, reasons = RiskManager().apply(cibles_brutes, state, prices, history, now=now)

    # --- 8) génération des ordres ---
    ledger = Ledger(cash_usd=state.get("cash_usd", 0.0), positions=state.get("positions", {}) or {})
    exchange = ExchangeSim(
        fee_taker_bps=config.FEE_TAKER_BPS,
        slippage_penalty_bps=config.SLIPPAGE_PENALTY_BPS,
        max_quote_age_seconds=config.MAX_QUOTE_AGE_SECONDS,
        min_notional_usd=config.MIN_NOTIONAL_USD,
    )

    fills_this_cycle: List[Fill] = []
    rejects_this_cycle: List[Reject] = []
    decisions: List[dict] = []

    for symbol in full_universe:
        asset_class = "crypto" if symbol in crypto_symbols else "equity"
        current_w = poids_actuel.get(symbol, 0.0)

        # --- action hors heures de marché : jamais évaluée, position conservée telle quelle ---
        if asset_class == "equity" and not market_open:
            decisions.append({
                "run_id": run_id,
                "ts": now.isoformat(),
                "symbol": symbol,
                "asset_class": asset_class,
                "market_open": False,
                "quote_available": False,
                "quote_source": None,
                "price_mid_ideal": None,
                "quote_ts": None,
                "quote_age_seconds": None,
                "strategy_signals": {},
                "poids_cible_brut": None,
                "poids_cible_apres_risk": None,
                "poids_actuel": current_w,
                "decision": "NO_TRADE",
                "reason": (
                    "marché US fermé (hors 09:30-16:00 America/New_York, jour ouvré NYSE) — "
                    "position conservée, aucune évaluation de signal"
                ),
                "circuit_breakers_snapshot": None,
            })
            continue

        quote = prices.get(symbol)
        quote_available = quote is not None
        signals_for_symbol = {
            strat_name: sigs[symbol] for strat_name, sigs in strategy_signals.items() if symbol in sigs
        }
        cb_snapshot = _cb_snapshot(state.get("circuit_breakers") or {}, now)

        if not quote_available:
            decisions.append({
                "run_id": run_id,
                "ts": now.isoformat(),
                "symbol": symbol,
                "asset_class": asset_class,
                "market_open": True,
                "quote_available": False,
                "quote_source": None,
                "price_mid_ideal": None,
                "quote_ts": None,
                "quote_age_seconds": None,
                "strategy_signals": signals_for_symbol,
                "poids_cible_brut": None,
                "poids_cible_apres_risk": None,
                "poids_actuel": current_w,
                "decision": "NO_TRADE",
                "reason": (
                    "prix indisponible/périmé ce cycle (échec ou expiration des sources "
                    "primaire/fallback) — aucun trade sur cet actif ce cycle (garde-fou "
                    "pessimiste, position conservée)"
                ),
                "circuit_breakers_snapshot": cb_snapshot,
            })
            continue

        quote_ts_dt = _parse_ts(quote.ts)
        quote_age_seconds = (now - quote_ts_dt).total_seconds() if quote_ts_dt else None

        raw = cibles_brutes.get(symbol)
        final = cibles_finales.get(symbol, current_w)
        base_reason = reasons.get(symbol, "aucun ajustement de risque documenté pour cet actif")
        if symbol in history_failed:
            base_reason += " ; historique clôturé insuffisant/indisponible — signal non calculable ce cycle"

        decision = "NO_TRADE"
        reason = base_reason
        diff = final - current_w

        if abs(diff) > EPSILON_WEIGHT:
            side = "BUY" if diff > 0 else "SELL"
            delta_usd = abs(diff) * equity_avant_cycle
            qty = delta_usd / quote.mid if quote.mid else 0.0
            result = exchange.execute_order(
                side=side, symbol=symbol, qty=qty, quote=quote,
                strategy="ensemble", run_id=run_id, now=now,
            )
            if isinstance(result, Fill):
                ledger.apply_fill(result)
                fills_this_cycle.append(result)
                decision = side
                reason = f"{base_reason} ; ordre exécuté ({side} qty={result.qty:.8f}, notional={result.notional_usd:.2f}$)"
            else:
                rejects_this_cycle.append(result)
                decision = "NO_TRADE"
                reason = f"{base_reason} ; ordre {side} rejeté par ExchangeSim : {result.reason}"

        decisions.append({
            "run_id": run_id,
            "ts": now.isoformat(),
            "symbol": symbol,
            "asset_class": asset_class,
            "market_open": True,
            "quote_available": True,
            "quote_source": quote.source,
            "price_mid_ideal": quote.mid,
            "quote_ts": quote.ts,
            "quote_age_seconds": quote_age_seconds,
            "strategy_signals": signals_for_symbol,
            "poids_cible_brut": raw,
            "poids_cible_apres_risk": final,
            "poids_actuel": current_w,
            "decision": decision,
            "reason": reason,
            "circuit_breakers_snapshot": cb_snapshot,
        })

    # --- 10) mise à jour des compteurs de circuit breakers stateful (§5.3.1) ---
    cb_state = state.setdefault("circuit_breakers", {})
    consecutive_losses = int(cb_state.get("consecutive_losses", 0) or 0)
    trade_history = list(state.get("trade_history_for_breakers", []) or [])
    realized_pnl_cycle = 0.0

    for fill in fills_this_cycle:
        if fill.side != "SELL":
            continue
        pnl = float(fill.realized_pnl_usd or 0.0)
        realized_pnl_cycle += pnl
        if pnl < 0:
            consecutive_losses += 1
        else:
            consecutive_losses = 0
        trade_history.append({"ts": fill.ts, "symbol": fill.symbol, "realized_pnl_usd": pnl})

    trade_history = trade_history[-20:]
    cb_state["consecutive_losses"] = consecutive_losses
    if consecutive_losses >= config.CB_CONSECUTIVE_LOSSES_TRIGGER:
        cooldown_until = now + timedelta(hours=config.CB_COOLDOWN_HOURS)
        existing_cd = _parse_ts(cb_state.get("cooldown_until"))
        if existing_cd is None or cooldown_until > existing_cd:
            cb_state["cooldown_until"] = cooldown_until.isoformat()

    # --- 11) équity de fin de cycle (après fills) ---
    final_positions = ledger.positions
    mark_prices_final: Dict[str, float] = {}
    for symbol in final_positions:
        mark = _mark_price(symbol, prices.get(symbol), final_positions)
        if mark is None:
            # Ne devrait jamais arriver : toute position détenue a nécessairement un
            # prix_moyen > 0 (invariant du Ledger) — filet de sécurité explicite plutôt
            # qu'un crash silencieux d'equity().
            raise RuntimeError(
                f"impossible de marquer {symbol} en fin de cycle (ni mid frais ni prix_moyen)"
            )
        mark_prices_final[symbol] = mark

    equity_fin_cycle = ledger.equity(mark_prices_final)

    equity_peak = float(state.get("equity_peak_usd") or 0.0)
    equity_peak_ts = state.get("equity_peak_ts")
    if equity_fin_cycle > equity_peak:
        equity_peak = equity_fin_cycle
        equity_peak_ts = now.isoformat()

    drawdown_pct = 0.0
    if equity_peak > 0:
        drawdown_pct = max(0.0, min(1.0, (equity_peak - equity_fin_cycle) / equity_peak))

    exposures: Dict[str, float] = {}
    if equity_fin_cycle > 0:
        for symbol, pos in final_positions.items():
            exposures[symbol] = (pos["qty"] * mark_prices_final[symbol]) / equity_fin_cycle
    gross_exposure_pct = sum(abs(v) for v in exposures.values())

    circuit_breakers_active = [
        key for key, flag in (
            ("flatten_mode", cb_state.get("flatten_mode", False)),
            ("manual_review_required", cb_state.get("manual_review_required", False)),
            ("dd_half_size_active", cb_state.get("dd_half_size_active", False)),
        )
        if flag
    ]
    snap = _cb_snapshot(cb_state, now)
    if snap["daily_loss_freeze"]:
        circuit_breakers_active.append("daily_loss_freeze")
    if snap["cooldown"]:
        circuit_breakers_active.append("cooldown")

    # --- 13) construction du nouveau state ---
    new_state = {
        "schema_version": 1,
        "last_run_id": run_id,
        "last_run_completed_at": now.isoformat(),
        "state_hash_prev": prev_hash,
        "cash_usd": ledger.cash_usd,
        "positions": final_positions,
        "equity_peak_usd": equity_peak,
        "equity_peak_ts": equity_peak_ts,
        "realized_pnl_cumulative_usd": float(state.get("realized_pnl_cumulative_usd", 0.0) or 0.0)
        + realized_pnl_cycle,
        "circuit_breakers": cb_state,
        "trade_history_for_breakers": trade_history,
    }

    # --- 14) écritures disque, dans l'ordre : trades -> equity -> decisions -> state ---
    for fill in fills_this_cycle:
        record = dataclasses.asdict(fill)
        record["cash_after_usd"] = ledger.cash_usd  # note : cash final du cycle (simplification
        # documentée — reconstruire le cash exact immédiatement après CE fill précis
        # nécessiterait de journaliser au fil de l'eau pendant la boucle d'ordres ; comme les
        # fills sont peu nombreux et strictement séquentiels ici, cash_after_usd du dernier
        # fill est exact, les fills intermédiaires portent une valeur légèrement en avance
        # (le cash après TOUS les fills du cycle) plutôt qu'après CE fill isolé.
        append_journal(config.TRADES_JSONL, record)

    append_journal(config.EQUITY_JSONL, {
        "run_id": run_id,
        "ts": now.isoformat(),
        "equity_usd": equity_fin_cycle,
        "cash_usd": ledger.cash_usd,
        "exposures": exposures,
        "gross_exposure_pct": gross_exposure_pct,
        "drawdown_pct": drawdown_pct,
        "equity_peak_usd": equity_peak,
        "circuit_breakers_active": circuit_breakers_active,
    })

    for record in decisions:
        append_journal(config.DECISIONS_JSONL, record)

    save_state(new_state)

    # --- 15) git_sync — dernière étape du programme ---
    n_trades = len(fills_this_cycle)
    message = (
        f"Cycle {run_id} : {n_trades} trade(s), equity={equity_fin_cycle:.2f}$, "
        f"DD={drawdown_pct:.2%}"
    )
    result = git_sync(repo, message, run_id=run_id)

    if result == "SUCCESS":
        logger.info("git_sync SUCCESS — cycle %s terminé (equity=%.2f$).", run_id, equity_fin_cycle)
        return 0
    if result == "ABORTED_DUPLICATE":
        logger.info(
            "git_sync ABORTED_DUPLICATE — un autre run a gagné la course pour run_id=%s, "
            "sortie propre.", run_id,
        )
        return 0

    logger.error("git_sync FAILED pour run_id=%s — état local non poussé.", run_id)
    return 1


if __name__ == "__main__":
    sys.exit(main())
