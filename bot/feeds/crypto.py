"""Adaptateur crypto : Coinbase Exchange public REST (source primaire des QUOTES), avec repli
API publique Kraken (`Ticker`), puis Binance public REST en DERNIER RECOURS. Aucune clé API
requise (endpoints publics uniquement).

--------------------------------------------------------------------------------------------
CORRECTIF INCIDENT PRODUCTION (2026-07-27) — feed crypto aveugle
--------------------------------------------------------------------------------------------
Diagnostic mesuré sur les journaux réels (`state/wallets/*/decisions.jsonl`) : Binance
renvoie HTTP 451 sur 100% des cycles horaires (géo-blocage des IP US des runners GitHub
Actions — source STRUCTURELLEMENT morte depuis ce dépôt, tous endpoints confondus, pas
seulement les quotes), et Coinbase renvoyait HTTP 429 sur ~35% des cycles parce que le
CHEMIN HISTORIQUE (`get_history_crypto`, pas les quotes) re-téléchargeait ~200 jours de
bougies HORAIRES pour chaque symbole À CHAQUE cycle (Binance mort -> repli Coinbase paginé
en profondeur, jusqu'à ~20 pages/symbole). Deux correctifs distincts, tous deux ici :

  1. **Quotes** (`get_prices_crypto`) : Binance rétrogradé en source de DERNIER RECOURS
     (toujours tentée — utile si le géo-blocage disparaît un jour — mais après Coinbase ET
     Kraken). Coinbase devient la source primaire ; Kraken (`_fetch_kraken_ticker`, endpoint
     public `Ticker`, aucune clé, fonctionne depuis les IP US) est ajouté comme repli
     intermédiaire. Mapping des paires vérifié symbole par symbole (`bot.config.
     CRYPTO_PAIR_KRAKEN` — BTC -> "XBT", DOGE -> "XDG", etc.) ; un symbole non couvert
     (ex. BNB, absent chez Kraken) saute simplement ce repli intermédiaire.
  2. **Historique** (`get_history_crypto`) : la portion réseau "vivante" est désormais
     STRICTEMENT plafonnée à `_RECENT_HOURLY_WINDOW_HOURS` (300h ≈ 12,5 jours — largement
     suffisant pour stabiliser la vol EWMA demi-vie 60h de `bot.strategies.
     quasi_passif_crypto`, cf. décroissance exponentielle : au-delà de ~5 demi-vies la
     contribution résiduelle est négligeable), soit AU PLUS 1-2 requêtes réseau par symbole
     par cycle (au lieu d'une pagination profonde). La portion plus ANCIENNE (jusqu'à
     `n_hours` demandé, ex. ~200 jours pour la SMA200) est complétée depuis le cache disque
     JOURNALIER `data-cache/crypto/<symbole>.csv.gz` (déjà produit quotidiennement par
     `tools/build_daily_cache.py`, lu ici EXACTEMENT comme `bot/feeds/daily.py` le fait déjà
     pour les actions/ETF depuis le 24/07) — jamais de nouvelle pagination réseau profonde.
     Chaque jour caché est répliqué sur ses 24 heures (00h-23h UTC) avec la MÊME clôture
     RÉELLE (celle du cache, jamais une valeur inventée ou interpolée) : cette réplication ne
     sert qu'à satisfaire la règle de complétude horaire de `bot.strategies.
     quasi_passif_crypto._daily_closes()` (24 heures distinctes par jour), la décision de
     tendance elle-même (`close > SMA200`) ne consommant jamais que la clôture RÉELLE de la
     dernière heure de chaque jour — identique à toutes les autres heures synthétiques du
     même jour. Objectif mesuré : ~290 requêtes/jour au lieu de ~4 900.

Principe cardinal INCHANGÉ : jamais de prix inventé. Toute exception réseau, tout JSON
invalide, tout bid/ask incohérent (bid<=0, ask<=0, bid>=ask) est traité comme un échec de la
source concernée -> tentative de la source suivante -> `None`/`HistoryUnavailableError` si
toutes échouent. Aucune exception n'est jamais laissée fuiter au niveau symbole individuel
depuis `get_prices_crypto`.
"""

