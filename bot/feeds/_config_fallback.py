"""Repli local des constantes de `bot/config.py` nécessaires à `bot.feeds`.

`bot/config.py` (hors périmètre de ce module) est la SOURCE DE VÉRITÉ unique
documentée dans `docs/ARCHITECTURE.md` §2. Ce fichier ne fait que permettre à
`bot.feeds` de fonctionner et d'être testé de façon autonome si `bot/config.py`
n'est pas encore présent dans l'arbre (construction en parallèle par d'autres
agents) — dès que `bot/config.py` existe, ses valeurs sont utilisées en
priorité absolue et ce module ne sert plus que de garde-fou silencieux.

Aucune logique ici : uniquement des constantes, recopiées à l'identique de
`docs/ARCHITECTURE.md` §2 pour rester cohérentes avec la source de vérité.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

_DEFAULTS = {
    "SYMBOLS_CRYPTO": ["BTC", "ETH", "SOL", "DOGE", "LINK", "AVAX"],
    "CRYPTO_PAIR_BINANCE": {
        "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
        "DOGE": "DOGEUSDT", "LINK": "LINKUSDT", "AVAX": "AVAXUSDT",
    },
    "CRYPTO_PAIR_COINBASE": {
        "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
        "DOGE": "DOGE-USD", "LINK": "LINK-USD", "AVAX": "AVAX-USD",
    },
    "SYMBOLS_EQUITY": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META"],
    "STALENESS_MAX_SECONDS_CRYPTO": 300,
    "STALENESS_MAX_SECONDS_EQUITY": 300,
    "EQUITY_SYNTHETIC_SPREAD_ENABLED": False,
}


def _load() -> SimpleNamespace:
    try:
        real_config = importlib.import_module("bot.config")
    except ImportError:
        real_config = None

    values = dict(_DEFAULTS)
    if real_config is not None:
        for key in _DEFAULTS:
            if hasattr(real_config, key):
                values[key] = getattr(real_config, key)

        # Univers crypto étendu (multi-wallets, cf. bot/config.py:CRYPTO_SYMBOLS_30 / le
        # wallet "agressif") : la façade bot.feeds doit reconnaître TOUS les symboles
        # utilisés par N'IMPORTE quel wallet (pas seulement les 6 "majors" historiques),
        # pour router get_prices()/get_history() correctement quel que soit le wallet
        # appelant. Fusion, jamais de remplacement destructif des paires déjà résolues.
        if hasattr(real_config, "CRYPTO_SYMBOLS_30"):
            values["SYMBOLS_CRYPTO"] = sorted(
                set(values["SYMBOLS_CRYPTO"]) | set(real_config.CRYPTO_SYMBOLS_30)
            )
            merged_binance = dict(getattr(real_config, "CRYPTO_PAIR_BINANCE_30", {}))
            merged_binance.update(values["CRYPTO_PAIR_BINANCE"])
            values["CRYPTO_PAIR_BINANCE"] = merged_binance
            merged_coinbase = dict(getattr(real_config, "CRYPTO_PAIR_COINBASE_30", {}))
            merged_coinbase.update(values["CRYPTO_PAIR_COINBASE"])
            values["CRYPTO_PAIR_COINBASE"] = merged_coinbase
    return SimpleNamespace(**values)


# Résolu une fois à l'import. Les tests qui veulent forcer le repli peuvent
# recharger ce module (`importlib.reload`) après avoir masqué `bot.config`.
cfg = _load()
