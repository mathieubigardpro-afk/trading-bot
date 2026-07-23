"""bot/config.py — constantes de référence uniques du bot (ARCHITECTURE.md §2, §9 multi-wallets).

Source de vérité unique. Aucun autre module ne doit recopier ces valeurs en dur ; les
modules `bot.feeds`, `bot.risk`, `bot.persist` (et `bot.runner`) lisent ces attributs via
leurs modules `_config_fallback.py` / `config_fallback.py` respectifs.

--------------------------------------------------------------------------------------
ÉVOLUTION MULTI-WALLETS (2026-07-23)
--------------------------------------------------------------------------------------
Le bot ne gère plus un portefeuille unique de 100 000 $. Il gère désormais TROIS wallets
indépendants de 1 000 € chacun (`WALLETS` ci-dessous), vivant le même marché en parallèle,
avec trois profils de risque distincts (prudent / équilibré / agressif). L'ancien
calibrage "AGRESSIF" unique (`VOL_TARGET_ANNUALIZED`, `CB_*`, `CAP_PER_ASSET_*` ci-dessous)
est CONSERVÉ tel quel dans ce fichier pour :
  1. ne rien casser dans les modules bas niveau (`bot.risk`, `bot.sim`, `bot.persist`) qui
     restent génériques et paramétrables (aucune de ces constantes n'y est plus lue
     directement — chaque wallet construit son propre `RiskManager`/`ExchangeSim` avec les
     valeurs de son propre profil, cf. `bot/runner.py`) ;
  2. documenter/reproduire le calibrage de l'ancien portefeuille 100k$, désormais archivé
     tel quel dans `state/archive-100k/` (rien n'est détruit, cf. ARCHITECTURE.md §9).
"""

from __future__ import annotations

# ======================================================================================
# --- Univers "historique" (rétro-compatibilité bas niveau / archive 100k$) ---
# ======================================================================================
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

# --- Capital (archive 100k$ uniquement — les wallets actifs utilisent WALLETS[*].capital_initial_eur) ---
INITIAL_CASH_USD = 100_000.0

# --- Coûts (§4 rapport + durcissement pessimiste explicite du projet) ---
FEE_TAKER_BPS = 10          # 0.10%, tous ordres sont simulés "taker" (market/IOC)
SLIPPAGE_PENALTY_BPS = 5    # 0.05%, appliqué contre le sens de l'ordre, en plus du spread payé

# --- min_notional : AJUSTÉ 10$ -> 5$ pour les wallets 1 000€ (voir §9.4 ARCHITECTURE.md) ---
# Avec un capital ~1 080$ (1 000€) et des caps par actif de 20-30%, une position typique
# pèse 50-300$ ; la bande no-trade (5%) et le vol-scalar cold-start (x0.5) peuvent réduire
# un delta d'ordre isolé sous les 10$ initiaux (ex. rééquilibrage fin de cycle sur un petit
# actif). 5$ reste strictement positif et pessimiste (aucun exchange réel n'autoriserait un
# notional inférieur à quelques dollars de toute façon) tout en restant praticable aux
# montants réels de ce produit. Documenté explicitement — décision assumée, pas un oubli.
MIN_NOTIONAL_USD = 5.0
MAX_QUOTE_AGE_SECONDS = 120.0  # âge maximum d'une quote pour être utilisée par ExchangeSim

# --- Fraîcheur des prix (feeds — distinct de MAX_QUOTE_AGE_SECONDS ci-dessus utilisé par ExchangeSim) ---
STALENESS_MAX_SECONDS_CRYPTO = 300   # 5 min
STALENESS_MAX_SECONDS_EQUITY = 300   # 5 min (pendant heures de marché uniquement)

# --- FX EUR/USD (nouveau, multi-wallets) ---
# Deux sources gratuites sans clé, testables depuis des runners GitHub Actions ; fallback
# dernier taux connu (persisté par wallet dans state.json, marqué stale) si les deux échouent.
FX_FRANKFURTER_URL = "https://api.frankfurter.app/latest"
FX_ERAPI_URL = "https://open.er-api.com/v6/latest/EUR"
FX_HTTP_TIMEOUT_SECONDS = 10
FX_STALENESS_WARN_SECONDS = 3600 * 24 * 3  # 3 jours : au-delà, avertissement supplémentaire journalisé

# --- Spread synthétique actions (désactivé par défaut, cf. ARCHITECTURE.md §5.1) ---
EQUITY_SYNTHETIC_SPREAD_ENABLED = False

# --- Risque — calibrage "AGRESSIF" historique (archive 100k$ uniquement, voir bandeau ci-dessus) ---
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

