# DATA_REPORT — données marché du bot de paper trading

Généré le 2026-07-23T07:27:37.724766+00:00 (durée de génération : 200s).

Cette branche (`market-data`) est entièrement régénérée à chaque exécution de `tools/fetch_data.py` (voir `.github/workflows/fetch-data.yml`) — son historique git n'a pas de valeur en soi, seul le contenu du dernier commit compte.

## Sources

- Crypto horaire : archives bulk Binance (`https://data.binance.vision/data/spot/monthly/klines/{PAIR}/1h/{PAIR}-1h-{YYYY-MM}.zip`), complétées pour le mois en cours via l'API publique (`https://api.binance.com/api/v3/klines`).
- Actions (S&P 100) et ETF, quotidien, prix ajustés : primaire yfinance (yf.download / yf.Ticker.history, period=max, interval=1d, auto_adjust=True) ; repli par ticker stooq.com (`https://stooq.com/q/d/l/?s={ticker}.us&i=d`, User-Agent navigateur, séquentiel, pause >= 1.0s/requête) — utilisé uniquement si yfinance échoue pour un ticker.

## Crypto

- Fenêtre d'archive : 2022-01 → 2026-06 (+ complément mois courant via API).
- Fenêtre de complétude obligatoire (sinon exclusion) : depuis 2023-07.
- **30 paire(s) incluse(s)**, **0 exclue(s)**.

### Paires crypto incluses

| Symbole | Paire | Lignes | Début | Fin |
|---|---|---|---|---|
| AAVE | AAVEUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| ADA | ADAUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| ALGO | ALGOUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| APT | APTUSDT | 32422 | 2022-10-19T01:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| ARB | ARBUSDT | 28688 | 2023-03-23T15:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| ATOM | ATOMUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| AVAX | AVAXUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| BCH | BCHUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| BNB | BNBUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| BTC | BTCUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| DOGE | DOGEUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| DOT | DOTUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| ETC | ETCUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| ETH | ETHUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| FIL | FILUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| HBAR | HBARUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| ICP | ICPUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| INJ | INJUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| LINK | LINKUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| LTC | LTCUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| MANA | MANAUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| NEAR | NEARUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| OP | OPUSDT | 35775 | 2022-06-01T08:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| SAND | SANDUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| SOL | SOLUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| TRX | TRXUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| UNI | UNIUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| VET | VETUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| XLM | XLMUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |
| XRP | XRPUSDT | 39407 | 2022-01-01T00:00:00+00:00 | 2026-06-30T23:00:00+00:00 |

## Actions (S&P 100)

- **103 ticker(s) OK**, **1 échoué(s)/vide(s)**.
- Répartition par source : {'yfinance': 103, 'FAILED': 1}.

### Tickers actions en échec

- **BK** (source tentée : FAILED) : ERROR — yfinance et stooq (repli) ont tous deux échoué — dernière erreur stooq: réponse stooq vide/invalide (ticker inconnu, ou blocage/rate limiting persistant)

## ETF

- **18 ticker(s) OK**, **0 échoué(s)/vide(s)**.
- Répartition par source : {'yfinance': 18}.

## Format des fichiers

`data/{crypto,equities,etf}/{SYMBOLE}.csv.gz` — colonnes `timestamp,open,high,low,close,volume`, `timestamp` en ISO8601 UTC, dédoublonné et trié par ordre croissant. Crypto = bougies horaires ; actions/ETF = bougies journalières.
