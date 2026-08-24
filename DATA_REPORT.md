# DATA_REPORT — données marché du bot de paper trading

Généré le 2026-08-24T08:13:49.281428+00:00 (durée de génération : 436s).

Cette branche (`market-data`) est entièrement régénérée à chaque exécution de `tools/fetch_data.py` (voir `.github/workflows/fetch-data.yml`) — son historique git n'a pas de valeur en soi, seul le contenu du dernier commit compte.

## Sources

- Crypto horaire : archives bulk Binance (`https://data.binance.vision/data/spot/monthly/klines/{PAIR}/1h/{PAIR}-1h-{YYYY-MM}.zip`), complétées pour le mois en cours via l'API publique (`https://api.binance.com/api/v3/klines`).
- Actions (S&P 100) et ETF, quotidien, prix ajustés : primaire yfinance (yf.download / yf.Ticker.history, period=max, interval=1d, auto_adjust=True) ; repli par ticker stooq.com (`https://stooq.com/q/d/l/?s={ticker}.us&i=d`, User-Agent navigateur, séquentiel, pause >= 1.0s/requête) — utilisé uniquement si yfinance échoue pour un ticker.
- Funding rate perpétuels (futures USDT-M) : archives bulk (`https://data.binance.vision/data/futures/um/monthly/fundingRate/{PAIR}/{PAIR}-fundingRate-{YYYY-MM}.zip`), complétées via l'API publique (`https://fapi.binance.com/fapi/v1/fundingRate`).
- Klines perpétuelles 1h (futures USDT-M) : archives bulk (`https://data.binance.vision/data/futures/um/monthly/klines/{PAIR}/1h/{PAIR}-1h-{YYYY-MM}.zip`), complétées via l'API publique (`https://fapi.binance.com/fapi/v1/klines`).

## Crypto

- Fenêtre d'archive : 2022-01 → 2026-07 (+ complément mois courant via API).
- Fenêtre de complétude obligatoire (sinon exclusion) : depuis 2023-07.
- **30 paire(s) incluse(s)**, **0 exclue(s)**.

### Paires crypto incluses

| Symbole | Paire | Lignes | Début | Fin |
|---|---|---|---|---|
| AAVE | AAVEUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| ADA | ADAUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| ALGO | ALGOUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| APT | APTUSDT | 33166 | 2022-10-19T01:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| ARB | ARBUSDT | 29432 | 2023-03-23T15:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| ATOM | ATOMUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| AVAX | AVAXUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| BCH | BCHUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| BNB | BNBUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| BTC | BTCUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| DOGE | DOGEUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| DOT | DOTUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| ETC | ETCUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| ETH | ETHUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| FIL | FILUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| HBAR | HBARUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| ICP | ICPUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| INJ | INJUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| LINK | LINKUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| LTC | LTCUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| MANA | MANAUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| NEAR | NEARUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| OP | OPUSDT | 36519 | 2022-06-01T08:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| SAND | SANDUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| SOL | SOLUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| TRX | TRXUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| UNI | UNIUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| VET | VETUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| XLM | XLMUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| XRP | XRPUSDT | 40151 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |

## Actions (S&P 100)

- **103 ticker(s) OK**, **1 échoué(s)/vide(s)**.
- Répartition par source : {'yfinance': 103, 'FAILED': 1}.

### Tickers actions en échec

- **BK** (source tentée : FAILED) : ERROR — yfinance et stooq (repli) ont tous deux échoué — dernière erreur stooq: réponse stooq vide/invalide (ticker inconnu, ou blocage/rate limiting persistant)

## ETF

- **18 ticker(s) OK**, **0 échoué(s)/vide(s)**.
- Répartition par source : {'yfinance': 18}.

## Funding rate (perpétuels USDT-M)

- Fenêtre d'archive : 2022-01 → 2026-07 (+ complément mois courant via API).
- Seuil de flag |funding_rate| > 3% (conservé dans les données, jamais supprimé — journalisé ci-dessous).
- **30 paire(s) incluse(s)**, **0 exclue(s)**.

### Paires funding incluses