# --- Circuit breakers (archive 100k$, calibrage AGRESSIF historique) ---
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

# --- Chemins (archive 100k$) ---
STATE_DIR = "state"
STATE_JSON = f"{STATE_DIR}/state.json"
TRADES_JSONL = f"{STATE_DIR}/trades.jsonl"
EQUITY_JSONL = f"{STATE_DIR}/equity.jsonl"
DECISIONS_JSONL = f"{STATE_DIR}/decisions.jsonl"

# --- Chemins multi-wallets (nouveau) ---
WALLETS_DIR = f"{STATE_DIR}/wallets"
CYCLE_JSON = f"{STATE_DIR}/cycle.json"          # idempotence GLOBALE (un run_id = les 3 wallets)
ARCHIVE_100K_DIR = f"{STATE_DIR}/archive-100k"  # ancien portefeuille unique, conservé tel quel


def wallet_state_dir(wallet_id: str) -> str:
    return f"{WALLETS_DIR}/{wallet_id}"


def wallet_state_json(wallet_id: str) -> str:
    return f"{wallet_state_dir(wallet_id)}/state.json"


def wallet_trades_jsonl(wallet_id: str) -> str:
    return f"{wallet_state_dir(wallet_id)}/trades.jsonl"


def wallet_equity_jsonl(wallet_id: str) -> str:
    return f"{wallet_state_dir(wallet_id)}/equity.jsonl"


def wallet_decisions_jsonl(wallet_id: str) -> str:
    return f"{wallet_state_dir(wallet_id)}/decisions.jsonl"


# --- Historique requis pour les stratégies / filtres de régime ---
HISTORY_N_HOURS = max(720, REGIME_SMA_DAYS * 24)

# ======================================================================================
# --- Univers crypto étendu (30 paires) pour le wallet AGRESSIF ---
# Reprise exacte de `tools/fetch_data.py: CRYPTO_SYMBOLS` (le pipeline de données
# historiques), pour que l'univers "agressif" corresponde aux données réellement
# collectées par ce dépôt.
# ======================================================================================
CRYPTO_SYMBOLS_30 = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT", "LINK",
    "LTC", "TRX", "BCH", "ETC", "UNI", "ATOM", "NEAR", "FIL", "APT", "ARB",
    "OP", "INJ", "ICP", "HBAR", "AAVE", "ALGO", "SAND", "MANA", "XLM", "VET",
]
CRYPTO_PAIR_BINANCE_30 = {sym: f"{sym}USDT" for sym in CRYPTO_SYMBOLS_30}
CRYPTO_PAIR_COINBASE_30 = {sym: f"{sym}-USD" for sym in CRYPTO_SYMBOLS_30}

# --- Paliers de coûts (paliers "majors / mids / smalls") — wallet AGRESSIF uniquement.
# Les deux autres wallets (univers restreint à des majors) utilisent le palier "majors"
# uniforme. Valeurs pessimistes croissantes avec l'illiquidité présumée du palier (aucune
# donnée de profondeur de carnet réelle disponible pour ce projet — hypothèse documentée,
# à affiner si des données de liquidité réelles deviennent disponibles).
COST_TIER_MAJORS = ["BTC", "ETH", "BNB", "SOL", "XRP"]
COST_TIER_MIDS = [
    "ADA", "DOGE", "AVAX", "DOT", "LINK", "LTC", "TRX", "BCH", "ETC", "UNI", "ATOM",
]
COST_TIER_SMALLS = [
    "NEAR", "FIL", "APT", "ARB", "OP", "INJ", "ICP", "HBAR", "AAVE", "ALGO", "SAND",
    "MANA", "XLM", "VET",
]
COST_TIER_FEE_TAKER_BPS = {"majors": 10, "mids": 15, "smalls": 25}
COST_TIER_SLIPPAGE_PENALTY_BPS = {"majors": 5, "mids": 10, "smalls": 20}


def cost_tier_of(symbol: str) -> str:
    if symbol in COST_TIER_MAJORS:
        return "majors"
    if symbol in COST_TIER_MIDS:
        return "mids"
    if symbol in COST_TIER_SMALLS:
        return "smalls"
    return "smalls"  # symbole inconnu : palier le plus pessimiste par défaut


