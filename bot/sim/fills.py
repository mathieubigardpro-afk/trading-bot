"""Structures de données du simulateur d'exchange : Quote, Fill, Reject.

Quote est redéfini ici (mêmes champs que bot.feeds.Quote décrit dans ARCHITECTURE.md §5.1)
pour que bot/sim reste testable de façon autonome, sans dépendance dure sur bot/feeds qui
peut ne pas encore exister au moment où ce module est chargé. Duck-typing : ExchangeSim
n'exige rien de plus qu'un objet exposant .bid/.ask/.mid/.ts/.source, donc un Quote produit
par bot.feeds fonctionne indifféremment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Quote:
    bid: float
    ask: float
    mid: float
    ts: str        # ISO8601 UTC, horodatage côté source
    source: str     # "binance" | "coinbase" | "yahoo" | "yahoo_synthetic_spread" | ...


@dataclass
class Fill:
    """Un fill exécuté par ExchangeSim. Toujours au moins aussi mauvais que le mid pour le
    bot (achat au ask majoré, vente au bid minoré) — voir ExchangeSim.execute_order."""

    run_id: str
    ts: str
    symbol: str
    strategy: str
    side: str            # "BUY" | "SELL"
    qty: float
    notional_usd: float
    price_fill: float
    price_mid_ideal: float
    fees_usd: float
    slippage_usd: float
    quote_source: str
    quote_ts: str
    realized_pnl_usd: Optional[float] = None   # renseigné uniquement pour SELL (par le Ledger)

    @property
    def slippage_bps_implicit(self) -> float:
        """Slippage implicite en bps du notionnel, dérivé (pas un champ persistant du
        dataclass) — utile pour l'audit/les tests, ne casse pas le schéma trades.jsonl."""
        if self.notional_usd == 0:
            return 0.0
        return (self.slippage_usd / self.notional_usd) * 1e4


@dataclass
class Reject:
    """Un ordre refusé par ExchangeSim avant tout fill — jamais d'effet sur le Ledger."""

    run_id: str
    ts: str
    symbol: str
    strategy: str
    side: str
    reason: str
    quote_source: Optional[str] = None
    quote_ts: Optional[str] = None
