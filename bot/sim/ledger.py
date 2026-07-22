"""Ledger : grand livre du compte fictif (cash, positions, PnL).

Invariants stricts appliqués après CHAQUE opération (voir _check_invariants) :
  - cash_usd >= 0 (impossible d'acheter sans cash suffisant) ;
  - toute quantité de position >= 0 (long-only strict, impossible de survendre) ;
  - equity() exige un prix de marque explicite pour chaque position détenue (aucune
    supposition implicite sur un prix manquant — voir ARCHITECTURE.md §5.2).
"""

from __future__ import annotations

from typing import Dict, Optional

from .fills import Fill

EPSILON_QTY = 1e-9
EPSILON_CASH = 1e-6


class Ledger:
    def __init__(self, cash_usd: float, positions: Optional[Dict[str, dict]] = None):
        if cash_usd < -EPSILON_CASH:
            raise ValueError(f"cash_usd initial ne peut pas être négatif ({cash_usd})")
        self.cash_usd: float = float(cash_usd)
        self.positions: Dict[str, dict] = {}
        if positions:
            for symbol, pos in positions.items():
                qty = float(pos.get("qty", 0.0))
                prix_moyen = float(pos.get("prix_moyen", 0.0))
                if qty < -EPSILON_QTY:
                    raise ValueError(f"position initiale négative pour {symbol}: {qty}")
                if qty > EPSILON_QTY:
                    self.positions[symbol] = {"qty": qty, "prix_moyen": prix_moyen}
        self._check_invariants()

    def _check_invariants(self) -> None:
        assert self.cash_usd >= -EPSILON_CASH, f"invariant violé: cash négatif ({self.cash_usd})"
        for symbol, pos in self.positions.items():
            assert pos["qty"] >= -EPSILON_QTY, (
                f"invariant violé: position négative pour {symbol} ({pos['qty']})"
            )
            assert pos["prix_moyen"] >= 0, (
                f"invariant violé: prix moyen négatif pour {symbol} ({pos['prix_moyen']})"
            )

    def apply_fill(self, fill: Fill) -> None:
        symbol = fill.symbol
        side = fill.side.upper()

        if fill.qty <= 0:
            raise ValueError(f"Fill.qty doit être > 0 (reçu {fill.qty})")

        if side == "BUY":
            total_cost = fill.notional_usd + fill.fees_usd
            if total_cost - self.cash_usd > EPSILON_CASH:
                raise ValueError(
                    f"cash insuffisant pour acheter {symbol}: coût {total_cost:.2f}$ "
                    f"> cash disponible {self.cash_usd:.2f}$"
                )
            self.cash_usd -= total_cost
            if self.cash_usd < 0:
                self.cash_usd = 0.0  # neutralise un résidu flottant négatif infinitésimal

            pos = self.positions.get(symbol, {"qty": 0.0, "prix_moyen": 0.0})
            qty_avant = pos["qty"]
            prix_avant = pos["prix_moyen"]
            qty_apres = qty_avant + fill.qty
            cout_total_avant = qty_avant * prix_avant
            prix_moyen_apres = (cout_total_avant + fill.notional_usd) / qty_apres
            self.positions[symbol] = {"qty": qty_apres, "prix_moyen": prix_moyen_apres}
            fill.realized_pnl_usd = None

        elif side == "SELL":
            pos = self.positions.get(symbol)
            qty_detenue = pos["qty"] if pos else 0.0
            if fill.qty - qty_detenue > EPSILON_QTY:
                raise ValueError(
                    f"survente interdite (long-only strict) pour {symbol}: "
                    f"qty vendue {fill.qty} > position détenue {qty_detenue}"
                )
            prix_moyen_avant = pos["prix_moyen"] if pos else 0.0
            realized_pnl = (fill.price_fill - prix_moyen_avant) * fill.qty - fill.fees_usd

            self.cash_usd += (fill.notional_usd - fill.fees_usd)

            qty_apres = qty_detenue - fill.qty
            if qty_apres < EPSILON_QTY:
                self.positions.pop(symbol, None)
            else:
                self.positions[symbol] = {"qty": qty_apres, "prix_moyen": prix_moyen_avant}

            fill.realized_pnl_usd = realized_pnl

        else:
            raise ValueError(f"Fill.side invalide: {fill.side!r} (attendu BUY ou SELL)")

        self._check_invariants()

    def equity(self, mark_prices: Dict[str, float]) -> float:
        """cash + somme(qty * mark_prices[symbole]) pour chaque position détenue. Exige un
        prix mid explicite par symbole détenu — aucun repli implicite (voir docstring module)."""
        total = self.cash_usd
        for symbol, pos in self.positions.items():
            if symbol not in mark_prices or mark_prices[symbol] is None:
                raise ValueError(
                    f"prix de marque manquant pour {symbol}: l'appelant doit fournir "
                    f"explicitement un mid (dernier mid connu si marché fermé) pour chaque "
                    f"position détenue"
                )
            price = mark_prices[symbol]
            if price <= 0:
                raise ValueError(f"prix de marque invalide pour {symbol}: {price}")
            total += pos["qty"] * price
        assert total >= -EPSILON_CASH, f"invariant violé: equity négative ({total})"
        return max(total, 0.0)
