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
MAX_QUOTE_AGE_SECONDS = 120.0  # âge maximum d'une quote pour être utilisée par ExchangeSim (crypto,
# défaut générique — INCHANGÉ, cf. correctif incident production ci-dessous). Les actions/ETF
# utilisent `MAX_QUOTE_AGE_SECONDS_EQUITY` à la place (par-symbole, cf. bot/runner.py:
# `_exchange_for_wallet`, défini plus bas une fois `STALENESS_MAX_SECONDS_EQUITY` connu) : sans
# ce seuil dédié, une quote actions/ETF acceptée côté feeds (STALENESS_MAX_SECONDS_EQUITY=1500s)
# serait quand même rejetée à l'EXÉCUTION par ce seuil-ci (120s), ce qui aurait rendu le
# correctif ci-dessous inopérant en pratique.

# --- Fraîcheur des prix (feeds — distinct de MAX_QUOTE_AGE_SECONDS ci-dessus utilisé par ExchangeSim) ---
STALENESS_MAX_SECONDS_CRYPTO = 300   # 5 min — Binance/Coinbase sont réellement temps réel, INCHANGÉ.

# --- CORRECTIF INCIDENT PRODUCTION (2026-07-23T18/T19, marché NYSE ouvert 15h07 ET) ---
# Diagnostic confirmé par les journaux commités (state/wallets/*/decisions.jsonl) : les 103
# actions + 9 ETF des 3 wallets ont eu `quote_available=false` à 100% aux DEUX cycles, alors
# que le marché était ouvert et que la poche crypto (Binance/Coinbase, seuil 300s) a tradé
# normalement au même moment — écartant un bug de parsing/timezone générique (le même code de
# fraîcheur fonctionne pour la crypto) et une panne de source pure et simple (aucun code
# d'erreur HTTP observable dans les journaux, taux d'échec parfaitement uniforme sur TOUT le
# panel, y compris les titres les plus liquides comme AAPL, cycle après cycle).
# Cause structurelle : `bot/feeds/equities.py` interroge Yahoo Finance GRATUIT (aucune clé
# API, aucun abonnement) — le NBBO bid/ask de ce flux est soumis aux accords d'affichage
# différé imposés aux non-abonnés (SIP "non-professional/display-only"), qui retardent les
# actions NYSE/NASDAQ d'environ 15-20 minutes. L'ancien seuil `STALENESS_MAX_SECONDS_EQUITY`
# (300s = 5 min, recopié tel quel du calibrage crypto lors de l'intégration §11
# ARCHITECTURE.md) était donc STRUCTURELLEMENT inatteignable pour cette source gratuite en
# conditions réelles de marché ouvert — chaque quote, aussi "fraîche" soit-elle du point de vue
# de Yahoo, arrivait déjà périmée au sens de ce seuil. `docs/ARCHITECTURE.md` §8 signalait déjà
# cette hypothèse comme jamais mesurée empiriquement ; c'est désormais chose faite.
# Décision assumée (documentée ARCHITECTURE.md §5.1 et §12) : les stratégies actions/ETF
# câblées ici (xs_momentum_sp100, dual_momentum_etf) sont MENSUELLES — trader sur un prix
# différé de 15-20 min est méthodologiquement praticable pour ce produit (écart de l'ordre de
# quelques dixièmes de %, négligeable face à un horizon mensuel). Le seuil crypto ci-dessus
# (300s côté feeds) et `MAX_QUOTE_AGE_SECONDS` (120s côté ExchangeSim, cf. plus bas) NE
# BOUGENT PAS : la poche crypto tourne en cycle horaire actif sur un flux réellement temps réel.
STALENESS_MAX_SECONDS_EQUITY = 1500   # 25 min (pendant heures de marché uniquement)
# En-deçà de ce seuil, une quote actions/ETF plus vieille que
# EQUITY_QUOTE_REALTIME_THRESHOLD_SECONDS est tout de même utilisée mais journalisée
# honnêtement comme `delayed=true` (cf. bot/feeds/types.py:Quote.delayed) — jamais présentée en
# journal comme un prix temps réel alors qu'elle ne l'est pas. Réutilise le seuil crypto (300s)
# comme frontière "temps réel plausible / clairement différé" : sous 300s, la quote actions
# pourrait légitimement être quasi temps réel (marché calme, Yahoo parfois plus rapide que le
# délai réglementaire théorique) ; au-delà, on l'assume différée par construction.
EQUITY_QUOTE_REALTIME_THRESHOLD_SECONDS = STALENESS_MAX_SECONDS_CRYPTO  # 300 s
# Seuil de fraîcheur À L'EXÉCUTION (ExchangeSim) pour les actions/ETF — distinct de
# MAX_QUOTE_AGE_SECONDS (120s, crypto, INCHANGÉ ci-dessus) ; aligné sur STALENESS_MAX_SECONDS_EQUITY
# pour ne jamais rejeter à l'exécution une quote déjà acceptée côté feeds.
MAX_QUOTE_AGE_SECONDS_EQUITY = STALENESS_MAX_SECONDS_EQUITY  # 1500 s (25 min)

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
# Marge de +48h AJOUTÉE lors de l'intégration des stratégies (docs/ARCHITECTURE.md §11) :
# `bot.strategies.quasi_passif_crypto._daily_closes()` n'agrège que des JOURS CALENDAIRES
# COMPLETS (24 heures distinctes) et exige `REGIME_SMA_DAYS` (200) d'entre eux pour calculer sa
# SMA200. Sans marge, une fenêtre de EXACTEMENT `REGIME_SMA_DAYS*24` heures perd jusqu'à 23h au
# jour courant (toujours partiel, exclu par construction) ET jusqu'à 23h au jour le plus ancien
# de la fenêtre (le début de la fenêtre tombe rarement pile à minuit UTC) — pire cas 46h
# "perdues", pouvant ramener le nombre de jours complets disponibles à 199 au lieu de 200 selon
# l'heure du cycle, rendant la SMA200 structurellement incalculable à certaines heures. +48h
# couvre ce pire cas avec 2h de marge. Bug identifié et corrigé lors de l'intégration
# (bot/tests/test_daily_history_warmup_margin.py), pas une valeur arbitraire.
HISTORY_N_HOURS = max(720, REGIME_SMA_DAYS * 24 + 48)

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
# --- Univers actions / ETF (SPEC docs/config-strategies.json, addendum §11 ARCHITECTURE.md) ---
# Repris À L'IDENTIQUE des constantes de module de `bot/strategies/xs_momentum_sp100.py`
# (`UNIVERSE_SP100`, `MARKET_FILTER_SYMBOL`) et `bot/strategies/dual_momentum_etf.py`
# (`RISKY_UNIVERSE`, `BOND_BOGEY`) — dupliqué ici (plutôt qu'importé) pour que `bot/config.py`
# reste sans dépendance sur `bot/strategies/` (source de vérité unique et autonome, cf. bandeau
# de tête de ce fichier). Ces deux jeux de constantes DOIVENT rester synchronisés — vérifié
# explicitement par `bot/tests/test_config_strategies_sync.py`.
# ======================================================================================
EQUITIES_SP100_UNIVERSE = [
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "AIG", "AMD", "AMGN", "AMT", "AMZN",
    "AVGO", "AXP", "BA", "BAC", "BKNG", "BLK", "BMY", "BRK.B", "C", "CAT",
    "CHTR", "CL", "CMCSA", "COF", "COP", "COST", "CRM", "CSCO", "CVS", "CVX",
    "DE", "DHR", "DIS", "DOW", "DUK", "EMR", "F", "FDX", "GD", "GE",
    "GILD", "GM", "GOOG", "GOOGL", "GS", "HD", "HON", "IBM", "INTC", "INTU",
    "ISRG", "JNJ", "JPM", "KHC", "KMI", "KO", "LIN", "LLY", "LMT", "LOW",
    "MA", "MCD", "MDLZ", "MDT", "MET", "META", "MMM", "MO", "MRK", "MS",
    "MSFT", "NEE", "NFLX", "NKE", "NVDA", "ORCL", "PEP", "PFE", "PG", "PM",
    "PYPL", "QCOM", "RTX", "SBUX", "SCHW", "SO", "SPG", "T", "TGT", "TJX",
    "TMO", "TMUS", "TSLA", "TXN", "UNH", "UNP", "UPS", "USB", "V", "VZ",
    "WFC", "WMT", "XOM",
]
EQUITIES_MARKET_FILTER_SYMBOL = "SPY"  # filtre de régime xs_momentum_sp100 (jamais détenu pour lui-même côté actions)
ETF_RISKY_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "GLD", "DBC"]
ETF_BOND_BOGEY = "IEF"