from __future__ import annotations

import datetime as _dt
import logging

import pandas as pd
import requests

from bot.feeds import daily as _daily_cache  # réutilise le lecteur de cache disque JOURNALIER
# (cf. bandeau ci-dessus, point 2) — plutôt que dupliquer la logique de lecture/fraîcheur déjà
# éprouvée dans bot/feeds/daily.py pour les actions/ETF (`_cache_dir`, `_read_cache_manifest`,
# `_manifest_is_fresh`, `_load_disk_cache_symbol`).
from bot.feeds._config_fallback import cfg
from bot.feeds.types import HistoryUnavailableError, Quote

logger = logging.getLogger(__name__)

BINANCE_BASE_URL = "https://api.binance.com"
COINBASE_BASE_URL = "https://api.exchange.coinbase.com"
KRAKEN_BASE_URL = "https://api.kraken.com"

_HTTP_TIMEOUT_SECONDS = 10
_USER_AGENT = "trading-bot-paper/1.0 (+https://github.com/mathieubigardpro-afk/trading-bot)"

_COINBASE_MAX_CANDLES_PER_CALL = 300
_COINBASE_MAX_PAGES = 20  # garde-fou anti-boucle-infinie, largement suffisant (20*300=6000h)

# Fenêtre "vivante" (réseau) plafond de l'historique horaire — cf. bandeau module point 2.
# Volontairement égale à `_COINBASE_MAX_CANDLES_PER_CALL` : un seul appel Coinbase (une seule
# page) suffit TOUJOURS à la couvrir, quel que soit `n_hours` demandé par l'appelant.
_RECENT_HOURLY_WINDOW_HOURS = 300

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


