"""Adaptateur crypto : Binance public REST (source primaire) avec fallback
Coinbase Exchange public REST. Aucune clé API requise (endpoints publics).

Principe cardinal : jamais de prix inventé. Toute exception réseau, tout JSON
invalide, tout bid/ask incohérent (bid<=0, ask<=0, bid>=ask) est traité comme
un échec de la source concernée -> tentative de la source suivante -> `None`
si toutes échouent. Aucune exception n'est jamais laissée fuiter au niveau
symbole individuel depuis `get_prices_crypto`.
"""

from __future__ import annotations

import datetime as _dt
import logging

import pandas as pd
import requests

from bot.feeds._config_fallback import cfg
from bot.feeds.types import HistoryUnavailableError, Quote

logger = logging.getLogger(__name__)

BINANCE_BASE_URL = "https://api.binance.com"
COINBASE_BASE_URL = "https://api.exchange.coinbase.com"

_HTTP_TIMEOUT_SECONDS = 10
_USER_AGENT = "trading-bot-paper/1.0 (+https://github.com/mathieubigardpro-afk/trading-bot)"

_COINBASE_MAX_CANDLES_PER_CALL = 300
_COINBASE_MAX_PAGES = 20  # garde-fou anti-boucle-infinie, largement suffisant (20*300=6000h)

_session = requests.Session()
_session.headers.update({"User-Agent": _USER_AGENT})


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(ts: _dt.datetime) -> str:
    return ts.astimezone(_dt.timezone.utc).isoformat()


def _validate_bid_ask(bid: float, ask: float) -> bool:
    return bid > 0 and ask > 0 and bid < ask


_CLOCK_SKEW_TOLERANCE_SECONDS = 5.0


def _quote_is_fresh(quote_ts: _dt.datetime, max_age_seconds: float) -> bool:
    """True si `quote_ts` est dans la fenêtre [-tolérance_skew, max_age_seconds]
    par rapport à maintenant. Un horodatage "dans le futur" au-delà de la
    tolérance de déphasage d'horloge est traité comme suspect -> rejeté (pas
    de confiance aveugle dans une source qui daterait ses quotes dans le
    futur)."""
    age = (_now_utc() - quote_ts).total_seconds()
    if age < -_CLOCK_SKEW_TOLERANCE_SECONDS:
        return False
    return age <= max_age_seconds


