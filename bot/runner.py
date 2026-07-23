#!/usr/bin/env python3
"""bot/runner.py — point d'entrée du bot, orchestre le cycle horaire MULTI-WALLETS.

Évolution multi-wallets (docs/ARCHITECTURE.md §9) : un seul cycle horaire traite désormais
TROIS wallets indépendants (`bot.config.WALLETS` : prudent 🛡️, équilibré ⚖️, agressif 🔥),
séquentiellement, avec les MÊMES prix récupérés UNE SEULE FOIS et partagés entre eux.
Idempotence et intégrité :
  - un `run_id` couvre les 3 wallets à la fois (`state/cycle.json`, `bot.persist.cycle`) ;
  - chaque wallet garde SA PROPRE chaîne d'intégrité (`state_hash_prev`) indépendante des
    deux autres, dans son propre `state/wallets/<id>/state.json` ;
  - le cycle est TOUT-OU-RIEN : tout est calculé EN MÉMOIRE pour les 3 wallets avant la
    moindre écriture disque ; si un wallet lève une exception, RIEN n'est écrit pour AUCUN
    wallet et le cycle échoue proprement (code retour non nul), sans commit partiel ;
  - UN SEUL commit git par cycle réussi, couvrant les 12 fichiers de wallet + `cycle.json`.

Séquence :
  1. `pull_rebase`.
  2. Idempotence globale : `load_cycle_state()` + `compute_run_id()` ; sortie propre (code 0)
     si `run_id` déjà traité par CE cycle (les 3 wallets à la fois).
  3. Garde-fou anti-doublon post-crash (identique en principe à l'ancien portefeuille unique,
     étendu aux 12 fichiers des 3 wallets).
  4. Univers actif = UNION des univers crypto des 3 wallets ; `get_prices()` UNE fois,
     `get_history()` UNE fois par symbole avec prix disponible ; `get_fx_rate('EURUSD')` UNE
     fois (taux EUR/USD partagé, cf. `bot.feeds.fx`).
  5. Pour chaque wallet (séquentiel) : initialisation FX/capital si besoin (jamais de taux
     inventé — un wallet sans taux disponible reste NON INITIALISÉ, réessaie au cycle
     suivant), stratégies (`combine_strategies` avec le `profile` du wallet), `RiskManager`
     paramétré par le profil du wallet, `ExchangeSim` paramétré par les paliers de coûts du
     wallet, ordres, journalisation — TOUT calculé en mémoire (`WalletCycleResult`).
  6. Si les 3 wallets ont été traités sans exception : écritures disque (dans l'ordre
     trades -> equity -> decisions -> state, par wallet), puis `state/cycle.json`, puis
     `git_sync` — dernière étape du programme, UN SEUL commit.

Principe cardinal pessimiste (ARCHITECTURE.md §0) inchangé : un symbole sans prix frais et
valide ce cycle ne trade jamais ; un wallet sans taux EUR/USD ne s'initialise jamais sur un
taux halluciné.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

# Permet `python3 /chemin/vers/repo/bot/runner.py` (invocation directe par chemin, cas du
# scheduler horaire réel) sans dépendre d'un `pip install -e .` ni de `python3 -m bot.runner` :
# la racine du dépôt (parent de ce paquet `bot/`) doit être sur sys.path pour que
# `from bot import config` etc. fonctionnent, quel que soit le répertoire d'appel.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from bot import config
from bot.feeds import get_fx_rate, get_history, get_prices
from bot.feeds.types import HistoryUnavailableError, Quote
from bot.persist import (
    append_journal_many,
    compute_state_hash,
    git_sync,
    has_uncommitted_state_changes,
    init_state,
    is_cycle_already_done,
    load_cycle_state,
    load_state,
    pull_rebase,
    records_for_run,
    save_cycle_state,
    save_state,
)
from bot.risk import RiskManager
from bot.sim.exchange import ExchangeSim
from bot.sim.fills import Fill
from bot.sim.ledger import Ledger
from bot.strategies import combine_strategies, load_strategies

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot.runner")

EPSILON_WEIGHT = 1e-9


class WalletCycleError(Exception):
    """Levée quand le traitement d'UN wallet échoue de façon inattendue — capturée par
    `main()` pour garantir le comportement tout-ou-rien (aucune écriture pour AUCUN wallet)."""

    def __init__(self, wallet_id: str, original: Exception):
        self.wallet_id = wallet_id
        self.original = original
        super().__init__(f"échec du traitement du wallet {wallet_id!r}: {original}")


@dataclass
class WalletCycleResult:
    wallet_id: str
    new_state: dict
    trade_records: List[dict] = field(default_factory=list)
    equity_record: Optional[dict] = None
    decision_records: List[dict] = field(default_factory=list)
    n_trades: int = 0
    equity_usd: float = 0.0
    initialized: bool = False
    fx_rate_used: Optional[float] = None


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
        "regime_gate_blocked": False,
    }


def load_wallet_state(wallet_cfg: dict) -> dict:
    """Charge l'état du wallet, ou construit son état initial NON INITIALISÉ (fx.initial_rate
    = None, cash_usd = 0.0) si c'est son tout premier cycle."""
    path = config.wallet_state_json(wallet_cfg["id"])
    if not os.path.exists(path):
        return init_state(wallet_cfg["id"], wallet_cfg["capital_initial_eur"])
    return load_state(path)