# `bot.feeds` (get_prices/get_history) route un symbole vers l'adaptateur actions (Yahoo) s'il
# figure dans SYMBOLS_EQUITY (cf. bot/feeds/_config_fallback.py) — doit donc couvrir TOUT
# symbole actions/ETF réellement suivi par au moins un wallet (S&P100 + SPY + les 8 ETF risqués
# + IEF), sous peine de routage silencieusement incorrect (get_prices renverrait None faute de
# reconnaître le symbole).
SYMBOLS_EQUITY = sorted(
    set(EQUITIES_SP100_UNIVERSE)
    | {EQUITIES_MARKET_FILTER_SYMBOL}
    | set(ETF_RISKY_UNIVERSE)
    | {ETF_BOND_BOGEY}
)

# --- Pas de quantité (granularité d'exécution) actions/ETF — CORRECTIF AUDIT CRITIQUE #2 ---
# `bot.sim.exchange.DEFAULT_QTY_STEPS` ne couvrait, côté actions, que 6 megacaps historiques
# (pas=1.0, "lot entier") ; tout autre symbole (les 97 titres restants du S&P100 retenu, les 8
# ETF de `ETF_RISKY_UNIVERSE`, `ETF_BOND_BOGEY`) retombait sur
# `bot.sim.exchange.DEFAULT_UNKNOWN_SYMBOL_STEP = 1.0` (action entière). Or une position typique
# sur un wallet de ~1000-1100$ pèse ~30-40$ (poche actions, top_k=10 équipondéré x 30-35% du
# wallet) à ~120-200$ (poche ETF, top_k=3). Tout titre dont le cours dépasse ce budget (BKNG,
# ISRG, TMO, LIN, COST, ADBE, CAT, GS, MA, V, UNH, SPY, QQQ... nombreux dans l'univers retenu)
# ne peut alors JAMAIS être acheté (quantité arrondie à zéro par `floor_to_step`, ordre rejeté
# par `ExchangeSim` — vérifié empiriquement : SPY à 18.3% d'un wallet de 1000€ rejeté "quantité
# (0.3057) arrondie à zéro au pas réaliste (1.0)").
#
# Décision assumée (pas une "amélioration créative" des seuils SPEC, qui ne portent que sur les
# PARAMÈTRES DE STRATÉGIE — momentum/lookback/top_k/etc. — jamais sur la granularité d'exécution
# du simulateur, qui est un détail d'infrastructure `bot/sim` hors SPEC) : `bot.sim.ExchangeSim`
# est un simulateur MAISON sans contrainte réelle de stepSize façon carnet d'ordres crypto — il
# n'y a donc aucune raison de lui imposer artificiellement une granularité "lot entier" pour des
# actions/ETF, alors que des courtiers réels praticables pour ce produit (Trading212, Revolut,
# DEGIRO...) proposent des actions FRACTIONNAIRES. `QTY_STEP_EQUITY_ETF` (1/10 000e d'action)
# reste néanmoins un pas PESSIMISTE et STRICTEMENT ARRONDI VERS LE BAS (`floor_to_step`, jamais
# en faveur du bot) — il ne prétend reproduire aucun stepSize de courtier réel précis, seulement
# lever le blocage structurel identifié par l'audit sans jamais accorder plus de quantité qu'une
# règle de 4 décimales ne le permettrait. Couvre tout `SYMBOLS_EQUITY` (S&P100 + SPY + les 8 ETF
# risqués + IEF) — y compris les 6 megacaps déjà présentes dans
# `bot.sim.exchange.DEFAULT_QTY_STEPS` à pas=1.0, dont le pas est ici resserré pour la même
# raison (AAPL/MSFT/... sont elles aussi part de l'univers `xs_momentum_sp100`, avec le même
# budget par position que n'importe quel autre titre du S&P100 retenu).
QTY_STEP_EQUITY_ETF = 0.0001
QTY_STEPS_EQUITIES: dict[str, float] = {sym: QTY_STEP_EQUITY_ETF for sym in SYMBOLS_EQUITY}

