"""Types partagés du module `bot.feeds` — aucune dépendance réseau ici.

Isolé dans son propre fichier (plutôt que dans `__init__.py`) pour que
`crypto.py`, `equities.py` et `calendar.py` puissent tous l'importer sans
provoquer d'import circulaire avec la façade `bot/feeds/__init__.py`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Quote:
    """Un prix bid/ask/mid frais, horodaté côté source.

    `ts` est l'horodatage ISO8601 UTC de la quote telle que rapportée par la
    source (ou, quand la source ne fournit aucun horodatage propre — cas de
    Binance bookTicker — l'heure de réception de la réponse HTTP, qui est
    alors la meilleure approximation disponible et documentée comme telle
    dans le code de `crypto.py`).

    `delayed` : `True` si cette quote, bien qu'utilisée (dans le seuil de fraîcheur), est connue
    comme différée (au-delà de `config.EQUITY_QUOTE_REALTIME_THRESHOLD_SECONDS` pour les
    actions/ETF, cf. `bot/feeds/equities.py`) — jamais mis à `True` pour la crypto (Binance/
    Coinbase temps réel). Propagé jusqu'à `decisions.jsonl`/`trades.jsonl` (`quote_delayed`)
    pour journaliser honnêtement l'écart potentiel vs un prix "idéal" instantané, cf.
    `docs/ARCHITECTURE.md` §5.1/§12.
    """

    bid: float
    ask: float
    mid: float
    ts: str
    source: str
    delayed: bool = False


class HistoryUnavailableError(Exception):
    """Levée par `get_history()` quand moins de `n_hours` bougies clôturées
    valides n'ont pu être obtenues, sur AUCUNE source (primaire + fallback).
    L'appelant doit traiter ceci comme "signal désactivé pour ce symbole ce
    cycle" — ne jamais construire une history partielle silencieusement.
    """
