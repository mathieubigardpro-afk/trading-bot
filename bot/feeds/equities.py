"""Adaptateur actions US : Yahoo Finance public (v7 quote + v8 chart), repli `yfinance`.

Aucune clé API. Deux problèmes DISTINCTS identifiés en production le 2026-07-24, tous deux
corrigés dans ce module :

  1. (T16, marché ouvert) Yahoo gratuit (comptes non-abonnés) ne fournit quasi JAMAIS de
     `bid`/`ask` exploitables (champs absents ou à 0) sur `/v7/finance/quote`, même quand
     `regularMarketPrice` est parfaitement valide. Quand un dernier prix DIFFÉRÉ fiable existe
     (`regularMarketPrice > 0`, horodaté, dans la fenêtre de fraîcheur `STALENESS_MAX_SECONDS_
     EQUITY`) mais que bid/ask est absent/invalide, `_build_quote_from_result` reconstruit un
     bid/ask synthétique DÉFAVORABLE autour de ce prix plutôt que de rejeter la quote
     (`EQUITY_SYNTHETIC_SPREAD_ENABLED = True` par défaut, cf. `bot/config.py` pour le
     diagnostic complet et la justification des paliers de spread par classe d'actif).
  2. (T18/T19, marché ouvert, DIAGNOSTIQUÉ APRÈS le correctif #1) La requête `/v7/finance/quote`
     elle-même échoue purement et simplement — `429 Too Many Requests` observé en conditions
     réelles (`bot/feeds/equities.py` interrogé depuis un runner GitHub Actions, IP
     probablement rate-limitée par Yahoo, cf. bandeau incident ARCHITECTURE.md §12.6) — AVANT
     même d'atteindre `_build_quote_from_result` : aucune donnée bid/ask NI prix du tout pour
     AUCUN symbole. Repli : `_fetch_yfinance_last_prices` (bibliothèque `yfinance`, MÊME
     pipeline par lots déjà éprouvé en conditions réelles par `bot/feeds/daily.py`/
     `tools/build_daily_cache.py` — endpoint Yahoo distinct, non affecté par le même 429 le même
     jour) fournit un dernier prix intraday (1 minute), utilisé pour construire une quote
     synthétique EXACTEMENT comme le cas #1 (`source="yfinance_synthetic_spread"`).

Dans les deux cas, la `Quote` synthétique obtenue est marquée `synthetic_spread=True` ET
`delayed=True` — jamais confondue avec un vrai bid/ask coté temps réel, l'écart potentiel vs un
prix idéal instantané reste mesurable et honnêtement journalisé jusqu'à `decisions.jsonl`/
`trades.jsonl`. Si AUCUNE des deux sources ne fournit de prix exploitable du tout, la quote
reste `None` (no-trade strict, inchangé) — aucune donnée n'est jamais inventée à partir de rien.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import logging
import time

import pandas as pd
import requests

from bot.feeds._config_fallback import cfg
from bot.feeds.types import HistoryUnavailableError, Quote

try:  # dépendance optionnelle défensive — cf. bot/feeds/daily.py, même motif.
    import yfinance as yf

    _YFINANCE_AVAILABLE = True
    _YFINANCE_IMPORT_ERROR: str | None = None
except ImportError as _exc:  # pragma: no cover — dépendance non installée dans cet environnement
    yf = None  # type: ignore[assignment]
    _YFINANCE_AVAILABLE = False
    _YFINANCE_IMPORT_ERROR = str(_exc)

logger = logging.getLogger(__name__)

YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Repli yfinance (cf. bandeau de tête, cas #2) : lots + pause entre lots, motif directement
# repris de `bot/feeds/daily.py:YFINANCE_BATCH_SIZE`/`YFINANCE_BATCH_PAUSE_SECONDS` (seul
# pipeline déjà éprouvé en conditions réelles pour ce projet). Intraday 1 minute (pas quotidien)
# : c'est un prix D'EXÉCUTION, pas un historique — `period="1d"` suffit largement (on ne garde
# que la dernière bougie disponible).
YFINANCE_QUOTE_BATCH_SIZE = 15
YFINANCE_QUOTE_BATCH_PAUSE_SECONDS = 1.0
_YFINANCE_QUOTE_PERIOD = "1d"
_YFINANCE_QUOTE_INTERVAL = "1m"

# Symboles internes (`bot.config.EQUITIES_SP100_UNIVERSE`) dont le ticker Yahoo diffère du
# symbole interne — sans cette table, Yahoo ne reconnaît pas le symbole interne et le
# renvoie absent de `quoteResponse.result` (échec silencieux, systématique, pour CE
# symbole uniquement). Même correctif déjà appliqué côté historique journalier, cf.
# `bot/feeds/daily.py:YFINANCE_TICKER_OVERRIDES`/`STOOQ_TICKER_OVERRIDES` — resynchronisé ici.
_YAHOO_TICKER_OVERRIDES = {"BRK.B": "BRK-B"}


def _yahoo_ticker_for(symbol: str) -> str:
    return _YAHOO_TICKER_OVERRIDES.get(symbol, symbol)

_HTTP_TIMEOUT_SECONDS = 10
# Yahoo renvoie des erreurs (401/999) sans User-Agent de navigateur plausible.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Paliers de spread synthétique PAR CLASSE (bps, largeur TOTALE — cf. `_build_quote_from_
# result`, `bid = prix * (1 - s/2)`, `ask = prix * (1 + s/2)`) — repli `bot/feeds/_config_
# fallback.py` si `bot.config` non disponible (tests autonomes de `bot.feeds`). Calibrage et
# sources documentés en détail dans `bot/config.py` (mêmes constantes, source de vérité) :
# actions S&P100 "megacaps" 10 bps (double la borne haute usuelle 1-5 bps des megacaps US en
# continu), ETF très liquides (`bot.config.SYMBOLS_ETF`) 6 bps (multiple prudent d'un spread
# réel souvent < 1 bp pour SPY/QQQ).
_EQUITY_SYNTHETIC_SPREAD_BPS_DEFAULT = 10
_ETF_SYNTHETIC_SPREAD_BPS_DEFAULT = 6

_session = requests.Session()
_session.headers.update({"User-Agent": _USER_AGENT, "Accept": "application/json"})


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(ts: _dt.datetime) -> str:
    return ts.astimezone(_dt.timezone.utc).isoformat()


def _validate_bid_ask(bid: float, ask: float) -> bool:
    return bid > 0 and ask > 0 and bid < ask


_CLOCK_SKEW_TOLERANCE_SECONDS = 5.0


def _quote_is_fresh(quote_ts: _dt.datetime, max_age_seconds: float) -> bool:
    age = (_now_utc() - quote_ts).total_seconds()
    if age < -_CLOCK_SKEW_TOLERANCE_SECONDS:
        return False
    return age <= max_age_seconds


def _synthetic_spread_bps_for(internal_symbol: str) -> float:
    """Palier de spread synthétique pour `internal_symbol` (symbole INTERNE côté bot, pas le
    ticker Yahoo — important pour les overrides comme "BRK.B"/"BRK-B") : ETF de
    `bot.config.SYMBOLS_ETF` -> `ETF_SYNTHETIC_SPREAD_BPS` (6 bps par défaut), tout le reste
    (univers actions S&P100 réellement suivi par ce module) -> `EQUITY_SYNTHETIC_SPREAD_BPS`
    (10 bps par défaut). Cf. bandeau de tête de ce module / `bot/config.py` pour les sources."""
    etf_symbols = getattr(cfg, "SYMBOLS_ETF", ())
    if internal_symbol in etf_symbols:
        return float(getattr(cfg, "ETF_SYNTHETIC_SPREAD_BPS", _ETF_SYNTHETIC_SPREAD_BPS_DEFAULT))
    return float(getattr(cfg, "EQUITY_SYNTHETIC_SPREAD_BPS", _EQUITY_SYNTHETIC_SPREAD_BPS_DEFAULT))


def _build_synthetic_quote(internal_symbol: str, price: float, quote_ts: _dt.datetime, source: str) -> Quote:
    """Construit une quote synthétique DÉFAVORABLE (`bid = prix*(1-s/2)`, `ask = prix*(1+s/2)`)
    autour de `price`, `s` = palier de spread par classe d'actif (`_synthetic_spread_bps_for`).
    Toujours `delayed=True`/`synthetic_spread=True` — cf. bandeau de tête de ce module pour les
    deux cas d'appel (`_build_quote_from_result` : bid/ask Yahoo absent ; `get_prices_equity` :
    repli `yfinance` quand `/v7/finance/quote` échoue totalement)."""
    spread_bps = _synthetic_spread_bps_for(internal_symbol)
    half_spread = price * (spread_bps / 1e4) / 2.0
    return Quote(
        bid=price - half_spread,
        ask=price + half_spread,
        mid=price,
        ts=_iso(quote_ts),
        source=source,
        delayed=True,  # un prix synthétique n'est par construction jamais "temps réel"
        synthetic_spread=True,
    )


def _build_quote_from_result(result: dict, internal_symbol: str) -> Quote | None:
    symbol = result.get("symbol") or internal_symbol
    market_time_epoch = result.get("regularMarketTime")
    if market_time_epoch is None:
        logger.warning("yahoo quote sans regularMarketTime pour %s (aucun prix exploitable)", symbol)
        return None
    quote_ts = _dt.datetime.fromtimestamp(int(market_time_epoch), tz=_dt.timezone.utc)

    bid = result.get("bid")
    ask = result.get("ask")
    try:
        bid_f = float(bid) if bid is not None else None
        ask_f = float(ask) if ask is not None else None
    except (TypeError, ValueError):
        bid_f = ask_f = None

    if bid_f is not None and ask_f is not None and _validate_bid_ask(bid_f, ask_f):
        mid = (bid_f + ask_f) / 2.0
        return Quote(bid=bid_f, ask=ask_f, mid=mid, ts=_iso(quote_ts), source="yahoo")

    # Pas de bid/ask valide (cas FRÉQUENT côté Yahoo gratuit, cf. bandeau de tête de ce module)
    # -> spread synthétique DÉFAVORABLE autour du dernier prix différé fiable, SEULEMENT si
    # explicitement activé (`EQUITY_SYNTHETIC_SPREAD_ENABLED = True` par défaut, cf.
    # `bot/config.py` pour le diagnostic/la justification complets).
    if getattr(cfg, "EQUITY_SYNTHETIC_SPREAD_ENABLED", False):
        regular_price = result.get("regularMarketPrice")
        try:
            price = float(regular_price)
        except (TypeError, ValueError):
            price = None
        if price is not None and price > 0:
            logger.info(
                "yahoo quote sans bid/ask exploitable pour %s — spread synthétique autour du "
                "dernier prix différé (%.4f, ts=%s)",
                symbol, price, _iso(quote_ts),
            )
            return _build_synthetic_quote(internal_symbol, price, quote_ts, source="yahoo_synthetic_spread")

    logger.warning("yahoo quote sans bid/ask exploitable pour %s (no-trade strict)", symbol)
    return None


def _extract_yf_last_price(batch_df: "pd.DataFrame", yf_symbol: str, batch_len: int) -> tuple[float, _dt.datetime] | None:
    """Extrait `(dernier prix de clôture intraday, horodatage UTC)` d'un résultat `yfinance`
    potentiellement multi-tickers — même logique que `bot/feeds/daily.py:_extract_yf_ticker_
    frame`, adaptée à un DataFrame intraday (on ne garde que la DERNIÈRE bougie 1 minute)."""
    if batch_df is None or batch_df.empty:
        return None
    if isinstance(batch_df.columns, pd.MultiIndex):
        top_level = set(batch_df.columns.get_level_values(0))
        if yf_symbol not in top_level:
            return None
        sub = batch_df[yf_symbol]
    elif batch_len == 1:
        sub = batch_df
    else:
        return None
    if not isinstance(sub, pd.DataFrame) or "Close" not in sub.columns:
        return None

    closes = pd.to_numeric(sub["Close"], errors="coerce").dropna()
    if closes.empty:
        return None
    last_price = float(closes.iloc[-1])
    if last_price <= 0:
        return None

    last_ts = closes.index[-1]
    tzinfo = getattr(last_ts, "tzinfo", None)
    if tzinfo is None:
        # yfinance renvoie parfois un index tz-naive selon la version installée — on ne peut
        # alors pas garantir l'horodatage exact de la bougie ; "maintenant" reste la meilleure
        # approximation honnête disponible pour un fetch qui vient d'avoir lieu à l'instant.
        last_ts_utc = _now_utc()
    else:
        last_ts_utc = last_ts.to_pydatetime().astimezone(_dt.timezone.utc)
    return last_price, last_ts_utc


def _fetch_yfinance_last_prices(symbols: list[str]) -> dict[str, tuple[float, _dt.datetime] | None]:
    """Repli quand `/v7/finance/quote` échoue (429 observé en production 2026-07-24T19, cf.
    bandeau de tête de module) : dernier prix intraday via `yfinance` (téléchargement PAR LOTS,
    MÊME motif que `bot/feeds/daily.py`/`tools/build_daily_cache.py`, déjà éprouvé en conditions
    réelles le même jour depuis la même plage d'IP GitHub Actions — endpoint Yahoo distinct,
    visiblement pas soumis au même 429). Ne lève jamais d'exception : chaque échec individuel
    (lot ou symbole) laisse simplement `None` pour ce symbole."""
    results: dict[str, tuple[float, _dt.datetime] | None] = {s: None for s in symbols}
    if not symbols:
        return results
    if not _YFINANCE_AVAILABLE:
        logger.warning(
            "yfinance indisponible (%s) — aucun repli possible pour %s", _YFINANCE_IMPORT_ERROR, symbols
        )
        return results

    batches = [symbols[i : i + YFINANCE_QUOTE_BATCH_SIZE] for i in range(0, len(symbols), YFINANCE_QUOTE_BATCH_SIZE)]
    for batch_idx, batch in enumerate(batches, start=1):
        yf_symbols = [_yahoo_ticker_for(s) for s in batch]
        try:
            batch_df = yf.download(
                tickers=" ".join(yf_symbols),
                period=_YFINANCE_QUOTE_PERIOD,
                interval=_YFINANCE_QUOTE_INTERVAL,
                auto_adjust=True,
                group_by="ticker",
                threads=True,
                progress=False,
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 — yfinance peut lever des exceptions variées
            logger.warning(
                "yfinance repli quote lot %d/%d (%s) : échec (%s)", batch_idx, len(batches), batch, exc
            )
            batch_df = None

        if batch_df is not None and not batch_df.empty:
            for symbol, yf_symbol in zip(batch, yf_symbols):
                extracted = _extract_yf_last_price(batch_df, yf_symbol, len(batch))
                if extracted is not None:
                    results[symbol] = extracted
        else:
            logger.warning("yfinance repli quote lot %d/%d (%s) : aucune donnée exploitable", batch_idx, len(batches), batch)

        if batch_idx < len(batches):
            time.sleep(YFINANCE_QUOTE_BATCH_PAUSE_SECONDS)

    return results


def get_prices_equity(symbols: list[str]) -> dict[str, Quote | None]:
    """Retourne un Quote par symbole action demandé, ou None si Yahoo échoue, renvoie un JSON
    invalide, ou ne fournit AUCUN prix exploitable du tout (pas de `regularMarketPrice`/
    `regularMarketTime`) — cf. `_build_quote_from_result` pour le repli spread synthétique
    (activé par défaut) quand un prix différé fiable existe sans bid/ask valide."""
    if not symbols:
        return {}

    max_age = cfg.STALENESS_MAX_SECONDS_EQUITY
    # Seuil "temps réel plausible" (cf. bot/config.py) : au-delà, la quote actions/ETF est
    # tout de même utilisée (dans la limite de `max_age` ci-dessus) mais marquée `delayed=True`
    # — jamais rejetée silencieusement, jamais présentée comme temps réel. Repli 300s si absent
    # de `cfg` (compat tests qui construisent un `cfg` minimal).
    realtime_threshold = getattr(cfg, "EQUITY_QUOTE_REALTIME_THRESHOLD_SECONDS", 300)
    result: dict[str, Quote | None] = {sym: None for sym in symbols}

    def _finalize(sym: str, quote: Quote | None) -> Quote | None:
        """Applique la fraîcheur (`max_age`) et le marquage `delayed` au-delà du seuil temps
        réel plausible — commun aux deux sources (v7 quote ET repli `yfinance`)."""
        if quote is None:
            return None
        quote_ts = _dt.datetime.fromisoformat(quote.ts)
        if not _quote_is_fresh(quote_ts, max_age):
            logger.warning("quote périmée pour %s (source=%s)", sym, quote.source)
            return None
        age_seconds = (_now_utc() - quote_ts).total_seconds()
        if age_seconds > realtime_threshold:
            logger.info(
                "quote différée pour %s (%.0fs, seuil temps réel %.0fs, source=%s) — "
                "utilisée quand même (seuil de fraîcheur actions/ETF %.0fs), "
                "marquée delayed=true",
                sym, age_seconds, realtime_threshold, quote.source, max_age,
            )
            return dataclasses.replace(quote, delayed=True)
        return quote

    query_symbols = [_yahoo_ticker_for(sym) for sym in symbols]
    resp: requests.Response | None = None
    v7_failed_entirely = False
    try:
        resp = _session.get(
            YAHOO_QUOTE_URL,
            params={"symbols": ",".join(query_symbols)},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload["quoteResponse"]["result"]
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        # Diagnostic maximal (incident 2026-07-24T18, cf. ARCHITECTURE.md §12.5) : le statut
        # HTTP + un extrait du corps de réponse distinguent un blocage/quota Yahoo (401/429/999,
        # souvent accompagné d'une page HTML "consentement"/captcha au lieu du JSON attendu — cas
        # documenté de l'endpoint v7 quote gratuit sans "crumb"/cookie de session) d'une simple
        # panne réseau transitoire — invisible depuis `decisions.jsonl` seul (quote_source=None
        # dans les deux cas), cf. limite de journalisation déjà identifiée §12.2.
        status = getattr(resp, "status_code", None) if resp is not None else None
        snippet = None
        if resp is not None:
            try:
                snippet = resp.text[:300]
            except Exception:  # noqa: BLE001 — diagnostic best-effort uniquement
                snippet = None
        logger.warning(
            "yahoo v7 quote échec pour %s: %s (http_status=%s, extrait_reponse=%r)",
            symbols, exc, status, snippet,
        )
        # INCIDENT 2026-07-24T18/T19 (root cause #2, cf. bandeau de tête de module) : la requête
        # v7 ENTIÈRE peut échouer (429 observé en production) — on ne retourne plus `result`
        # (tout-None) directement ici : on tombe désormais dans le repli `yfinance` ci-dessous,
        # exactement comme si chaque symbole avait individuellement échoué à trouver une ligne.
        rows = []
        v7_failed_entirely = True

    if not v7_failed_entirely:
        by_symbol = {row.get("symbol"): row for row in rows if isinstance(row, dict)}
        for sym in symbols:
            row = by_symbol.get(_yahoo_ticker_for(sym))
            if row is None:
                logger.warning("yahoo v7 quote: pas de résultat pour %s", sym)
                continue
            quote = _build_quote_from_result(row, internal_symbol=sym)
            result[sym] = _finalize(sym, quote)

    # Repli yfinance (root cause #2) : uniquement pour les symboles encore sans quote après le
    # passage v7 ci-dessus — que ce soit parce que la requête v7 a échoué en bloc (429/timeout/
    # JSON invalide) ou parce qu'un symbole précis manquait de la réponse/du prix exploitable.
    missing = [sym for sym in symbols if result[sym] is None]
    if missing:
        logger.info(
            "repli yfinance pour %d/%d symbole(s) sans quote v7 (%s)",
            len(missing), len(symbols), missing,
        )
        yf_prices = _fetch_yfinance_last_prices(missing)
        for sym in missing:
            found = yf_prices.get(sym)
            if found is None:
                continue
            price, price_ts = found
            quote = _build_synthetic_quote(sym, price, price_ts, source="yfinance_synthetic_spread")
            result[sym] = _finalize(sym, quote)

    return result


def get_history_equity(symbol: str, n_hours: int) -> pd.DataFrame:
    """Bougies horaires clôturées via Yahoo v8 chart (`interval=1h`,
    `range=730d` — borne intraday connue de Yahoo Finance). Lève
    `HistoryUnavailableError` si moins de `n_hours` bougies valides ne sont
    obtenues (aucun fallback alternatif prévu par l'architecture pour les
    actions)."""
    url = YAHOO_CHART_URL.format(symbol=_yahoo_ticker_for(symbol))
    try:
        resp = _session.get(
            url,
            params={"interval": "1h", "range": "730d"},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        payload = resp.json()
        chart_result = payload["chart"]["result"][0]
        timestamps = chart_result["timestamp"]
        quote_block = chart_result["indicators"]["quote"][0]
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as exc:
        raise HistoryUnavailableError(
            f"equity {symbol}: échec de récupération/parsing Yahoo v8 chart ({exc})"
        ) from exc

    now = _now_utc()
    records = []
    opens = quote_block.get("open", [])
    highs = quote_block.get("high", [])
    lows = quote_block.get("low", [])
    closes = quote_block.get("close", [])
    volumes = quote_block.get("volume", [])

    for idx, t in enumerate(timestamps):
        if t is None:
            continue
        candle_close_time = _dt.datetime.fromtimestamp(int(t) + 3600, tz=_dt.timezone.utc)
        if candle_close_time > now:
            continue  # bougie encore en formation

        o, h, l, c, v = (
            opens[idx] if idx < len(opens) else None,
            highs[idx] if idx < len(highs) else None,
            lows[idx] if idx < len(lows) else None,
            closes[idx] if idx < len(closes) else None,
            volumes[idx] if idx < len(volumes) else None,
        )
        if None in (o, h, l, c):
            continue  # trou de données (hors séance, jour férié non filtré côté Yahoo, etc.)

        records.append(
            {
                "ts": pd.to_datetime(int(t), unit="s", utc=True),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v) if v is not None else 0.0,
            }
        )

    if not records:
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    else:
        df = pd.DataFrame.from_records(records).set_index("ts").sort_index()

    df = df.tail(n_hours)
    if len(df) < n_hours:
        raise HistoryUnavailableError(
            f"equity {symbol}: seulement {len(df)}/{n_hours} bougies clôturées valides obtenues via Yahoo"
        )

    return df
