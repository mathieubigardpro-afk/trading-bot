# DATA_REPORT — données marché du bot de paper trading

Généré le 2026-11-14T12:02:47.271622+00:00 (durée de génération : 479s).

Cette branche (`market-data`) est entièrement régénérée à chaque exécution de `tools/fetch_data.py` (voir `.github/workflows/fetch-data.yml`) — son historique git n'a pas de valeur en soi, seul le contenu du dernier commit compte.

## Sources

- Crypto horaire : archives bulk Binance (`https://data.binance.vision/data/spot/monthly/klines/{PAIR}/1h/{PAIR}-1h-{YYYY-MM}.zip`), complétées pour le mois en cours via l'API publique (`https://api.binance.com/api/v3/klines`).
- Actions (S&P 100) et ETF, quotidien, prix ajustés : primaire yfinance (yf.download / yf.Ticker.history, period=max, interval=1d, auto_adjust=True) ; repli par ticker stooq.com (`https://stooq.com/q/d/l/?s={ticker}.us&i=d`, User-Agent navigateur, séquentiel, pause >= 1.0s/requête) — utilisé uniquement si yfinance échoue pour un ticker.
- Funding rate perpétuels (futures USDT-M) : archives bulk (`https://data.binance.vision/data/futures/um/monthly/fundingRate/{PAIR}/{PAIR}-fundingRate-{YYYY-MM}.zip`), complétées via l'API publique (`https://fapi.binance.com/fapi/v1/fundingRate`).
- Klines perpétuelles 1h (futures USDT-M) : archives bulk (`https://data.binance.vision/data/futures/um/monthly/klines/{PAIR}/1h/{PAIR}-1h-{YYYY-MM}.zip`), complétées via l'API publique (`https://fapi.binance.com/fapi/v1/klines`).

## Crypto

- Fenêtre d'archive : 2022-01 → 2026-10 (+ complément mois courant — et mois de queue manquants, backlog #18 — via API).
- Fenêtre de complétude obligatoire (sinon exclusion) : depuis 2023-07.
- **30 paire(s) incluse(s)**, **0 exclue(s)**.

### Paires crypto incluses

| Symbole | Paire | Lignes | Début | Fin |
|---|---|---|---|---|
| AAVE | AAVEUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| ADA | ADAUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| ALGO | ALGOUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| APT | APTUSDT | 35374 | 2022-10-19T01:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| ARB | ARBUSDT | 31640 | 2023-03-23T15:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| ATOM | ATOMUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| AVAX | AVAXUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| BCH | BCHUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| BNB | BNBUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| BTC | BTCUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| DOGE | DOGEUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| DOT | DOTUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| ETC | ETCUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| ETH | ETHUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| FIL | FILUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| HBAR | HBARUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| ICP | ICPUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| INJ | INJUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| LINK | LINKUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| LTC | LTCUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| MANA | MANAUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| NEAR | NEARUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| OP | OPUSDT | 38727 | 2022-06-01T08:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| SAND | SANDUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| SOL | SOLUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| TRX | TRXUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| UNI | UNIUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| VET | VETUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| XLM | XLMUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| XRP | XRPUSDT | 42359 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |

## Actions (S&P 100)

- **103 ticker(s) OK**, **1 échoué(s)/vide(s)**.
- Répartition par source : {'yfinance': 103, 'FAILED': 1}.

### Tickers actions en échec

- **BK** (source tentée : FAILED) : ERROR — yfinance et stooq (repli) ont tous deux échoué — dernière erreur stooq: réponse stooq vide/invalide (ticker inconnu, ou blocage/rate limiting persistant)

## ETF

- **18 ticker(s) OK**, **0 échoué(s)/vide(s)**.
- Répartition par source : {'yfinance': 18}.

## Funding rate (perpétuels USDT-M)

- Fenêtre d'archive : 2022-01 → 2026-10 (+ complément mois courant via API).
- Seuil de flag |funding_rate| > 3% (conservé dans les données, jamais supprimé — journalisé ci-dessous).
- **30 paire(s) incluse(s)**, **0 exclue(s)**.

### Paires funding incluses