def wallet_journal_paths(wallet_id: str) -> List[str]:
    return [
        config.wallet_state_json(wallet_id),
        config.wallet_trades_jsonl(wallet_id),
        config.wallet_equity_jsonl(wallet_id),
        config.wallet_decisions_jsonl(wallet_id),
    ]


def all_wallet_paths() -> List[str]:
    paths: List[str] = [config.CYCLE_JSON]
    for wallet_id in config.WALLET_IDS:
        paths.extend(wallet_journal_paths(wallet_id))
    return paths


def _exchange_for_wallet(wallet_cfg: dict) -> ExchangeSim:
    universe = wallet_cfg["univers_crypto"]
    fee_by_symbol = {
        sym: config.COST_TIER_FEE_TAKER_BPS[config.cost_tier_of(sym)] for sym in universe
    }
    slippage_by_symbol = {
        sym: config.COST_TIER_SLIPPAGE_PENALTY_BPS[config.cost_tier_of(sym)] for sym in universe
    }
    return ExchangeSim(
        fee_taker_bps=config.FEE_TAKER_BPS,
        slippage_penalty_bps=config.SLIPPAGE_PENALTY_BPS,
        max_quote_age_seconds=config.MAX_QUOTE_AGE_SECONDS,
        min_notional_usd=config.MIN_NOTIONAL_USD,
        fee_taker_bps_by_symbol=fee_by_symbol,
        slippage_penalty_bps_by_symbol=slippage_by_symbol,
    )


def _risk_manager_for_wallet(wallet_cfg: dict) -> RiskManager:
    r = wallet_cfg["risque"]
    universe = wallet_cfg["univers_crypto"]
    return RiskManager(
        vol_target_annualized=r["vol_target_annualized"],
        vol_ewma_halflife_hours=r["vol_ewma_halflife_hours"],
        vol_coldstart_min_points=r["vol_coldstart_min_points"],
        vol_coldstart_scalar=r["vol_coldstart_scalar"],
        cap_per_asset_crypto=r["cap_per_asset"],
        cap_per_asset_equity=r["cap_per_asset"],  # wallets 100% crypto : même cap (non utilisé)
        gross_exposure_max=r["gross_exposure_max"],
        no_trade_band=r["no_trade_band"],
        cb_daily_loss_freeze_pct=r["cb_daily_loss_freeze_pct"],
        cb_daily_loss_freeze_hours=r["cb_daily_loss_freeze_hours"],
        cb_consecutive_losses_trigger=r["cb_consecutive_losses_trigger"],
        cb_cooldown_hours=r["cb_cooldown_hours"],
        cb_dd_half_size_pct=r["cb_dd_half_size_pct"],
        cb_dd_flatten_pct=r["cb_dd_flatten_pct"],
        symbols_crypto=universe,
        symbols_equity=[],
    )


