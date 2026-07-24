"""Adaptateur actions US : Yahoo Finance public (v7 quote + v8 chart).

Aucune clé API. Yahoo gratuit (comptes non-abonnés) ne fournit quasi JAMAIS de `bid`/`ask`
exploitables (champs absents ou à 0) — correctif incident production 2026-07-24T16 (marché
ouvert, les 112 actions/ETF de l'univers en `quote_available=false` malgré un
`regularMarketPrice` Yahoo parfaitement valide pour la plupart). Quand un dernier prix DIFFÉRÉ
fiable existe (`regularMarketPrice > 0`, horodaté, dans la fenêtre de fraîcheur
`STALENESS_MAX_SECONDS_EQUITY`) mais que bid/ask est absent/invalide, `_build_quote_from_result`
reconstruit un bid/ask synthétique DÉFAVORABLE autour de ce prix plutôt que de rejeter la quote
(`EQUITY_SYNTHETIC_SPREAD_ENABLED = True` par défaut désormais, cf. `bot/config.py` pour le
diagnostic complet et la justification des paliers de spread par classe d'actif). La `Quote`
obtenue est marquée `synthetic_spread=True` ET `delayed=True` — jamais confondue avec un vrai
bid/ask coté temps réel, l'écart potentiel vs un prix idéal instantané reste mesurable et
honnêtement journalisé jusqu'à `decisions.jsonl`/`trades.jsonl`. Si Yahoo ne fournit AUCUN prix
exploitable du tout (pas de `regularMarketPrice`/`regularMarketTime`), la quote reste `None`
(no-trade strict, inchangé) — aucune donnée n'est jamais inventée à partir de rien.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import logging

import pandas as pd
import requests

from bot.feeds._config_fallback import cfg
from bot.feeds.types import HistoryUnavailableError, Quote

logger = logging.getLogger(__name__)

YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

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
            spread_bps = _synthetic_spread_bps_for(internal_symbol)
            half_spread = price * (spread_bps / 1e4) / 2.0
            logger.info(
                "yahoo quote sans bid/ask exploitable pour %s — spread synthétique %.1fbps "
                "autour du dernier prix différé (%.4f, ts=%s)",
                symbol, spread_bps, price, _iso(quote_ts),
            )
            return Quote(
                bid=price - half_spread,
                ask=price + half_spread,
                mid=price,
                ts=_iso(quote_ts),
                source="yahoo_synthetic_spread",
                delayed=True,  # un prix synthétique n'est par construction jamais "temps réel"
                synthetic_spread=True,
            )

    logger.warning("yahoo quote sans bid/ask exploitable pour %s (no-trade strict)", symbol)
    return None


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

    query_symbols = [_yahoo_ticker_for(sym) for sym in symbols]
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
        logger.warning("yahoo v7 quote échec pour %s: %s", symbols, exc)
        return result

    by_symbol = {row.get("symbol"): row for row in rows if isinstance(row, dict)}
    for sym in symbols:
        row = by_symbol.get(_yahoo_ticker_for(sym))
        if row is None:
            logger.warning("yahoo v7 quote: pas de résultat pour %s", sym)
            continue
        quote = _build_quote_from_result(row, internal_symbol=sym)
        if quote is not None:
            quote_ts = _dt.datetime.fromisoformat(quote.ts)
            if not _quote_is_fresh(quote_ts, max_age):
                logger.warning("yahoo quote périmée pour %s", sym)
                quote = None
            else:
                age_seconds = (_now_utc() - quote_ts).total_seconds()
                if age_seconds > realtime_threshold:
                    logger.info(
                        "yahoo quote différée pour %s (%.0fs, seuil temps réel %.0fs) — "
                        "utilisée quand même (seuil de fraîcheur actions/ETF %.0fs), "
                        "marquée delayed=true",
                        sym, age_seconds, realtime_threshold, max_age,
                    )
                    quote = dataclasses.replace(quote, delayed=True)
        result[sym] = quote

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
