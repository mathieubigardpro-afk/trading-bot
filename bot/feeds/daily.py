"""bot/feeds/daily.py — historique JOURNALIER (bougies clôturées) pour stratégies bas-fréquence.

Interface publique attendue par `bot.strategies.*` pour tout signal calculé sur des clôtures
quotidiennes (filtre SMA200 crypto, momentum cross-sectionnel S&P100, dual-momentum ETF —
cf. `docs/config-strategies.json` / `docs/SELECTION-FINALE.md`) :

    get_daily_history(symbol: str, n_days: int, asset_class: str) -> pd.DataFrame

Colonnes retournées : `[open, high, low, close, volume]`, index `ts` (`pandas.DatetimeIndex`
UTC, strictement croissant, sans doublon). Contient EXACTEMENT les `n_days` dernières bougies
JOURNALIÈRES CLÔTURÉES — jamais la bougie du jour en cours (cf. principe §0.4 ARCHITECTURE.md,
identique en esprit à `bot.feeds.get_history` pour l'horaire).

Sources :
  - crypto (`asset_class="crypto"`) : Binance klines publiques (`interval=1d`), fallback
    Coinbase Exchange candles publiques (`granularity=86400`). Symboles internes ("BTC", ...)
    résolus via `bot.feeds._config_fallback.cfg` (déjà fusionné avec l'univers 30 cryptos du
    wallet agressif, cf. `bot/config.py:CRYPTO_SYMBOLS_30`).
  - actions/ETF (`asset_class="equity"`/`"equities"`/`"etf"`) : yfinance en source primaire
    (téléchargement PAR LOTS avec pauses + retries — même motif que `tools/fetch_data.py`,
    seul pipeline déjà éprouvé en conditions réelles pour ce projet), repli séquentiel
    stooq.com PAR TICKER si yfinance échoue.

Robustesse (principe pessimiste, ARCHITECTURE.md §0.2/§0.3) :
  - Aucune donnée inventée ni interpolée : un symbole qui ne peut fournir `n_days` bougies
    quotidiennes valides sur AUCUNE source lève `HistoryUnavailableError` — jamais de
    DataFrame partiel renvoyé silencieusement comme s'il était complet. `is_daily_history_
    available()` offre une variante booléenne pour les appelants qui préfèrent tester la
    disponibilité avant de trader (le runner peut ainsi marquer le symbole "indisponible ce
    cycle" plutôt que de traiter une exception).
  - Validation systématique avant tout retour : timestamps strictement croissants, aucun
    doublon, `open/high/low/close > 0` (toute ligne invalide est écartée, jamais corrigée).

Cache mémoire PAR PROCESSUS (donc par cycle horaire — chaque exécution de `bot/runner.py` est
un processus Python neuf qui se termine avant le prochain cycle, cf. ARCHITECTURE.md §0.1
"statelessness du conteneur") : le module mémorise, pour chaque `(asset_class, symbol)`, le
meilleur historique déjà obtenu CE cycle — un second (ou troisième) appel du même symbole par
un autre wallet ne déclenche AUCUN nouvel appel réseau. `prefetch_daily_history()` est le point
d'entrée recommandé pour un GROUPE de symboles equity/ETF (ex. les 103 constituants du
S&P100) : il effectue le téléchargement par lots une seule fois et alimente ce cache, avant
que `get_daily_history()` ne soit appelé symbole par symbole par les stratégies.
`clear_daily_cache()` réinitialise le cache (tests, ou un futur usage multi-cycles au sein
d'un même processus).
"""

from __future__ import annotations

import datetime as _dt
import io
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from bot.feeds._config_fallback import cfg
from bot.feeds.types import HistoryUnavailableError

try:  # dépendance optionnelle défensive — cf. bandeau ci-dessous et tools/fetch_data.py.
    import yfinance as yf

    _YFINANCE_AVAILABLE = True
    _YFINANCE_IMPORT_ERROR: Optional[str] = None
except ImportError as _exc:  # pragma: no cover — dépendance non installée dans cet environnement
    yf = None  # type: ignore[assignment]
    _YFINANCE_AVAILABLE = False
    _YFINANCE_IMPORT_ERROR = str(_exc)

