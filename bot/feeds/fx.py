"""bot/feeds/fx.py — taux de change EUR/USD (nouveau module, évolution multi-wallets).

Chaque wallet comptabilise en USD (les marchés crypto cotent en USD) mais a un capital
nominal en EUR (1 000 €). Ce module fournit `get_fx_rate()`, utilisé UNE SEULE FOIS par
cycle par `bot/runner.py` (taux partagé entre les 3 wallets, comme les prix crypto).

Deux sources gratuites, sans clé API, documentées comme testables depuis des runners
GitHub Actions (le réseau est bloqué dans l'environnement de développement où ce module a
été écrit — voir docs/ARCHITECTURE.md §9.2, premier vrai test réseau sur GitHub Actions) :

  1. https://api.frankfurter.app/latest?from=EUR&to=USD (source primaire)
  2. https://open.er-api.com/v6/latest/EUR (repli)

Si les deux échouent, le repli final est le DERNIER TAUX CONNU persisté dans l'état d'un
wallet (`state["fx"]["last_rate"]`), fourni par l'appelant via `last_known` — jamais
inventé ici. Le taux EUR/USD bouge peu d'un cycle horaire à l'autre : un taux de la veille
reste défendable, à condition d'être marqué explicitement `stale=True` (jamais silencieux).

Principe cardinal du projet (ARCHITECTURE.md §0.3) respecté : si aucune des trois sources
(primaire, repli réseau, dernier taux connu) ne peut fournir un taux, `get_fx_rate()`
retourne `None` — ne JAMAIS inventer un taux de change.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass
from typing import Optional

import requests

from bot.feeds._config_fallback import cfg as _feeds_cfg

logger = logging.getLogger(__name__)

try:  # bot.config est la source de vérité ; ce module reste utilisable sans lui (tests isolés).
    from bot import config as _config
except ImportError:  # pragma: no cover — garde-fou de construction en parallèle
    _config = None  # type: ignore[assignment]

FRANKFURTER_URL = getattr(_config, "FX_FRANKFURTER_URL", "https://api.frankfurter.app/latest")
ERAPI_URL = getattr(_config, "FX_ERAPI_URL", "https://open.er-api.com/v6/latest/EUR")
_HTTP_TIMEOUT_SECONDS = getattr(_config, "FX_HTTP_TIMEOUT_SECONDS", 10)

_USER_AGENT = "trading-bot-paper/1.0 (+https://github.com/mathieubigardpro-afk/trading-bot)"
_session = requests.Session()
_session.headers.update({"User-Agent": _USER_AGENT})


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(ts: _dt.datetime) -> str:
    return ts.astimezone(_dt.timezone.utc).isoformat()


@dataclass
class FxRate:
    rate: float          # 1 EUR = `rate` USD
    ts: str               # ISO8601 UTC — horodatage de la quote (heure de réception pour les
                            # sources réseau ; horodatage ORIGINAL conservé pour un repli stale)
    source: str            # "frankfurter" | "open_er_api" | "dernier_taux_connu"
    stale: bool = False   # True si ce taux provient du repli "dernier taux connu" (pas frais)


def _validate_rate(rate: object) -> Optional[float]:
    try:
        r = float(rate)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if r <= 0 or r != r or r in (float("inf"), float("-inf")):  # r!=r détecte NaN
        return None
    return r


def _fetch_frankfurter() -> Optional[FxRate]:
    try:
        resp = _session.get(
            FRANKFURTER_URL, params={"from": "EUR", "to": "USD"}, timeout=_HTTP_TIMEOUT_SECONDS
        )
        resp.raise_for_status()
        data = resp.json()
        rate = _validate_rate((data.get("rates") or {}).get("USD"))
    except (requests.RequestException, ValueError, AttributeError) as exc:
        logger.warning("fx frankfurter.app échec: %s", exc)
        return None
    if rate is None:
        logger.warning("fx frankfurter.app: taux USD absent/invalide dans la réponse")
        return None
    return FxRate(rate=rate, ts=_iso(_now_utc()), source="frankfurter", stale=False)


def _fetch_open_er_api() -> Optional[FxRate]:
    try:
        resp = _session.get(ERAPI_URL, timeout=_HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") != "success":
            logger.warning("fx open.er-api.com: result != success (%s)", data.get("result"))
            return None
        rate = _validate_rate((data.get("rates") or {}).get("USD"))
    except (requests.RequestException, ValueError, AttributeError) as exc:
        logger.warning("fx open.er-api.com échec: %s", exc)
        return None
    if rate is None:
        logger.warning("fx open.er-api.com: taux USD absent/invalide dans la réponse")
        return None
    return FxRate(rate=rate, ts=_iso(_now_utc()), source="open_er_api", stale=False)


def get_fx_rate(
    pair: str = "EURUSD",
    last_known: Optional[dict] = None,
) -> Optional[FxRate]:
    """Retourne le taux EUR/USD courant, ou `None` si strictement aucune source n'a pu en
    fournir un (ni réseau, ni dernier taux connu).

    `last_known` : dict optionnel `{"rate": float, "ts": str}` — dernier taux persisté dans
    `state["fx"]["last_rate"]`/`state["fx"]["last_rate_ts"]` d'UN wallet quelconque (le taux
    EUR/USD est le même pour tous les wallets, donc n'importe quel wallet déjà initialisé
    fait l'affaire comme source de repli). Utilisé UNIQUEMENT si les deux sources réseau
    échouent — jamais préféré à un taux frais.

    Seul `EURUSD` est supporté pour l'instant (unique paire dont ce bot a besoin) —
    `ValueError` explicite sur toute autre valeur plutôt qu'un comportement silencieux.
    """
    if pair != "EURUSD":
        raise ValueError(f"paire non supportée: {pair!r} (seule 'EURUSD' est implémentée)")

    rate = _fetch_frankfurter()
    if rate is not None:
        return rate

    rate = _fetch_open_er_api()
    if rate is not None:
        return rate

    if last_known:
        fallback_rate = _validate_rate(last_known.get("rate"))
        if fallback_rate is not None:
            fallback_ts = last_known.get("ts") or _iso(_now_utc())
            logger.warning(
                "fx: les deux sources réseau ont échoué — repli sur le dernier taux connu "
                "(%.4f, %s), marqué stale=True",
                fallback_rate, fallback_ts,
            )
            return FxRate(
                rate=fallback_rate, ts=str(fallback_ts), source="dernier_taux_connu", stale=True
            )

    logger.error(
        "fx: aucune source disponible (frankfurter + open.er-api + aucun dernier taux connu) "
        "— aucun taux EUR/USD fourni ce cycle (jamais de taux inventé)."
    )
    return None
