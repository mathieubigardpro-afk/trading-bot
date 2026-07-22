"""bot.sim — simulateur d'exchange maison, PESSIMISTE par construction.

Voir docs/ARCHITECTURE.md §5.2 pour le contrat d'interface, et docs/rapport-recherche.md
§3-4 pour le framework de risque et les hypothèses de coûts sous-jacentes.
"""

from .exchange import DEFAULT_QTY_STEPS, ExchangeSim, floor_to_step
from .fills import Fill, Quote, Reject
from .ledger import Ledger

__all__ = [
    "ExchangeSim",
    "Ledger",
    "Fill",
    "Quote",
    "Reject",
    "floor_to_step",
    "DEFAULT_QTY_STEPS",
]