logger = logging.getLogger(__name__)

__all__ = [
    "get_daily_history",
    "prefetch_daily_history",
    "is_daily_history_available",
    "clear_daily_cache",
    "MIN_WARMUP_DAYS",
]

# ------------------------------------------------------------------------------------------
# NOTE D'INTÉGRATION IMPORTANTE (à l'attention de l'agent qui câble ce module dans le runner) :
# `.github/workflows/bot.yml` installe actuellement `pandas numpy requests` uniquement — PAS
# `yfinance`. Ce module reste fonctionnel sans yfinance (import optionnel défensif ci-dessus,
# repli intégral sur stooq pour actions/ETF), mais avec une disponibilité dégradée (stooq
# seul). Si ce module est câblé en production pour les poches actions/ETF, ajouter `yfinance`
# à l'étape "Install dependencies" de bot.yml (même dépendance que fetch-data.yml) — hors
# périmètre de ce module (`bot/feeds/` uniquement, cf. consigne de mission).
# ------------------------------------------------------------------------------------------

BINANCE_BASE_URL = "https://api.binance.com"
COINBASE_BASE_URL = "https://api.exchange.coinbase.com"
STOOQ_BASE_URL = "https://stooq.com/q/d/l/"

_HTTP_TIMEOUT_SECONDS = 20
_USER_AGENT = "trading-bot-paper/1.0 (+https://github.com/mathieubigardpro-afk/trading-bot)"
_STOOQ_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_session = requests.Session()
_session.headers.update({"User-Agent": _USER_AGENT})

# Session dédiée pour stooq (User-Agent "navigateur" distinct, même raison que
# tools/fetch_data.py : ne pas faire fuiter ce UA vers Binance/Coinbase, ni inversement).
_stooq_session = requests.Session()
_stooq_session.headers.update({"User-Agent": _STOOQ_USER_AGENT, "Accept": "text/csv,*/*"})

# --- Profondeur / lots / pauses -------------------------------------------------------------
MIN_WARMUP_DAYS = 400  # warmup SMA200 (200j) + marge, cf. docs/config-strategies.json

YFINANCE_BATCH_SIZE = 15
YFINANCE_BATCH_PAUSE_SECONDS = 2.0
_STOOQ_FALLBACK_MIN_PAUSE_SECONDS = 1.0

_MAX_ATTEMPTS_DEFAULT = 4
_BASE_BACKOFF_SECONDS = 1.5
_MAX_BACKOFF_SECONDS = 20.0

_BINANCE_DAILY_MAX_PAGES = 6           # 6*1000j largement suffisant (aucune stratégie ne
_BINANCE_DAILY_PER_CALL_LIMIT = 1000   # demande plus de quelques années de bougies quotidiennes)
_COINBASE_DAILY_MAX_PAGES = 6
_COINBASE_DAILY_PER_CALL_LIMIT = 300   # limite documentée de l'API Coinbase (toute granularité)

STOOQ_TICKER_OVERRIDES = {"BRK.B": "brk-b"}
YFINANCE_TICKER_OVERRIDES = {"BRK.B": "BRK-B"}

_NY_TZ = ZoneInfo("America/New_York")
_EMPTY_OHLCV = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _yfinance_symbol_for(ticker: str) -> str:
    return YFINANCE_TICKER_OVERRIDES.get(ticker, ticker)


def _stooq_symbol_for(ticker: str) -> str:
    return STOOQ_TICKER_OVERRIDES.get(ticker, ticker).lower()


def _normalize_asset_class(asset_class: str) -> str:
    key = (asset_class or "").strip().lower()
    if key == "crypto":
        return "crypto"
    if key in ("equity", "equities", "action", "actions", "stock", "stocks"):
        return "equity"
    if key in ("etf", "etfs"):
        return "etf"
    raise ValueError(
        f"asset_class inconnu: {asset_class!r} (attendu 'crypto' | 'equity'/'equities' | 'etf')"
    )


# ============================================================================================
# Cache mémoire par processus (= par cycle horaire, cf. docstring module)
# ============================================================================================


@dataclass
class _CacheEntry:
    df: pd.DataFrame
    attempted_n_days: int
    error: Optional[str]


