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
from typing import Dict, List, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = ["StrategyBase", "combine_strategies", "load_strategies"]


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
        """
        raise NotImplementedError


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