def _fetch_kraken_ticker(pair: str) -> Quote | None:
    """API publique Kraken, endpoint `Ticker` (aucune clé requise) — repli intermédiaire entre
    Coinbase (primaire) et Binance (dernier recours), cf. bandeau module. Le dict `result` de
    la réponse ne contient qu'UNE entrée (on ne demande jamais qu'une seule paire à la fois) ;
    sa CLÉ suit une convention interne à Kraken qui ne correspond pas forcément à `pair`
    (préfixes X/Z historiques) — on prend donc la première (et seule) valeur du dict plutôt que
    de supposer un nom de clé précis."""
    url = f"{KRAKEN_BASE_URL}/0/public/Ticker"
    try:
        resp = _session.get(url, params={"pair": pair}, timeout=_HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        errors = data.get("error") or []
        if errors:
            logger.warning("kraken ticker erreur API pour %s: %s", pair, errors)
            return None
        result = data.get("result") or {}
        entry = next(iter(result.values()))
        bid = float(entry["b"][0])
        ask = float(entry["a"][0])
    except (requests.RequestException, ValueError, KeyError, TypeError, StopIteration) as exc:
        logger.warning("kraken ticker échec pour %s: %s", pair, exc)
        return None

    if not _validate_bid_ask(bid, ask):
        logger.warning("kraken ticker bid/ask invalide pour %s: bid=%s ask=%s", pair, bid, ask)
        return None

    # Ticker Kraken ne renvoie aucun horodatage propre à la quote (comme bookTicker Binance) :
    # instantané du carnet au moment de la réponse HTTP -> heure de réception = meilleure
    # approximation disponible de "l'heure source" pour cet endpoint précis.
    now = _now_utc()
    mid = (bid + ask) / 2.0
    return Quote(bid=bid, ask=ask, mid=mid, ts=_iso(now), source="kraken")


def _fresh_or_none(quote: Quote | None, max_age: float, label: str, symbol: str) -> Quote | None:
    if quote is None:
        return None
    quote_ts = _dt.datetime.fromisoformat(quote.ts)
    if not _quote_is_fresh(quote_ts, max_age):
        logger.warning("%s périmé pour %s", label, symbol)
        return None
    return quote


def get_prices_crypto(symbols: list[str]) -> dict[str, Quote | None]:
    """Retourne un Quote frais (bid/ask/mid) par symbole crypto demandé, ou None si Coinbase,
    Kraken ET Binance ont tous échoué ou renvoyé une quote périmée/invalide.

    Ordre des sources (correctif 2026-07-27, cf. bandeau module) : Coinbase (primaire) ->
    Kraken (repli) -> Binance (dernier recours, 451 systématique depuis les runners GitHub
    Actions mais toujours tenté -- redevient utile si le géo-blocage disparaît)."""
    max_age = cfg.STALENESS_MAX_SECONDS_CRYPTO
    result: dict[str, Quote | None] = {}

    for symbol in symbols:
        coinbase_pair = cfg.CRYPTO_PAIR_COINBASE.get(symbol)
        kraken_pair = cfg.CRYPTO_PAIR_KRAKEN.get(symbol)
        binance_pair = cfg.CRYPTO_PAIR_BINANCE.get(symbol)

        quote: Quote | None = None
        if coinbase_pair is not None:
            quote = _fresh_or_none(_fetch_coinbase_ticker(coinbase_pair), max_age, "coinbase ticker", symbol)

        if quote is None and kraken_pair is not None:
            quote = _fresh_or_none(_fetch_kraken_ticker(kraken_pair), max_age, "kraken ticker", symbol)

        if quote is None and binance_pair is not None:
            quote = _fresh_or_none(_fetch_binance_bookticker(binance_pair), max_age, "binance bookTicker", symbol)

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


_SYNTH_HOURLY_COLUMNS = ["open", "high", "low", "close", "volume"]


def _synthesize_hourly_from_daily_cache(symbol: str, before_ts: _dt.datetime, n_hours_needed: int) -> pd.DataFrame:
    """Complète la portion ANCIENNE de l'historique horaire (au-delà de `_RECENT_HOURLY_
    WINDOW_HOURS`, cf. bandeau module point 2) depuis le cache disque JOURNALIER crypto
    (`data-cache/crypto/<symbole>.csv.gz`, produit par `tools/build_daily_cache.py`), jamais
    par une nouvelle requête réseau. Chaque jour du cache produit 24 lignes horaires (00h-23h
    UTC, strictement AVANT `before_ts`) partageant TOUTES la MÊME clôture RÉELLE (celle du
    cache) -- aucune donnée n'est inventée ni interpolée : la réplication ne sert qu'à
    satisfaire `bot.strategies.quasi_passif_crypto._daily_closes()`, qui exige 24 heures
    distinctes présentes pour retenir un jour comme "complet", et dont la décision de tendance
    ne consomme de toute façon que la clôture de la DERNIÈRE heure du jour -- identique à
    toutes les heures synthétiques du même jour, donc strictement équivalente à la clôture
    réelle en cache.

    Retourne un DataFrame vide (jamais une exception) si le cache est absent, périmé
    (`bot.feeds.daily.CACHE_MAX_AGE_DAYS`), ou ne couvre pas ce symbole -- l'appelant traite
    alors l'historique comme insuffisant, exactement comme en cas d'échec réseau."""
    empty = pd.DataFrame(columns=_SYNTH_HOURLY_COLUMNS)
    if n_hours_needed <= 0:
        return empty

    cache_dir = _daily_cache._cache_dir()
    manifest = _daily_cache._read_cache_manifest(cache_dir)
    if not _daily_cache._manifest_is_fresh(manifest, _now_utc()):
        return empty

    daily_df = _daily_cache._load_disk_cache_symbol(cache_dir, "crypto", symbol)
    if daily_df is None or daily_df.empty:
        return empty

    cutoff_date = before_ts.astimezone(_dt.timezone.utc).date()
    daily_df = daily_df[daily_df.index.date < cutoff_date]
    if daily_df.empty:
        return empty

    # ceil(n_hours_needed / 24) + 2 jours de marge : la jonction entre la portion synthétique
    # (jours calendaires UTC complets) et la portion réseau "vivante" (fenêtre glissante
    # alignée sur l'heure d'appel, jamais sur minuit UTC) laisse structurellement jusqu'à ~23h
    # de trou au jour de la jonction (même nature que la marge +48h documentée pour
    # `bot.config.HISTORY_N_HOURS`) -- 2 jours de marge couvrent ce pire cas très
    # confortablement, pour un coût nul (lecture disque déjà en mémoire, aucun appel réseau
    # supplémentaire).
    n_days_needed = -(-n_hours_needed // 24) + 2
    daily_df = daily_df.tail(n_days_needed)

    idx: list = []
    rows: list = []
    for day_ts, row in daily_df.iterrows():
        day_start = _dt.datetime(day_ts.year, day_ts.month, day_ts.day, tzinfo=_dt.timezone.utc)
        close = float(row["close"])
        for hour in range(24):
            idx.append(day_start + _dt.timedelta(hours=hour))
            rows.append({"open": close, "high": close, "low": close, "close": close, "volume": 0.0})

    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, name="ts"))[_SYNTH_HOURLY_COLUMNS].sort_index()