_CACHE: Dict[Tuple[str, str], _CacheEntry] = {}


def clear_daily_cache() -> None:
    """Vide le cache mémoire. Utilisé par les tests pour l'isolation ; peut aussi servir à un
    appelant qui voudrait forcer un rafraîchissement au sein d'un même processus long-vivant
    (cas non standard pour ce bot, qui tourne un cycle par processus)."""
    _CACHE.clear()


def _cache_key(asset_class: str, symbol: str) -> Tuple[str, str]:
    return (asset_class, symbol)


def _cache_satisfies(asset_class: str, symbol: str, n_days: int) -> bool:
    entry = _CACHE.get(_cache_key(asset_class, symbol))
    if entry is None:
        return False
    return len(entry.df) >= n_days or entry.attempted_n_days >= n_days


def _store_cache(asset_class: str, symbol: str, df: pd.DataFrame, n_days: int, error: Optional[str]) -> _CacheEntry:
    key = _cache_key(asset_class, symbol)
    existing = _CACHE.get(key)
    best_df = df
    if existing is not None and len(existing.df) > len(df):
        best_df = existing.df
    attempted = n_days
    if existing is not None:
        attempted = max(attempted, existing.attempted_n_days)
    # Une entrée n'est plus en erreur si le meilleur historique connu couvre désormais
    # `attempted` jours ; sinon on garde le message le plus récent (le plus informatif).
    final_error = None if len(best_df) >= attempted else error
    entry = _CacheEntry(df=best_df, attempted_n_days=attempted, error=final_error)
    _CACHE[key] = entry
    return entry


# ============================================================================================
# Validation commune (principe cardinal : jamais de donnée inventée/interpolée)
# ============================================================================================


def _validate_ohlcv(df: Optional[pd.DataFrame], context: str) -> pd.DataFrame:
    """Écarte (sans jamais corriger/interpoler) toute ligne invalide, dédoublonne l'index et
    garantit un index strictement croissant. Ne lève jamais d'exception — un DataFrame vide en
    résultat est un signal normal traité en amont/aval (cf. `get_daily_history`)."""
    if df is None or df.empty:
        return _EMPTY_OHLCV.copy()

    out = df.copy()
    before = len(out)
    for col in ("open", "high", "low", "close"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)

    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out[(out["open"] > 0) & (out["high"] > 0) & (out["low"] > 0) & (out["close"] > 0)]
    out = out[~out.index.duplicated(keep="last")]
    out = out.sort_index()

    dropped = before - len(out)
    if dropped:
        logger.warning("%s: %d bougie(s) invalide(s)/dupliquée(s) écartée(s) lors de la validation", context, dropped)

    if out.empty:
        return _EMPTY_OHLCV.copy()
    return out[["open", "high", "low", "close", "volume"]]


# ============================================================================================
# Crypto — Binance klines 1d (primaire) + Coinbase candles 86400s (repli)
# ============================================================================================


