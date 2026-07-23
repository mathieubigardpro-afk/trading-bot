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
  4. Univers actif, TOUTES POCHES confondues (docs/ARCHITECTURE.md §11) : crypto = UNION des
     univers crypto des 3 wallets (`get_prices()`, `get_history()` horaire) ; actions/ETF = UNION
     des poches actions/ETF des 3 wallets, dérivée de `bot.config.WALLETS[*]["pockets"]`
     (`get_prices()` dans le MÊME appel — la façade route automatiquement crypto vs actions ;
     `prefetch_daily_history()`/`get_daily_history()` pour l'historique JOURNALIER clôturé) ;
     `get_fx_rate('EURUSD')` UNE fois (taux EUR/USD partagé, cf. `bot.feeds.fx`) ;
     `is_us_market_open(now)` UNE fois (gating actions/ETF, crypto reste 24/7) ;
     `load_strategies()` UNE fois (`{name: instance}`, partagé).
  5. Pour chaque wallet (séquentiel) : initialisation FX/capital si besoin (jamais de taux
     inventé — un wallet sans taux disponible reste NON INITIALISÉ, réessaie au cycle
     suivant), cibles par poche combinées et mises à l'échelle par `capital_alloc_pct`
     (`_combine_pockets`, §11.2), `RiskManager` "par-dessus" paramétré par le profil du wallet
     (§11.3), `ExchangeSim` paramétré par les paliers de coûts du wallet, ordres (aucun ordre
     actions/ETF si marché fermé, §11.4), journalisation — TOUT calculé en mémoire
     (`WalletCycleResult`).
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
from bot.feeds import (
    get_daily_history,
    get_fx_rate,
    get_history,
    get_prices,
    is_us_market_open,
    prefetch_daily_history,
)
from bot.feeds import MIN_WARMUP_DAYS as DAILY_MIN_WARMUP_DAYS
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
from bot.strategies import StrategyBase, load_strategies

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot.runner")

EPSILON_WEIGHT = 1e-9

# ==========================================================================================
# Poches actions/ETF (docs/ARCHITECTURE.md §11, docs/config-strategies.json, addendum
# multi-stratégies) : quels symboles chaque `strategy_ref` de `bot.config.WALLETS[*]["pockets"]`
# a besoin (a) de POUVOIR TRADER (`*_TRADABLE_SYMBOLS`, utilisé pour les quotes d'exécution
# `get_prices()` et la boucle de décision), et (b) de LIRE en historique JOURNALIER clôturé
# (`*_DATA_SYMBOLS`, superset qui ajoute les symboles "filtre de régime seulement", ex. SPY pour
# `xs_momentum_sp100` — jamais détenu par cette poche mais nécessaire à son signal). Dérivé de
# `bot.config` (source de vérité unique), jamais recopié en dur ici.
# ==========================================================================================
EQUITIES_TRADABLE_SYMBOLS: List[str] = list(config.EQUITIES_SP100_UNIVERSE)
EQUITIES_DATA_SYMBOLS: List[str] = sorted(
    set(EQUITIES_TRADABLE_SYMBOLS) | {config.EQUITIES_MARKET_FILTER_SYMBOL}
)
ETF_TRADABLE_SYMBOLS: List[str] = sorted(set(config.ETF_RISKY_UNIVERSE) | {config.ETF_BOND_BOGEY})
ETF_DATA_SYMBOLS: List[str] = list(ETF_TRADABLE_SYMBOLS)

POCKET_STRATEGY_TRADABLE_SYMBOLS: Dict[str, List[str]] = {
    "xs_momentum_sp100": EQUITIES_TRADABLE_SYMBOLS,
    "dual_momentum_etf": ETF_TRADABLE_SYMBOLS,
}
POCKET_STRATEGY_DATA_SYMBOLS: Dict[str, List[str]] = {
    "xs_momentum_sp100": EQUITIES_DATA_SYMBOLS,
    "dual_momentum_etf": ETF_DATA_SYMBOLS,
}
_EQUITIES_TRADABLE_SET = set(EQUITIES_TRADABLE_SYMBOLS)
_ETF_TRADABLE_SET = set(ETF_TRADABLE_SYMBOLS)

DAILY_HISTORY_ASSET_CLASS = "equities"  # cf. _gather_daily_history() : valeur unique passée à
# bot.feeds.daily quel que soit le rôle réel (actions S&P100 ou ETF) — le fetch sous-jacent
# (yfinance lots + repli stooq) est IDENTIQUE pour "equity" et "etf" (bot/feeds/daily.py ne
# différencie que la clé de cache), et SPY apparaît dans LES DEUX univers (filtre xs_momentum +
# membre de l'univers risqué dual-momentum) : utiliser une classe unique évite de le
# télécharger deux fois sous deux clés de cache distinctes.


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


def _noncrypto_tradable_symbols(wallet_cfg: dict) -> List[str]:
    """Symboles actions/ETF TRADABLES (hors filtre de régime seul, ex. SPY sans poche ETF) pour
    les poches de CE wallet, dérivés de `wallet_cfg["pockets"]` (docs/ARCHITECTURE.md §11)."""
    out: List[str] = []
    for pocket in wallet_cfg.get("pockets", []) or []:
        ref = pocket.get("strategy_ref")
        if ref in POCKET_STRATEGY_TRADABLE_SYMBOLS:
            out.extend(POCKET_STRATEGY_TRADABLE_SYMBOLS[ref])
    return sorted(set(out))


def _asset_class_of(symbol: str, crypto_universe) -> str:
    if symbol in crypto_universe:
        return "crypto"
    if symbol in _EQUITIES_TRADABLE_SET:
        return "equities"
    if symbol in _ETF_TRADABLE_SET:
        return "etf"
    return "unknown"


def _no_trade_band_scale_by_symbol(wallet_cfg: dict) -> Dict[str, float]:
    """Correctif audit critique #1 (no-trade band appliquée wallet-wide au lieu de par poche,
    cf. `bot/risk/manager.py.RiskManager.apply(no_trade_band_by_symbol=...)`).

    `{symbole: capital_alloc_pct}` de la poche NON-CASH qui porte ce symbole, pour TOUT symbole
    potentiellement tradable par CE wallet (crypto ET actions/ETF), couvrant le PLEIN univers
    tradable de chaque poche — pas seulement les symboles ayant une cible non nulle ce cycle —
    pour qu'une position déjà détenue mais sortie du top-k (cible retombée à 0) reste soumise à
    la bande de SA poche, pas à un défaut wallet-wide implicite.

    Un symbole tradable par deux poches distinctes du même wallet n'existe pas en pratique dans
    le SPEC actuel (poches actions/ETF/crypto ont des univers disjoints), mais si cela devait
    arriver, on garde l'alloc la PLUS PETITE (bande la plus étroite = comportement le plus
    permissif au trade = le plus pessimiste vis-à-vis du risque de rester bloqué à tort).
    """
    scale: Dict[str, float] = {}
    for pocket in wallet_cfg.get("pockets", []) or []:
        ref = pocket.get("strategy_ref")
        asset_class = pocket.get("asset_class")
        alloc = float(pocket.get("capital_alloc_pct", 0.0) or 0.0)
        if not ref or alloc <= 0:
            continue
        if asset_class == "crypto":
            symbols = wallet_cfg.get("univers_crypto") or []
        else:
            symbols = POCKET_STRATEGY_TRADABLE_SYMBOLS.get(ref, [])
        for sym in symbols:
            prev = scale.get(sym)
            scale[sym] = alloc if prev is None else min(prev, alloc)
    return scale


def _gather_daily_history(symbols: List[str]) -> Tuple[Dict[str, "object"], Set[str]]:
    """Récupère l'historique JOURNALIER clôturé (`bot.feeds.get_daily_history`) pour tous les
    `symbols` demandés (S&P100 + SPY + univers ETF risqué + IEF, UNION de tous les wallets, cf.
    `main()`), en préchargeant le cache par lots (`prefetch_daily_history`) avant de relire
    symbole par symbole. Ne lève JAMAIS d'exception — un échec réseau (attendu dans ce bac à
    sable, réseau bloqué) ou un historique insuffisant se traduit par une absence d'entrée pour
    ce symbole dans le dict retourné (+ présence dans le second élément, l'ensemble des échecs),
    jamais par un cycle interrompu (principe pessimiste défensif, ARCHITECTURE.md §0.2/§0.3)."""
    daily_history: Dict[str, "object"] = {}
    failed: Set[str] = set()
    if not symbols:
        return daily_history, failed

    try:
        prefetch_daily_history(symbols, asset_class=DAILY_HISTORY_ASSET_CLASS, n_days=DAILY_MIN_WARMUP_DAYS)
    except Exception as exc:  # noqa: BLE001 — un échec de préchargement ne doit jamais stopper le cycle
        logger.error(
            "prefetch_daily_history a échoué de façon inattendue (%s) — tentative individuelle "
            "par symbole malgré tout (get_daily_history a son propre cache/repli).",
            exc,
        )

    for sym in symbols:
        try:
            daily_history[sym] = get_daily_history(sym, DAILY_MIN_WARMUP_DAYS, asset_class=DAILY_HISTORY_ASSET_CLASS)
        except HistoryUnavailableError as exc:
            logger.warning("historique journalier indisponible pour %s: %s", sym, exc)
            failed.add(sym)
        except Exception as exc:  # noqa: BLE001 — défense en profondeur (dépendance externe fragile)
            logger.error("get_daily_history a levé une exception inattendue pour %s: %s", sym, exc)
            failed.add(sym)

    return daily_history, failed


def _combine_pockets(
    wallet_cfg: dict,
    history_hourly: Dict[str, "object"],
    daily_history: Dict[str, "object"],
    working_state: dict,
    strategies_by_name: Dict[str, StrategyBase],
) -> Tuple[Dict[str, float], Dict[str, Dict[str, float]]]:
    """Agrège TOUTES les poches d'un wallet en UNE cible brute par symbole, exprimée en
    FRACTION DE L'ÉQUITY TOTALE du wallet — `poids_intra_poche(symbole) * capital_alloc_pct`
    (docs/config-strategies.json, docs/SELECTION-FINALE.md §3, mission §3 : "le capital d'une
    poche = part × equity du wallet"). C'est cette fonction, PAS
    `bot.strategies.combine_strategies` (moyenne équi-pondérée générique jamais câblée en
    production, cf. docstring de `bot/strategies/__init__.py`), qui remplace le "placeholder"
    documenté par ARCHITECTURE.md §5.5/§8.

    Une poche "cash" (`strategy_ref=None`) ou une poche dont la stratégie référencée est absente
    de `strategies_by_name` (config incohérente / module de stratégie cassé à l'import, cf.
    `load_strategies()`) ne contribue simplement aucune cible — jamais une extrapolation.
    """
    cibles: Dict[str, float] = {}
    signals: Dict[str, Dict[str, float]] = {}
    for pocket in wallet_cfg.get("pockets", []) or []:
        strategy_ref = pocket.get("strategy_ref")
        if not strategy_ref:
            continue
        strat = strategies_by_name.get(strategy_ref)
        if strat is None:
            logger.warning(
                "wallet %s: stratégie '%s' référencée par une poche mais absente de "
                "load_strategies() ce cycle — poche ignorée (aucune cible émise).",
                wallet_cfg.get("id"), strategy_ref,
            )
            continue
        asset_class = pocket.get("asset_class")
        pocket_history = history_hourly if asset_class == "crypto" else daily_history
        raw_weights = strat.target_weights(pocket_history, working_state, profile=wallet_cfg) or {}
        signals[strategy_ref] = dict(raw_weights)
        alloc = float(pocket["capital_alloc_pct"])
        for symbol, w in raw_weights.items():
            cibles[symbol] = cibles.get(symbol, 0.0) + float(w) * alloc
    return cibles, signals


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
    # Correctif audit critique #2 : fractions d'action/ETF autorisées (cf.
    # bot/config.py:QTY_STEPS_EQUITIES — `ExchangeSim.qty_steps` fusionne cette table PAR-DESSUS
    # ses propres défauts crypto, `bot.sim.exchange.DEFAULT_QTY_STEPS`, laissés inchangés).
    return ExchangeSim(
        fee_taker_bps=config.FEE_TAKER_BPS,
        slippage_penalty_bps=config.SLIPPAGE_PENALTY_BPS,
        max_quote_age_seconds=config.MAX_QUOTE_AGE_SECONDS,
        min_notional_usd=config.MIN_NOTIONAL_USD,
        qty_steps=dict(config.QTY_STEPS_EQUITIES),
        fee_taker_bps_by_symbol=fee_by_symbol,
        slippage_penalty_bps_by_symbol=slippage_by_symbol,
    )


def _risk_manager_for_wallet(wallet_cfg: dict) -> RiskManager:
    """RiskManager PORTEFEUILLE (toutes poches confondues), appliqué "par-dessus" les cibles déjà
    combinées et mises à l'échelle par poche (`_combine_pockets`) — cf. mission d'intégration et
    docs/ARCHITECTURE.md §11 pour le détail du raisonnement ci-dessous.

    Design décidé (documenté, pas improvisé) : `wallet_cfg["risque"]` (`vol_target_annualized`,
    `gross_exposure_max`, `cap_per_asset`) correspond EXACTEMENT aux paramètres de la variante
    SPEC de la SEULE poche crypto (`docs/config-strategies.json` ->
    `crypto_quasi_passif_vol_targete.variants.*`) et est déjà appliqué EN INTERNE par
    `bot.strategies.quasi_passif_crypto`, sur des poids RELATIFS À LA POCHE, AVANT la mise à
    l'échelle par `capital_alloc_pct`. Réutiliser ces mêmes valeurs ici, au niveau PORTEFEUILLE
    (poids déjà exprimés en fraction de l'équity TOTALE du wallet), écraserait à tort les poches
    actions/ETF (ex. `gross_exposure_max` prudent = 0.40 couperait la poche ETF visée à 55%) —
    bug évité explicitement :
      - `vol_target_annualized=50.0` (borne haute autorisée par le constructeur,
        `0 < x <= 50`) neutralise le scalaire de vol-targeting PORTEFEUILLE à ~1.0 dans toutes
        les conditions réalistes — usage explicitement documenté par
        `bot/risk/manager.py.RiskManager.__init__` ("les tests ont légitimement besoin de
        neutraliser le vol targeting... en fixant une cible très supérieure à toute vol
        réaliste"), pas un détournement. Le vol-targeting réel de la poche crypto reste entier,
        fait par la stratégie elle-même.
      - `cap_per_asset_equity=1.0` : aucun cap par actif dédié n'est spécifié par le SPEC pour
        les poches actions/ETF (sizing déjà borné par construction : top_k équipondéré). Réutiliser
        le cap crypto (0.20-0.30) écraserait une concentration LÉGITIME (dual-momentum peut placer
        100% de sa poche sur IEF si les 3 candidats échouent le momentum absolu, ex. 55% du wallet
        prudent, très au-delà d'un cap crypto).
      - `gross_exposure_max=1.0` : jamais contraignant par construction (la somme des
        `capital_alloc_pct` non-cash de chaque wallet est strictement < 1.0, cf.
        `bot/config.py:WALLETS`) — 1.0 est le plafond trivial (100% de l'équity), pas une valeur
        inventée pour l'occasion.
      - `cap_per_asset_crypto` et les circuit breakers (`cb_*`) restent ceux du profil du
        wallet, appliqués WALLET-WIDE comme le veut `docs/SELECTION-FINALE.md` §5 ("Drawdown
        wallet > cb_dd_flatten_pct... le circuit breaker existant s'applique") — c'est la vraie
        valeur ajoutée de ce passage RiskManager "par-dessus".
      - `no_trade_band=r["no_trade_band"]` (constructeur) reste le réglage de PROFIL (5%), mais
        n'est plus appliqué wallet-wide TEL QUEL au niveau `apply()` : CORRECTIF AUDIT CRITIQUE
        #1 — un book équipondéré `top_k=10` mis à l'échelle par `capital_alloc_pct=35%` donne un
        poids intrinsèque de 3.5%/titre, toujours sous une bande de 5% wallet-wide, ce qui gelait
        silencieusement la poche actions à 0% en permanence (constaté empiriquement, 103/103
        décisions NO_TRADE malgré des cibles brutes non nulles). `process_wallet()` fournit donc
        `no_trade_band_by_symbol=_no_trade_band_scale_by_symbol(wallet_cfg)` à `apply()` : la
        bande RÉELLEMENT comparée pour un symbole devient `no_trade_band * capital_alloc_pct de
        sa poche`, cohérent avec le fait que la cible elle-même est déjà mise à l'échelle par ce
        même facteur dans `_combine_pockets()`. Voir docstring de tête de
        `bot/risk/manager.py` (étape 6) pour le détail.
      - `vol_coldstart_scalar=1.0` (au lieu de la valeur du profil, 0.5) : COMPAGNON OBLIGÉ de
        `vol_target_annualized=50.0` ci-dessus, PAS un second réglage indépendant. Le flag
        "coldstart" de `bot.risk.vol_targeting.portfolio_vol_annualized` se déclenche dès qu'UN
        SEUL symbole à poids non nul manque d'historique HORAIRE exploitable — ce qui est
        SYSTÉMATIQUEMENT le cas ici pour toute poche actions/ETF (leur historique est
        JOURNALIER, jamais horaire, cf. `history_hourly` transmis à `RiskManager.apply()` :
        aucune entrée pour ces symboles). Sans neutraliser aussi `vol_coldstart_scalar`, CE
        SEUL FAIT réduirait de moitié TOUTES les cibles du cycle (crypto y compris, le scalaire
        de vol-targeting étant unique pour tout le portefeuille) dès qu'un wallet détient une
        poche actions/ETF non nulle — bug identifié en test d'intégration
        (`bot/tests/test_integration_full_cycle.py`) et corrigé ici, pas un oubli.
    """
    r = wallet_cfg["risque"]
    crypto_universe = list(wallet_cfg["univers_crypto"])
    noncrypto_universe = _noncrypto_tradable_symbols(wallet_cfg)
    return RiskManager(
        vol_target_annualized=50.0,
        vol_ewma_halflife_hours=r["vol_ewma_halflife_hours"],
        vol_coldstart_min_points=r["vol_coldstart_min_points"],
        vol_coldstart_scalar=1.0,
        cap_per_asset_crypto=r["cap_per_asset"],
        cap_per_asset_equity=1.0,
        gross_exposure_max=1.0,
        no_trade_band=r["no_trade_band"],
        cb_daily_loss_freeze_pct=r["cb_daily_loss_freeze_pct"],
        cb_daily_loss_freeze_hours=r["cb_daily_loss_freeze_hours"],
        cb_consecutive_losses_trigger=r["cb_consecutive_losses_trigger"],
        cb_cooldown_hours=r["cb_cooldown_hours"],
        cb_dd_half_size_pct=r["cb_dd_half_size_pct"],
        cb_dd_flatten_pct=r["cb_dd_flatten_pct"],
        symbols_crypto=crypto_universe,
        symbols_equity=noncrypto_universe,
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
    daily_history: Optional[Dict[str, "object"]] = None,
    daily_history_failed: Optional[Set[str]] = None,
    market_open: Optional[bool] = None,
    strategies_by_name: Optional[Dict[str, StrategyBase]] = None,
) -> WalletCycleResult:
    """Traite un cycle complet pour UN wallet, EN MÉMOIRE (aucune écriture disque ici — c'est
    à `main()` de le faire, uniquement si les 3 wallets ont réussi, cf. docstring module).

    Poches multi-classes (docs/ARCHITECTURE.md §11) : `daily_history` (bougies JOURNALIÈRES
    clôturées actions/ETF, cf. `_gather_daily_history`), `market_open` (`is_us_market_open(now)`
    — calculé par l'appelant si `None`, exposé en paramètre pour les tests) et
    `strategies_by_name` (`{StrategyBase.name: instance}` — `load_strategies()` appelé par
    l'appelant si `None`, jamais de fetch réseau ici : cette fonction reste par ailleurs pure
    vis-à-vis du réseau) complètent les arguments crypto historiques (`history_all` = bougies
    HORAIRES crypto, `history_failed_all`).
    """
    wallet_id = wallet_cfg["id"]
    crypto_universe = list(wallet_cfg["univers_crypto"])
    noncrypto_universe = _noncrypto_tradable_symbols(wallet_cfg)
    full_universe = sorted(set(crypto_universe) | set(noncrypto_universe))

    prices = {sym: prices_all.get(sym) for sym in full_universe}
    history_hourly = {sym: history_all[sym] for sym in crypto_universe if sym in history_all}
    history_hourly_failed = {sym for sym in crypto_universe if sym in history_failed_all}

    daily_history = daily_history if daily_history is not None else {}
    daily_history_failed = daily_history_failed if daily_history_failed is not None else set()
    market_open = is_us_market_open(now) if market_open is None else bool(market_open)
    strategies_by_name = (
        strategies_by_name if strategies_by_name is not None
        else {s.name: s for s in load_strategies()}
    )

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
            "asset_class": _asset_class_of(sym, crypto_universe),
            "market_open": True if sym in crypto_universe else market_open,
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
        } for sym in full_universe]

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
            "strategy_state": dict(state.get("strategy_state") or {}),
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
    # Champ persistant générique (mission d'intégration §3) : aucune des 3 stratégies câblées
    # aujourd'hui n'en a besoin (rebalancements mensuels dérivés PUREMENT du calendrier, cf.
    # docstrings de `xs_momentum_sp100`/`dual_momentum_etf` — "Point dur (1)") ; le champ est
    # néanmoins propagé tel quel, cycle après cycle, pour qu'une future stratégie qui en aurait
    # besoin puisse le lire/écrire via `state["strategy_state"]` sans changement de schéma.
    working_state["strategy_state"] = dict(state.get("strategy_state") or {})

    equity_avant_cycle, poids_actuel = _estimate_equity_and_weights(cash_usd, positions_in, prices)

    cibles_brutes, strategy_signals = _combine_pockets(
        wallet_cfg, history_hourly, daily_history, working_state, strategies_by_name
    )

    risk_manager = _risk_manager_for_wallet(wallet_cfg)
    no_trade_band_scale = _no_trade_band_scale_by_symbol(wallet_cfg)
    cibles_finales, reasons = risk_manager.apply(
        cibles_brutes, working_state, prices, history_hourly, now=now,
        no_trade_band_by_symbol=no_trade_band_scale,
    )

    ledger = Ledger(cash_usd=cash_usd, positions=positions_in)
    exchange = _exchange_for_wallet(wallet_cfg)

    fills_this_cycle: List[Fill] = []
    decisions: List[dict] = []

    for symbol in full_universe:
        current_w = poids_actuel.get(symbol, 0.0)
        quote = prices.get(symbol)
        quote_available = quote is not None
        asset_class = _asset_class_of(symbol, crypto_universe)
        symbol_market_open = True if asset_class == "crypto" else market_open
        signals_for_symbol = {
            strat_name: sigs[symbol] for strat_name, sigs in strategy_signals.items() if symbol in sigs
        }
        cb_snapshot = _cb_snapshot(working_state.get("circuit_breakers") or {}, now)

        if not quote_available:
            decisions.append({
                "run_id": run_id, "ts": now.isoformat(), "wallet_id": wallet_id,
                "symbol": symbol, "asset_class": asset_class, "market_open": symbol_market_open,
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
        if asset_class == "crypto" and symbol in history_hourly_failed:
            base_reason += " ; historique horaire clôturé insuffisant/indisponible — signal non calculable ce cycle"
        if asset_class in ("equities", "etf") and symbol in daily_history_failed:
            base_reason += " ; historique journalier clôturé insuffisant/indisponible — signal non calculable ce cycle"

        decision = "NO_TRADE"
        reason = base_reason
        diff = final - current_w

        if not symbol_market_open:
            # ARCHITECTURE.md §7 : jamais d'ordre actions/ETF hors séance régulière NYSE, quelle
            # que soit la cible calculée — position existante conservée telle quelle.
            decision = "NO_TRADE"
            reason = (
                f"{base_reason} ; marché actions/ETF fermé (séance régulière NYSE "
                "09:30-16:00 America/New_York uniquement) — aucun ordre, position conservée"
            )
        elif abs(diff) > EPSILON_WEIGHT:
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
            "symbol": symbol, "asset_class": asset_class, "market_open": symbol_market_open,
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
        "strategy_state": working_state.get("strategy_state") or {},
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

    # --- 4) univers actif ce cycle = UNION des univers des 3 wallets, toutes classes d'actifs
    # confondues (crypto + poches actions/ETF, docs/ARCHITECTURE.md §11) ---
    crypto_symbols_all: List[str] = sorted({
        sym for w in config.WALLETS for sym in w["univers_crypto"]
    })

    daily_symbols_all: Set[str] = set()
    noncrypto_tradable_all: Set[str] = set()
    for w in config.WALLETS:
        for pocket in w.get("pockets", []) or []:
            ref = pocket.get("strategy_ref")
            if ref in POCKET_STRATEGY_DATA_SYMBOLS:
                daily_symbols_all |= set(POCKET_STRATEGY_DATA_SYMBOLS[ref])
                noncrypto_tradable_all |= set(POCKET_STRATEGY_TRADABLE_SYMBOLS[ref])

    all_price_symbols: List[str] = sorted(set(crypto_symbols_all) | noncrypto_tradable_all)
    # `get_prices()` (façade bot.feeds) route automatiquement chaque symbole vers l'adaptateur
    # crypto (Binance) ou actions/ETF (Yahoo) selon `bot.config.SYMBOLS_CRYPTO`/`SYMBOLS_EQUITY`
    # — UN seul appel couvre les 3 wallets, toutes classes d'actifs confondues.
    prices: Dict[str, Optional[Quote]] = get_prices(all_price_symbols) if all_price_symbols else {}
    for sym in all_price_symbols:
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

    # --- historique JOURNALIER clôturé actions/ETF (S&P100 + SPY + univers ETF risqué + IEF),
    # UNE fois, partagé entre les wallets qui portent une poche actions et/ou ETF ---
    daily_history, daily_history_failed = _gather_daily_history(sorted(daily_symbols_all))

    # --- marché actions/ETF ouvert ce cycle ? (bot.feeds.calendar, séance régulière NYSE
    # 09:30-16:00 America/New_York) : UNE fois, partagé (crypto reste 24/7, non concerné) ---
    market_open_now = is_us_market_open(now)

    # --- stratégies concrètes découvertes UNE fois (bot/strategies/*.py), partagées ---
    strategies_by_name: Dict[str, StrategyBase] = {s.name: s for s in load_strategies()}
    if not strategies_by_name:
        logger.warning(
            "load_strategies() n'a découvert AUCUNE stratégie concrète ce cycle — les 3 "
            "wallets tourneront en mode 'évalue, journalise, ne trade pas' (aucune cible "
            "brute, positions existantes conservées)."
        )

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
                    daily_history=daily_history,
                    daily_history_failed=daily_history_failed,
                    market_open=market_open_now,
                    strategies_by_name=strategies_by_name,
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