| Symbole | Paire | Lignes | Début | Fin |
|---|---|---|---|---|
| AAVE | AAVEUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| ADA | ADAUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| ALGO | ALGOUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| APT | APTUSDT | 4423 | 2022-10-18T16:00:00+00:00 | 2026-10-31T16:00:00+00:00 |
| ARB | ARBUSDT | 3956 | 2023-03-23T08:00:00.001000+00:00 | 2026-10-31T16:00:00+00:00 |
| ATOM | ATOMUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| AVAX | AVAXUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| BCH | BCHUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| BNB | BNBUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| BTC | BTCUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| DOGE | DOGEUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| DOT | DOTUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| ETC | ETCUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| ETH | ETHUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| FIL | FILUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| HBAR | HBARUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| ICP | ICPUSDT | 4566 | 2022-09-01T00:00:00.012000+00:00 | 2026-10-31T16:00:00+00:00 |
| INJ | INJUSDT | 4612 | 2022-08-16T16:00:00+00:00 | 2026-10-31T16:00:00+00:00 |
| LINK | LINKUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| LTC | LTCUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| MANA | MANAUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| NEAR | NEARUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| OP | OPUSDT | 4841 | 2022-06-01T08:00:00.011000+00:00 | 2026-10-31T16:00:00+00:00 |
| SAND | SANDUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| SOL | SOLUSDT | 5370 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| TRX | TRXUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| UNI | UNIUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| VET | VETUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| XLM | XLMUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |
| XRP | XRPUSDT | 5295 | 2022-01-01T00:00:00.006000+00:00 | 2026-10-31T16:00:00+00:00 |

## Klines perpétuelles (futures USDT-M, horaire)

- Fenêtre d'archive : 2022-01 → 2026-10 (+ complément mois courant via API).
- **30 paire(s) incluse(s)**, **0 exclue(s)**.

### Paires perp incluses

| Symbole | Paire | Lignes | Début | Fin |
|---|---|---|---|---|
| AAVE | AAVEUSDT | 42360 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| ADA | ADAUSDT | 42360 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| ALGO | ALGOUSDT | 42360 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| APT | APTUSDT | 35374 | 2022-10-19T02:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| ARB | ARBUSDT | 31641 | 2023-03-23T15:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| ATOM | ATOMUSDT | 42360 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| AVAX | AVAXUSDT | 42360 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| BCH | BCHUSDT | 42360 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| BNB | BNBUSDT | 42360 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| BTC | BTCUSDT | 42360 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| DOGE | DOGEUSDT | 42360 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| DOT | DOTUSDT | 42360 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| ETC | ETCUSDT | 42360 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| ETH | ETHUSDT | 42360 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| FIL | FILUSDT | 42240 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| HBAR | HBARUSDT | 42240 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| ICP | ICPUSDT | 41734 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| INJ | INJUSDT | 36886 | 2022-08-17T02:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| LINK | LINKUSDT | 42360 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| LTC | LTCUSDT | 42240 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| MANA | MANAUSDT | 42240 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| NEAR | NEARUSDT | 42240 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| OP | OPUSDT | 38722 | 2022-06-01T14:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| SAND | SANDUSDT | 42240 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| SOL | SOLUSDT | 42240 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| TRX | TRXUSDT | 42240 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| UNI | UNIUSDT | 42360 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| VET | VETUSDT | 42240 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| XLM | XLMUSDT | 42240 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |
| XRP | XRPUSDT | 42240 | 2022-01-01T00:00:00+00:00 | 2026-10-31T23:00:00+00:00 |

## Format des fichiers

`data/{crypto,equities,etf,perp}/{SYMBOLE}.csv.gz` — colonnes `timestamp,open,high,low,close,volume`, `timestamp` en ISO8601 UTC, dédoublonné et trié par ordre croissant. Crypto/perp = bougies horaires ; actions/ETF = bougies journalières.
`data/funding/{SYMBOLE}.csv.gz` — colonnes `timestamp,funding_rate` (+ `funding_interval_hours` si disponible pour ce symbole, colonne absente sinon), `timestamp` en ISO8601 UTC, dédoublonné et trié par ordre croissant. Les funding rates extrêmes (|taux| > seuil) sont conservés et flaggés, jamais supprimés.

## Anomalies de données (actions/ETF)

Scan automatique (`tools/check_data_anomalies.py`, backlog P0#11) : **19 anomalie(s)** sur 121 fichier(s) (equities, etf). Détail : `DATA_ANOMALIES.md` / `anomalies.json`. Journal de revue humaine — aucune donnée n'est corrigée ni exclue par ce scan.
