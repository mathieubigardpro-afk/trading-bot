"""bot/config.py — constantes de référence uniques du bot (ARCHITECTURE.md §2).

Source de vérité unique. Aucun autre module ne doit recopier ces valeurs en dur ; les
modules `bot.feeds`, `bot.risk`, `bot.persist` (et `bot.runner`) lisent ces attributs via
leurs modules `_config_fallback.py` / `config_fallback.py` respectifs, qui font
`importlib.import_module("bot.config")` et donnent la priorité à CE fichier dès qu'un
attribut y est défini — leurs propres valeurs par défaut ne servent que tant que ce fichier
n'existe pas (construction en parallèle du reste du dépôt).

Note de calibrage IMPORTANT (écart assumé avec le tableau brut d'ARCHITECTURE.md §2) :
Le profil du projet est explicitement AGRESSIF. Les modules `bot/risk/` déjà livrés et
testés (voir `bot/risk/config_fallback.py`, `bot/risk/circuit_breakers.py`) ont été
calibrés avec des seuils de circuit breakers et des caps par actif RECALIBRÉS par rapport
aux valeurs génériques listées dans le corps du texte d'ARCHITECTURE.md §2 (3%/15%/25%,
cap unique 25%) — qui elles-mêmes proviennent du rapport de recherche §3C, explicitement
générique et non spécifique au profil agressif retenu pour ce bot. Ce fichier reprend donc
le calibrage AGRESSIF déjà implémenté et couvert par 37 tests dans `bot/tests/test_risk_manager.py`
et `bot/tests/test_circuit_breakers.py`, plutôt que les valeurs génériques : perte 24h > 4%
(pas 3%), drawdown > 20%/30% (pas 15%/25%), cap par actif différencié 25% crypto / 15% action
(pas un cap unique 25%). Les breakers eux-mêmes (leur existence, jamais leur suppression) et
le principe pessimiste général restent conformes à ARCHITECTURE.md/rapport-recherche.md.
"""

from __future__ import annotations

# --- Univers ---
SYMBOLS_CRYPTO = ["BTC", "ETH", "SOL", "DOGE", "LINK", "AVAX"]
CRYPTO_PAIR_BINANCE = {  # symbole interne -> paire Binance
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "DOGE": "DOGEUSDT", "LINK": "LINKUSDT", "AVAX": "AVAXUSDT",
}
CRYPTO_PAIR_COINBASE = {  # fallback si Binance indisponible
    "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
    "DOGE": "DOGE-USD", "LINK": "LINK-USD", "AVAX": "AVAX-USD",
}
SYMBOLS_EQUITY = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META"]  # panel megacaps, ajustable

# --- Capital ---
INITIAL_CASH_USD = 100_000.0

# --- Coûts (§4 rapport + durcissement pessimiste explicite du projet) ---
FEE_TAKER_BPS = 10          # 0.10%, tous ordres sont simulés "taker" (market/IOC)
SLIPPAGE_PENALTY_BPS = 5    # 0.05%, appliqué contre le sens de l'ordre, en plus du spread payé
MIN_NOTIONAL_USD = 10.0     # notionnel minimum d'un ordre pour être exécuté (pessimiste)
MAX_QUOTE_AGE_SECONDS = 120.0  # âge maximum d'une quote pour être utilisée par ExchangeSim

# --- Fraîcheur des prix (feeds — distinct de MAX_QUOTE_AGE_SECONDS ci-dessus utilisé par ExchangeSim) ---
STALENESS_MAX_SECONDS_CRYPTO = 300   # 5 min
STALENESS_MAX_SECONDS_EQUITY = 300   # 5 min (pendant heures de marché uniquement)

# --- Spread synthétique actions (désactivé par défaut, cf. ARCHITECTURE.md §5.1) ---
EQUITY_SYNTHETIC_SPREAD_ENABLED = False

# --- Risque — calibrage AGRESSIF (voir note en tête de fichier) ---
VOL_TARGET_ANNUALIZED_MIN = 0.25
VOL_TARGET_ANNUALIZED_MAX = 0.30
VOL_TARGET_ANNUALIZED = 0.275          # point médian utilisé par défaut
CAP_PER_ASSET_CRYPTO = 0.25            # 25% équity max par actif crypto
CAP_PER_ASSET_EQUITY = 0.15            # 15% équity max par actif action
CAP_PER_ASSET = CAP_PER_ASSET_CRYPTO   # alias documentaire (ARCHITECTURE.md §2), non lu directement
GROSS_EXPOSURE_MAX = 0.80              # 80% équity max, somme des expositions absolues
NO_TRADE_BAND = 0.05                   # ±5 points de % autour de la cible : pas d'ordre
VOL_EWMA_HALFLIFE_HOURS = 60           # 48-72h, rapport §3A
VOL_COLDSTART_MIN_POINTS = 30          # sous ce seuil d'historique -> vol_scalar prudent
VOL_COLDSTART_SCALAR = 0.5

# --- Circuit breakers (calibrage AGRESSIF explicite, voir note en tête de fichier) ---
CB_DAILY_LOSS_FREEZE_PCT = 0.04        # perte 24h glissantes > 4% -> gel nouvelles entrées 24h
CB_DAILY_LOSS_FREEZE_HOURS = 24
CB_CONSECUTIVE_LOSSES_TRIGGER = 5      # 5 pertes consécutives -> cooldown
CB_COOLDOWN_HOURS = 24
CB_DD_HALF_SIZE_PCT = 0.20             # drawdown > 20% -> tailles cibles /2
CB_DD_FLATTEN_PCT = 0.30               # drawdown > 30% -> flatten total + observation

# --- Filtres de régime (§3D rapport — non implémentés dans bot/risk, cf. sa docstring) ---
REGIME_SMA_DAYS = 200
REGIME_ATR_PERCENTILE_WINDOW_DAYS = 90
REGIME_ATR_PERCENTILE_MAX = 0.90

# --- Chemins ---
STATE_DIR = "state"
STATE_JSON = f"{STATE_DIR}/state.json"
TRADES_JSONL = f"{STATE_DIR}/trades.jsonl"
EQUITY_JSONL = f"{STATE_DIR}/equity.jsonl"
DECISIONS_JSONL = f"{STATE_DIR}/decisions.jsonl"

# --- Historique requis pour les stratégies / filtres de régime ---
HISTORY_N_HOURS = max(720, REGIME_SMA_DAYS * 24)