| Symbole | Paire | Lignes | Début | Fin |
|---|---|---|---|---|
| AAVE | AAVEUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| ADA | ADAUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| ALGO | ALGOUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| APT | APTUSDT | 4147 | 2022-10-18T16:00:00+00:00 | 2026-07-31T16:00:00+00:00 |
| ARB | ARBUSDT | 3680 | 2023-03-23T08:00:00.001000+00:00 | 2026-07-31T16:00:00+00:00 |
| ATOM | ATOMUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| AVAX | AVAXUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| BCH | BCHUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| BNB | BNBUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| BTC | BTCUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| DOGE | DOGEUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| DOT | DOTUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| ETC | ETCUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| ETH | ETHUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| FIL | FILUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| HBAR | HBARUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| ICP | ICPUSDT | 4290 | 2022-09-01T00:00:00.012000+00:00 | 2026-07-31T16:00:00+00:00 |
| INJ | INJUSDT | 4336 | 2022-08-16T16:00:00+00:00 | 2026-07-31T16:00:00+00:00 |
| LINK | LINKUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| LTC | LTCUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| MANA | MANAUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| NEAR | NEARUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| OP | OPUSDT | 4565 | 2022-06-01T08:00:00.011000+00:00 | 2026-07-31T16:00:00+00:00 |
| SAND | SANDUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| SOL | SOLUSDT | 5094 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| TRX | TRXUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| UNI | UNIUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| VET | VETUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| XLM | XLMUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |
| XRP | XRPUSDT | 5019 | 2022-01-01T00:00:00.006000+00:00 | 2026-07-31T16:00:00+00:00 |

## Klines perpétuelles (futures USDT-M, horaire)

- Fenêtre d'archive : 2022-01 → 2026-07 (+ complément mois courant via API).
- **30 paire(s) incluse(s)**, **0 exclue(s)**.

### Paires perp incluses

| Symbole | Paire | Lignes | Début | Fin |
|---|---|---|---|---|
| AAVE | AAVEUSDT | 40152 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| ADA | ADAUSDT | 40152 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| ALGO | ALGOUSDT | 40152 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| APT | APTUSDT | 33166 | 2022-10-19T02:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| ARB | ARBUSDT | 29433 | 2023-03-23T15:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| ATOM | ATOMUSDT | 40152 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| AVAX | AVAXUSDT | 40152 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| BCH | BCHUSDT | 40152 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| BNB | BNBUSDT | 40152 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| BTC | BTCUSDT | 40152 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| DOGE | DOGEUSDT | 40152 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| DOT | DOTUSDT | 40152 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| ETC | ETCUSDT | 40152 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| ETH | ETHUSDT | 40152 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| FIL | FILUSDT | 40032 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| HBAR | HBARUSDT | 40032 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| ICP | ICPUSDT | 39526 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| INJ | INJUSDT | 34678 | 2022-08-17T02:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| LINK | LINKUSDT | 40152 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| LTC | LTCUSDT | 40032 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| MANA | MANAUSDT | 40032 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| NEAR | NEARUSDT | 40032 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| OP | OPUSDT | 36514 | 2022-06-01T14:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| SAND | SANDUSDT | 40032 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| SOL | SOLUSDT | 40032 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| TRX | TRXUSDT | 40032 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| UNI | UNIUSDT | 40152 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| VET | VETUSDT | 40032 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| XLM | XLMUSDT | 40032 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |
| XRP | XRPUSDT | 40032 | 2022-01-01T00:00:00+00:00 | 2026-07-31T23:00:00+00:00 |

## Format des fichiers

`data/{crypto,equities,etf,perp}/{SYMBOLE}.csv.gz` — colonnes `timestamp,open,high,low,close,volume`, `timestamp` en ISO8601 UTC, dédoublonné et trié par ordre croissant. Crypto/perp = bougies horaires ; actions/ETF = bougies journalières.
`data/funding/{SYMBOLE}.csv.gz` — colonnes `timestamp,funding_rate` (+ `funding_interval_hours` si disponible pour ce symbole, colonne absente sinon), `timestamp` en ISO8601 UTC, dédoublonné et trié par ordre croissant. Les funding rates extrêmes (|taux| > seuil) sont conservés et flaggés, jamais supprimés.

## Anomalies de données (actions/ETF)

Scan automatique (`tools/check_data_anomalies.py`, backlog P0#11) : **19 anomalie(s)** sur 121 fichier(s) (equities, etf). Détail : `DATA_ANOMALIES.md` / `anomalies.json`. Journal de revue humaine — aucune donnée n'est corrigée ni exclue par ce scan.
