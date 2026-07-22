"""Façade publique de `bot.feeds` — routage crypto/actions.

Voir `docs/ARCHITECTURE.md` §5.1 pour le contrat exact. Ce module ne fait
aucune requête réseau lui-même : il délègue à `bot.feeds.crypto` ou
`bot.feeds.equities` selon l'appartenance du symbole à
`config.SYMBOLS_CRYPTO` / `config.SYMBOLS_EQUITY`, et fusionne les résultats
dans un seul dict conservant l'ordre de la liste `symbols` reçue en entrée.
"""

from __future__ import annotations

import logging

import pandas as pd

from bot.feeds import calendar  # noqa: F401  (réexporté pour bot.feeds.calendar.is_us_market_open)
from bot.feeds._config_fallback import cfg
from bot.feeds.calendar import is_us_market_open  # noqa: F401  (réexport de confort)
from bot.feeds.crypto import get_history_crypto, get_prices_crypto
from bot.feeds.equities import get_history_equity, get_prices_equity
from bot.feeds.types import HistoryUnavailableError, Quote  # noqa: F401  (réexport)

logger = logging.getLogger(__name__)

__all__ = [
    "Quote",
    "HistoryUnavailableError",
    "get_prices",
    "get_history",
    "is_us_market_open",
]


def _asset_class(symbol: str) -> str | None:
    if symbol in cfg.SYMBOLS_CRYPTO:
        return "crypto"
    if symbol in cfg.SYMBOLS_EQUITY:
        return "equity"
    return None


def get_prices(symbols: list[str]) -> dict[str, Quote | None]:
    """Retourne un `Quote` par symbole demandé (ou `None` si indisponible).
    Route automatiquement vers l'adaptateur crypto ou actions. Un symbole
    hors de l'univers connu (`config.SYMBOLS_CRYPTO`/`SYMBOLS_EQUITY`) est
    retourné à `None` avec un avertissement journalisé — ce n'est jamais une
    exception qui remonte, conformément au contrat de `get_prices`."""
    crypto_symbols = [s for s in symbols if _asset_class(s) == "crypto"]
    equity_symbols = [s for s in symbols if _asset_class(s) == "equity"]
    unknown_symbols = [s for s in symbols if _asset_class(s) is None]

    result: dict[str, Quote | None] = {}
    if crypto_symbols:
        result.update(get_prices_crypto(crypto_symbols))
    if equity_symbols:
        result.update(get_prices_equity(equity_symbols))
    for sym in unknown_symbols:
        logger.warning("get_prices: symbole hors univers connu, ignoré: %s", sym)
        result[sym] = None

    # Préserve l'ordre d'entrée de `symbols` dans le dict retourné.
    return {sym: result[sym] for sym in symbols}


def get_history(symbol: str, n_hours: int) -> pd.DataFrame:
    """Retourne EXACTEMENT les `n_hours` dernières bougies clôturées pour
    `symbol` (colonnes open/high/low/close/volume, index timestamp UTC
    croissant). Lève `HistoryUnavailableError` si moins de `n_hours` bougies
    valides ne peuvent être obtenues. Lève `ValueError` si `symbol` n'est
    dans aucun des deux univers connus (erreur de configuration, distincte
    d'une simple indisponibilité de données marché)."""
    asset_class = _asset_class(symbol)
    if asset_class == "crypto":
        return get_history_crypto(symbol, n_hours)
    if asset_class == "equity":
        return get_history_equity(symbol, n_hours)
    raise ValueError(f"get_history: symbole hors univers connu: {symbol}")