# Pas de quantité (granularité d'exécution) pour les 24 paires supplémentaires du wallet
# agressif (au-delà des 6 "majors" déjà calibrées dans bot/sim/exchange.py:DEFAULT_QTY_STEPS).
# Approximation pessimiste (arrondie pour rester praticable sur des positions de 50-300$ sans
# prétendre reproduire le stepSize Binance exact — ce dépôt ne dispose pas d'un flux
# `exchangeInfo` calibré ; documenté ici comme hypothèse assumée, à affiner ultérieurement).
QTY_STEPS_EXTENDED: dict[str, float] = {
    "BNB": 0.001, "XRP": 0.1, "ADA": 1.0, "DOT": 0.1, "LTC": 0.001, "TRX": 1.0,
    "BCH": 0.001, "ETC": 0.01, "UNI": 0.1, "ATOM": 0.1, "NEAR": 0.1, "FIL": 0.1,
    "APT": 0.1, "ARB": 1.0, "OP": 0.1, "INJ": 0.01, "ICP": 0.1, "HBAR": 1.0,
    "AAVE": 0.001, "ALGO": 1.0, "SAND": 1.0, "MANA": 1.0, "XLM": 1.0, "VET": 10.0,
}

# ======================================================================================
# --- WALLETS : les 3 portefeuilles indépendants (cœur pédagogique du produit) ---
# ======================================================================================
WALLETS = [
    {
        "id": "prudent",
        "emoji": "🛡️",
        "label": "Prudent",
        "capital_initial_eur": 1000.0,
        "univers_crypto": ["BTC", "ETH"],
        "risque": {
            "vol_target_annualized": 0.10,
            "gross_exposure_max": 0.40,
            "cap_per_asset": 0.20,
            "vol_ewma_halflife_hours": VOL_EWMA_HALFLIFE_HOURS,
            "vol_coldstart_min_points": VOL_COLDSTART_MIN_POINTS,
            "vol_coldstart_scalar": VOL_COLDSTART_SCALAR,
            "no_trade_band": NO_TRADE_BAND,
            "cb_daily_loss_freeze_pct": 0.02,
            "cb_daily_loss_freeze_hours": 24,
            "cb_consecutive_losses_trigger": CB_CONSECUTIVE_LOSSES_TRIGGER,
            "cb_cooldown_hours": CB_COOLDOWN_HOURS,
            "cb_dd_half_size_pct": 0.10,
            "cb_dd_flatten_pct": 0.15,
        },
    },
    {
        "id": "equilibre",
        "emoji": "⚖️",
        "label": "Équilibré",
        "capital_initial_eur": 1000.0,
        "univers_crypto": ["BTC", "ETH", "SOL", "DOGE", "LINK", "AVAX"],
        "risque": {
            "vol_target_annualized": 0.20,
            "gross_exposure_max": 0.70,
            "cap_per_asset": 0.25,
            "vol_ewma_halflife_hours": VOL_EWMA_HALFLIFE_HOURS,
            "vol_coldstart_min_points": VOL_COLDSTART_MIN_POINTS,
            "vol_coldstart_scalar": VOL_COLDSTART_SCALAR,
            "no_trade_band": NO_TRADE_BAND,
            "cb_daily_loss_freeze_pct": 0.03,
            "cb_daily_loss_freeze_hours": 24,
            "cb_consecutive_losses_trigger": CB_CONSECUTIVE_LOSSES_TRIGGER,
            "cb_cooldown_hours": CB_COOLDOWN_HOURS,
            "cb_dd_half_size_pct": 0.15,
            "cb_dd_flatten_pct": 0.25,
        },
    },
    {
        "id": "agressif",
        "emoji": "🔥",
        "label": "Agressif",
        "capital_initial_eur": 1000.0,
        "univers_crypto": list(CRYPTO_SYMBOLS_30),
        "risque": {
            "vol_target_annualized": 0.35,
            "gross_exposure_max": 0.90,
            "cap_per_asset": 0.30,
            "vol_ewma_halflife_hours": VOL_EWMA_HALFLIFE_HOURS,
            "vol_coldstart_min_points": VOL_COLDSTART_MIN_POINTS,
            "vol_coldstart_scalar": VOL_COLDSTART_SCALAR,
            "no_trade_band": NO_TRADE_BAND,
            "cb_daily_loss_freeze_pct": 0.05,
            "cb_daily_loss_freeze_hours": 24,
            "cb_consecutive_losses_trigger": CB_CONSECUTIVE_LOSSES_TRIGGER,
            "cb_cooldown_hours": CB_COOLDOWN_HOURS,
            "cb_dd_half_size_pct": 0.20,
            "cb_dd_flatten_pct": 0.35,
        },
    },
]

WALLET_IDS = [w["id"] for w in WALLETS]


def wallet_config(wallet_id: str) -> dict:
    for w in WALLETS:
        if w["id"] == wallet_id:
            return w
    raise KeyError(f"wallet inconnu: {wallet_id!r} (connus: {WALLET_IDS})")
