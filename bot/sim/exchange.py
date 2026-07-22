"""ExchangeSim : simulateur d'exchange PESSIMISTE.

Principe cardinal (docs/rapport-recherche.md §3-4, docs/ARCHITECTURE.md §0.2-0.3) : toute
ambiguïté de modélisation est tranchée en défaveur du bot. Concrètement ici :
  - un achat se fait TOUJOURS au ask réel, majoré d'une pénalité de slippage ;
  - une vente se fait TOUJOURS au bid réel, minoré d'une pénalité de slippage ;
  - des frais taker s'appliquent sur le notionnel obtenu (jamais sur un notionnel "idéal") ;
  - une quote trop vieille, invalide, absente, un notionnel trop petit ou une quantité qui
    s'arrondit à zéro au pas de l'actif => rejet inconditionnel, aucun fill n'est produit ;
  - jamais de repli sur un prix mémorisé : la quote doit venir de l'appel réseau du cycle.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Dict, Optional, Union

from .fills import Fill, Quote, Reject

# Défauts pessimistes — indépendants de bot.config pour que bot/sim reste autonome et
# testable ; le runner peut les surcharger explicitement via le constructeur si besoin.
DEFAULT_MAX_QUOTE_AGE_SECONDS = 120.0
DEFAULT_MIN_NOTIONAL_USD = 10.0

# Pas de quantité réaliste par actif (granularité d'exécution). Valeurs crypto proches des
# stepSize Binance usuels ; actions traitées en lots entiers (pas de fractionnaire supposé).
DEFAULT_QTY_STEPS: Dict[str, float] = {
    # crypto
    "BTC": 0.00001,
    "ETH": 0.0001,
    "SOL": 0.001,
    "DOGE": 1.0,
    "LINK": 0.01,
    "AVAX": 0.01,
    # actions megacaps — lots entiers
    "AAPL": 1.0,
    "MSFT": 1.0,
    "GOOGL": 1.0,
    "AMZN": 1.0,
    "NVDA": 1.0,
    "META": 1.0,
}
# Repli si un symbole hors de la table ci-dessus est rencontré. On choisit le pas le plus
# conservateur (le plus grossier, whole-unit) car on ne connaît pas la nature exacte de
# l'actif — mieux vaut sous-trader que sur-trader une quantité irréaliste.
DEFAULT_UNKNOWN_SYMBOL_STEP = 1.0


def floor_to_step(qty: float, step: float) -> float:
    """Arrondit qty vers le bas au multiple de `step` le plus proche (jamais vers le haut —
    on ne veut jamais accorder au bot plus de quantité qu'un exchange réel ne le permettrait)."""
    if step is None or step <= 0:
        return max(0.0, qty)
    n = math.floor(qty / step + 1e-9)  # tolérance epsilon contre le bruit flottant
    floored = n * step
    if floored < 0:
        floored = 0.0
    # évite les résidus du type 0.1 + 0.2 en sortie
    return round(floored, 12)


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ExchangeSim:
    def __init__(
        self,
        fee_taker_bps: float,
        slippage_penalty_bps: float,
        max_quote_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
        min_notional_usd: float = DEFAULT_MIN_NOTIONAL_USD,
        qty_steps: Optional[Dict[str, float]] = None,
    ):
        if fee_taker_bps < 0 or slippage_penalty_bps < 0:
            raise ValueError("fee_taker_bps et slippage_penalty_bps doivent être >= 0")
        self.fee_taker_bps = float(fee_taker_bps)
        self.slippage_penalty_bps = float(slippage_penalty_bps)
        self.max_quote_age_seconds = float(max_quote_age_seconds)
        self.min_notional_usd = float(min_notional_usd)
        self.qty_steps: Dict[str, float] = dict(DEFAULT_QTY_STEPS)
        if qty_steps:
            self.qty_steps.update(qty_steps)

    def step_for(self, symbol: str) -> float:
        return self.qty_steps.get(symbol, DEFAULT_UNKNOWN_SYMBOL_STEP)

    def execute_order(
        self,
        side: str,
        symbol: str,
        qty: float,
        quote: Optional[Quote],
        strategy: str,
        run_id: str,
        now: Optional[datetime] = None,
    ) -> Union[Fill, Reject]:
        """Exécute (ou rejette) un ordre. Ne modifie jamais le Ledger — c'est à l'appelant
        d'appliquer le Fill retourné via Ledger.apply_fill()."""
        now = now or datetime.now(timezone.utc)
        side_u = str(side).upper()

        quote_source = getattr(quote, "source", None) if quote is not None else None
        quote_ts = getattr(quote, "ts", None) if quote is not None else None

        def reject(reason: str) -> Reject:
            return Reject(
                run_id=run_id,
                ts=now.isoformat(),
                symbol=symbol,
                strategy=strategy,
                side=side_u,
                reason=reason,
                quote_source=quote_source,
                quote_ts=quote_ts,
            )

        if side_u not in ("BUY", "SELL"):
            return reject(f"side invalide: {side!r} (attendu BUY ou SELL)")

        if quote is None:
            return reject("quote indisponible (None) — aucun trade sans prix frais réel")

        if quote.bid is None or quote.ask is None or quote.mid is None:
            return reject("quote incomplète (bid/ask/mid manquant)")

        if quote.bid <= 0 or quote.ask <= 0:
            return reject(f"quote invalide (bid={quote.bid}, ask={quote.ask} doivent être > 0)")

        if quote.bid >= quote.ask:
            return reject(f"quote invalide (bid={quote.bid} >= ask={quote.ask})")

        try:
            quote_dt = _parse_ts(quote.ts)
            age_seconds = (now - quote_dt).total_seconds()
        except Exception as exc:  # noqa: BLE001 — on transforme toute erreur de parsing en rejet
            return reject(f"horodatage de quote illisible ({quote.ts!r}): {exc}")

        if age_seconds > self.max_quote_age_seconds:
            return reject(
                f"quote périmée ({age_seconds:.1f}s > seuil {self.max_quote_age_seconds:.0f}s)"
            )
        if age_seconds < -5.0:
            # tolérance légère au décalage d'horloge ; au-delà, quote incohérente/dans le futur
            return reject(f"horodatage de quote dans le futur ({age_seconds:.1f}s)")

        if qty is None or qty <= 0:
            return reject(f"quantité demandée non positive ({qty})")

        step = self.step_for(symbol)
        qty_rounded = floor_to_step(qty, step)
        if qty_rounded <= 0:
            return reject(
                f"quantité ({qty}) arrondie à zéro au pas réaliste ({step}) pour {symbol}"
            )

        if side_u == "BUY":
            price_fill = quote.ask * (1 + self.slippage_penalty_bps / 1e4)
        else:  # SELL
            price_fill = quote.bid * (1 - self.slippage_penalty_bps / 1e4)

        notional_usd = qty_rounded * price_fill
        if notional_usd < self.min_notional_usd:
            return reject(
                f"notionnel ({notional_usd:.2f}$) < minimum ({self.min_notional_usd:.2f}$)"
            )

        fees_usd = notional_usd * self.fee_taker_bps / 1e4
        slippage_usd = abs(price_fill - quote.mid) * qty_rounded

        return Fill(
            run_id=run_id,
            ts=now.isoformat(),
            symbol=symbol,
            strategy=strategy,
            side=side_u,
            qty=qty_rounded,
            notional_usd=notional_usd,
            price_fill=price_fill,
            price_mid_ideal=quote.mid,
            fees_usd=fees_usd,
            slippage_usd=slippage_usd,
            quote_source=quote_source,
            quote_ts=quote_ts,
        )
