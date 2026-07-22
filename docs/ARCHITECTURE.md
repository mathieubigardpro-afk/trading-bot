# ARCHITECTURE — Bot de Paper Trading 100% Maison

*Document de référence pour l'implémentation. À lire avant de coder quoi que ce soit dans `bot/`.
Complète `docs/rapport-recherche.md` (dont sont repris : le framework de risque §3, les hypothèses
de coûts §4, la checklist §5, les formules de métriques §6) — mais **remplace intégralement le
choix de plateforme du rapport (Alpaca)** : ce bot n'utilise AUCUN broker externe. Il possède son
propre simulateur d'exchange (`bot/sim/`), alimenté par des prix publics réels, et son propre
grand livre (ledger). Le "compte" n'existe nulle part ailleurs que dans `state/` de ce dépôt.*

---

## 0. Principes non négociables

1. **Statelessness du conteneur, state-fulness du dépôt.** Chaque run part d'un clone vierge.
   Toute la mémoire du bot vit dans `state/*.json` et `state/*.jsonl`, committée et pushée à
   chaque cycle réussi. Un run qui ne pousse pas son état n'a **rien changé** du point de vue du
   prochain run.
2. **Pessimisme systématique.** Toute ambiguïté de modélisation (prix, spread, fill, latence) doit
   être tranchée en défaveur du bot. Un simulateur maison est structurellement suspect de
   complaisance — c'est à l'implémentation de prouver le contraire à chaque ligne.
3. **Jamais de prix stocké arrangé.** Un fill utilise exclusivement un prix retourné à l'instant T
   par un appel réseau à une API de marché publique. Si l'appel échoue, est trop vieux, ou renvoie
   une donnée invalide (bid ≥ ask, prix ≤ 0, etc.), l'actif concerné **ne trade pas ce cycle** —
   jamais de fallback sur un prix mémorisé.
4. **Pas de look-ahead bias.** Toute décision à l'heure H utilise exclusivement des bougies
   **clôturées** jusqu'à H-1. `get_history()` ne renvoie jamais la bougie en cours de formation.
