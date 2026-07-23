#!/usr/bin/env python3
"""tools/fetch_data.py — téléchargeur de données historiques pour le bot de paper trading.

Construit un jeu de données local (crypto horaire, actions S&P 100 quotidien, ETF
quotidien) destiné aux futurs backtests, et le publie sur la branche orpheline
`market-data` du dépôt (séparée de `main` pour ne pas alourdir l'historique de code).

Sources publiques utilisées (aucune clé API requise) :
  - Crypto horaire : archives bulk mensuelles Binance
    (https://data.binance.vision/data/spot/monthly/klines/{PAIR}/1h/{PAIR}-1h-{YYYY-MM}.zip)
    de 2022-01 au dernier mois calendaire complet, puis complément du mois courant via
    l'API publique `GET https://api.binance.com/api/v3/klines`.
  - Actions (S&P 100) et ETF, quotidien : `https://stooq.com/q/d/l/?s={ticker}.us&i=d`
    (prix ajustés, historique complet disponible côté stooq).

Principes de conception (ce script tournera pour de vrai sur GitHub Actions, jamais
testé en réseau réel dans l'environnement de développement où il a été écrit — voir
note en tête de `.github/workflows/fetch-data.yml`) :
  - Aucune exception réseau individuelle ne doit faire planter l'ensemble du run : chaque
    téléchargement élémentaire (un mois d'un symbole crypto, un ticker action/ETF) est
    isolé, retente avec backoff exponentiel + jitter, et à l'épuisement des tentatives est
    journalisé en erreur explicite puis traité comme "manquant" (jamais de exception qui
    remonte jusqu'à `main()`).
  - 404 (ressource inexistante côté source, ex. paire non listée à cette date) est
    distingué explicitement d'une erreur réseau/transitoire (5xx, timeout, connexion) :
    seul le second cas est retenté.
  - Biais du survivant tracé explicitement : toute paire crypto dont l'historique n'est pas
    complet sur la fenêtre [2023-07 .. dernier mois complet] est EXCLUE de `data/crypto/`
    et listée avec sa raison précise dans `MANIFEST.json`.
  - Le script écrit son résultat dans un répertoire de staging HORS du dépôt (aucune
    interférence avec le working tree de la branche courante pendant les téléchargements,
    qui peuvent prendre du temps), puis ne bascule le dépôt sur la branche orpheline
    `market-data` que pour l'étape finale de publication (commit + push --force), avant de
    restaurer l'état (commit) du dépôt tel qu'il était au démarrage.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import io
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
from requests.adapters import HTTPAdapter

try:
    import yfinance as yf
    _YFINANCE_AVAILABLE = True
    _YFINANCE_IMPORT_ERROR: Optional[str] = None
except ImportError as _exc:  # pragma: no cover — dépendance optionnelle défensive
    yf = None  # type: ignore[assignment]
    _YFINANCE_AVAILABLE = False
    _YFINANCE_IMPORT_ERROR = str(_exc)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("tools.fetch_data")

# --------------------------------------------------------------------------------------
# Univers
# --------------------------------------------------------------------------------------

# ~30 paires crypto USDT curatées (Binance spot). Si une paire n'existe pas du tout côté
# Binance (symbole erroné, jamais listé), tous ses mois seront "NOT_FOUND" -> elle sera
# exclue automatiquement par la règle de complétude 2023-07 ci-dessous, sans traitement
# particulier nécessaire ici.
CRYPTO_SYMBOLS = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT", "LINK",
    "LTC", "TRX", "BCH", "ETC", "UNI", "ATOM", "NEAR", "FIL", "APT", "ARB",
    "OP", "INJ", "ICP", "HBAR", "AAVE", "ALGO", "SAND", "MANA", "XLM", "VET",
]

# Panel S&P 100 (~100 tickers, valeur en dur — cf. mission). Format stooq : `{ticker}.us`
# en minuscules, sauf overrides explicites (ex. classes d'actions avec point -> tiret).
SP100_TICKERS = [
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "AIG", "AMD", "AMGN", "AMT", "AMZN",
    "AVGO", "AXP", "BA", "BAC", "BK", "BKNG", "BLK", "BMY", "BRK.B", "C",
    "CAT", "CHTR", "CL", "CMCSA", "COF", "COP", "COST", "CRM", "CSCO", "CVS",
    "CVX", "DE", "DHR", "DIS", "DOW", "DUK", "EMR", "F", "FDX", "GD",
    "GE", "GILD", "GM", "GOOG", "GOOGL", "GS", "HD", "HON", "IBM", "INTC",
    "INTU", "ISRG", "JNJ", "JPM", "KHC", "KMI", "KO", "LIN", "LLY", "LMT",
    "LOW", "MA", "MCD", "MDLZ", "MDT", "MET", "META", "MMM", "MO", "MRK",
    "MS", "MSFT", "NEE", "NFLX", "NKE", "NVDA", "ORCL", "PEP", "PFE", "PG",
    "PM", "PYPL", "QCOM", "RTX", "SBUX", "SCHW", "SO", "SPG", "T", "TGT",
    "TJX", "TMO", "TMUS", "TSLA", "TXN", "UNH", "UNP", "UPS", "USB", "V",
    "VZ", "WFC", "WMT", "XOM",
]

ETF_TICKERS = [
    "SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "TLT", "IEF", "LQD", "HYG",
    "GLD", "SLV", "DBC", "XLE", "XLK", "XLF", "XLV", "XLU",
]

# Overrides de symbole côté stooq (ticker interne -> slug stooq), pour les rares tickers
# dont la notation diffère (classes d'actions avec point).
STOOQ_TICKER_OVERRIDES = {
    "BRK.B": "brk-b",
}

# Overrides de symbole côté Yahoo/yfinance (ticker interne -> symbole Yahoo), même besoin
# que ci-dessus mais notation Yahoo (tiret majuscule, pas de point).
YFINANCE_TICKER_OVERRIDES = {
    "BRK.B": "BRK-B",
}

CRYPTO_ARCHIVE_START = (2022, 1)  # première année-mois de l'archive bulk téléchargée
CRYPTO_FULL_COVERAGE_REQUIRED_FROM = (2023, 7)  # fenêtre de complétude obligatoire

BINANCE_VISION_BASE = "https://data.binance.vision"
BINANCE_API_BASE = "https://api.binance.com"
STOOQ_BASE = "https://stooq.com/q/d/l/"

_HTTP_TIMEOUT_SECONDS = 30
_USER_AGENT = "trading-bot-paper-data-fetcher/1.0 (+https://github.com/mathieubigardpro-afk/trading-bot)"

# Actions/ETF (§ addendum post-run réel) : Yahoo Finance (via `yfinance`) est la source
# PRIMAIRE. stooq.com a été testé en conditions réelles sur les runners GitHub Actions et y
# renvoie des réponses vides/invalides pour 100% des tickers (122/122 échecs observés) —
# vraisemblablement un blocage des IP datacenter GitHub ou un rate limiting agressif côté
# stooq. stooq reste néanmoins en repli PAR TICKER (au cas où il ne s'agirait que d'un
# rate limiting sensible à la cadence des requêtes), avec un User-Agent de navigateur
# explicite et une pause d'au moins 1s entre deux requêtes séquentielles (jamais en
# parallèle pour ce repli, contrairement à la phase yfinance qui peut paralléliser par lots).
YFINANCE_BATCH_SIZE = 15
YFINANCE_BATCH_PAUSE_SECONDS = 2.0
YFINANCE_SINGLE_RETRY_PAUSE_SECONDS = 1.5
STOOQ_FALLBACK_MIN_PAUSE_SECONDS = 1.0
STOOQ_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_ATTEMPTS_DEFAULT = 5
BASE_BACKOFF_SECONDS = 1.5
MAX_BACKOFF_SECONDS = 60.0

GIT_AUTHOR_NAME = "Trading Bot Data Fetcher"
GIT_AUTHOR_EMAIL = "bot@trading.local"


# --------------------------------------------------------------------------------------
# HTTP défensif : session, retries/backoff, distinction 404 vs erreur transitoire
# --------------------------------------------------------------------------------------


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT, "Accept": "*/*"})
    adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


@dataclass
class FetchResult:
    status: str  # "OK" | "NOT_FOUND" | "ERROR"
    response: Optional[requests.Response] = None
    error: Optional[str] = None


def fetch_with_retries(
    session: requests.Session,
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    max_attempts: int = MAX_ATTEMPTS_DEFAULT,
    timeout: float = _HTTP_TIMEOUT_SECONDS,
    context: str = "",
) -> FetchResult:
    """GET défensif avec backoff exponentiel + jitter.

    - 404 -> `FetchResult("NOT_FOUND")` immédiatement, JAMAIS retenté (une ressource
      absente à cette date le restera : ce n'est pas une panne transitoire).
    - 200 -> `FetchResult("OK", response=...)`.
    - 429/418 (rate limit) -> respecte `Retry-After` si présent, sinon backoff exponentiel,
      retenté jusqu'à `max_attempts`.
    - 5xx, timeout, erreur de connexion (y compris proxy bloquant, cas de cet
      environnement de développement) -> backoff exponentiel + jitter, retenté jusqu'à
      `max_attempts`, puis `FetchResult("ERROR")` avec le détail journalisé.
    - Tout autre code HTTP inattendu -> journalisé et retourné tel quel (`OK` avec le
      `response` correspondant ; c'est à l'appelant de vérifier `response.status_code` s'il
      a besoin d'une sémantique plus fine que 200/404).
    """
    last_error = "inconnue"
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt >= max_attempts:
                logger.error(
                    "%s: échec réseau définitif après %d tentative(s) sur %s (%s)",
                    context, attempt, url, last_error,
                )
                return FetchResult(status="ERROR", error=last_error)
            delay = min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            delay += random.uniform(0, delay * 0.25)
            logger.warning(
                "%s: erreur réseau (tentative %d/%d) sur %s (%s) — nouvelle tentative dans %.1fs",
                context, attempt, max_attempts, url, last_error, delay,
            )
            time.sleep(delay)
            continue

        if resp.status_code == 404:
            return FetchResult(status="NOT_FOUND", response=resp)

        if resp.status_code in (429, 418):
            retry_after_hdr = resp.headers.get("Retry-After")
            try:
                delay = float(retry_after_hdr) if retry_after_hdr else None
            except ValueError:
                delay = None
            if delay is None:
                delay = min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            logger.warning(
                "%s: rate-limité (HTTP %d) sur %s — pause %.1fs (tentative %d/%d)",
                context, resp.status_code, url, delay, attempt, max_attempts,
            )
            time.sleep(delay)
            last_error = f"HTTP {resp.status_code} (rate limit)"
            continue

        if resp.status_code >= 500:
            last_error = f"HTTP {resp.status_code}"
            if attempt >= max_attempts:
                logger.error(
                    "%s: échec serveur définitif (HTTP %d) après %d tentative(s) sur %s",
                    context, resp.status_code, attempt, url,
                )
                return FetchResult(status="ERROR", error=last_error)
            delay = min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            delay += random.uniform(0, delay * 0.25)
            logger.warning(
                "%s: erreur serveur HTTP %d (tentative %d/%d) sur %s — nouvelle tentative dans %.1fs",
                context, resp.status_code, attempt, max_attempts, url, delay,
            )
            time.sleep(delay)
            continue

        if resp.status_code != 200:
            logger.warning(
                "%s: code HTTP inattendu %d sur %s (traité comme échec non retentable)",
                context, resp.status_code, url,
            )
            return FetchResult(status="ERROR", response=resp, error=f"HTTP {resp.status_code}")

        return FetchResult(status="OK", response=resp)

    return FetchResult(status="ERROR", error=last_error)


# --------------------------------------------------------------------------------------
# Utilitaires temps / mois
# --------------------------------------------------------------------------------------


def _yyyymm(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _add_month(year: int, month: int, n: int = 1) -> Tuple[int, int]:
    idx = (year * 12 + (month - 1)) + n
    return idx // 12, idx % 12 + 1


def last_complete_month(now: Optional[datetime] = None) -> Tuple[int, int]:
    """Dernier mois calendaire ENTIÈREMENT clos par rapport à `now` (UTC)."""
    now = now or datetime.now(timezone.utc)
    return _add_month(now.year, now.month, -1)


def month_range(start: Tuple[int, int], end: Tuple[int, int]) -> List[Tuple[int, int]]:
    """Liste inclusive de (année, mois) de `start` à `end` (chronologique)."""
    months: List[Tuple[int, int]] = []
    y, m = start
    while (y, m) <= end:
        months.append((y, m))
        y, m = _add_month(y, m, 1)
    return months


def _month_start_utc(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------------------
# Normalisation d'epoch (défensif : Binance a fait évoluer ms/us/ns selon endpoints)
# --------------------------------------------------------------------------------------


def _epoch_to_utc_ts(value) -> pd.Timestamp:
    """Convertit un entier epoch de longueur ambiguë (ms le plus souvent, mais on se
    protège contre us/ns) en `pd.Timestamp` UTC, en se basant sur le nombre de chiffres."""
    v = int(value)
    digits = len(str(abs(v)))
    if digits >= 18:
        unit = "ns"
    elif digits >= 15:
        unit = "us"
    else:
        unit = "ms"
    return pd.to_datetime(v, unit=unit, utc=True)


# --------------------------------------------------------------------------------------
# Crypto : archives bulk mensuelles Binance
# --------------------------------------------------------------------------------------

_BINANCE_KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]


def _parse_binance_csv_bytes(raw: bytes) -> pd.DataFrame:
    """Parse le CSV contenu dans une archive mensuelle Binance klines.

    Défensif vis-à-vis d'un changement de format connu de Binance : certains exports
    récents ajoutent une ligne d'en-tête (`open_time,open,high,...`) alors que le format
    historique n'en a aucune (première colonne directement numérique). On détecte le cas
    en inspectant le premier caractère de la première ligne.
    """
    text = raw.decode("utf-8", errors="replace")
    first_line = text.splitlines()[0] if text else ""
    has_header = bool(first_line) and not first_line.split(",")[0].strip().lstrip("-").isdigit()

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if has_header:
        rows = rows[1:]

    records = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            open_time = _epoch_to_utc_ts(row[0])
            o, h, l, c, v = (float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]))
        except (ValueError, TypeError):
            continue
        records.append({"timestamp": open_time, "open": o, "high": h, "low": l, "close": c, "volume": v})

    if not records:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    return pd.DataFrame.from_records(records)


def download_binance_month(
    session: requests.Session, pair: str, year: int, month: int, max_attempts: int
) -> Tuple[str, Optional[pd.DataFrame]]:
    """Télécharge et parse le zip mensuel klines 1h pour `pair`/`year-month`.

    Retourne (status, df) avec status in {"OK", "NOT_FOUND", "ERROR"}. `df` est non-None
    uniquement si status == "OK".
    """
    ym = _yyyymm(year, month)
    url = f"{BINANCE_VISION_BASE}/data/spot/monthly/klines/{pair}/1h/{pair}-1h-{ym}.zip"
    result = fetch_with_retries(
        session, url, max_attempts=max_attempts, context=f"binance-bulk {pair} {ym}"
    )
    if result.status != "OK":
        return result.status, None

    try:
        with zipfile.ZipFile(io.BytesIO(result.response.content)) as zf:
            names = zf.namelist()
            if not names:
                logger.warning("archive vide pour %s %s", pair, ym)
                return "ERROR", None
            csv_bytes = zf.read(names[0])
    except zipfile.BadZipFile as exc:
        logger.warning("archive zip corrompue pour %s %s: %s", pair, ym, exc)
        return "ERROR", None

    df = _parse_binance_csv_bytes(csv_bytes)
    if df.empty:
        logger.warning("archive %s %s parsée mais vide (0 bougie exploitable)", pair, ym)
        return "ERROR", None
    return "OK", df


def fetch_binance_current_month_completion(
    session: requests.Session, pair: str, month_start: datetime, max_attempts: int
) -> pd.DataFrame:
    """Complète le mois courant (non couvert par les archives bulk, qui ne publient que des
    mois entièrement clos) via l'API klines publique, avec pagination par tranches de 1000
    bougies. N'exclut que la bougie encore en formation à l'heure de l'appel (close_time
    futur)."""
    now = datetime.now(timezone.utc)
    all_records: List[dict] = []
    cursor_ms = int(month_start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    max_pages = 10  # 10*1000h largement suffisant pour un seul mois en cours (~744h max)

    for _ in range(max_pages):
        if cursor_ms >= end_ms:
            break
        result = fetch_with_retries(
            session,
            f"{BINANCE_API_BASE}/api/v3/klines",
            params={"symbol": pair, "interval": "1h", "startTime": cursor_ms, "limit": 1000},
            max_attempts=max_attempts,
            context=f"binance-api-completion {pair}",
        )
        if result.status != "OK":
            # Pas d'archive de repli possible ici (c'est déjà le repli) : on journalise et on
            # s'arrête avec ce qu'on a pu obtenir jusque-là, plutôt que de bloquer tout le
            # symbole pour un mois courant partiellement indisponible.
            logger.warning(
                "binance-api-completion %s: arrêt de la pagination (%s), %d bougie(s) déjà obtenue(s)",
                pair, result.status, len(all_records),
            )
            break

        try:
            rows = result.response.json()
        except ValueError as exc:
            logger.warning("binance-api-completion %s: JSON invalide (%s)", pair, exc)
            break
        if not isinstance(rows, list) or not rows:
            break

        advanced = False
        for row in rows:
            try:
                open_time_raw, o, h, l, c, v, close_time_raw = row[0], row[1], row[2], row[3], row[4], row[5], row[6]
            except (IndexError, TypeError):
                continue
            close_ts = _epoch_to_utc_ts(close_time_raw)
            if close_ts >= pd.Timestamp(now):
                continue  # bougie encore en formation, exclue systématiquement
            all_records.append({
                "timestamp": _epoch_to_utc_ts(open_time_raw),
                "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(v),
            })
            cursor_ms = max(cursor_ms, int(open_time_raw) + 1)
            advanced = True

        if len(rows) < 1000 or not advanced:
            break

    if not all_records:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    return pd.DataFrame.from_records(all_records)


@dataclass
class CryptoSymbolResult:
    symbol: str
    pair: str
    included: bool
    reason: str
    df: Optional[pd.DataFrame] = None
    months_ok: List[str] = field(default_factory=list)
    months_missing: List[str] = field(default_factory=list)
    months_error: List[str] = field(default_factory=list)


def process_crypto_symbol(
    session: requests.Session,
    symbol: str,
    archive_months: List[Tuple[int, int]],
    required_months: List[Tuple[int, int]],
    max_attempts: int,
) -> CryptoSymbolResult:
    pair = f"{symbol}USDT"
    logger.info("crypto %s (%s) : début (%d mois d'archive à couvrir)", symbol, pair, len(archive_months))

    frames: List[pd.DataFrame] = []
    months_ok: List[str] = []
    months_missing: List[str] = []
    months_error: List[str] = []

    for (y, m) in archive_months:
        ym = _yyyymm(y, m)
        status, df = download_binance_month(session, pair, y, m, max_attempts)
        if status == "OK":
            frames.append(df)
            months_ok.append(ym)
        elif status == "NOT_FOUND":
            months_missing.append(ym)
        else:
            # Erreur réseau/serveur persistante après retries : traitée prudemment comme
            # "non disponible" pour la vérification de complétude (on ne peut pas prouver
            # qu'elle existe), mais distinguée explicitement de NOT_FOUND dans le manifeste.
            months_error.append(ym)

    required_set = {_yyyymm(y, m) for (y, m) in required_months}
    covered_set = set(months_ok)
    missing_in_required = sorted(required_set - covered_set)

    if missing_in_required:
        reason = (
            f"historique incomplet sur la fenêtre de complétude requise "
            f"[{_yyyymm(*CRYPTO_FULL_COVERAGE_REQUIRED_FROM)} .. "
            f"{_yyyymm(*required_months[-1])}] : {len(missing_in_required)} mois manquant(s) "
            f"({', '.join(missing_in_required[:6])}{'…' if len(missing_in_required) > 6 else ''}) "
            f"— exclu (trace du biais du survivant)"
        )
        logger.warning("crypto %s : EXCLU — %s", symbol, reason)
        return CryptoSymbolResult(
            symbol=symbol, pair=pair, included=False, reason=reason,
            months_ok=months_ok, months_missing=months_missing, months_error=months_error,
        )

    # Complément du mois courant (non couvert par les archives bulk, qui ne publient que
    # des mois entièrement clos).
    now = datetime.now(timezone.utc)
    current_month_start = _month_start_utc(now.year, now.month)
    completion_df = fetch_binance_current_month_completion(session, pair, current_month_start, max_attempts)
    if not completion_df.empty:
        frames.append(completion_df)

    if not frames:
        reason = "aucune bougie exploitable obtenue (toutes sources en échec) — exclu"
        logger.warning("crypto %s : EXCLU — %s", symbol, reason)
        return CryptoSymbolResult(
            symbol=symbol, pair=pair, included=False, reason=reason,
            months_ok=months_ok, months_missing=months_missing, months_error=months_error,
        )

    full_df = pd.concat(frames, ignore_index=True)
    full_df = full_df.drop_duplicates(subset="timestamp", keep="last").sort_values("timestamp")
    full_df = full_df.reset_index(drop=True)

    logger.info(
        "crypto %s : INCLUS — %d bougies, %s -> %s (%d mois manquants hors fenêtre requise, %d erreurs)",
        symbol, len(full_df), full_df["timestamp"].iloc[0], full_df["timestamp"].iloc[-1],
        len(months_missing), len(months_error),
    )
    return CryptoSymbolResult(
        symbol=symbol, pair=pair, included=True, reason="historique complet depuis la fenêtre requise",
        df=full_df, months_ok=months_ok, months_missing=months_missing, months_error=months_error,
    )


# --------------------------------------------------------------------------------------
# Actions / ETF : Yahoo Finance (yfinance) en primaire, stooq.com en repli par ticker
# --------------------------------------------------------------------------------------
#
# Historique (cf. addendum plus haut) : stooq.com a échoué à 122/122 tickers lors du
# premier run réel sur les runners GitHub Actions ("réponse stooq vide/invalide" pour
# chaque ticker) — vraisemblablement un blocage des IP datacenter ou un rate limiting
# agressif, jamais reproductible dans cet environnement de développement (réseau bloqué
# de toute façon par le proxy local). Yahoo Finance (via `yfinance`) est donc désormais la
# source PRIMAIRE, avec repli séquentiel par ticker sur stooq (User-Agent navigateur, pause
# >= 1s entre requêtes) pour le cas où le blocage ne serait qu'un rate limiting sensible à
# la source/cadence des requêtes plutôt qu'un blocage total de stooq.


@dataclass
class DailySeriesResult:
    ticker: str
    status: str  # "OK" | "EMPTY" | "ERROR"
    source: str  # "yfinance" | "stooq_fallback" | "FAILED"
    df: Optional[pd.DataFrame] = None
    error: Optional[str] = None
    rows: int = 0
    symbol_used: Optional[str] = None  # symbole effectivement interrogé chez la source


def _stooq_symbol_for(ticker: str) -> str:
    return STOOQ_TICKER_OVERRIDES.get(ticker, ticker).lower()


def _yfinance_symbol_for(ticker: str) -> str:
    return YFINANCE_TICKER_OVERRIDES.get(ticker, ticker)


def _clean_ohlcv_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Nettoyage commun (types numériques, drop NaN, dédoublonnage, tri) appliqué après
    normalisation des colonnes, quelle que soit la source (yfinance ou stooq)."""
    df = df.dropna(subset=["timestamp"]).copy()
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0)
    df = df.drop_duplicates(subset="timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)
    return df


def fetch_stooq_fallback(ticker: str, max_attempts: int) -> DailySeriesResult:
    """Repli stooq PAR TICKER : session dédiée avec User-Agent de navigateur (distinct du
    User-Agent identifiant le bot utilisé pour Binance/l'API interne), appelé de façon
    strictement séquentielle par l'appelant (jamais en parallèle) avec une pause >= 1s
    entre deux tickers pour respecter un éventuel rate limiting côté stooq."""
    stooq_symbol = _stooq_symbol_for(ticker)
    # Session dédiée (plutôt que la session HTTP partagée du reste du script) : on ne veut
    # pas que ce User-Agent "navigateur" fuite vers les autres sources (Binance, API stooq
    # future éventuelle), et inversement.
    fallback_session = requests.Session()
    fallback_session.headers.update({"User-Agent": STOOQ_FALLBACK_USER_AGENT, "Accept": "text/csv,*/*"})

    result = fetch_with_retries(
        fallback_session, STOOQ_BASE, params={"s": f"{stooq_symbol}.us", "i": "d"},
        max_attempts=max_attempts, context=f"stooq-fallback {ticker}",
    )
    if result.status != "OK":
        return DailySeriesResult(
            ticker=ticker, status="ERROR", source="stooq_fallback",
            error=result.error or result.status, symbol_used=stooq_symbol,
        )

    text = result.response.text
    if not text or text.strip().lower().startswith("no data") or "<html" in text[:200].lower():
        return DailySeriesResult(
            ticker=ticker, status="EMPTY", source="stooq_fallback",
            error="réponse stooq vide/invalide (ticker inconnu, ou blocage/rate limiting persistant)",
            symbol_used=stooq_symbol,
        )

    try:
        df = pd.read_csv(io.StringIO(text))
    except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        return DailySeriesResult(ticker=ticker, status="ERROR", source="stooq_fallback", error=f"CSV invalide: {exc}", symbol_used=stooq_symbol)

    expected_cols = {"Date", "Open", "High", "Low", "Close", "Volume"}
    if not expected_cols.issubset(set(df.columns)):
        return DailySeriesResult(
            ticker=ticker, status="ERROR", source="stooq_fallback",
            error=f"colonnes inattendues: {list(df.columns)}", symbol_used=stooq_symbol,
        )

    df = df.rename(columns={
        "Date": "timestamp", "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })[["timestamp", "open", "high", "low", "close", "volume"]]
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = _clean_ohlcv_frame(df)

    if df.empty:
        return DailySeriesResult(ticker=ticker, status="EMPTY", source="stooq_fallback", error="0 ligne exploitable après nettoyage", symbol_used=stooq_symbol)

    return DailySeriesResult(ticker=ticker, status="OK", source="stooq_fallback", df=df, rows=len(df), symbol_used=stooq_symbol)


def _normalize_yf_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalise un DataFrame issu de `yfinance` (index Date, colonnes Open/High/Low/
    Close/Volume — `auto_adjust=True` donc déjà ajusté des splits/dividendes, pas de
    colonne "Adj Close" séparée) vers le schéma commun `timestamp,open,high,low,close,
    volume` en UTC."""
    df = raw.copy()
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    if not {"open", "high", "low", "close"}.issubset(set(df.columns)):
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    if "volume" not in df.columns:
        df["volume"] = 0.0

    idx = df.index
    if getattr(idx, "tz", None) is None:
        # Index quotidien tz-naive : les données actions/ETF US de yfinance sont
        # implicitement en heure de séance America/New_York — localisation explicite avant
        # conversion UTC plutôt qu'une supposition silencieuse d'UTC (qui décalerait la
        # date calendaire de la bougie journalière).
        idx = idx.tz_localize("America/New_York", ambiguous="NaT", nonexistent="shift_forward")
    df.index = idx.tz_convert("UTC")
    df = df.reset_index()
    df = df.rename(columns={df.columns[0]: "timestamp"})
    df = df[["timestamp", "open", "high", "low", "close", "volume"]]
    return _clean_ohlcv_frame(df)


def _extract_yf_ticker_frame(batch_df: pd.DataFrame, yf_symbol: str, batch_len: int) -> Optional[pd.DataFrame]:
    """Extrait le sous-DataFrame d'un ticker donné depuis le résultat (potentiellement
    multi-tickers, colonnes MultiIndex `(ticker, champ)` avec `group_by="ticker"`) de
    `yf.download()`. Gère aussi le cas où un seul ticker a été demandé (colonnes plates,
    pas de MultiIndex, selon la version de `yfinance`)."""
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


def fetch_yfinance_batch(tickers: List[str], batch_size: int, pause_between_batches: float, max_attempts: int) -> Dict[str, Optional[pd.DataFrame]]:
    """Télécharge l'historique quotidien maximal (`period="max"`, `auto_adjust=True`) d'une
    liste de tickers via `yfinance`, par lots raisonnables avec pause entre chaque lot
    (limiter la cadence de requêtes vers Yahoo). Un lot entier retenté avec backoff en cas
    d'échec réseau ; un ticker absent/vide du résultat d'un lot par ailleurs réussi est
    laissé à `None` ici — il sera retenté individuellement par l'appelant."""
    results: Dict[str, Optional[pd.DataFrame]] = {t: None for t in tickers}
    if not _YFINANCE_AVAILABLE:
        logger.warning("yfinance indisponible (%s) — repli direct sur stooq pour tous les tickers", _YFINANCE_IMPORT_ERROR)
        return results

    batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]
    for batch_idx, batch in enumerate(batches, start=1):
        yf_symbols = [_yfinance_symbol_for(t) for t in batch]
        batch_df: Optional[pd.DataFrame] = None
        for attempt in range(1, max_attempts + 1):
            try:
                batch_df = yf.download(
                    tickers=" ".join(yf_symbols), period="max", interval="1d",
                    auto_adjust=True, group_by="ticker", threads=True,
                    progress=False, timeout=30,
                )
                break
            except Exception as exc:  # noqa: BLE001 — yfinance peut lever des exceptions variées
                if attempt >= max_attempts:
                    logger.error(
                        "yfinance lot %d/%d (%s) : échec définitif après %d tentative(s) (%s)",
                        batch_idx, len(batches), batch, attempt, exc,
                    )
                    batch_df = None
                    break
                delay = min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                logger.warning(
                    "yfinance lot %d/%d (%s) : erreur (tentative %d/%d, %s) — nouvelle tentative dans %.1fs",
                    batch_idx, len(batches), batch, attempt, max_attempts, exc, delay,
                )
                time.sleep(delay)

        if batch_df is not None and not batch_df.empty:
            for ticker, yf_symbol in zip(batch, yf_symbols):
                sub = _extract_yf_ticker_frame(batch_df, yf_symbol, len(batch))
                if sub is not None:
                    normalized = _normalize_yf_frame(sub)
                    if not normalized.empty:
                        results[ticker] = normalized
        else:
            logger.warning("yfinance lot %d/%d (%s) : aucune donnée exploitable pour ce lot", batch_idx, len(batches), batch)

        if batch_idx < len(batches):
            time.sleep(pause_between_batches)

    return results


def fetch_yfinance_single(ticker: str, max_attempts: int) -> Optional[pd.DataFrame]:
    """Repli/retry individuel `yfinance` pour un ticker absent/vide du téléchargement par
    lot (ticker isolé qui a pu échouer indépendamment du reste de son lot)."""
    if not _YFINANCE_AVAILABLE:
        return None
    yf_symbol = _yfinance_symbol_for(ticker)
    for attempt in range(1, max_attempts + 1):
        try:
            hist = yf.Ticker(yf_symbol).history(period="max", interval="1d", auto_adjust=True)
        except Exception as exc:  # noqa: BLE001
            if attempt >= max_attempts:
                logger.error("yfinance retry individuel %s : échec définitif après %d tentative(s) (%s)", ticker, attempt, exc)
                return None
            delay = min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            logger.warning("yfinance retry individuel %s : erreur (tentative %d/%d, %s) — nouvelle tentative dans %.1fs", ticker, attempt, max_attempts, exc, delay)
            time.sleep(delay)
            continue

        if hist is None or hist.empty:
            logger.warning("yfinance retry individuel %s : résultat vide (tentative %d/%d)", ticker, attempt, max_attempts)
            if attempt >= max_attempts:
                return None
            time.sleep(YFINANCE_SINGLE_RETRY_PAUSE_SECONDS)
            continue

        normalized = _normalize_yf_frame(hist)
        return normalized if not normalized.empty else None

    return None


def fetch_daily_series(ticker: str, yfinance_batch_results: Dict[str, Optional[pd.DataFrame]], max_attempts: int) -> DailySeriesResult:
    """Résout la série quotidienne d'un ticker en combinant, dans l'ordre : (1) le résultat
    du lot `yfinance` déjà téléchargé, (2) un retry `yfinance` individuel, (3) le repli
    stooq par ticker. Ne retourne "FAILED" que si les trois étapes ont échoué."""
    df = yfinance_batch_results.get(ticker)
    if df is not None and not df.empty:
        return DailySeriesResult(ticker=ticker, status="OK", source="yfinance", df=df, rows=len(df), symbol_used=_yfinance_symbol_for(ticker))

    df = fetch_yfinance_single(ticker, max_attempts)
    if df is not None and not df.empty:
        return DailySeriesResult(ticker=ticker, status="OK", source="yfinance", df=df, rows=len(df), symbol_used=_yfinance_symbol_for(ticker))

    time.sleep(max(STOOQ_FALLBACK_MIN_PAUSE_SECONDS, 1.0))  # pause avant le repli, cf. contrat stooq
    stooq_result = fetch_stooq_fallback(ticker, max_attempts)
    if stooq_result.status == "OK":
        return stooq_result

    return DailySeriesResult(
        ticker=ticker, status="ERROR", source="FAILED",
        error=f"yfinance et stooq (repli) ont tous deux échoué — dernière erreur stooq: {stooq_result.error}",
        symbol_used=stooq_result.symbol_used,
    )


# --------------------------------------------------------------------------------------
# Écriture des fichiers de sortie
# --------------------------------------------------------------------------------------


def write_gz_csv(df: pd.DataFrame, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out = df.copy()
    out["timestamp"] = out["timestamp"].apply(lambda ts: pd.Timestamp(ts).tz_convert("UTC").isoformat())
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        out.to_csv(f, index=False, columns=["timestamp", "open", "high", "low", "close", "volume"])


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


def run_crypto(session: requests.Session, staging_dir: str, max_attempts: int, workers: int) -> dict:
    end_month = last_complete_month()
    archive_months = month_range(CRYPTO_ARCHIVE_START, end_month)
    required_months = month_range(CRYPTO_FULL_COVERAGE_REQUIRED_FROM, end_month)

    included: Dict[str, dict] = {}
    excluded: Dict[str, dict] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(process_crypto_symbol, session, sym, archive_months, required_months, max_attempts): sym
            for sym in CRYPTO_SYMBOLS
        }
        for fut in concurrent.futures.as_completed(futures):
            sym = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001 — isolation stricte entre symboles
                logger.error("crypto %s : exception non gérée, symbole exclu par prudence (%s)", sym, exc)
                excluded[sym] = {"pair": f"{sym}USDT", "reason": f"exception interne: {exc}"}
                continue

            entry = {
                "pair": res.pair,
                "months_ok": len(res.months_ok),
                "months_missing": res.months_missing,
                "months_error": res.months_error,
            }
            if res.included and res.df is not None and not res.df.empty:
                out_path = os.path.join(staging_dir, "data", "crypto", f"{sym}.csv.gz")
                write_gz_csv(res.df, out_path)
                entry["rows"] = len(res.df)
                entry["first_ts"] = pd.Timestamp(res.df["timestamp"].iloc[0]).isoformat()
                entry["last_ts"] = pd.Timestamp(res.df["timestamp"].iloc[-1]).isoformat()
                entry["reason"] = res.reason
                included[sym] = entry
            else:
                entry["reason"] = res.reason
                excluded[sym] = entry

    return {
        "archive_from": _yyyymm(*CRYPTO_ARCHIVE_START),
        "archive_to": _yyyymm(*end_month),
        "full_coverage_required_from": _yyyymm(*CRYPTO_FULL_COVERAGE_REQUIRED_FROM),
        "included": included,
        "excluded": excluded,
    }


def run_daily_universe(
    session: requests.Session, staging_dir: str, tickers: List[str], subdir: str,
    max_attempts: int, workers: int, min_years: float,
) -> dict:
    """Résout l'historique quotidien de `tickers` (actions ou ETF) : yfinance par lots en
    primaire (parallélisable en interne, pause entre lots), puis pour les tickers restés
    sans donnée, un retry `yfinance` individuel suivi si besoin d'un repli stooq — cette
    dernière étape est délibérément SÉQUENTIELLE (jamais de `ThreadPoolExecutor`) avec une
    pause >= 1s entre chaque ticker, pour respecter un éventuel rate limiting côté stooq.

    Un échec, même total, d'une des deux sources pour un sous-ensemble de tickers ne fait
    JAMAIS échouer ce run : chaque ticker en échec est journalisé et documenté avec sa
    raison précise dans le manifeste (`status` + `source` + `error`), le reste du pipeline
    continue normalement (cf. mission : "l'échec partiel d'une source ne fait pas échouer
    tout le job").
    """
    results: Dict[str, dict] = {}

    logger.info(
        "%s : phase 1/2 — téléchargement par lots yfinance (%d ticker(s), lots de %d, pause %.1fs)",
        subdir, len(tickers), YFINANCE_BATCH_SIZE, YFINANCE_BATCH_PAUSE_SECONDS,
    )
    batch_results = fetch_yfinance_batch(tickers, YFINANCE_BATCH_SIZE, YFINANCE_BATCH_PAUSE_SECONDS, max_attempts)

    still_missing = [t for t in tickers if batch_results.get(t) is None]
    if still_missing:
        logger.info(
            "%s : phase 2/2 — %d ticker(s) sans donnée après le lot yfinance, retry individuel "
            "yfinance puis repli stooq séquentiel (pause >= %.1fs/ticker)",
            subdir, len(still_missing), STOOQ_FALLBACK_MIN_PAUSE_SECONDS,
        )

    for ticker in tickers:
        if batch_results.get(ticker) is not None:
            res = DailySeriesResult(
                ticker=ticker, status="OK", source="yfinance",
                df=batch_results[ticker], rows=len(batch_results[ticker]),
                symbol_used=_yfinance_symbol_for(ticker),
            )
        else:
            # Séquentiel et déjà rythmé en interne (fetch_daily_series applique la pause
            # avant tout repli stooq) — ne JAMAIS paralléliser cette branche.
            res = fetch_daily_series(ticker, batch_results, max_attempts)

        if res.status == "OK" and res.df is not None and not res.df.empty:
            out_path = os.path.join(staging_dir, "data", subdir, f"{ticker}.csv.gz")
            write_gz_csv(res.df, out_path)
            first_ts = pd.Timestamp(res.df["timestamp"].iloc[0])
            last_ts = pd.Timestamp(res.df["timestamp"].iloc[-1])
            span_years = (last_ts - first_ts).days / 365.25
            entry = {
                "status": "OK",
                "source": res.source,
                "symbol_used": res.symbol_used,
                "rows": res.rows,
                "first_ts": first_ts.isoformat(),
                "last_ts": last_ts.isoformat(),
                "span_years": round(span_years, 2),
            }
            if span_years < min_years:
                entry["warning"] = (
                    f"historique disponible ({span_years:.1f} ans) inférieur au minimum visé "
                    f"({min_years} ans) — probablement une introduction en bourse/cotation récente"
                )
            results[ticker] = entry
            logger.info("%s %s : OK via %s (%d lignes, %.1f ans)", subdir, ticker, res.source, res.rows, span_years)
        else:
            results[ticker] = {
                "status": res.status,
                "source": res.source,
                "symbol_used": res.symbol_used,
                "error": res.error,
            }
            logger.warning("%s %s : %s via %s (%s)", subdir, ticker, res.status, res.source, res.error)

    return results


# --------------------------------------------------------------------------------------
# Publication git : branche orpheline `market-data`, force-push
# --------------------------------------------------------------------------------------


def _run_git(repo_dir: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", repo_dir, *args], capture_output=True, text=True, check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} a échoué (code {result.returncode}): {result.stderr.strip()}")
    return result


def publish_to_orphan_branch(repo_dir: str, staging_dir: str, branch: str, push: bool) -> Optional[str]:
    """Publie le contenu de `staging_dir` (data/, MANIFEST.json, DATA_REPORT.md) sur la
    branche orpheline `branch`, en écrasant tout contenu distant existant (force-push —
    c'est une branche de données entièrement régénérable à chaque run).

    Restaure le dépôt sur son commit de départ (HEAD détaché) une fois la publication
    terminée, pour ne jamais laisser un job CI dans un état de branche inattendu.

    Retourne le sha du commit créé sur `branch`, ou None si `push=False` (mode dry-run) ou
    si l'étape push a été sautée.
    """
    starting_sha = _run_git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    logger.info("publication market-data : point de départ HEAD=%s", starting_sha[:12])

    # Nettoyage défensif : si une branche locale `branch` existe déjà d'un run précédent
    # dans ce même clone, on la supprime — cette branche est intégralement régénérée à
    # chaque exécution, aucune valeur à conserver dans son historique local.
    existing = _run_git(repo_dir, "branch", "--list", branch, check=False).stdout.strip()
    if existing:
        # Si on est actuellement dessus (improbable en CI, possible en test local), on
        # revient d'abord au commit de départ.
        current = _run_git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
        if current == branch:
            _run_git(repo_dir, "checkout", "--detach", starting_sha)
        _run_git(repo_dir, "branch", "-D", branch)

    try:
        _run_git(repo_dir, "checkout", "--orphan", branch)
        # `checkout --orphan` conserve les fichiers du commit précédent dans l'index et le
        # working tree : on repart d'un working tree strictement vide avant d'y déposer le
        # contenu de staging_dir.
        _run_git(repo_dir, "rm", "-rf", "--cached", ".", check=False)
        for entry in os.listdir(repo_dir):
            if entry == ".git":
                continue
            full = os.path.join(repo_dir, entry)
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
            else:
                try:
                    os.remove(full)
                except OSError:
                    pass

        for name in ("data", "MANIFEST.json", "DATA_REPORT.md"):
            src = os.path.join(staging_dir, name)
            if not os.path.exists(src):
                continue
            dst = os.path.join(repo_dir, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

        _run_git(repo_dir, "add", "-A")
        status = _run_git(repo_dir, "status", "--porcelain").stdout.strip()
        if not status:
            logger.warning("publication market-data : rien à committer (staging vide ?)")
            commit_sha = None
        else:
            _run_git(
                repo_dir, "-c", f"user.name={GIT_AUTHOR_NAME}", "-c", f"user.email={GIT_AUTHOR_EMAIL}",
                "commit", "-m", f"Données marché régénérées {datetime.now(timezone.utc).isoformat()}",
            )
            commit_sha = _run_git(repo_dir, "rev-parse", "HEAD").stdout.strip()
            logger.info("publication market-data : commit local %s créé sur %s", commit_sha[:12], branch)

            if push:
                push_result = _run_git(repo_dir, "push", "--force", "origin", branch, check=False)
                if push_result.returncode != 0:
                    logger.error(
                        "publication market-data : push --force vers origin/%s a échoué : %s",
                        branch, push_result.stderr.strip(),
                    )
                    raise RuntimeError("échec du push --force sur la branche market-data")
                logger.info("publication market-data : push --force vers origin/%s réussi", branch)
            else:
                logger.info("publication market-data : mode dry-run (--skip-push), commit local uniquement, pas de push")
    finally:
        # Restauration systématique de l'état de départ, même en cas d'échec.
        _run_git(repo_dir, "checkout", "--detach", starting_sha, check=False)
        _run_git(repo_dir, "branch", "-D", branch, check=False)
        logger.info("dépôt restauré sur HEAD=%s (détaché)", starting_sha[:12])

    return commit_sha


# --------------------------------------------------------------------------------------
# Rapport / manifeste
# --------------------------------------------------------------------------------------


def build_manifest(crypto_report: dict, equities_report: dict, etf_report: dict, started_at: datetime, ended_at: datetime) -> dict:
    return {
        "generated_at": ended_at.isoformat(),
        "generation_duration_seconds": round((ended_at - started_at).total_seconds(), 1),
        "sources": {
            "crypto_bulk_archive": f"{BINANCE_VISION_BASE}/data/spot/monthly/klines/{{PAIR}}/1h/{{PAIR}}-1h-{{YYYY-MM}}.zip",
            "crypto_current_month_completion": f"{BINANCE_API_BASE}/api/v3/klines",
            "equities_etf_daily_primary": "yfinance (yf.download / yf.Ticker.history, period=max, interval=1d, auto_adjust=True)",
            "equities_etf_daily_fallback": f"stooq.com (`{STOOQ_BASE}?s={{ticker}}.us&i=d`, User-Agent navigateur, séquentiel, pause >= {STOOQ_FALLBACK_MIN_PAUSE_SECONDS}s/requête) — utilisé uniquement si yfinance échoue pour un ticker",
        },
        "crypto": crypto_report,
        "equities": equities_report,
        "etf": etf_report,
        "counts": {
            "crypto_included": len(crypto_report.get("included", {})),
            "crypto_excluded": len(crypto_report.get("excluded", {})),
            "equities_ok": sum(1 for v in equities_report.values() if v.get("status") == "OK"),
            "equities_failed": sum(1 for v in equities_report.values() if v.get("status") != "OK"),
            "equities_by_source": _tally_by_source(equities_report),
            "etf_ok": sum(1 for v in etf_report.values() if v.get("status") == "OK"),
            "etf_failed": sum(1 for v in etf_report.values() if v.get("status") != "OK"),
            "etf_by_source": _tally_by_source(etf_report),
        },
    }


def _tally_by_source(report: Dict[str, dict]) -> Dict[str, int]:
    tally: Dict[str, int] = {}
    for entry in report.values():
        src = entry.get("source", "inconnu")
        tally[src] = tally.get(src, 0) + 1
    return tally


def build_report_md(manifest: dict) -> str:
    counts = manifest["counts"]
    lines = [
        "# DATA_REPORT — données marché du bot de paper trading",
        "",
        f"Généré le {manifest['generated_at']} (durée de génération : {manifest['generation_duration_seconds']:.0f}s).",
        "",
        "Cette branche (`market-data`) est entièrement régénérée à chaque exécution de "
        "`tools/fetch_data.py` (voir `.github/workflows/fetch-data.yml`) — son historique git "
        "n'a pas de valeur en soi, seul le contenu du dernier commit compte.",
        "",
        "## Sources",
        "",
        f"- Crypto horaire : archives bulk Binance (`{manifest['sources']['crypto_bulk_archive']}`), "
        f"complétées pour le mois en cours via l'API publique "
        f"(`{manifest['sources']['crypto_current_month_completion']}`).",
        f"- Actions (S&P 100) et ETF, quotidien, prix ajustés : primaire "
        f"{manifest['sources']['equities_etf_daily_primary']} ; repli par ticker "
        f"{manifest['sources']['equities_etf_daily_fallback']}.",
        "",
        "## Crypto",
        "",
        f"- Fenêtre d'archive : {manifest['crypto']['archive_from']} → {manifest['crypto']['archive_to']} "
        f"(+ complément mois courant via API).",
        f"- Fenêtre de complétude obligatoire (sinon exclusion) : depuis {manifest['crypto']['full_coverage_required_from']}.",
        f"- **{counts['crypto_included']} paire(s) incluse(s)**, **{counts['crypto_excluded']} exclue(s)**.",
        "",
    ]

    if manifest["crypto"]["excluded"]:
        lines.append("### Exclusions crypto (trace du biais du survivant)")
        lines.append("")
        for sym, info in sorted(manifest["crypto"]["excluded"].items()):
            lines.append(f"- **{sym}** (`{info.get('pair', '?')}`) : {info.get('reason', 'raison non renseignée')}")
        lines.append("")

    if manifest["crypto"]["included"]:
        lines.append("### Paires crypto incluses")
        lines.append("")
        lines.append("| Symbole | Paire | Lignes | Début | Fin |")
        lines.append("|---|---|---|---|---|")
        for sym, info in sorted(manifest["crypto"]["included"].items()):
            lines.append(
                f"| {sym} | {info.get('pair')} | {info.get('rows')} | {info.get('first_ts')} | {info.get('last_ts')} |"
            )
        lines.append("")

    lines.append("## Actions (S&P 100)")
    lines.append("")
    lines.append(f"- **{counts['equities_ok']} ticker(s) OK**, **{counts['equities_failed']} échoué(s)/vide(s)**.")
    lines.append(f"- Répartition par source : {counts['equities_by_source']}.")
    lines.append("")
    failed_eq = {k: v for k, v in manifest["equities"].items() if v.get("status") != "OK"}
    if failed_eq:
        lines.append("### Tickers actions en échec")
        lines.append("")
        for tick, info in sorted(failed_eq.items()):
            lines.append(f"- **{tick}** (source tentée : {info.get('source')}) : {info.get('status')} — {info.get('error')}")
        lines.append("")

    lines.append("## ETF")
    lines.append("")
    lines.append(f"- **{counts['etf_ok']} ticker(s) OK**, **{counts['etf_failed']} échoué(s)/vide(s)**.")
    lines.append(f"- Répartition par source : {counts['etf_by_source']}.")
    lines.append("")
    failed_etf = {k: v for k, v in manifest["etf"].items() if v.get("status") != "OK"}
    if failed_etf:
        lines.append("### Tickers ETF en échec")
        lines.append("")
        for tick, info in sorted(failed_etf.items()):
            lines.append(f"- **{tick}** (source tentée : {info.get('source')}) : {info.get('status')} — {info.get('error')}")
        lines.append("")

    lines.append("## Format des fichiers")
    lines.append("")
    lines.append(
        "`data/{crypto,equities,etf}/{SYMBOLE}.csv.gz` — colonnes "
        "`timestamp,open,high,low,close,volume`, `timestamp` en ISO8601 UTC, dédoublonné et "
        "trié par ordre croissant. Crypto = bougies horaires ; actions/ETF = bougies journalières."
    )
    lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# main()
# --------------------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    default_repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", default=default_repo_dir, help="racine du dépôt git (défaut : parent de tools/)")
    parser.add_argument("--branch", default="market-data", help="branche orpheline de publication (défaut: market-data)")
    parser.add_argument("--skip-push", action="store_true", help="ne pousse pas sur origin (commit local uniquement, pour tests)")
    parser.add_argument("--skip-git", action="store_true", help="ne touche pas du tout au dépôt git (écrit seulement dans --staging-dir)")
    parser.add_argument("--staging-dir", default=None, help="répertoire de staging (défaut : dossier temporaire)")
    parser.add_argument(
        "--only", default="crypto,equities,etf",
        help="sous-ensemble à exécuter, séparé par des virgules (défaut: crypto,equities,etf)",
    )
    parser.add_argument("--workers", type=int, default=8, help="parallélisme des téléchargements (défaut: 8)")
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS_DEFAULT, help="tentatives max par requête HTTP")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    started_at = datetime.now(timezone.utc)
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    staging_dir = args.staging_dir or tempfile.mkdtemp(prefix="fetch_data_staging_")
    os.makedirs(staging_dir, exist_ok=True)
    logger.info("répertoire de staging : %s", staging_dir)

    session = build_session()

    crypto_report = {"archive_from": None, "archive_to": None, "full_coverage_required_from": None, "included": {}, "excluded": {}}
    equities_report: dict = {}
    etf_report: dict = {}

    if "crypto" in only:
        logger.info("=== Crypto : %d symbole(s) curatés, %d worker(s) ===", len(CRYPTO_SYMBOLS), args.workers)
        crypto_report = run_crypto(session, staging_dir, args.max_attempts, args.workers)
    else:
        logger.info("=== Crypto : sauté (--only=%s) ===", args.only)

    if "equities" in only:
        logger.info("=== Actions S&P 100 : %d ticker(s) ===", len(SP100_TICKERS))
        equities_report = run_daily_universe(
            session, staging_dir, SP100_TICKERS, "equities", args.max_attempts, args.workers, min_years=8.0
        )
    else:
        logger.info("=== Actions : sauté (--only=%s) ===", args.only)

    if "etf" in only:
        logger.info("=== ETF : %d ticker(s) ===", len(ETF_TICKERS))
        etf_report = run_daily_universe(
            session, staging_dir, ETF_TICKERS, "etf", args.max_attempts, args.workers, min_years=10.0
        )
    else:
        logger.info("=== ETF : sauté (--only=%s) ===", args.only)

    ended_at = datetime.now(timezone.utc)
    manifest = build_manifest(crypto_report, equities_report, etf_report, started_at, ended_at)
    report_md = build_report_md(manifest)

    with open(os.path.join(staging_dir, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    with open(os.path.join(staging_dir, "DATA_REPORT.md"), "w", encoding="utf-8") as f:
        f.write(report_md)

    logger.info(
        "Résumé : crypto inclus=%d exclus=%d | actions ok=%d échec=%d | etf ok=%d échec=%d",
        manifest["counts"]["crypto_included"], manifest["counts"]["crypto_excluded"],
        manifest["counts"]["equities_ok"], manifest["counts"]["equities_failed"],
        manifest["counts"]["etf_ok"], manifest["counts"]["etf_failed"],
    )

    if args.skip_git:
        logger.info("--skip-git : pas de publication, fichiers laissés dans %s", staging_dir)
        return 0

    try:
        publish_to_orphan_branch(args.repo_dir, staging_dir, args.branch, push=not args.skip_push)
    except RuntimeError as exc:
        logger.error("échec de la publication git : %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
