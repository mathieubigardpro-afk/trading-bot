"""bot/strategies/ — interface commune des stratégies (ARCHITECTURE.md §5.5).

En V1, aucune stratégie concrète n'est livrée (`donchian.py`, `momentum_ema.py`,
`mean_reversion_rsi2.py` arriveront après la phase de backtest walk-forward, cf.
`docs/rapport-recherche.md` §2 et `docs/ARCHITECTURE.md` §8). Ce module ne contient que :

  - `StrategyBase` : l'interface abstraite que toute stratégie concrète devra respecter.
  - `combine_strategies()` : combinaison par moyenne équi-pondérée (placeholder documenté).
  - `load_strategies()` : découverte dynamique des stratégies concrètes présentes dans ce
    paquet (utilisée par `bot/runner.py`). Tant qu'aucune stratégie concrète n'est déposée
    ici, elle retourne une liste vide et le runner tourne en mode "évalue, journalise, ne
    trade jamais" (cibles brutes = positions actuelles), conformément à la consigne du
    projet ("le bot tourne, évalue, journalise, ne trade pas" en l'absence de stratégies).
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from abc import ABC, abstractmethod
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "StrategyBase",
    "combine_strategies",
    "load_strategies",
    "apply_missing_data_policy",
    "MISSING_DATA_MAX_CYCLES_DEFAULT",
]

# --- Politique commune "case 3" — données indisponibles ce cycle (ARCHITECTURE.md §12.4) ----
# Nombre de cycles horaires consécutifs de données manquantes/insuffisantes toléré pour UN
# symbole avant liquidation par prudence (position jugée "aveugle" trop longtemps). 24 cycles
# horaires = ~24h. Repris par les 3 stratégies concrètes de ce paquet via
# `apply_missing_data_policy()` ci-dessous — module unique de vérité pour ce garde-fou (pas
# dupliqué par valeur dans chaque module de stratégie, à la différence des constantes SPEC qui,
# elles, restent dupliquées par choix d'autonomie de module, cf. docstrings respectives).
MISSING_DATA_MAX_CYCLES_DEFAULT = 24


class StrategyBase(ABC):
    """Interface commune de toute stratégie concrète (ARCHITECTURE.md §5.5, §9 multi-wallets).

    `name` : identifiant stable utilisé comme clé dans `strategy_signals` (decisions.jsonl)
    et comme préfixe éventuel de `client_order_id`/`strategy` dans trades.jsonl.
    """

    name: str = "strategy_base"

    @abstractmethod
    def target_weights(
        self,
        history: Dict[str, pd.DataFrame],
        state: dict,
        profile: dict | None = None,
    ) -> Dict[str, float]:
        """Retourne un poids cible BRUT par symbole (0..1, long-only ; 0 = flat), calculé
        exclusivement à partir de `history` (bougies clôturées), de `state` (positions
        actuelles du wallet) et de `profile` (config du wallet courant, cf.
        `bot.config.wallet_config()` — `id`, `emoji`, `label`, `univers_crypto`, `risque`
        entre autres). Pure fonction : aucun appel réseau, aucune écriture disque.

        Multi-wallets (docs/ARCHITECTURE.md §9) : une MÊME classe de stratégie peut être
        instanciée pour plusieurs wallets simultanément, avec des réglages différents selon
        `profile` (ex. lookback plus court pour le wallet agressif) — `profile` est donc
        REÇU ici plutôt que fixé à la construction de la stratégie, pour permettre une seule
        instance réutilisable si son implémentation le souhaite, ou pour simplement ignorer
        ce paramètre (défaut `None`) si la stratégie n'en a pas besoin.

        Exception documentée à "pure fonction" (ARCHITECTURE.md §11.5/§12.4) : une stratégie
        PEUT muter `state["strategy_state"][self.name]` EN PLACE (aucun appel réseau, aucune
        écriture disque — seulement de la mémoire de processus, persistée par l'appelant via
        `state.json` en fin de cycle) pour se souvenir, d'un cycle horaire à l'autre, du nombre
        de cycles consécutifs où les données d'un symbole étaient manquantes/insuffisantes —
        cf. `apply_missing_data_policy()` ci-dessous, seul mécanisme prévu à cet effet.
        """
        raise NotImplementedError


def apply_missing_data_policy(
    state: Optional[dict],
    strategy_name: str,
    weights: Dict[str, float],
    missing_symbols: Iterable[str],
    max_missing_cycles: int = MISSING_DATA_MAX_CYCLES_DEFAULT,
) -> Dict[str, float]:
    """Politique commune "case 3" partagée par les stratégies concrètes de ce paquet
    (`quasi_passif_crypto`, `xs_momentum_sp100`, `dual_momentum_etf`) — cf.
    `docs/ARCHITECTURE.md` §12.4, correctif du défaut identifié sur l'incident XLM
    2026-07-23T18/T19 : un symbole dont l'historique est manquant/insuffisant/en échec de
    fetch CE cycle (transitoire, ex. un seul cycle horaire raté) ne doit JAMAIS être traité
    comme une décision de sortie (poids 0 explicite) — cela force une liquidation, puis un
    probable rachat au cycle suivant une fois la donnée revenue, générant un aller-retour à
    double frais sur un simple hoquet de fetch, sans aucun signal réel de marché.

    Distinction à la charge de l'APPELANT (chaque stratégie), PAS de cette fonction :
      - `weights` : poids déjà DÉCIDÉS ce cycle (cas 1 "tendance/signal off confirmé" -> 0.0,
        cas 2 "tendance/signal on" -> poids normal) — donnée disponible, décision réelle.
      - `missing_symbols` : symboles pour lesquels AUCUNE décision fiable n'a pu être prise ce
        cycle faute de données (cas 3) — ne doivent PAS figurer dans `weights`.

    Comportement (cas 3) :
      - Sous le garde-fou (`max_missing_cycles`, 24 cycles horaires consécutifs par défaut,
        ~24h) : le symbole est OMIS du dict retourné. `bot.risk.manager.RiskManager.apply`
        traite déjà nativement l'absence d'un symbole dans `cibles_brutes` comme "aucune cible
        brute fournie -> poids conservé" (`raw is None -> interim[symbol] = current_w`, cf.
        commentaire de `xs_momentum_sp100.target_weights`) — AUCUN mécanisme de gel
        supplémentaire n'est donc nécessaire côté risque : le "gel" EST l'omission.
      - Au-delà du garde-fou (position jugée "aveugle" trop longtemps) : le symbole est mis à
        0.0 explicitement dans le dict retourné (liquidation par prudence, décision journalisée
        distinctement par `bot/runner.py` via `state["strategy_state"]`) et son compteur est
        réinitialisé.
      - Un symbole absent de `missing_symbols` (donnée redevenue disponible, ou jamais
        manquante) voit son compteur immédiatement remis à zéro.

    Compteurs persistés dans
    `state["strategy_state"][strategy_name]["missing_data_cycles"][symbole]` (mutation EN
    PLACE de `state`, cf. docstring de `StrategyBase.target_weights`). Si `state` n'est pas un
    dict exploitable (défense en profondeur, ne devrait jamais arriver en usage normal), la
    fonction se contente de geler (omettre) SANS compteur ni exception — jamais de liquidation
    automatique sans mémoire fiable du nombre de cycles écoulés (posture pessimiste par défaut).

    `state["strategy_state"][strategy_name]["liquidated_by_missing_data_this_cycle"]` :
    `{symbole: nombre_de_cycles_consécutifs}` REBÂTI INTÉGRALEMENT à CHAQUE appel (jamais
    accumulé) — ne contient QUE les symboles liquidés par le garde-fou CE cycle précis. Lu par
    `bot/runner.py` juste après l'appel à `target_weights()` (même cycle, cf.
    `bot/runner.py:_combine_pockets`) pour journaliser une raison distincte de la sortie de
    tendance légitime (cas 1) dans `decisions.jsonl`.
    """
    missing_symbols = list(missing_symbols)
    result = dict(weights)

    if not isinstance(state, dict):
        # Pas d'état mutable exploitable : gel du cycle courant seulement (jamais de compteur
        # ni de liquidation automatique sans mémoire fiable, posture pessimiste par défaut).
        for symbol in missing_symbols:
            result.pop(symbol, None)
        return result

    strategy_state_all = state.get("strategy_state")
    if not isinstance(strategy_state_all, dict):
        strategy_state_all = {}
        state["strategy_state"] = strategy_state_all

    own_state = strategy_state_all.get(strategy_name)
    if not isinstance(own_state, dict):
        own_state = {}
        strategy_state_all[strategy_name] = own_state

    counters = own_state.get("missing_data_cycles")
    if not isinstance(counters, dict):
        counters = {}
        own_state["missing_data_cycles"] = counters

    # Rebâti INTÉGRALEMENT à chaque appel (jamais accumulé d'un cycle à l'autre) : reflète
    # UNIQUEMENT les liquidations par prudence décidées CE cycle précis — permet à
    # `bot/runner.py` de journaliser une raison distincte ("liquidé après N cycles consécutifs
    # de donnée manquante") sans avoir à deviner la cause a posteriori à partir du seul poids
    # 0.0 final (indiscernable, sinon, d'une vraie sortie de tendance cas 1).
    liquidated_this_cycle: Dict[str, int] = {}

    for symbol in missing_symbols:
        result.pop(symbol, None)  # jamais fourni par l'appelant en principe, sécurité
        consecutive = int(counters.get(symbol, 0) or 0) + 1
        if consecutive >= int(max_missing_cycles):
            result[symbol] = 0.0  # liquidation par prudence : position aveugle trop longtemps
            counters.pop(symbol, None)
            liquidated_this_cycle[symbol] = consecutive
        else:
            counters[symbol] = consecutive

    # Symboles NON manquants ce cycle (décidés ou jamais en défaut) : compteur remis à zéro.
    for symbol in list(counters.keys()):
        if symbol not in missing_symbols:
            counters.pop(symbol, None)

    own_state["liquidated_by_missing_data_this_cycle"] = liquidated_this_cycle

    return result


def combine_strategies(
    strategies: List[StrategyBase],
    history: Dict[str, pd.DataFrame],
    state: dict,
    profile: dict | None = None,
) -> Tuple[Dict[str, float], Dict[str, Dict[str, float]]]:
    """Combine plusieurs stratégies par MOYENNE ÉQUI-PONDÉRÉE de leurs poids par symbole
    (placeholder documenté — cf. ARCHITECTURE.md §5.5 et §8, à remplacer par une allocation
    calibrée une fois les backtests walk-forward disponibles).

    Retourne `(cibles_brutes_combinees, signaux_par_strategie)` où le second élément
    alimente `strategy_signals` dans `decisions.jsonl`. Si `strategies` est vide, retourne
    `({}, {})` — aucun signal, équivalent à 100% cash / positions inchangées (c'est au
    runner de décider du repli exact, cf. `bot/runner.py`).

    `profile` (multi-wallets) : transmis tel quel à chaque `target_weights()` — c'est le
    wallet courant qui est évalué, cf. `bot/runner.py`.
    """
    signals_par_strategie: Dict[str, Dict[str, float]] = {}
    if not strategies:
        return {}, signals_par_strategie

    sums: Dict[str, float] = {}
    counts: Dict[str, int] = {}
    for strat in strategies:
        weights = strat.target_weights(history, state, profile) or {}
        signals_par_strategie[strat.name] = dict(weights)
        for symbol, w in weights.items():
            sums[symbol] = sums.get(symbol, 0.0) + float(w)
            counts[symbol] = counts.get(symbol, 0) + 1

    cibles_brutes = {sym: sums[sym] / counts[sym] for sym in sums}
    return cibles_brutes, signals_par_strategie


def load_strategies() -> List[StrategyBase]:
    """Découverte dynamique des stratégies concrètes déposées dans `bot/strategies/`.

    Parcourt tous les sous-modules de ce paquet (hors `__init__`) et instancie toute classe
    qui hérite de `StrategyBase` (et n'est pas elle-même abstraite). Si le paquet ne contient
    aucun module de stratégie concret (cas V1), retourne `[]` — le runner interprète cela
    comme "aucune stratégie active ce cycle".

    Une erreur d'import/instanciation sur UN module de stratégie ne doit jamais faire
    planter tout le cycle horaire (principe pessimiste : mieux vaut ne pas trader que
    planter) — elle est journalisée en avertissement et ce module est simplement ignoré.
    """
    strategies: List[StrategyBase] = []
    package = importlib.import_module(__name__)
    for module_info in pkgutil.iter_modules(package.__path__):
        mod_name = module_info.name
        if mod_name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{mod_name}")
        except Exception as exc:  # noqa: BLE001 — isolation stricte entre stratégies
            logger.warning("load_strategies: échec d'import du module %s: %s", mod_name, exc)
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is StrategyBase or not issubclass(obj, StrategyBase):
                continue
            if inspect.isabstract(obj):
                continue
            if obj.__module__ != module.__name__:
                continue  # évite les classes ré-importées (ex. StrategyBase lui-même)
            try:
                strategies.append(obj())
            except Exception as exc:  # noqa: BLE001
                logger.warning("load_strategies: échec d'instanciation de %s: %s", obj, exc)
    return strategies