5. **Idempotence stricte.** Un `run_id` ne peut produire des effets (trades, écritures d'état)
   qu'une seule fois. Un doublon détecté = sortie silencieuse et propre, code retour 0.
6. **L'état n'est modifié qu'après réussite complète du cycle**, écrit atomiquement, et le
   `git push` est la toute dernière étape du programme.

---

## 1. Arborescence du dépôt

```
repo/
├── bot/
│   ├── __init__.py
│   ├── config.py              # toutes les constantes (symboles, seuils, bps, caps)
│   ├── runner.py               # point d'entrée, orchestre le cycle horaire
│   ├── feeds/
│   │   ├── __init__.py         # get_prices(), get_history() — façade publique
│   │   ├── crypto.py           # adaptateur Binance public (+ fallback Coinbase)
│   │   ├── equities.py         # adaptateur Yahoo Finance public (chart + quote)
│   │   └── calendar.py         # is_us_market_open(), jours fériés NYSE
│   ├── sim/
│   │   ├── __init__.py
│   │   ├── exchange.py         # ExchangeSim.execute_order()
│   │   ├── ledger.py           # Ledger (cash, positions, equity)
│   │   └── fills.py            # dataclass Fill
│   ├── risk/
│   │   ├── __init__.py
│   │   ├── manager.py          # RiskManager.apply()
│   │   ├── vol_targeting.py    # calcul EWMA vol, scalar
│   │   └── circuit_breakers.py # logique des 4 breakers §3C du rapport
│   ├── strategies/
│   │   ├── __init__.py         # StrategyBase, combine_strategies()
│   │   ├── donchian.py          # (arrivera après backtests — squelette seulement en V1)
│   │   ├── momentum_ema.py      # idem
│   │   └── mean_reversion_rsi2.py # idem
│   └── persist/
│       ├── __init__.py
│       ├── state.py             # load_state(), save_state(), compute_state_hash()
│       ├── journal.py           # append_journal()
│       └── git_sync.py          # git_sync()
├── state/
│   ├── state.json
│   ├── trades.jsonl
│   ├── equity.jsonl
│   └── decisions.jsonl
├── dashboard/                   # hors scope de ce document (lecture seule des journaux)
└── docs/
    ├── rapport-recherche.md
    └── ARCHITECTURE.md          # ce document
```

---

## 2. Configuration — `bot/config.py` (constantes de référence)

```python
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

# --- Fraîcheur des prix ---
STALENESS_MAX_SECONDS_CRYPTO = 300   # 5 min
STALENESS_MAX_SECONDS_EQUITY = 300   # 5 min (pendant heures de marché uniquement)

# --- Risque — calibrage AGRESSIF (rapport §3, breakers INCHANGÉS) ---
VOL_TARGET_ANNUALIZED_MIN = 0.25
VOL_TARGET_ANNUALIZED_MAX = 0.30
VOL_TARGET_ANNUALIZED = 0.275          # point médian utilisé par défaut
CAP_PER_ASSET = 0.25                   # 25% équity max par actif (crypto ET actions)
GROSS_EXPOSURE_MAX = 0.80              # 80% équity max, somme des expositions absolues
NO_TRADE_BAND = 0.05                   # ±5% autour de la cible : pas d'ordre
VOL_EWMA_HALFLIFE_HOURS = 60           # 48-72h, rapport §3A
VOL_COLDSTART_MIN_POINTS = 30          # sous ce seuil d'historique -> vol_scalar prudent
VOL_COLDSTART_SCALAR = 0.5

# --- Circuit breakers (§3C rapport — INCHANGÉS) ---
CB_DAILY_LOSS_FREEZE_PCT = 0.03        # perte 24h glissantes > 3% -> gel nouvelles entrées 24h
CB_DAILY_LOSS_FREEZE_HOURS = 24
CB_CONSECUTIVE_LOSSES_TRIGGER = 5      # 5 pertes consécutives -> cooldown
CB_COOLDOWN_HOURS = 24                 # borne basse de la fourchette 24-48h du rapport
CB_DD_HALF_SIZE_PCT = 0.15             # drawdown > 15% -> tailles cibles /2
CB_DD_FLATTEN_PCT = 0.25               # drawdown > 25% -> flatten total + observation

# --- Filtres de régime (§3D rapport) ---
REGIME_SMA_DAYS = 200
REGIME_ATR_PERCENTILE_WINDOW_DAYS = 90
REGIME_ATR_PERCENTILE_MAX = 0.90

# --- Chemins ---
STATE_DIR = "state"
STATE_JSON = f"{STATE_DIR}/state.json"
TRADES_JSONL = f"{STATE_DIR}/trades.jsonl"
EQUITY_JSONL = f"{STATE_DIR}/equity.jsonl"
DECISIONS_JSONL = f"{STATE_DIR}/decisions.jsonl"
```

Toute constante ci-dessus est la source de vérité unique — aucun module ne doit recopier ces
valeurs en dur.

---

## 3. Schéma d'état — `state/`

### 3.1 `state/state.json`

Un seul objet JSON, réécrit intégralement (pas d'append) à chaque run réussi, de façon **atomique**
(écriture dans `state.json.tmp` puis `os.replace()`).

```json
{
  "schema_version": 1,
  "last_run_id": "2026-07-22T14",
  "last_run_completed_at": "2026-07-22T14:03:41.208112+00:00",
  "state_hash_prev": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b85",
  "cash_usd": 41235.77,
  "positions": {
    "BTC": {"qty": 0.18452193, "prix_moyen": 61340.12},
    "ETH": {"qty": 2.5, "prix_moyen": 3210.55},
    "AAPL": {"qty": 40.0, "prix_moyen": 198.30}
  },
  "equity_peak_usd": 104820.55,
  "equity_peak_ts": "2026-07-15T18:00:00+00:00",
  "realized_pnl_cumulative_usd": 1875.30,
  "circuit_breakers": {
    "flatten_mode": false,
    "manual_review_required": false,
    "daily_loss_freeze_until": null,
    "cooldown_until": null,
    "consecutive_losses": 2,
    "dd_half_size_active": false
  },
  "trade_history_for_breakers": [
    {"ts": "2026-07-22T09:00:00+00:00", "symbol": "SOL", "realized_pnl_usd": -42.10},
    {"ts": "2026-07-22T11:00:00+00:00", "symbol": "ETH", "realized_pnl_usd": -18.55}
  ]
}
```

Notes de champs :
- `positions` : uniquement les symboles à `qty > 0` (une position vendue à zéro est retirée de
  l'objet, pas mise à `qty: 0`).
- `prix_moyen` : coût moyen pondéré (moyenne mobile pondérée par quantité), recalculé à chaque
  achat, inchangé lors d'une vente partielle.
- `equity_peak_usd` / `equity_peak_ts` : pic historique de l'équity mark-to-market, sert de
  référence au calcul de drawdown pour les circuit breakers 15%/25%. Mis à jour uniquement si la
  nouvelle équity dépasse le pic actuel.
- `circuit_breakers.manual_review_required` : passé à `true` automatiquement quand
  `flatten_mode` s'active (DD>25%). **Seul un humain éditant ce fichier peut le repasser à
  `false`** — le bot ne le fait jamais lui-même. Tant que `true`, le runner reste en mode
  observation (aucun ordre, mais les cycles continuent de tourner et de journaliser).
- `trade_history_for_breakers` : fenêtre glissante (on ne garde que les 20 dernières entrées) des
  PnL réalisés des ventes, utilisée pour compter les pertes consécutives. Alimentée à chaque fill
  de vente.
- `state_hash_prev` : `sha256` hex du `state.json` **tel qu'il existait au tout début de ce run**
  (avant toute modification), sérialisé canoniquement — voir §4.3. Permet de vérifier après-coup
  qu'aucun état intermédiaire n'a été perdu ou falsifié entre deux commits.

### 3.2 `state/trades.jsonl`

Une ligne JSON par fill exécuté (append-only, jamais réécrit ni tronqué).

```json
{"run_id": "2026-07-22T14", "ts": "2026-07-22T14:03:12.554011+00:00", "symbol": "ETH", "strategy": "donchian_55h", "side": "BUY", "qty": 0.42911, "notional_usd": 1400.00, "price_fill": 3262.55, "price_mid_ideal": 3259.10, "fees_usd": 1.40, "slippage_usd": 1.48, "quote_source": "binance", "quote_ts": "2026-07-22T14:03:11.900000+00:00", "cash_after_usd": 39835.77}
```

Champs obligatoires : `run_id, ts, symbol, strategy, side (BUY|SELL), qty, notional_usd,
price_fill, price_mid_ideal, fees_usd, slippage_usd, quote_source, quote_ts, cash_after_usd`.
Pour une vente, ajouter `realized_pnl_usd` (PnL réalisé sur la quantité vendue, calculé contre
`prix_moyen` avant la vente).

`slippage_usd` = `abs(price_fill - price_mid_ideal) * qty` — c'est la mesure d'écart entre le
prix "idéal" (mid au moment de la décision) et le prix réellement obtenu, incluant spread payé +
pénalité de slippage. C'est la métrique-clé pour auditer le réalisme du simulateur dans le temps.

### 3.3 `state/equity.jsonl`

Une ligne par run (même quand aucun trade n'a eu lieu — sert de série temporelle pour le vol
targeting et le dashboard).

```json
{"run_id": "2026-07-22T14", "ts": "2026-07-22T14:03:41.208112+00:00", "equity_usd": 100482.10, "cash_usd": 39835.77, "exposures": {"BTC": 0.1134, "ETH": 0.1360, "AAPL": 0.0789}, "gross_exposure_pct": 0.3283, "drawdown_pct": 0.0413, "equity_peak_usd": 104820.55, "circuit_breakers_active": ["dd_half_size_active"]}
```

- `exposures[symbole]` = `qty * prix_mark_to_market / equity_usd` (fraction de l'équity, signé
  positif toujours car long-only).
- `gross_exposure_pct` = somme des `exposures` (valeurs absolues, ici toutes positives).
- `drawdown_pct` = `(equity_peak_usd - equity_usd) / equity_peak_usd`, borné à `[0, 1]`.
- `prix_mark_to_market` = **mid** price du cycle courant (pas bid/ask), sauf si le marché actions
  est fermé, auquel cas on garde le dernier mid connu (journalisé dans `decisions.jsonl` comme
  `"marché fermé, mark au dernier prix connu"`).

### 3.4 `state/decisions.jsonl`

Une ligne **par run et par actif de l'univers complet** (crypto + actions), y compris quand la
décision est "pas de trade". C'est le journal exhaustif d'audit — rien n'est jamais silencieux ici,
même si l'action finale est silencieuse en trading.

```json
{"run_id": "2026-07-22T14", "ts": "2026-07-22T14:03:05.113000+00:00", "symbol": "DOGE", "asset_class": "crypto", "market_open": true, "quote_available": true, "quote_source": "binance", "price_mid_ideal": 0.14210, "quote_ts": "2026-07-22T14:03:04.800000+00:00", "quote_age_seconds": 0.3, "strategy_signals": {"donchian_55h": 0.0, "momentum_ema": 0.0, "mean_reversion_rsi2": 0.05}, "poids_cible_brut": 0.017, "poids_cible_apres_risk": 0.0, "poids_actuel": 0.0, "decision": "NO_TRADE", "reason": "poids_cible_apres_risk (0.0) dans la no-trade band autour de poids_actuel (0.0)", "circuit_breakers_snapshot": {"flatten_mode": false, "daily_loss_freeze": false, "cooldown": false, "dd_half_size": false, "regime_gate_blocked": false}}
```

Exemple pour une action hors heures de marché :

```json
{"run_id": "2026-07-22T14", "ts": "2026-07-22T14:03:05.900000+00:00", "symbol": "AAPL", "asset_class": "equity", "market_open": false, "quote_available": false, "quote_source": null, "price_mid_ideal": null, "quote_ts": null, "quote_age_seconds": null, "strategy_signals": {}, "poids_cible_brut": null, "poids_cible_apres_risk": null, "poids_actuel": 0.0789, "decision": "NO_TRADE", "reason": "marché US fermé (hors 09:30-16:00 America/New_York, jour ouvré NYSE) — position conservée, aucune évaluation de signal", "circuit_breakers_snapshot": null}
```

Exemple pour un actif dont le prix est indisponible/périmé (principe cardinal pessimiste) :

```json
{"run_id": "2026-07-22T14", "ts": "2026-07-22T14:03:06.400000+00:00", "symbol": "AVAX", "asset_class": "crypto", "market_open": true, "quote_available": false, "quote_source": "binance", "price_mid_ideal": null, "quote_ts": "2026-07-22T13:52:00+00:00", "quote_age_seconds": 666.0, "strategy_signals": {}, "poids_cible_brut": null, "poids_cible_apres_risk": null, "poids_actuel": 0.0, "decision": "NO_TRADE", "reason": "quote périmée (666s > seuil 300s) sur source primaire et fallback — aucun trade sur cet actif ce cycle"}
```

`decision` ∈ `{"BUY", "SELL", "NO_TRADE"}`. `reason` est **toujours** renseigné, y compris pour
`BUY`/`SELL` (ex. `"signal ensemble positif, hors no-trade band, sizing vol-target appliqué"`).

---

## 4. Idempotence et intégrité

### 4.1 `run_id`

```python
from datetime import datetime, timezone

def compute_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H")   # ex. "2026-07-22T14"
```

### 4.2 Règle de déduplication

Au tout début de `runner.main()`, **avant tout appel réseau de prix ou toute construction de
signal** :

1. `state = load_state()`.
2. `run_id = compute_run_id()`.
3. Si `state["last_run_id"] == run_id` → log en clair ("run déjà traité, abandon silencieux
   propre") et `sys.exit(0)` immédiatement. Aucune écriture, aucun commit, aucun appel réseau
   supplémentaire.
4. Sinon, poursuivre le cycle normalement avec ce `run_id`.

Ceci gère à la fois : les doubles exécutions accidentelles du scheduler pour la même heure, et les
retries manuels dans la même fenêtre horaire.

### 4.3 Chaîne d'intégrité (`state_hash_prev`)

```python
import hashlib, json

def compute_state_hash(state: dict) -> str:
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

Au début du run, avant modification : `prev_hash = compute_state_hash(state)` (le `state` tel que
chargé depuis le disque, `state_hash_prev` de cette version inclus — donc la chaîne s'étend
récursivement). En fin de cycle, le nouveau `state["state_hash_prev"] = prev_hash` avant écriture.
Un script d'audit peut ainsi rejouer `state.json` à travers l'historique git et vérifier qu'aucun
maillon n'a été altéré hors du processus normal.

### 4.4 Écriture atomique et séquence de sortie

- `save_state()` : écrit dans `state/state.json.tmp` (même filesystem), `f.flush()`,
  `os.fsync(f.fileno())`, puis `os.replace("state/state.json.tmp", "state/state.json")`.
- `append_journal()` : ouverture en mode `"a"`, une ligne `json.dumps(record) + "\n"`, `flush()` +
  `os.fsync()`. Jamais de réécriture complète des fichiers `.jsonl`.
- **Ordre strict de fin de cycle** :
  1. Tous les fills exécutés et le ledger mis à jour en mémoire.
  2. `append_journal(trades.jsonl, ...)` pour chaque fill.
  3. `append_journal(equity.jsonl, ...)` (une ligne).
  4. `append_journal(decisions.jsonl, ...)` pour chaque actif de l'univers.
  5. `save_state(nouveau_state)` (écrit `state.json` en dernier parmi les fichiers d'état, une
     fois que tout le reste a réussi).
  6. `git add state/*.json state/*.jsonl && git commit -m "..." && git push` — **dernière étape du
     programme**, aucune étape logique après.

### 4.5 Gestion des échecs de push

```python
def git_sync(repo_dir: str, message: str, max_retries: int = 3) -> str:
    """Retourne 'SUCCESS', 'ABORTED_DUPLICATE' ou 'FAILED'."""
```

Séquence :
1. `git add state/state.json state/trades.jsonl state/equity.jsonl state/decisions.jsonl`
2. `git commit -m message`
3. `git push` — si succès, retourner `SUCCESS`.
4. Si échec (non-fast-forward) : `git pull --rebase origin main`.
   - Si le rebase produit un **conflit sur `state/state.json`** : c'est le signe qu'un autre run a
     déjà traité (ou est en train de traiter) le même `run_id`. Résolution : `git rebase --abort`,
     recharger `state.json` depuis `origin/main` (`git show origin/main:state/state.json`), vérifier
     son `last_run_id`. S'il est **≥** au `run_id` local → retourner `ABORTED_DUPLICATE` (course
     perdue, sortie propre, code retour 0 — ce n'est pas une erreur). Sinon, réessayer la boucle
     (jusqu'à `max_retries`).
   - Si le rebase réussit sans conflit (cas des `.jsonl` — conflits improbables car append-only,
     résolubles par concaténation des deux côtés si nécessaire), retenter `git push`.
5. Après `max_retries` échecs, retourner `FAILED` — le runner logge une erreur explicite et sort
   avec un code non-zéro (l'état local reste non modifié sur disque au sens git, le prochain run
   reparfaitement d'un clone propre depuis `origin/main`, donc aucune corruption possible).

**Sécurité** : `git_sync` ne touche jamais aux credentials — le remote est déjà authentifié sur le
clone fourni. Ne jamais logger l'URL du remote (`git remote -v`) ni son contenu.

---

## 5. Interfaces des modules (signatures exactes)

### 5.1 `bot/feeds/`

```python
from dataclasses import dataclass
import pandas as pd

@dataclass
class Quote:
    bid: float
    ask: float
    mid: float
    ts: str          # ISO8601 UTC, horodatage de la quote côté source (pas l'heure de réception)
    source: str       # "binance" | "coinbase" | "yahoo" | "yahoo_synthetic_spread"

def get_prices(symbols: list[str]) -> dict[str, Quote | None]:
    """
    Retourne un Quote par symbole demandé, ou None si aucune source n'a pu fournir un prix
    valide et frais (bid>0, ask>0, bid<ask, âge < seuil de fraîcheur de config).
    Ne lève jamais d'exception pour un symbole individuel en échec — l'absence de Quote (None)
    EST le signal d'échec, à traiter en amont (no-trade).
    Route automatiquement crypto -> bot.feeds.crypto, equity -> bot.feeds.equities selon
    l'appartenance à config.SYMBOLS_CRYPTO / config.SYMBOLS_EQUITY.
    """

def get_history(symbol: str, n_hours: int) -> pd.DataFrame:
    """
    Retourne un DataFrame indexé par timestamp UTC horaire croissant, colonnes
    [open, high, low, close, volume], contenant EXACTEMENT les n_hours dernières bougies
    CLÔTURÉES (jamais la bougie en cours de formation à l'heure d'appel).
    Lève HistoryUnavailableError si moins de n_hours bougies ne peuvent être obtenues
    (l'appelant décide alors du repli : signal désactivé pour ce symbole ce cycle).
    """
```

- `bot/feeds/crypto.py` : `get_prices` interroge
  `GET https://api.binance.com/api/v3/ticker/bookTicker?symbol=<PAIR>` (bid/ask réels) ; en cas
  d'échec HTTP/timeout/JSON invalide, fallback
  `GET https://api.exchange.coinbase.com/products/<PAIR>/ticker`. `get_history` utilise
  `GET https://api.binance.com/api/v3/klines?symbol=<PAIR>&interval=1h&limit=n+2` puis **exclut
  systématiquement la dernière bougie renvoyée** (potentiellement encore ouverte) — on ne garde
  que les bougies dont `close_time < now`.
- `bot/feeds/equities.py` : `get_prices` interroge
  `GET https://query1.finance.yahoo.com/v7/finance/quote?symbols=<SYM>`. Si les champs `bid`/`ask`
  sont absents, nuls ou invalides (cas fréquent hors séance), **ne pas tenter** de générer un
  quote synthétique — retourner `None` pour ce symbole (`quote_available: false` en décision). Le
  spread synthétique (`source="yahoo_synthetic_spread"`, largeur fixe 15 bps autour de
  `regularMarketPrice`) n'est utilisé **que si strictement nécessaire pour ne pas bloquer tout
  trading actions**, décision à valider par un agent de backtest ultérieur — en V1, préférer le
  no-trade strict par défaut (poser `EQUITY_SYNTHETIC_SPREAD_ENABLED = False` dans `config.py`).
  `get_history` utilise
  `GET https://query1.finance.yahoo.com/v8/finance/chart/<SYM>?interval=1h&range=730d` (bornage
  Yahoo ~2 ans en horaire), même règle d'exclusion de la dernière bougie.
- `bot/feeds/calendar.py` :

```python
def is_us_market_open(ts: "datetime") -> bool:
    """
    True si ts (tz-aware) tombe un jour ouvré NYSE, entre 09:30 et 16:00 America/New_York,
    hors jours fériés NYSE. Liste des jours fériés maintenue en dur dans ce module (mise à jour
    annuelle manuelle) — ne dépend d'aucun appel réseau.
    """
```

### 5.2 `bot/sim/`

```python
from dataclasses import dataclass

@dataclass
class Fill:
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
    realized_pnl_usd: float | None = None   # renseigné uniquement pour SELL

class ExchangeSim:
    def __init__(self, fee_taker_bps: float, slippage_penalty_bps: float):
        ...

    def execute_order(self, side: str, symbol: str, qty: float, quote: Quote,
                       strategy: str, run_id: str) -> Fill:
        """
        Modèle de fill PESSIMISTE, aucune négociation possible :
          BUY  -> price_fill = quote.ask * (1 + slippage_penalty_bps / 10_000)
          SELL -> price_fill = quote.bid * (1 - slippage_penalty_bps / 10_000)
          notional_usd = qty * price_fill
          fees_usd     = notional_usd * fee_taker_bps / 10_000
          slippage_usd = abs(price_fill - quote.mid) * qty
        Précondition : l'appelant garantit que `quote` est fraîche (vérifiée en amont via
        get_prices) — cette méthode ne revalide pas la fraîcheur, elle exécute.
        Ne modifie PAS le Ledger — c'est à l'appelant (runner) d'appliquer le Fill au Ledger.
        """

class Ledger:
    def __init__(self, cash_usd: float, positions: dict[str, dict]):
        ...  # positions: {symbol: {"qty": float, "prix_moyen": float}}

    def apply_fill(self, fill: Fill) -> None:
        """
        BUY : cash -= (notional_usd + fees_usd) ; qty += fill.qty ;
              prix_moyen = moyenne pondérée (qty_avant*prix_avant + notional_usd) / qty_apres.
        SELL: cash += (notional_usd - fees_usd) ; qty -= fill.qty ;
              realized_pnl_usd = (fill.price_fill - prix_moyen_avant) * fill.qty - fees_usd ;
              prix_moyen inchangé ; si qty tombe à ~0 (< 1e-9), position supprimée du dict.
        Lève ValueError si une vente dépasse la quantité détenue (garde-fou — ne doit jamais
        arriver si RiskManager a bien clampé les cibles).
        """

    def equity(self, mark_prices: dict[str, float]) -> float:
        """
        cash + somme(qty * mark_prices[symbole]) pour chaque position détenue.
        mark_prices attend un prix MID par symbole (pas bid/ask) ; pour un symbole sans mark
        price disponible ce cycle (ex. action hors séance), l'appelant doit fournir le dernier
        mid connu explicitement — cette méthode ne fait aucune supposition implicite.
        """
```

### 5.3 `bot/risk/`

```python
def apply(
    cibles_brutes: dict[str, float],      # poids bruts par symbole issus des stratégies, 0..1
    state: dict,                           # state.json chargé
    prices: dict[str, "Quote | None"],     # sortie de get_prices()
    history: dict[str, "pd.DataFrame"],    # sortie de get_history() par symbole
) -> tuple[dict[str, float], dict[str, str]]:
    """
    Retourne (cibles_finales, raisons) où raisons[symbole] documente chaque ajustement appliqué
    (à concaténer avec ';' si plusieurs facteurs jouent). Pipeline appliqué DANS CET ORDRE :

    1. Vol targeting portefeuille :
       - vol_horaire_ewma = EWMA(rendements horaires de equity.jsonl, halflife=60h)
       - si < VOL_COLDSTART_MIN_POINTS observations -> vol_scalar = VOL_COLDSTART_SCALAR (0.5)
       - sinon : vol_annualisee = vol_horaire_ewma * sqrt(8760)
                 vol_scalar = min(1, VOL_TARGET_ANNUALIZED / vol_annualisee)
       - cibles *= vol_scalar (appliqué uniformément à tous les symboles)

    2. Cap par actif : cible[sym] = min(cible[sym], CAP_PER_ASSET) pour chaque symbole
       (crypto ET equity, même cap 25%).

    3. Cap d'exposition brute : si sum(cibles.values()) > GROSS_EXPOSURE_MAX, tout réduire au
       prorata pour ramener la somme exactement à GROSS_EXPOSURE_MAX.

    4. Filtres de régime (crypto uniquement, §3D rapport) — appliqués comme un plafond
       supplémentaire, pas comme override total : pour tout symbole où
       (prix < SMA200_journalier) OU (ATR14/prix > percentile_90(fenêtre 90j)),
       toute AUGMENTATION de position est bloquée (cible[sym] = min(cible[sym], poids_actuel[sym]))
       — une position existante peut toujours être réduite/sortie, jamais renforcée.

    5. Circuit breakers, dans cet ordre (le plus sévère écrase les précédents) :
       a. flatten_mode déjà actif dans state -> cibles = {sym: 0 pour tout sym}, raison
          "flatten_mode actif, revue manuelle requise (manual_review_required=true)".
       b. drawdown_actuel > CB_DD_FLATTEN_PCT (0.25) -> cibles = {sym: 0}, ET
          RiskManager positionne un flag de sortie `activate_flatten_mode=True` que le runner
          reporte dans le nouveau state (flatten_mode=true, manual_review_required=true).
       c. sinon si drawdown_actuel > CB_DD_HALF_SIZE_PCT (0.15) -> cibles *= 0.5,
          flag `dd_half_size_active=True` reporté dans le state.
       d. si daily_loss_freeze_until (state) est encore dans le futur, OU si le calcul du
          rendement 24h glissantes de equity.jsonl < -CB_DAILY_LOSS_FREEZE_PCT (déclenche un
          nouveau freeze de 24h à partir de maintenant) -> pour tout symbole où
          cible[sym] > poids_actuel[sym] : cible[sym] = poids_actuel[sym] (bloque les nouvelles
          entrées/renforcements, autorise toujours les réductions/sorties).
       e. même logique de blocage "nouvelles entrées" si state.circuit_breakers.cooldown_until
          est dans le futur (déclenché ailleurs, cf. §5.3.1 ci-dessous, à partir de
          consecutive_losses >= CB_CONSECUTIVE_LOSSES_TRIGGER).

    6. No-trade band : pour chaque symbole, si abs(cible_finale - poids_actuel) < NO_TRADE_BAND,
       cible_finale = poids_actuel (aucun ordre ne sera généré par le runner pour ce symbole).

    7. Garde-fou prix : pour tout symbole où prices[sym] is None (indisponible/périmé),
       cible_finale[sym] = poids_actuel[sym] inconditionnellement (dernier mot, écrase tout
       calcul précédent) — reason = "prix indisponible/périmé, position conservée telle quelle".
    """
```

**5.3.1 Mise à jour des breakers "stateful"** (comptage des pertes consécutives, déclenchement du
cooldown) est effectuée par le **runner**, pas par `RiskManager.apply` (qui est un calcul pur sans
effet de bord sur le disque) : après application des fills du cycle, le runner parcourt les
ventes du cycle par ordre chronologique, met à jour `state.circuit_breakers.consecutive_losses`
(incrémenté à chaque vente à `realized_pnl_usd < 0`, remis à 0 à la première vente gagnante), et si
le compteur atteint `CB_CONSECUTIVE_LOSSES_TRIGGER`, positionne
`cooldown_until = now + CB_COOLDOWN_HOURS`. Ce nouvel état sera lu par `RiskManager.apply` au
**prochain** cycle (jamais rétroactivement dans le cycle courant).

### 5.4 `bot/persist/`

```python
def load_state(path: str = config.STATE_JSON) -> dict:
    """Charge et parse state.json. Si le fichier n'existe pas (premier run de l'histoire du
    dépôt), retourne l'état initial : cash_usd=INITIAL_CASH_USD, positions={}, last_run_id=None,
    equity_peak_usd=INITIAL_CASH_USD, circuit_breakers par défaut (tout à false/null)."""

def save_state(state: dict, path: str = config.STATE_JSON) -> None:
    """Écriture atomique (tmp + os.replace), voir §4.4."""

def compute_state_hash(state: dict) -> str:
    """Voir §4.3."""

def append_journal(path: str, record: dict) -> None:
    """Append une ligne JSON + '\\n', flush + fsync. Voir §4.4."""

def git_sync(repo_dir: str, message: str, max_retries: int = 3) -> str:
    """Voir §4.5. Retourne 'SUCCESS' | 'ABORTED_DUPLICATE' | 'FAILED'."""
```

### 5.5 `bot/strategies/`

```python
from abc import ABC, abstractmethod

class StrategyBase(ABC):
    name: str   # identifiant stable utilisé comme clé dans strategy_signals / trades.jsonl

    @abstractmethod
    def target_weights(self, history: dict[str, "pd.DataFrame"], state: dict) -> dict[str, float]:
        """
        Retourne un poids cible BRUT par symbole (0..1, long-only ; 0 = flat), calculé
        exclusivement à partir de `history` (bougies clôturées) et de `state` (positions
        actuelles, si la stratégie a besoin de connaître son propre historique de position pour
        gérer un stop/trailing). Ne fait AUCUN appel réseau, AUCUNE écriture disque — pure
        fonction de ses arguments. Les implémentations concrètes (Donchian, EMA momentum,
        RSI(2) mean-reversion — cf. rapport §2) arrivent après la phase de backtest walk-forward ;
        en V1 ce module ne contient que l'interface + un squelette qui retourne {} (aucun signal,
        équivalent à 100% cash) pour permettre au runner et au risk manager d'être testés de bout
        en bout avant que les stratégies ne soient calibrées.
        """

def combine_strategies(
    strategies: list[StrategyBase], history: dict[str, "pd.DataFrame"], state: dict
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """
    Combine plusieurs stratégies par MOYENNE ÉQUI-PONDÉRÉE de leurs poids par symbole
    (placeholder documenté — à remplacer par une allocation calibrée une fois les backtests
    walk-forward disponibles, cf. rapport §2 et §5.5). Retourne (cibles_brutes_combinees,
    signaux_par_strategie) où le second élément alimente `strategy_signals` dans
    decisions.jsonl.
    """
```

---

## 6. Séquence exacte d'un cycle horaire — `bot/runner.py: main()`

1. **Idempotence (§4.2)** : `state = load_state()`, `run_id = compute_run_id()`. Si
   `state["last_run_id"] == run_id` → log + `sys.exit(0)`.
2. `prev_hash = compute_state_hash(state)` (avant toute modification).
3. **Détermination de l'univers actif ce cycle** :
   - Crypto : toujours actif (`config.SYMBOLS_CRYPTO`), 24/7.
   - Actions : `market_open = is_us_market_open(now_utc)`. Si `False`, les symboles actions ne
     sont **pas** envoyés à `get_prices`/`get_history`/aux stratégies — ils sont directement
     journalisés dans `decisions.jsonl` avec `decision="NO_TRADE"`,
     `reason="marché US fermé ..."`, `poids_actuel` inchangé lu depuis `state.positions`. Leurs
     positions existantes ne sont ni évaluées ni modifiées, mais **sont tout de même incluses**
     dans le calcul d'`equity` du cycle, marquées au **dernier `prix_moyen`/dernier mid connu**
     (le plus récent entre le dernier fill et la dernière quote journalisée) — jamais à 0.
4. **Récupération des prix** : `prices = get_prices(symbols_actifs_ce_cycle)` où
   `symbols_actifs_ce_cycle` = crypto (toujours) + actions (seulement si `market_open`).
   Pour chaque symbole, si `prices[sym] is None` ou âge de la quote > seuil de fraîcheur, le
   symbole est marqué `quote_available=false` et sera figé à `poids_actuel` (garde-fou §5.3
   étape 7) — journalisé immédiatement dans `decisions.jsonl` avec la raison précise.
5. **Récupération de l'historique** : pour chaque symbole avec `quote_available=true`,
   `history[sym] = get_history(sym, n_hours=max(720, REGIME_SMA_DAYS*24))` (assez pour SMA200
   journalier agrégé + ATR14 + fenêtre percentile 90j). Si `HistoryUnavailableError`, traiter comme
   `quote_available=false` pour ce symbole (pas de signal calculable en toute rigueur).
6. **Calcul des signaux bruts** : `cibles_brutes, strategy_signals = combine_strategies(
   strategies_actives, history, state)` — uniquement sur les symboles avec historique disponible.
7. **Application du RiskManager** : `cibles_finales, raisons = RiskManager.apply(cibles_brutes,
   state, prices, history)`.
8. **Génération des ordres** : pour chaque symbole où `cible_finale != poids_actuel` (hors no-trade
   band, déjà géré à l'étape 7) et `quote_available=true` :
   - `side = "BUY"` si `cible_finale > poids_actuel`, sinon `"SELL"`.
   - `delta_usd = abs(cible_finale - poids_actuel) * equity_avant_cycle`.
   - `qty = delta_usd / prices[sym].mid` (approximation de départ ; le fill réel se fait au
     bid/ask, cf. §5.2).
   - `fill = exchange_sim.execute_order(side, sym, qty, prices[sym], strategy="ensemble", run_id)`.
   - `ledger.apply_fill(fill)`.
   - Ce `fill` est accumulé pour l'écriture dans `trades.jsonl` (étape 11).
9. **Journalisation "prix mid idéal" pour TOUS les actifs évalués** (trade ou non) : chaque entrée
   de `decisions.jsonl` de ce cycle contient `price_mid_ideal = prices[sym].mid` capturé **avant**
   tout ordre, même pour les symboles en `NO_TRADE` — c'est la mesure de référence indépendante du
   fill, permettant de comparer a posteriori "ce que le marché valait au moment de la décision" vs
   "ce que le bot a effectivement payé/reçu" sur les cycles où un trade a eu lieu.
10. **Mise à jour des compteurs de circuit breakers stateful** (§5.3.1) à partir des ventes de ce
    cycle (consecutive_losses, cooldown_until).
11. **Calcul de l'équity de fin de cycle** : `mark_prices` = mid courant pour tout symbole avec
    quote fraîche ce cycle, sinon dernier mid connu (actions hors séance, ou crypto en échec de
    quote — cas rare, déjà loggé comme anomalie en décision). `equity = ledger.equity(mark_prices)`.
    Mise à jour de `equity_peak_usd`/`equity_peak_ts` si nouveau sommet. Calcul de `drawdown_pct`.
12. **Application différée des breakers "structurels"** décidés par `RiskManager.apply` à l'étape 7
    (flags `activate_flatten_mode`, `dd_half_size_active`) dans le nouvel objet `state` (pas
    seulement dans les cibles du cycle courant — ils doivent persister pour les cycles suivants).
13. **Construction du nouveau `state`** : `cash_usd`, `positions` depuis le `ledger` final,
    `equity_peak_*`, `circuit_breakers` mis à jour, `realized_pnl_cumulative_usd` incrémenté des
    PnL réalisés du cycle, `last_run_id = run_id`, `last_run_completed_at = now.isoformat()`,
    `state_hash_prev = prev_hash`.
14. **Écritures disque, dans l'ordre du §4.4** : `trades.jsonl` (tous les fills du cycle),
    `equity.jsonl` (une ligne), `decisions.jsonl` (une ligne par symbole de l'univers complet,
    crypto + actions, y compris ceux non évalués faute de marché ouvert ou de prix), puis
    `save_state(nouveau_state)`.
15. **git_sync** : `git_sync(repo_dir, message=f"Cycle {run_id} : {n_trades} trade(s), equity=
    {equity:.2f}$, DD={drawdown_pct:.2%}")`. Si retour `ABORTED_DUPLICATE` → sortie propre code 0
    (un autre run a gagné la course pour ce `run_id`, aucune anomalie). Si `FAILED` → log d'erreur
    explicite, sortie code non-zéro (alerting externe au scheduler, hors scope de ce module).
16. Fin du programme. Rien ne s'exécute après le `git push`.

---

## 7. Cas particulier : actions US hors heures de marché — résumé opérationnel

- Aucun ordre n'est jamais généré pour un symbole action quand `is_us_market_open(now) == False`
  (nuit, week-end, jour férié NYSE, pré/post-market exclus — seulement la séance régulière
  09:30-16:00 America/New_York est "ouverte" au sens de ce bot).
- Les positions actions existantes sont conservées telles quelles (aucune vente forcée à la
  fermeture).
- Elles restent marquées dans le calcul d'équity/drawdown au dernier prix connu, donc leur PnL
  latent continue d'apparaître dans `equity.jsonl` même hors séance (pas de "trou" dans la courbe
  d'équity le week-end).
- `decisions.jsonl` documente explicitement `market_open=false` pour chacune à chaque cycle
  horaire, même la nuit — la fraîcheur du dashboard doit pouvoir distinguer "marché fermé, normal"
  de "erreur de flux de prix".

---

## 8. Points explicitement laissés ouverts pour les prochains agents (backtests / stratégies)

- Le contenu réel de `bot/strategies/donchian.py`, `momentum_ema.py`, `mean_reversion_rsi2.py`
  (paramètres de lookback, filtres ADX/vol) est **hors scope de ce document** — à livrer après
  walk-forward validation (rapport §5, points 1-8), en respectant strictement l'interface
  `StrategyBase.target_weights()` définie ici.
- La pondération de `combine_strategies` (actuellement équi-pondérée) est un placeholder explicite
  à revoir une fois des statistiques de backtest walk-forward disponibles.
- `EQUITY_SYNTHETIC_SPREAD_ENABLED` : décision à trancher après mesure empirique de la
  disponibilité réelle des champs bid/ask Yahoo Finance en conditions de marché ouvert.
- Panel exact des megacaps actions (`SYMBOLS_EQUITY`) proposé ici à titre de valeur par défaut,
  modifiable dans `bot/config.py` sans impact sur le reste de l'architecture.

---

## 9. Addendum post-audit adversarial (2026-07-22) — correctifs d'intégrité

Un audit adversarial (exécution réelle sur copie isolée, réseau mocké déterministe, vrai code
`bot.risk`/`bot.sim`/`bot.persist`/`bot.runner`) a confirmé le réalisme pessimiste du
simulateur (fills, quotes périmées, panne API totale, double exécution concurrente, 4 circuit
breakers) mais a mis en évidence 3 failles corrigées ici :

1. **`verify_chain()` (finding CRITIQUE)** — `state_hash_prev` est un sha256 public : un
   attaquant (ou un bug de commit) peut fabriquer un nouveau commit avec un `state.json`
   arbitraire tout en recopiant correctement le hash réel précédent, ce qui passait `ok=True`.
   Correctif : `verify_chain()` vérifie désormais en plus un **invariant de conservation**
   entre chaque paire de commits successifs — toute variation de `cash_usd` et de
   `positions[symbole].qty` doit être exactement justifiée par les fills apparus dans
   `trades.jsonl` entre ces deux commits (aucun fill ⇒ aucune variation tolérée), et
   `trades.jsonl` doit rester un strict ajout (préfixe inchangé) d'un commit à l'autre. Voir
   `bot/persist/audit.py`.
2. **Fill fantôme après crash (finding MAJEUR)** — un `kill -9` entre deux `append_journal()`
   individuels au sein de la boucle de fills d'un cycle pouvait laisser une ligne orpheline
   dans `trades.jsonl` sans que `state.json` (écrit en dernier) ne l'intègre jamais ; le cycle
   suivant, ignorant tout du run_id, rejouait alors le cycle en entier et doublait le fill.
   Correctif double : (a) tous les fills d'un cycle sont désormais écrits en un seul appel
   `append_journal_many()` (un seul `write()`+`fsync()`, jamais un sous-ensemble sur crash) ;
   (b) `bot/runner.py` vérifie en tout début de cycle, via `records_for_run()`, qu'aucun
   enregistrement n'existe déjà pour le `run_id` visé dans `trades.jsonl`/`equity.jsonl`/
   `decisions.jsonl` alors que `state.json` l'ignore — signe d'un crash précédent — et
   s'arrête net (code non nul, aucune écriture) plutôt que de deviner une réconciliation.
3. **Idempotence aveugle à l'état distant (finding MAJEUR)** — si un crash survient après
   `save_state()` mais avant `git_sync()`, `state.json` local porte déjà `last_run_id = run_id`
   ; une invocation suivante pour le même run_id sortait alors en silence (code 0) sans jamais
   retenter la synchronisation, laissant le cycle réel engagé localement mais invisible sur
   `origin`. Correctif : avant de conclure à un doublon, `bot/runner.py` appelle
   `has_uncommitted_state_changes()` — si `state/*` porte des modifications non commitées, il
   reprend `git_sync()` au lieu d'abandonner en silence.

Tests de non-régression : `bot/tests/test_persist_audit.py` (invariant de conservation),
`bot/tests/test_persist_journal.py` (écriture groupée + détection d'orphelins),
`bot/tests/test_persist_git_sync.py` (détection de changements non commités),
`bot/tests/test_runner_crash_recovery.py` (scénarios de bout en bout : orphelin post-crash,
reprise de git_sync, cycle réel, idempotence).