def _empty_journals_touch(paths: List[str]) -> None:
    """Garantit l'existence des fichiers `.jsonl` (même vides) : `git add` échoue sur un
    chemin totalement absent (pathspec ne correspond à aucun fichier)."""
    for path in paths:
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8"):
                pass


def process_wallet(
    wallet_cfg: dict,
    state: dict,
    run_id: str,
    now: datetime,
    prices_all: Dict[str, Optional[Quote]],
    history_all: Dict[str, "object"],
    history_failed_all: Set[str],
    fx_resolved,
) -> WalletCycleResult:
    """Traite un cycle complet pour UN wallet, EN MÉMOIRE (aucune écriture disque ici — c'est
    à `main()` de le faire, uniquement si les 3 wallets ont réussi, cf. docstring module)."""
    wallet_id = wallet_cfg["id"]
    universe = list(wallet_cfg["univers_crypto"])
    prices = {sym: prices_all.get(sym) for sym in universe}
    history = {sym: history_all[sym] for sym in universe if sym in history_all}
    history_failed = {sym for sym in universe if sym in history_failed_all}

    prev_hash = compute_state_hash(state)

    # --- initialisation FX / capital (jamais de taux inventé) ---
    fx_state = dict(state.get("fx") or {})
    for k, v in (
        ("initial_rate", None), ("last_rate", None), ("last_rate_ts", None),
        ("last_rate_source", None), ("last_rate_stale", False),
    ):
        fx_state.setdefault(k, v)

    already_initialized = fx_state.get("initial_rate") is not None
    fx_note: str

    if fx_resolved is not None:
        fx_state["last_rate"] = fx_resolved.rate
        fx_state["last_rate_ts"] = fx_resolved.ts
        fx_state["last_rate_source"] = fx_resolved.source
        fx_state["last_rate_stale"] = fx_resolved.stale

    cash_usd = float(state.get("cash_usd", 0.0) or 0.0)
    positions_in = dict(state.get("positions", {}) or {})
    initialized_now = already_initialized

    if not already_initialized:
        if fx_resolved is not None:
            fx_state["initial_rate"] = fx_resolved.rate
            capital_usd = float(state["initial_eur"]) * fx_resolved.rate
            cash_usd = capital_usd
            positions_in = {}
            initialized_now = True
            fx_note = (
                f"wallet initialisé ce cycle : {state['initial_eur']:.2f}€ x "
                f"{fx_resolved.rate:.4f} (EUR/USD, source={fx_resolved.source}"
                f"{', STALE' if fx_resolved.stale else ''}) = {capital_usd:.2f}$"
            )
        else:
            initialized_now = False
            fx_note = (
                "wallet NON INITIALISÉ : aucun taux EUR/USD disponible ce cycle (frankfurter, "
                "open.er-api, et aucun dernier taux connu ont tous échoué) — jamais de taux "
                "inventé, capital non converti, aucun trade possible ; nouvelle tentative au "
                "prochain cycle horaire"
            )
    else:
        fx_note = "wallet déjà initialisé (taux EUR/USD figé lors d'un cycle précédent)"

    if not initialized_now:
        # --- wallet en attente de taux FX : cycle "à plat", propre et intégralement journalisé ---
        decision_records = [{
            "run_id": run_id,
            "ts": now.isoformat(),
            "wallet_id": wallet_id,
            "symbol": sym,
            "asset_class": "crypto",
            "market_open": True,
            "quote_available": False,
            "quote_source": None,
            "price_mid_ideal": None,
            "quote_ts": None,
            "quote_age_seconds": None,
            "strategy_signals": {},
            "poids_cible_brut": None,
            "poids_cible_apres_risk": None,
            "poids_actuel": 0.0,
            "decision": "NO_TRADE",
            "reason": fx_note,
            "circuit_breakers_snapshot": None,
        } for sym in universe]

        equity_record = {
            "run_id": run_id,
            "ts": now.isoformat(),
            "wallet_id": wallet_id,
            "equity_usd": 0.0,
            "equity_eur": 0.0,
            "cash_usd": 0.0,
            "exposures": {},
            "gross_exposure_pct": 0.0,
            "drawdown_pct": 0.0,
            "equity_peak_usd": 0.0,
            "circuit_breakers_active": [],
            "fx_rate_used": None,
            "note": fx_note,
        }

        new_state = {
            "schema_version": state["schema_version"],
            "wallet_id": wallet_id,
            "initial_eur": state["initial_eur"],
            "last_run_id": run_id,
            "last_run_completed_at": now.isoformat(),
            "state_hash_prev": prev_hash,
            "cash_usd": 0.0,
            "positions": {},
            "equity_peak_usd": 0.0,
            "equity_peak_ts": state.get("equity_peak_ts"),
            "realized_pnl_cumulative_usd": float(state.get("realized_pnl_cumulative_usd", 0.0) or 0.0),
            "fx": fx_state,
            "circuit_breakers": state.get("circuit_breakers") or {
                "flatten_mode": False, "manual_review_required": False,
                "daily_loss_freeze_until": None, "cooldown_until": None,
                "consecutive_losses": 0, "dd_half_size_active": False,
            },
            "trade_history_for_breakers": list(state.get("trade_history_for_breakers", []) or []),
        }

        return WalletCycleResult(
            wallet_id=wallet_id, new_state=new_state, trade_records=[],
            equity_record=equity_record, decision_records=decision_records,
            n_trades=0, equity_usd=0.0, initialized=False, fx_rate_used=None,
        )

    # --- wallet initialisé (ce cycle ou un précédent) : cycle de trading normal ---
    working_state = dict(state)
    working_state["cash_usd"] = cash_usd
    working_state["positions"] = positions_in

    equity_avant_cycle, poids_actuel = _estimate_equity_and_weights(cash_usd, positions_in, prices)

    strategies_actives = load_strategies()
    if strategies_actives:
        cibles_brutes, strategy_signals = combine_strategies(
            strategies_actives, history, working_state, profile=wallet_cfg
        )
    else:
        cibles_brutes = dict(poids_actuel)
        strategy_signals = {}

    risk_manager = _risk_manager_for_wallet(wallet_cfg)
    cibles_finales, reasons = risk_manager.apply(cibles_brutes, working_state, prices, history, now=now)

    ledger = Ledger(cash_usd=cash_usd, positions=positions_in)
    exchange = _exchange_for_wallet(wallet_cfg)

    fills_this_cycle: List[Fill] = []
    decisions: List[dict] = []

    for symbol in universe:
        current_w = poids_actuel.get(symbol, 0.0)
        quote = prices.get(symbol)
        quote_available = quote is not None
        signals_for_symbol = {
            strat_name: sigs[symbol] for strat_name, sigs in strategy_signals.items() if symbol in sigs
        }
        cb_snapshot = _cb_snapshot(working_state.get("circuit_breakers") or {}, now)

        if not quote_available:
            decisions.append({
                "run_id": run_id, "ts": now.isoformat(), "wallet_id": wallet_id,
                "symbol": symbol, "asset_class": "crypto", "market_open": True,
                "quote_available": False, "quote_source": None, "price_mid_ideal": None,
                "quote_ts": None, "quote_age_seconds": None,
                "strategy_signals": signals_for_symbol, "poids_cible_brut": None,
                "poids_cible_apres_risk": None, "poids_actuel": current_w,
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
                decision = "NO_TRADE"
                reason = f"{base_reason} ; ordre {side} rejeté par ExchangeSim : {result.reason}"

        decisions.append({
            "run_id": run_id, "ts": now.isoformat(), "wallet_id": wallet_id,
            "symbol": symbol, "asset_class": "crypto", "market_open": True,
            "quote_available": True, "quote_source": quote.source,
            "price_mid_ideal": quote.mid, "quote_ts": quote.ts,
            "quote_age_seconds": quote_age_seconds, "strategy_signals": signals_for_symbol,
            "poids_cible_brut": raw, "poids_cible_apres_risk": final, "poids_actuel": current_w,
            "decision": decision, "reason": reason, "circuit_breakers_snapshot": cb_snapshot,
        })

    # --- circuit breakers stateful (§5.3.1) ---
    cb_state = dict(working_state.get("circuit_breakers") or {})
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
    risk_cfg = wallet_cfg["risque"]
    if consecutive_losses >= risk_cfg["cb_consecutive_losses_trigger"]:
        cooldown_until = now + timedelta(hours=risk_cfg["cb_cooldown_hours"])
        existing_cd = _parse_ts(cb_state.get("cooldown_until"))
        if existing_cd is None or cooldown_until > existing_cd:
            cb_state["cooldown_until"] = cooldown_until.isoformat()

    # --- équity de fin de cycle ---
    final_positions = ledger.positions
    mark_prices_final: Dict[str, float] = {}
    for symbol in final_positions:
        mark = _mark_price(symbol, prices.get(symbol), final_positions)
        if mark is None:
            raise RuntimeError(
                f"[{wallet_id}] impossible de marquer {symbol} en fin de cycle "
                "(ni mid frais ni prix_moyen)"
            )
        mark_prices_final[symbol] = mark

    equity_fin_cycle = ledger.equity(mark_prices_final)

    equity_peak = float(working_state.get("equity_peak_usd") or 0.0)
    equity_peak_ts = working_state.get("equity_peak_ts")
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

    fx_rate_used = fx_state.get("last_rate")
    equity_eur = (equity_fin_cycle / fx_rate_used) if fx_rate_used else None

    new_state = {
        "schema_version": state["schema_version"],
        "wallet_id": wallet_id,
        "initial_eur": state["initial_eur"],
        "last_run_id": run_id,
        "last_run_completed_at": now.isoformat(),
        "state_hash_prev": prev_hash,
        "cash_usd": ledger.cash_usd,
        "positions": final_positions,
        "equity_peak_usd": equity_peak,
        "equity_peak_ts": equity_peak_ts,
        "realized_pnl_cumulative_usd": float(state.get("realized_pnl_cumulative_usd", 0.0) or 0.0)
        + realized_pnl_cycle,
        "fx": fx_state,
        "circuit_breakers": cb_state,
        "trade_history_for_breakers": trade_history,
    }

    trade_records = []
    for fill in fills_this_cycle:
        record = dataclasses.asdict(fill)
        record["wallet_id"] = wallet_id
        record["cash_after_usd"] = ledger.cash_usd
        trade_records.append(record)

    equity_record = {
        "run_id": run_id,
        "ts": now.isoformat(),
        "wallet_id": wallet_id,
        "equity_usd": equity_fin_cycle,
        "equity_eur": equity_eur,
        "cash_usd": ledger.cash_usd,
        "exposures": exposures,
        "gross_exposure_pct": gross_exposure_pct,
        "drawdown_pct": drawdown_pct,
        "equity_peak_usd": equity_peak,
        "circuit_breakers_active": circuit_breakers_active,
        "fx_rate_used": fx_rate_used,
        "note": fx_note,
    }

    return WalletCycleResult(
        wallet_id=wallet_id,
        new_state=new_state,
        trade_records=trade_records,
        equity_record=equity_record,
        decision_records=decisions,
        n_trades=len(fills_this_cycle),
        equity_usd=equity_fin_cycle,
        initialized=True,
        fx_rate_used=fx_rate_used,
    )


def _format_wallet_summary(wallet_cfg: dict, result: WalletCycleResult) -> str:
    emoji = wallet_cfg["emoji"]
    if not result.initialized or not result.fx_rate_used:
        return f"{emoji} init. en attente"
    equity_eur = result.equity_usd / result.fx_rate_used
    return f"{emoji} {equity_eur:.0f}€"


def main(now: Optional[datetime] = None) -> int:
    """Point d'entrée du cycle horaire multi-wallets. `now` est optionnel (défaut : horloge
    système réelle) — exposé explicitement pour les tests d'intégration."""
    now = now or datetime.now(timezone.utc)
    repo = repo_dir()

    # --- 1) pull_rebase : repartir de l'état le plus récent poussé, AVANT toute lecture ---
    pull_result = pull_rebase(repo)
    if pull_result != "SUCCESS":
        logger.warning(
            "pull_rebase a échoué (%s) — poursuite avec l'état local du clone tel quel.",
            pull_result,
        )

    all_paths = all_wallet_paths()

    # --- 2) idempotence GLOBALE (state/cycle.json couvre les 3 wallets à la fois) ---
    cycle_state = load_cycle_state(config.CYCLE_JSON, config.WALLET_IDS)
    run_id = compute_run_id(now)

    if is_cycle_already_done(cycle_state, run_id):
        if has_uncommitted_state_changes(repo, all_paths):
            logger.warning(
                "cycle déjà marqué comme traité (last_run_id=%s) MAIS des changements non "
                "commités subsistent — reprise de git_sync avant de conclure à un doublon.",
                run_id,
            )
            message = f"Cycle {run_id} : reprise de git_sync après interruption pré-push"
            result = git_sync(repo, message, run_id=run_id, state_path=config.CYCLE_JSON, paths=all_paths)
            if result in ("SUCCESS", "ABORTED_DUPLICATE"):
                logger.info("Reprise de git_sync terminée (%s) pour run_id=%s.", result, run_id)
                return 0
            logger.error("Reprise de git_sync a échoué (FAILED) pour run_id=%s.", run_id)
            return 1

        logger.info(
            "cycle déjà traité pour run_id=%s (last_run_id=%s) — abandon silencieux propre.",
            run_id, cycle_state.get("last_run_id"),
        )
        return 0

    # --- garde-fou anti-doublon post-crash (étendu aux 3 wallets) ---
    orphaned: Dict[str, List[dict]] = {}
    for wallet_id in config.WALLET_IDS:
        for path in (
            config.wallet_trades_jsonl(wallet_id),
            config.wallet_equity_jsonl(wallet_id),
            config.wallet_decisions_jsonl(wallet_id),
        ):
            recs = records_for_run(path, run_id)
            if recs:
                orphaned[path] = recs
    if orphaned:
        logger.error(
            "ANOMALIE CRITIQUE : des enregistrements pour run_id=%s existent déjà dans %s "
            "alors que state/cycle.json ne le connaît pas comme last_run_id — signe d'un "
            "cycle précédent interrompu en cours de journalisation. Arrêt immédiat, AUCUNE "
            "écriture, AUCUN appel réseau — revue manuelle requise.",
            run_id, sorted(orphaned.keys()),
        )
        return 1

    logger.info("Démarrage du cycle multi-wallets run_id=%s (%s)", run_id, config.WALLET_IDS)

    # --- 4) univers actif ce cycle = UNION des univers des 3 wallets ---
    crypto_symbols_all: List[str] = sorted({
        sym for w in config.WALLETS for sym in w["univers_crypto"]
    })
    prices: Dict[str, Optional[Quote]] = get_prices(crypto_symbols_all) if crypto_symbols_all else {}
    for sym in crypto_symbols_all:
        prices.setdefault(sym, None)

    history: Dict[str, "object"] = {}
    history_failed: Set[str] = set()
    for sym in crypto_symbols_all:
        if prices.get(sym) is None:
            continue
        try:
            history[sym] = get_history(sym, config.HISTORY_N_HOURS)
        except HistoryUnavailableError as exc:
            logger.warning("historique indisponible pour %s: %s", sym, exc)
            history_failed.add(sym)

    # --- FX EUR/USD : UNE fois, partagé entre les 3 wallets ---
    wallet_states: Dict[str, dict] = {}
    last_known_fx: Optional[dict] = None
    for wallet_cfg in config.WALLETS:
        wstate = load_wallet_state(wallet_cfg)
        wallet_states[wallet_cfg["id"]] = wstate
        fx = wstate.get("fx") or {}
        if last_known_fx is None and fx.get("last_rate"):
            last_known_fx = {"rate": fx["last_rate"], "ts": fx.get("last_rate_ts")}

    try:
        fx_resolved = get_fx_rate("EURUSD", last_known=last_known_fx)
    except Exception as exc:  # noqa: BLE001 — défense en profondeur, jamais de crash sur la FX
        logger.error("get_fx_rate a levé une exception inattendue : %s", exc)
        fx_resolved = None

    if fx_resolved is None:
        logger.warning(
            "aucun taux EUR/USD disponible ce cycle — tout wallet non encore initialisé le "
            "restera (aucun taux inventé), réessai au prochain cycle horaire."
        )
    elif fx_resolved.stale:
        logger.warning(
            "taux EUR/USD STALE utilisé ce cycle (source=%s, ts=%s) — les deux sources "
            "réseau ont échoué, repli sur le dernier taux connu.",
            fx_resolved.source, fx_resolved.ts,
        )

    # --- 5) traitement des 3 wallets, EN MÉMOIRE, tout-ou-rien ---
    results: List[WalletCycleResult] = []
    try:
        for wallet_cfg in config.WALLETS:
            wallet_id = wallet_cfg["id"]
            try:
                result = process_wallet(
                    wallet_cfg, wallet_states[wallet_id], run_id, now,
                    prices, history, history_failed, fx_resolved,
                )
            except Exception as exc:  # noqa: BLE001 — capturé plus haut, ré-empaqueté avec le wallet fautif
                raise WalletCycleError(wallet_id, exc) from exc
            results.append(result)
    except WalletCycleError as exc:
        logger.error(
            "ÉCHEC du cycle : le traitement du wallet %r a levé une exception (%s) — "
            "principe tout-ou-rien : AUCUNE écriture, AUCUN commit pour AUCUN wallet ce "
            "cycle. Intervention manuelle requise.",
            exc.wallet_id, exc.original,
        )
        return 1

    # --- 6) tous les wallets ont réussi : écritures disque, puis UN SEUL commit ---
    for wallet_cfg, result in zip(config.WALLETS, results):
        wallet_id = wallet_cfg["id"]
        append_journal_many(config.wallet_trades_jsonl(wallet_id), result.trade_records)
        if result.equity_record is not None:
            append_journal_many(config.wallet_equity_jsonl(wallet_id), [result.equity_record])
        append_journal_many(config.wallet_decisions_jsonl(wallet_id), result.decision_records)
        save_state(result.new_state, config.wallet_state_json(wallet_id))

    new_cycle_state = {
        "schema_version": cycle_state.get("schema_version", 1),
        "last_run_id": run_id,
        "last_run_completed_at": now.isoformat(),
        "wallet_ids": list(config.WALLET_IDS),
    }
    save_cycle_state(new_cycle_state, config.CYCLE_JSON)

    _empty_journals_touch(all_paths)

    total_trades = sum(r.n_trades for r in results)
    summary = " | ".join(
        _format_wallet_summary(wallet_cfg, result) for wallet_cfg, result in zip(config.WALLETS, results)
    )
    message = f"Cycle {run_id} : {summary} — {total_trades} trade(s)"

    result = git_sync(repo, message, run_id=run_id, state_path=config.CYCLE_JSON, paths=all_paths)

    if result == "SUCCESS":
        logger.info("git_sync SUCCESS — cycle %s terminé (%s).", run_id, summary)
        return 0
    if result == "ABORTED_DUPLICATE":
        logger.info(
            "git_sync ABORTED_DUPLICATE — un autre run a gagné la course pour run_id=%s.",
            run_id,
        )
        return 0

    logger.error("git_sync FAILED pour run_id=%s — état local non poussé.", run_id)
    return 1


if __name__ == "__main__":
    sys.exit(main())