# ======================================================================================
# --- Univers crypto resserré du wallet AGRESSIF (12 actifs diversifiés) ---
# CHANGEMENT ADOPTÉ vs l'ancien univers 30 cryptos complet (cf. docs/SELECTION-FINALE.md §3 et
# docs/config-strategies.json:_meta.changements_proposes_vs_config_actuel) : panier resserré
# justifié par l'analyse de diversification (ENB) -- majors pour la liquidité/les coûts
# (BTC, ETH, SOL, BNB, XRP) + diversificateurs à faible corrélation BTC (TRX, XLM, HBAR, ICP,
# OP, UNI, FIL). Remplace l'ancien `CRYPTO_SYMBOLS_30` comme univers du wallet agressif -- ce
# dernier reste défini ci-dessus pour l'archive 100k$ / rétro-compatibilité bas niveau
# (`bot.sim.exchange.DEFAULT_QTY_STEPS`, tests bas niveau), mais N'EST PLUS l'univers du wallet
# agressif en production.
# ======================================================================================
CRYPTO_SYMBOLS_AGRESSIF_12 = [
    "BTC", "ETH", "SOL", "BNB", "XRP",
    "TRX", "XLM", "HBAR", "ICP", "OP", "UNI", "FIL",
]

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
        # Poches par classe d'actif (docs/config-strategies.json -> wallets.prudent.pockets ;
        # docs/SELECTION-FINALE.md §3). `capital_alloc_pct` = part du capital TOTAL du wallet
        # (pas de la seule poche) ; `strategy_ref` = `StrategyBase.name` de la stratégie
        # concrète qui porte cette poche (résolue dynamiquement par bot/runner.py via
        # `load_strategies()`), ou `None` pour une poche "cash" (réserve, aucun ordre généré).
        # La somme des `capital_alloc_pct` non-cash est TOUJOURS < 1.0 (le reliquat est la
        # réserve cash implicite) -- propriété exploitée par bot/runner.py pour borner le cap
        # d'exposition brute globale sans avoir à en inventer un nouveau, cf. ARCHITECTURE.md §11.
        "pockets": [
            {"asset_class": "etf", "capital_alloc_pct": 0.55, "strategy_ref": "dual_momentum_etf"},
            {"asset_class": "crypto", "capital_alloc_pct": 0.30, "strategy_ref": "quasi_passif_crypto"},
            {"asset_class": "cash", "capital_alloc_pct": 0.15, "strategy_ref": None},
        ],
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
        "pockets": [
            {"asset_class": "equities", "capital_alloc_pct": 0.35, "strategy_ref": "xs_momentum_sp100"},
            {"asset_class": "etf", "capital_alloc_pct": 0.25, "strategy_ref": "dual_momentum_etf"},
            {"asset_class": "crypto", "capital_alloc_pct": 0.30, "strategy_ref": "quasi_passif_crypto"},
            {"asset_class": "cash", "capital_alloc_pct": 0.10, "strategy_ref": None},
        ],
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
        # Univers resserré à 12 actifs (changement ADOPTÉ, cf. bandeau CRYPTO_SYMBOLS_AGRESSIF_12
        # ci-dessus et docs/SELECTION-FINALE.md §3) -- remplace l'ancien univers 30 cryptos complet.
        "univers_crypto": list(CRYPTO_SYMBOLS_AGRESSIF_12),
        "pockets": [
            {"asset_class": "equities", "capital_alloc_pct": 0.30, "strategy_ref": "xs_momentum_sp100"},
            {"asset_class": "crypto", "capital_alloc_pct": 0.60, "strategy_ref": "quasi_passif_crypto"},
            {"asset_class": "cash", "capital_alloc_pct": 0.10, "strategy_ref": None},
        ],
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