def get_history_crypto(symbol: str, n_hours: int) -> pd.DataFrame:
    """Voir `bot.feeds.get_history` pour le contrat complet.

    Correctif 2026-07-27 (cf. bandeau module point 2) : la portion réseau "vivante" (Binance
    klines puis repli Coinbase candles, comme avant) est désormais plafonnée à
    `_RECENT_HOURLY_WINDOW_HOURS` quel que soit `n_hours` demandé -- au-delà, la portion plus
    ancienne est complétée depuis le cache disque JOURNALIER (`_synthesize_hourly_from_daily_
    cache`), jamais par une pagination réseau profonde. Pour `n_hours <= _RECENT_HOURLY_
    WINDOW_HOURS` (cas historique de ce module, ex. tests), le comportement est
    STRICTEMENT INCHANGÉ : la fenêtre vivante couvre déjà tout `n_hours`, le cache n'est
    jamais consulté."""
    binance_pair = cfg.CRYPTO_PAIR_BINANCE.get(symbol)
    coinbase_pair = cfg.CRYPTO_PAIR_COINBASE.get(symbol)

    live_window = min(n_hours, _RECENT_HOURLY_WINDOW_HOURS)

    df = None
    if binance_pair is not None:
        df = _fetch_binance_klines(binance_pair, live_window)

    if (df is None or len(df) < live_window) and coinbase_pair is not None:
        fallback_df = _fetch_coinbase_candles(coinbase_pair, live_window)
        if fallback_df is not None and (df is None or len(fallback_df) > len(df)):
            df = fallback_df

    if df is None:
        df = pd.DataFrame(columns=_SYNTH_HOURLY_COLUMNS)

    if len(df) < n_hours:
        missing = n_hours - len(df)
        before_ts = df.index.min() if not df.empty else _now_utc()
        synthetic = _synthesize_hourly_from_daily_cache(symbol, before_ts, missing)
        if not synthetic.empty:
            df = pd.concat([synthetic, df]).sort_index()
            df = df[~df.index.duplicated(keep="last")]  # la portion vivante (réelle) prime toujours

    if df is None or len(df) < n_hours:
        got = 0 if df is None else len(df)
        raise HistoryUnavailableError(
            f"crypto {symbol}: seulement {got}/{n_hours} bougies obtenues "
            f"(réseau plafonné à {_RECENT_HOURLY_WINDOW_HOURS}h + repli cache disque journalier épuisés)"
        )

    return df.tail(n_hours)