def _fetch_binance_bookticker(pair: str) -> Quote | None:
    url = f"{BINANCE_BASE_URL}/api/v3/ticker/bookTicker"
    try:
        resp = _session.get(url, params={"symbol": pair}, timeout=_HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        bid = float(data["bidPrice"])
        ask = float(data["askPrice"])
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        logger.warning("binance bookTicker échec pour %s: %s", pair, exc)
        return None

    if not _validate_bid_ask(bid, ask):
        logger.warning("binance bookTicker bid/ask invalide pour %s: bid=%s ask=%s", pair, bid, ask)
        return None

    # bookTicker ne renvoie aucun horodatage propre à la quote : c'est un
    # instantané du carnet d'ordres au moment de la réponse HTTP. L'heure de
    # réception est donc la meilleure approximation disponible de "l'heure
    # source" pour cet endpoint précis (documenté explicitement ici).
    now = _now_utc()
    mid = (bid + ask) / 2.0
    return Quote(bid=bid, ask=ask, mid=mid, ts=_iso(now), source="binance")


def _fetch_coinbase_ticker(pair: str) -> Quote | None:
    url = f"{COINBASE_BASE_URL}/products/{pair}/ticker"
    try:
        resp = _session.get(url, timeout=_HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        bid = float(data["bid"])
        ask = float(data["ask"])
        ts_raw = data["time"]
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        logger.warning("coinbase ticker échec pour %s: %s", pair, exc)
        return None

    if not _validate_bid_ask(bid, ask):
        logger.warning("coinbase ticker bid/ask invalide pour %s: bid=%s ask=%s", pair, bid, ask)
        return None

    try:
        quote_ts = _parse_coinbase_time(ts_raw)
    except ValueError as exc:
        logger.warning("coinbase ticker horodatage invalide pour %s: %s", pair, exc)
        return None

    mid = (bid + ask) / 2.0
    return Quote(bid=bid, ask=ask, mid=mid, ts=_iso(quote_ts), source="coinbase")


def _parse_coinbase_time(ts_raw: str) -> _dt.datetime:
    # Coinbase renvoie par ex. "2026-05-16T07:03:03.644646259Z" — nanosecondes,
    # non supporté nativement par fromisoformat avant troncature.
    raw = ts_raw.rstrip("Z")
    if "." in raw:
        head, frac = raw.split(".", 1)
        frac = (frac + "000000")[:6]  # tronque/complète à 6 chiffres (microsecondes)
        raw = f"{head}.{frac}"
    dt = _dt.datetime.fromisoformat(raw)
    return dt.replace(tzinfo=_dt.timezone.utc)


def get_prices_crypto(symbols: list[str]) -> dict[str, Quote | None]:
    """Retourne un Quote frais (bid/ask/mid) par symbole crypto demandé, ou
    None si Binance ET Coinbase ont tous les deux échoué ou renvoyé une
    quote périmée/invalide."""
    max_age = cfg.STALENESS_MAX_SECONDS_CRYPTO
    result: dict[str, Quote | None] = {}

    for symbol in symbols:
        binance_pair = cfg.CRYPTO_PAIR_BINANCE.get(symbol)
        coinbase_pair = cfg.CRYPTO_PAIR_COINBASE.get(symbol)

        quote: Quote | None = None
        if binance_pair is not None:
            quote = _fetch_binance_bookticker(binance_pair)
            if quote is not None:
                quote_ts = _dt.datetime.fromisoformat(quote.ts)
                if not _quote_is_fresh(quote_ts, max_age):
                    logger.warning("binance bookTicker périmé pour %s", symbol)
                    quote = None

        if quote is None and coinbase_pair is not None:
            quote = _fetch_coinbase_ticker(coinbase_pair)
            if quote is not None:
                quote_ts = _dt.datetime.fromisoformat(quote.ts)
                if not _quote_is_fresh(quote_ts, max_age):
                    logger.warning("coinbase ticker périmé pour %s", symbol)
                    quote = None

        result[symbol] = quote

    return result


def _fetch_binance_klines(pair: str, n_hours: int) -> pd.DataFrame | None:
    url = f"{BINANCE_BASE_URL}/api/v3/klines"
    limit = min(n_hours + 5, 1000)  # 1000 = max autorisé par Binance par appel
    try:
        resp = _session.get(
            url,
            params={"symbol": pair, "interval": "1h", "limit": limit},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        rows = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("binance klines échec pour %s: %s", pair, exc)
        return None

    if not isinstance(rows, list):
        logger.warning("binance klines format inattendu pour %s", pair)
        return None

    now_ms = int(_now_utc().timestamp() * 1000)
    records = []
    for row in rows:
        try:
            open_time_ms, o, h, l, c, v, close_time_ms = row[0], row[1], row[2], row[3], row[4], row[5], row[6]
        except (IndexError, TypeError):
            continue
        if int(close_time_ms) >= now_ms:
            continue  # bougie encore en formation, exclue systématiquement
        records.append(
            {
                "ts": pd.to_datetime(int(open_time_ms), unit="ms", utc=True),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v),
            }
        )

    if not records:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame.from_records(records).set_index("ts").sort_index()
    return df.tail(n_hours)


def _fetch_coinbase_candles(pair: str, n_hours: int) -> pd.DataFrame | None:
    url = f"{COINBASE_BASE_URL}/products/{pair}/candles"
    now = _now_utc()
    current_hour_start = now.replace(minute=0, second=0, microsecond=0)
    end_time = current_hour_start  # exclut la bougie en cours de formation
    collected: dict[int, dict] = {}

    for _ in range(_COINBASE_MAX_PAGES):
        if len(collected) >= n_hours:
            break
        start_time = end_time - _dt.timedelta(hours=_COINBASE_MAX_CANDLES_PER_CALL)
        try:
            resp = _session.get(
                url,
                params={
                    "granularity": 3600,
                    "start": start_time.isoformat(),
                    "end": end_time.isoformat(),
                },
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            rows = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("coinbase candles échec pour %s: %s", pair, exc)
            break

        if not isinstance(rows, list) or not rows:
            break

        for row in rows:
            try:
                t, low, high, open_, close, volume = row
            except (ValueError, TypeError):
                continue
            t = int(t)
            if t >= int(current_hour_start.timestamp()):
                continue
            collected[t] = {
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(volume),
            }

        end_time = start_time

    if not collected:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame.from_dict(collected, orient="index")
    df.index = pd.to_datetime(df.index, unit="s", utc=True)
    df = df.sort_index()[["open", "high", "low", "close", "volume"]]
    return df.tail(n_hours)


def get_history_crypto(symbol: str, n_hours: int) -> pd.DataFrame:
    """Voir `bot.feeds.get_history` pour le contrat complet. Essaie Binance
    klines, puis Coinbase candles en repli si Binance ne fournit pas assez de
    bougies clôturées valides."""
    binance_pair = cfg.CRYPTO_PAIR_BINANCE.get(symbol)
    coinbase_pair = cfg.CRYPTO_PAIR_COINBASE.get(symbol)

    df = None
    if binance_pair is not None:
        df = _fetch_binance_klines(binance_pair, n_hours)

    if (df is None or len(df) < n_hours) and coinbase_pair is not None:
        fallback_df = _fetch_coinbase_candles(coinbase_pair, n_hours)
        if fallback_df is not None and (df is None or len(fallback_df) > len(df)):
            df = fallback_df

    if df is None or len(df) < n_hours:
        got = 0 if df is None else len(df)
        raise HistoryUnavailableError(
            f"crypto {symbol}: seulement {got}/{n_hours} bougies clôturées obtenues "
            f"(Binance + fallback Coinbase épuisés)"
        )

    return df
