"""Adaptateur actions US : Yahoo Finance public (v7 quote + v8 chart).

Aucune clé API. Pas de fallback bid/ask alternatif prévu par l'architecture
pour les actions : si Yahoo ne fournit pas de bid/ask valide, on retourne
`None` (no-trade strict) plutôt que d'inventer un spread synthétique — sauf
si `EQUITY_SYNTHETIC_SPREAD_ENABLED` est explicitement activé dans
`bot/config.py` (`False` par défaut, cf. `docs/ARCHITECTURE.md` §5.1).
"""

from __future__ import annotations

import datetime as _dt
import logging

import pandas as pd
import requests

from bot.feeds._config_fallback import cfg
from bot.feeds.types import HistoryUnavailableError, Quote

logger = logging.getLogger(__name__)

YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

_HTTP_TIMEOUT_SECONDS = 10
# Yahoo renvoie des erreurs (401/999) sans User-Agent de navigateur plausible.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_SYNTHETIC_SPREAD_BPS = 15  # 0.15%, largeur totale, moitié de chaque côté du mid

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


def _build_quote_from_result(result: dict) -> Quote | None:
    symbol = result.get("symbol", "?")
    market_time_epoch = result.get("regularMarketTime")
    if market_time_epoch is None:
        logger.warning("yahoo quote sans regularMarketTime pour %s", symbol)
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

    # Pas de bid/ask valide -> spread synthétique SEULEMENT si explicitement
    # activé (désactivé par défaut, cf. docstring module).
    if getattr(cfg, "EQUITY_SYNTHETIC_SPREAD_ENABLED", False):
        regular_price = result.get("regularMarketPrice")
        try:
            price = float(regular_price)
        except (TypeError, ValueError):
            price = None
        if price is not None and price > 0:
            half_spread = price * (_SYNTHETIC_SPREAD_BPS / 1e4) / 2.0
            return Quote(
                bid=price - half_spread,
                ask=price + half_spread,
                mid=price,
                ts=_iso(quote_ts),
                source="yahoo_synthetic_spread",
            )

    logger.warning("yahoo quote sans bid/ask exploitable pour %s (no-trade strict)", symbol)
    return None


def get_prices_equity(symbols: list[str]) -> dict[str, Quote | None]:
    """Retourne un Quote par symbole action demandé, ou None si Yahoo échoue,
    renvoie un JSON invalide, ou ne fournit pas de bid/ask exploitable et
    frais (et que le spread synthétique est désactivé, cas par défaut)."""
    if not symbols:
        return {}

    max_age = cfg.STALENESS_MAX_SECONDS_EQUITY
    result: dict[str, Quote | None] = {sym: None for sym in symbols}

    try:
        resp = _session.get(
            YAHOO_QUOTE_URL,
            params={"symbols": ",".join(symbols)},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload["quoteResponse"]["result"]
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        logger.warning("yahoo v7 quote échec pour %s: %s", symbols, exc)
        return result

    by_symbol = {row.get("symbol"): row for row in rows if isinstance(row, dict)}
    for sym in symbols:
        row = by_symbol.get(sym)
        if row is None:
            logger.warning("yahoo v7 quote: pas de résultat pour %s", sym)
            continue
        quote = _build_quote_from_result(row)
        if quote is not None:
            quote_ts = _dt.datetime.fromisoformat(quote.ts)
            if not _quote_is_fresh(quote_ts, max_age):
                logger.warning("yahoo quote périmée pour %s", sym)
                quote = None
        result[sym] = quote

    return result


def get_history_equity(symbol: str, n_hours: int) -> pd.DataFrame:
    """Bougies horaires clôturées via Yahoo v8 chart (`interval=1h`,
    `range=730d` — borne intraday connue de Yahoo Finance). Lève
    `HistoryUnavailableError` si moins de `n_hours` bougies valides ne sont
    obtenues (aucun fallback alternatif prévu par l'architecture pour les
    actions)."""
    url = YAHOO_CHART_URL.format(symbol=symbol)
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