def _request_json_with_retries(
    session: requests.Session, url: str, params: dict, context: str, max_attempts: int = 2
):
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(url, params=params, timeout=_HTTP_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < max_attempts:
                time.sleep(min(_MAX_BACKOFF_SECONDS, _BASE_BACKOFF_SECONDS * attempt))
    logger.warning("%s: échec après %d tentative(s) (%s)", context, max_attempts, last_exc)
    return None


def _fetch_binance_daily_klines(pair: str, n_days: int) -> Optional[pd.DataFrame]:
    """`GET /api/v3/klines?interval=1d`, paginé en arrière si besoin (`endTime` décroissant).
    Exclut systématiquement toute bougie dont `close_time` n'est pas encore dans le passé (la
    bougie du jour UTC en cours de formation)."""
    now_ms = int(_now_utc().timestamp() * 1000)
    target = n_days + 5  # petite marge (la bougie du jour en cours est toujours exclue en plus)
    collected: Dict[int, dict] = {}
    end_time_ms: Optional[int] = None

    for _page in range(_BINANCE_DAILY_MAX_PAGES):
        if len(collected) >= target:
            break
        params: dict = {"symbol": pair, "interval": "1d", "limit": _BINANCE_DAILY_PER_CALL_LIMIT}
        if end_time_ms is not None:
            params["endTime"] = end_time_ms

        rows = _request_json_with_retries(
            _session, f"{BINANCE_BASE_URL}/api/v3/klines", params, context=f"binance-daily {pair}"
        )
        if not isinstance(rows, list) or not rows:
            break

        earliest_open: Optional[int] = None
        for row in rows:
            try:
                open_time_ms, o, h, l, c, v, close_time_ms = row[0], row[1], row[2], row[3], row[4], row[5], row[6]
            except (IndexError, TypeError):
                continue
            if int(close_time_ms) >= now_ms:
                continue  # bougie du jour en cours de formation, jamais retenue
            open_time_ms = int(open_time_ms)
            collected[open_time_ms] = {
                "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(v),
            }
            if earliest_open is None or open_time_ms < earliest_open:
                earliest_open = open_time_ms

        if earliest_open is None or len(rows) < _BINANCE_DAILY_PER_CALL_LIMIT:
            break  # plus rien à paginer en arrière
        end_time_ms = earliest_open - 1

    if not collected:
        return _EMPTY_OHLCV.copy()

    df = pd.DataFrame.from_dict(collected, orient="index")
    df.index = pd.to_datetime(df.index, unit="ms", utc=True)
    df.index.name = "ts"
    return df.sort_index()[["open", "high", "low", "close", "volume"]]


def _fetch_coinbase_daily_candles(pair: str, n_days: int) -> Optional[pd.DataFrame]:
    """`GET /products/<pair>/candles?granularity=86400`, paginé en arrière (300
    bougies/appel max, limite documentée de l'API). Exclut toute bougie dont le début tombe
    dans la journée UTC courante (encore en formation)."""
    now = _now_utc()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_time = today_start
    collected: Dict[int, dict] = {}
    target = n_days + 5

    for _page in range(_COINBASE_DAILY_MAX_PAGES):
        if len(collected) >= target:
            break
        start_time = end_time - _dt.timedelta(days=_COINBASE_DAILY_PER_CALL_LIMIT)
        params = {"granularity": 86400, "start": start_time.isoformat(), "end": end_time.isoformat()}
        rows = _request_json_with_retries(
            _session, f"{COINBASE_BASE_URL}/products/{pair}/candles", params, context=f"coinbase-daily {pair}"
        )
        if not isinstance(rows, list) or not rows:
            break

        today_start_epoch = int(today_start.timestamp())
        for row in rows:
            try:
                t, low, high, open_, close, volume = row
            except (ValueError, TypeError):
                continue
            t = int(t)
            if t >= today_start_epoch:
                continue
            collected[t] = {
                "open": float(open_), "high": float(high), "low": float(low),
                "close": float(close), "volume": float(volume),
            }
        end_time = start_time

    if not collected:
        return _EMPTY_OHLCV.copy()

    df = pd.DataFrame.from_dict(collected, orient="index")
    df.index = pd.to_datetime(df.index, unit="s", utc=True)
    df.index.name = "ts"
    return df.sort_index()[["open", "high", "low", "close", "volume"]]


def _fetch_daily_crypto(symbol: str, n_days: int) -> pd.DataFrame:
    binance_pair = cfg.CRYPTO_PAIR_BINANCE.get(symbol)
    coinbase_pair = cfg.CRYPTO_PAIR_COINBASE.get(symbol)

    df: Optional[pd.DataFrame] = None
    if binance_pair is not None:
        df = _fetch_binance_daily_klines(binance_pair, n_days)

    if (df is None or len(df) < n_days) and coinbase_pair is not None:
        fallback_df = _fetch_coinbase_daily_candles(coinbase_pair, n_days)
        if fallback_df is not None and (df is None or len(fallback_df) > len(df)):
            df = fallback_df

    if df is None:
        logger.warning("crypto %s: aucune source (Binance + Coinbase) n'a fourni de bougies quotidiennes", symbol)
        df = _EMPTY_OHLCV.copy()

    return _validate_ohlcv(df, context=f"crypto {symbol}")


# ============================================================================================
# Actions / ETF — yfinance (lots) primaire, stooq (séquentiel/ticker) en repli
# ============================================================================================


def _yf_period_for(n_days: int) -> str:
    """Choisit le plus petit `period` yfinance couvrant `n_days` bougies de bourse avec une
    marge confortable pour les week-ends/jours fériés (~252 séances / 365j calendaires)."""
    calendar_days_needed = n_days * 1.55 + 40
    years_needed = calendar_days_needed / 365.25
    if years_needed <= 2:
        return "2y"
    if years_needed <= 5:
        return "5y"
    if years_needed <= 10:
        return "10y"
    return "max"


def _normalize_yf_daily(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalise un DataFrame `yfinance` (index Date, colonnes Open/High/Low/Close/Volume,
    `auto_adjust=True`) vers un schéma intermédiaire commun `ts_ny,open,high,low,close,volume`
    (`ts_ny` = date de séance America/New_York, tz-aware) — même logique que
    `tools/fetch_data.py:_normalize_yf_frame` (localisation explicite America/New_York plutôt
    qu'une supposition silencieuse d'UTC, qui décalerait la date calendaire de la bougie)."""
    df = raw.copy()
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    if not {"open", "high", "low", "close"}.issubset(set(df.columns)):
        return pd.DataFrame(columns=["ts_ny", "open", "high", "low", "close", "volume"])
    if "volume" not in df.columns:
        df["volume"] = 0.0

    idx = df.index
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize(_NY_TZ, ambiguous="NaT", nonexistent="shift_forward")
    else:
        idx = idx.tz_convert(_NY_TZ)

    df = df.reset_index(drop=True)
    df["ts_ny"] = idx
    return df[["ts_ny", "open", "high", "low", "close", "volume"]]


def _build_ts_ny_from_stooq_dates(date_series: pd.Series) -> pd.Series:
    dates = pd.to_datetime(date_series, errors="coerce")
    return dates.dt.tz_localize(_NY_TZ, ambiguous="NaT", nonexistent="shift_forward")


def _finalize_intermediate_daily(df: pd.DataFrame, context: str) -> pd.DataFrame:
    """Étape commune yfinance/stooq : nettoyage numérique, dédoublonnage, exclusion de la
    séance du jour calendaire America/New_York courant (jamais close, donc jamais retenue —
    même si elle apparaît dans la réponse de la source), puis conversion de l'index en UTC."""
    if df is None or df.empty:
        return _EMPTY_OHLCV.copy()

    out = df.dropna(subset=["ts_ny"]).copy()
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close"])
    out["volume"] = out["volume"].fillna(0.0)
    out = out.drop_duplicates(subset="ts_ny", keep="last").sort_values("ts_ny")

    today_ny = _now_utc().astimezone(_NY_TZ).date()
    out = out[out["ts_ny"].dt.date < today_ny]
    if out.empty:
        return _EMPTY_OHLCV.copy()

    out = out.copy()
    out["ts"] = out["ts_ny"].dt.tz_convert("UTC")
    out = out.set_index("ts")[["open", "high", "low", "close", "volume"]]
    return _validate_ohlcv(out, context=context)


def _extract_yf_ticker_frame(batch_df: pd.DataFrame, yf_symbol: str, batch_len: int) -> Optional[pd.DataFrame]:
    """Même logique que `tools/fetch_data.py:_extract_yf_ticker_frame` : extrait le
    sous-DataFrame d'un ticker depuis un résultat potentiellement multi-tickers
    (`group_by="ticker"` -> colonnes MultiIndex), ou le résultat tel quel si un seul ticker a
    été demandé au lot (colonnes plates selon la version de `yfinance`)."""
    if batch_df is None or batch_df.empty:
        return None
    if isinstance(batch_df.columns, pd.MultiIndex):
        top_level = set(batch_df.columns.get_level_values(0))
        if yf_symbol not in top_level:
            return None
        sub = batch_df[yf_symbol]
        if not isinstance(sub, pd.DataFrame) or sub.dropna(how="all").empty:
            return None
        return sub
    if batch_len == 1:
        return batch_df
    return None


def _fetch_yfinance_batch(tickers: List[str], period: str) -> Dict[str, Optional[pd.DataFrame]]:
    """Téléchargement PAR LOTS (`YFINANCE_BATCH_SIZE` tickers/lot, pause entre lots, retries
    avec backoff par lot) — motif directement repris de `tools/fetch_data.py:
    fetch_yfinance_batch`, seul pipeline déjà éprouvé en conditions réelles pour ce projet."""
    results: Dict[str, Optional[pd.DataFrame]] = {t: None for t in tickers}
    if not _YFINANCE_AVAILABLE:
        logger.warning(
            "yfinance indisponible (%s) — repli direct sur stooq pour tous les tickers demandés",
            _YFINANCE_IMPORT_ERROR,
        )
        return results

    batches = [tickers[i : i + YFINANCE_BATCH_SIZE] for i in range(0, len(tickers), YFINANCE_BATCH_SIZE)]
    for batch_idx, batch in enumerate(batches, start=1):
        yf_symbols = [_yfinance_symbol_for(t) for t in batch]
        batch_df: Optional[pd.DataFrame] = None
        for attempt in range(1, _MAX_ATTEMPTS_DEFAULT + 1):
            try:
                batch_df = yf.download(
                    tickers=" ".join(yf_symbols), period=period, interval="1d",
                    auto_adjust=True, group_by="ticker", threads=True, progress=False, timeout=30,
                )
                break
            except Exception as exc:  # noqa: BLE001 — yfinance peut lever des exceptions variées
                if attempt >= _MAX_ATTEMPTS_DEFAULT:
                    logger.error(
                        "yfinance-daily lot %d/%d (%s): échec définitif après %d tentative(s) (%s)",
                        batch_idx, len(batches), batch, attempt, exc,
                    )
                    batch_df = None
                    break
                delay = min(_MAX_BACKOFF_SECONDS, _BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                logger.warning(
                    "yfinance-daily lot %d/%d (%s): erreur (tentative %d/%d, %s) — nouvel essai dans %.1fs",
                    batch_idx, len(batches), batch, attempt, _MAX_ATTEMPTS_DEFAULT, exc, delay,
                )
                time.sleep(delay)

        if batch_df is not None and not batch_df.empty:
            for ticker, yf_symbol in zip(batch, yf_symbols):
                sub = _extract_yf_ticker_frame(batch_df, yf_symbol, len(batch))
                if sub is not None:
                    normalized = _normalize_yf_daily(sub)
                    if not normalized.empty:
                        results[ticker] = normalized
        else:
            logger.warning("yfinance-daily lot %d/%d (%s): aucune donnée exploitable", batch_idx, len(batches), batch)

        if batch_idx < len(batches):
            time.sleep(YFINANCE_BATCH_PAUSE_SECONDS)

    return results


def _fetch_yfinance_single(ticker: str, period: str) -> Optional[pd.DataFrame]:
    if not _YFINANCE_AVAILABLE:
        return None
    yf_symbol = _yfinance_symbol_for(ticker)
    for attempt in range(1, _MAX_ATTEMPTS_DEFAULT + 1):
        try:
            hist = yf.Ticker(yf_symbol).history(period=period, interval="1d", auto_adjust=True)
        except Exception as exc:  # noqa: BLE001
            if attempt >= _MAX_ATTEMPTS_DEFAULT:
                logger.error("yfinance-daily retry individuel %s: échec définitif (%s)", ticker, exc)
                return None
            delay = min(_MAX_BACKOFF_SECONDS, _BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            logger.warning("yfinance-daily retry individuel %s: erreur (tentative %d/%d, %s)", ticker, attempt, _MAX_ATTEMPTS_DEFAULT, exc)
            time.sleep(delay)
            continue

        if hist is None or hist.empty:
            if attempt >= _MAX_ATTEMPTS_DEFAULT:
                return None
            time.sleep(_STOOQ_FALLBACK_MIN_PAUSE_SECONDS)
            continue

        normalized = _normalize_yf_daily(hist)
        return normalized if not normalized.empty else None

    return None


def _fetch_stooq_daily(ticker: str) -> Optional[pd.DataFrame]:
    stooq_symbol = _stooq_symbol_for(ticker)
    last_exc: Optional[Exception] = None
    text: Optional[str] = None

    for attempt in range(1, _MAX_ATTEMPTS_DEFAULT + 1):
        try:
            resp = _stooq_session.get(
                STOOQ_BASE_URL, params={"s": f"{stooq_symbol}.us", "i": "d"}, timeout=_HTTP_TIMEOUT_SECONDS
            )
            resp.raise_for_status()
            text = resp.text
            break
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS_DEFAULT:
                time.sleep(min(_MAX_BACKOFF_SECONDS, _BASE_BACKOFF_SECONDS * attempt))

    if text is None:
        logger.warning("stooq-daily: échec réseau pour %s après %d tentative(s) (%s)", ticker, _MAX_ATTEMPTS_DEFAULT, last_exc)
        return None

    if not text or text.strip().lower().startswith("no data") or "<html" in text[:200].lower():
        logger.warning("stooq-daily: réponse vide/invalide pour %s (ticker inconnu ou blocage)", ticker)
        return None

    try:
        raw = pd.read_csv(io.StringIO(text))
    except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        logger.warning("stooq-daily: CSV invalide pour %s: %s", ticker, exc)
        return None

    expected_cols = {"Date", "Open", "High", "Low", "Close", "Volume"}
    if not expected_cols.issubset(set(raw.columns)):
        logger.warning("stooq-daily: colonnes inattendues pour %s: %s", ticker, list(raw.columns))
        return None

    raw = raw.rename(columns={
        "Date": "date_raw", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume",
    })
    raw["ts_ny"] = _build_ts_ny_from_stooq_dates(raw["date_raw"])
    return raw[["ts_ny", "open", "high", "low", "close", "volume"]]


def _resolve_equity_etf_single(symbol: str, n_days: int) -> pd.DataFrame:
    """Résolution PAR TICKER (repli/retry, ou usage direct de `get_daily_history` sans
    `prefetch_daily_history` préalable) : yfinance individuel, puis stooq si insuffisant."""
    period = _yf_period_for(n_days)
    df: Optional[pd.DataFrame] = None

    raw = _fetch_yfinance_single(symbol, period)  # déjà normalisé (ts_ny,open,high,low,close,volume)
    if raw is not None and not raw.empty:
        df = _finalize_intermediate_daily(raw, context=f"yfinance {symbol}")

    if df is None or len(df) < n_days:
        time.sleep(_STOOQ_FALLBACK_MIN_PAUSE_SECONDS)
        stooq_intermediate = _fetch_stooq_daily(symbol)
        if stooq_intermediate is not None:
            stooq_df = _finalize_intermediate_daily(stooq_intermediate, context=f"stooq {symbol}")
            if df is None or len(stooq_df) > len(df):
                df = stooq_df

    if df is None:
        df = _EMPTY_OHLCV.copy()
    return df


def prefetch_daily_history(symbols: List[str], asset_class: str, n_days: int = MIN_WARMUP_DAYS) -> Dict[str, str]:
    """Précharge le cache pour un GROUPE de symboles equity/ETF en téléchargement PAR LOTS
    (recommandé pour tout univers de plus de quelques tickers — ex. les 103 constituants du
    S&P100 de `xs_momentum_sp100`, ou les 8 ETF de `dual_momentum_multiclasse_etf`). N'a AUCUN
    effet pour `asset_class="crypto"` (chaque paire crypto est déjà une requête indépendante
    peu coûteuse, aucun lot à constituer — `ValueError` explicite pour éviter un faux sentiment
    d'optimisation). Ne lève jamais d'exception réseau : chaque échec individuel est journalisé
    et reflété dans le dict de statuts retourné (`"cache"` / `"ok"` / `"insuffisant"` /
    `"indisponible"`) — `get_daily_history()` reste la seule source de vérité pour savoir si un
    symbole est réellement tradable ce cycle (elle relit ce même cache en premier lieu)."""
    normalized_class = _normalize_asset_class(asset_class)
    if normalized_class == "crypto":
        raise ValueError(
            "prefetch_daily_history: sans objet pour asset_class='crypto' — appelez "
            "get_daily_history() directement pour chaque symbole crypto."
        )

    to_fetch = [s for s in symbols if not _cache_satisfies(normalized_class, s, n_days)]
    statuses: Dict[str, str] = {s: "cache" for s in symbols if s not in to_fetch}
    if not to_fetch:
        return statuses

    period = _yf_period_for(n_days)
    batch_results = _fetch_yfinance_batch(to_fetch, period)

    still_missing: List[str] = []
    for symbol in to_fetch:
        raw = batch_results.get(symbol)
        if raw is None or raw.empty:
            still_missing.append(symbol)
            continue
        finalized = _finalize_intermediate_daily(raw, context=f"yfinance {symbol}")
        error = None if len(finalized) >= n_days else (
            f"equity/etf {symbol}: {len(finalized)}/{n_days} bougies obtenues via yfinance (lot)"
        )
        _store_cache(normalized_class, symbol, finalized, n_days, error)
        statuses[symbol] = "ok" if error is None else "insuffisant"

    for symbol in still_missing:
        df = _resolve_equity_etf_single(symbol, n_days)
        error = None if len(df) >= n_days else (
            f"equity/etf {symbol}: seulement {len(df)}/{n_days} bougies obtenues (yfinance + stooq épuisés)"
        )
        _store_cache(normalized_class, symbol, df, n_days, error)
        statuses[symbol] = "ok" if error is None else "indisponible"

    return statuses


def _fetch_daily_equity_etf(symbol: str, n_days: int) -> pd.DataFrame:
    return _resolve_equity_etf_single(symbol, n_days)


# ============================================================================================
# Interface publique
# ============================================================================================


def get_daily_history(symbol: str, n_days: int, asset_class: str) -> pd.DataFrame:
    """Retourne EXACTEMENT les `n_days` dernières bougies JOURNALIÈRES CLÔTURÉES de `symbol`
    (colonnes `open/high/low/close/volume`, index `ts` UTC croissant), ou lève
    `HistoryUnavailableError` si moins de `n_days` bougies valides n'ont pu être obtenues sur
    AUCUNE source (jamais de DataFrame partiel silencieusement traité comme complet).

    `asset_class` ∈ `{"crypto", "equity"/"equities", "etf"/"etfs"}` (insensible à la casse).
    `symbol` : ticker interne crypto (ex. "BTC", résolu via `bot.feeds._config_fallback.cfg`)
    ou ticker actions/ETF standard (ex. "AAPL", "SPY").

    Cache mémoire par processus (cf. docstring module) : un appel avec un `(asset_class,
    symbol)` déjà résolu CE cycle pour un `n_days` égal ou inférieur ne déclenche AUCUN appel
    réseau supplémentaire, que le résultat précédent ait réussi ou échoué.
    """
    if not isinstance(n_days, int) or n_days <= 0:
        raise ValueError(f"n_days doit être un entier strictement positif (reçu {n_days!r})")
    normalized_class = _normalize_asset_class(asset_class)
    if not symbol:
        raise ValueError("symbol ne peut pas être vide")

    entry = _CACHE.get(_cache_key(normalized_class, symbol))
    if entry is not None:
        if len(entry.df) >= n_days:
            return entry.df.tail(n_days).copy()
        if entry.attempted_n_days >= n_days:
            raise HistoryUnavailableError(
                entry.error or f"{normalized_class} {symbol}: historique quotidien indisponible (déjà tenté ce cycle)"
            )

    if normalized_class == "crypto":
        df = _fetch_daily_crypto(symbol, n_days)
    else:
        df = _fetch_daily_equity_etf(symbol, n_days)

    error = None
    if len(df) < n_days:
        error = f"{normalized_class} {symbol}: seulement {len(df)}/{n_days} bougies quotidiennes clôturées valides obtenues"

    _store_cache(normalized_class, symbol, df, n_days, error)

    if error:
        raise HistoryUnavailableError(error)
    return df.tail(n_days).copy()


def is_daily_history_available(symbol: str, n_days: int, asset_class: str) -> bool:
    """Variante booléenne de `get_daily_history` — pratique pour le runner/les stratégies qui
    préfèrent tester la disponibilité avant de calculer un signal plutôt que d'intercepter
    `HistoryUnavailableError` à chaque appel. Bénéficie du même cache (aucun appel réseau
    redondant avec un `get_daily_history` déjà effectué ce cycle pour le même symbole)."""
    try:
        get_daily_history(symbol, n_days, asset_class)
        return True
    except HistoryUnavailableError:
        return False
