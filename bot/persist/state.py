"""bot/persist/state.py — état persistant du bot (`state/state.json`).

Rôles de ce module (ARCHITECTURE.md §3.1, §4.3, §4.4, §5.4) :
  - `init_state()` : état initial (100 000 $ cash, aucune position) pour le tout premier run
    de l'histoire du dépôt.
  - `load_state()` : lit `state.json` s'il existe, avec **validation de schéma stricte** —
    un fichier corrompu ou incomplet lève `StateValidationError`, il n'y a JAMAIS de repli
    silencieux sur des valeurs par défaut pour un fichier qui existe mais est invalide (le
    repli sur `init_state()` ne s'applique qu'à l'ABSENCE du fichier, cas légitime du tout
    premier run).
  - `save_state()` : écriture atomique (tmp + os.replace), et valide le state avant de l'écrire
    (on n'écrit jamais un state cassé sur disque).
  - `compute_state_hash()` : sha256 du JSON canonique (clés triées), utilisé pour la chaîne
    d'intégrité `state_hash_prev`.
  - `is_run_already_done()` : petit utilitaire d'idempotence (ARCHITECTURE.md §4.2) exposé ici
    pour que `runner.py` (et les tests) n'aient pas à ré-implémenter la comparaison de run_id.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any

from ._config_fallback import cfg

# Sentinelle utilisée comme `state_hash_prev` du tout premier état (pas de parent réel à
# hasher) — sha256 de la chaîne vide, convention reprise de l'exemple ARCHITECTURE.md §3.1.
GENESIS_HASH = hashlib.sha256(b"").hexdigest()

SCHEMA_VERSION = 1

_CB_BOOL_FIELDS = ("flatten_mode", "manual_review_required", "dd_half_size_active")
_CB_TS_OR_NONE_FIELDS = ("daily_loss_freeze_until", "cooldown_until")
# `equity_window_24h` : champ OPTIONNEL ajouté par `bot.risk.circuit_breakers.evaluate_breakers`
# (fenêtre glissante d'équity servant à calculer la perte 24h glissante sans dépendre d'une
# série externe non fournie à RiskManager.apply — voir docstring de ce module). Absent d'un
# state.json produit avant l'intégration de bot/risk (ex. init_state()) : c'est pour cela
# qu'il n'est PAS dans `_CB_BOOL_FIELDS`/`_CB_TS_OR_NONE_FIELDS` (jamais requis), seulement
# toléré et validé s'il est présent.
_CB_OPTIONAL_FIELDS = ("equity_window_24h",)


class StateValidationError(Exception):
    """state.json existe mais est corrompu, incomplet, ou mal typé.

    Ne JAMAIS attraper cette exception pour retomber silencieusement sur des valeurs par
    défaut : c'est le comportement voulu (principe cardinal pessimiste — un état suspect ne
    doit jamais être "réparé" en silence, cf. ARCHITECTURE.md §0). Laisser remonter au runner,
    qui doit s'arrêter en erreur explicite plutôt que de trader sur un état halluciné.
    """


def _fail(msg: str) -> None:
    raise StateValidationError(msg)


def init_state(wallet_id: str = "default", capital_initial_eur: float = 1000.0) -> dict:
    """Construit l'état initial en mémoire (aucune écriture disque, fonction pure).

    Utilisé automatiquement par `load_state()` quand `state.json` n'existe pas encore (tout
    premier run pour ce wallet), et disponible en appel direct (scripts d'audit, tests,
    initialisation manuelle explicite).

    Multi-wallets (voir docs/ARCHITECTURE.md §9) : un wallet naît toujours NON INITIALISÉ
    (`cash_usd=0.0`, `fx.initial_rate=None`) — le capital USD réel (`capital_initial_eur`
    converti au taux EUR/USD du tout premier cycle où un taux est disponible) n'est fixé que
    par `bot/runner.py`, jamais inventé ici. `wallet_id="default"` et
    `capital_initial_eur=1000.0` restent les valeurs par défaut pour les appels génériques
    (tests bas niveau de `bot.persist` qui ne portent pas sur la logique multi-wallets).
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "wallet_id": str(wallet_id),
        "initial_eur": float(capital_initial_eur),
        "last_run_id": None,
        "last_run_completed_at": None,
        "state_hash_prev": GENESIS_HASH,
        "cash_usd": 0.0,
        "positions": {},
        "equity_peak_usd": 0.0,
        "equity_peak_ts": None,
        "realized_pnl_cumulative_usd": 0.0,
        "fx": {
            "initial_rate": None,     # EUR/USD figé au tout premier cycle initialisé, jamais réécrit ensuite
            "last_rate": None,        # dernier taux EUR/USD connu (frais ou stale), pour repli
            "last_rate_ts": None,
            "last_rate_source": None,  # "frankfurter" | "open_er_api" | "dernier_taux_connu" | None
            "last_rate_stale": False,
        },
        "circuit_breakers": {
            "flatten_mode": False,
            "manual_review_required": False,
            "daily_loss_freeze_until": None,
            "cooldown_until": None,
            "consecutive_losses": 0,
            "dd_half_size_active": False,
        },
        "trade_history_for_breakers": [],
    }


def _require(state: dict, key: str, types, field_label: str | None = None) -> Any:
    label = field_label or key
    if key not in state:
        _fail(f"champ obligatoire manquant dans state.json : '{label}'")
    value = state[key]
    # bool est une sous-classe d'int en Python : on refuse explicitement qu'un booléen se
    # fasse passer pour un nombre/une chaîne attendue ailleurs (ex. cash_usd=True).
    if isinstance(value, bool) and types is not bool and not (isinstance(types, tuple) and bool in types):
        _fail(f"champ '{label}' : type invalide (booléen reçu, attendu {types})")
    if not isinstance(value, types):
        _fail(f"champ '{label}' : type invalide, attendu {types}, reçu {type(value).__name__}")
    return value


def _require_finite_number(state: dict, key: str, *, positive: bool = False, strictly_positive: bool = False) -> float:
    value = _require(state, key, (int, float))
    if not math.isfinite(value):
        _fail(f"champ '{key}' : doit être un nombre fini, reçu {value!r}")
    if strictly_positive and value <= 0:
        _fail(f"champ '{key}' : doit être strictement positif, reçu {value!r}")
    if positive and value < 0:
        _fail(f"champ '{key}' : doit être positif ou nul, reçu {value!r}")
    return value


def validate_schema(state: Any) -> None:
    """Valide strictement la forme de `state`.

    Lève `StateValidationError` au premier problème rencontré, avec un message précis
    identifiant le champ fautif. Ne renvoie jamais de valeur par défaut à la place d'un champ
    manquant ou invalide — c'est le contrat central de ce module.
    """
    if not isinstance(state, dict):
        _fail(f"state.json doit contenir un objet JSON (dict), reçu {type(state).__name__}")

    schema_version = _require(state, "schema_version", int)
    if schema_version != SCHEMA_VERSION:
        _fail(f"schema_version non supporté : {schema_version!r} (attendu {SCHEMA_VERSION})")

    wallet_id = _require(state, "wallet_id", str)
    if not wallet_id:
        _fail("champ 'wallet_id' : ne doit pas être vide")

    _require_finite_number(state, "initial_eur", strictly_positive=True)

    _require(state, "last_run_id", (str, type(None)))
    _require(state, "last_run_completed_at", (str, type(None)))

    state_hash_prev = _require(state, "state_hash_prev", (str, type(None)))
    if state_hash_prev is not None:
        if len(state_hash_prev) != 64 or any(c not in "0123456789abcdef" for c in state_hash_prev.lower()):
            _fail("champ 'state_hash_prev' : doit être un hex sha256 de 64 caractères (ou null)")

    _require_finite_number(state, "cash_usd")

    positions = _require(state, "positions", dict)
    for symbol, pos in positions.items():
        if not isinstance(symbol, str) or not symbol:
            _fail(f"positions : clé de symbole invalide {symbol!r}")
        if not isinstance(pos, dict):
            _fail(f"positions['{symbol}'] : doit être un objet, reçu {type(pos).__name__}")
        for pk in ("qty", "prix_moyen"):
            if pk not in pos:
                _fail(f"positions['{symbol}'] : champ manquant '{pk}'")
            pv = pos[pk]
            if isinstance(pv, bool) or not isinstance(pv, (int, float)):
                _fail(f"positions['{symbol}']['{pk}'] : doit être numérique")
            if not math.isfinite(pv):
                _fail(f"positions['{symbol}']['{pk}'] : doit être fini")
            if pv <= 0:
                _fail(
                    f"positions['{symbol}']['{pk}'] : doit être > 0 (une position soldée à "
                    "zéro doit être retirée de l'objet 'positions', pas mise à qty=0)"
                )

    # Multi-wallets : un wallet non encore initialisé (fx.initial_rate=None, en attente d'un
    # taux EUR/USD) a légitimement equity_peak_usd=0.0 (aucun capital converti pour l'instant)
    # — assoupli de "strictement positif" à "positif ou nul" par rapport au schéma pré-wallets
    # (voir docs/ARCHITECTURE.md §9.1).
    _require_finite_number(state, "equity_peak_usd", positive=True)
    _require(state, "equity_peak_ts", (str, type(None)))
    _require_finite_number(state, "realized_pnl_cumulative_usd")

    fx = _require(state, "fx", dict)
    fx_rate = _require(fx, "initial_rate", (int, float, type(None)))
    if fx_rate is not None:
        if isinstance(fx_rate, bool) or not math.isfinite(fx_rate) or fx_rate <= 0:
            _fail("fx.initial_rate : doit être un nombre fini strictement positif, ou null")
    fx_last_rate = _require(fx, "last_rate", (int, float, type(None)))
    if fx_last_rate is not None:
        if isinstance(fx_last_rate, bool) or not math.isfinite(fx_last_rate) or fx_last_rate <= 0:
            _fail("fx.last_rate : doit être un nombre fini strictement positif, ou null")
    _require(fx, "last_rate_ts", (str, type(None)))
    _require(fx, "last_rate_source", (str, type(None)))
    fx_stale = _require(fx, "last_rate_stale", bool)
    extra_fx_keys = set(fx) - {"initial_rate", "last_rate", "last_rate_ts", "last_rate_source", "last_rate_stale"}
    if extra_fx_keys:
        _fail(f"fx : champ(s) inattendu(s) {sorted(extra_fx_keys)}")
    del fx_stale  # déjà validé par _require, juste pour lisibilité (pas de contrainte de valeur)

    cb = _require(state, "circuit_breakers", dict)
    for f in _CB_BOOL_FIELDS:
        if f not in cb:
            _fail(f"circuit_breakers.{f} manquant")
        if not isinstance(cb[f], bool):
            _fail(f"circuit_breakers.{f} : doit être un booléen, reçu {type(cb[f]).__name__}")
    for f in _CB_TS_OR_NONE_FIELDS:
        if f not in cb:
            _fail(f"circuit_breakers.{f} manquant")
        if cb[f] is not None and not isinstance(cb[f], str):
            _fail(f"circuit_breakers.{f} : doit être une chaîne ISO8601 ou null")
    if "consecutive_losses" not in cb:
        _fail("circuit_breakers.consecutive_losses manquant")
    cl = cb["consecutive_losses"]
    if isinstance(cl, bool) or not isinstance(cl, int) or cl < 0:
        _fail("circuit_breakers.consecutive_losses : doit être un entier >= 0")

    if "equity_window_24h" in cb:
        window = cb["equity_window_24h"]
        if not isinstance(window, list):
            _fail("circuit_breakers.equity_window_24h : doit être une liste")
        for i, entry in enumerate(window):
            if not isinstance(entry, dict):
                _fail(f"circuit_breakers.equity_window_24h[{i}] : doit être un objet")
            if "ts" not in entry or not isinstance(entry["ts"], str) or not entry["ts"]:
                _fail(f"circuit_breakers.equity_window_24h[{i}] : 'ts' doit être une chaîne non vide")
            if "equity" not in entry:
                _fail(f"circuit_breakers.equity_window_24h[{i}] : champ manquant 'equity'")
            eq = entry["equity"]
            if isinstance(eq, bool) or not isinstance(eq, (int, float)) or not math.isfinite(eq):
                _fail(f"circuit_breakers.equity_window_24h[{i}] : 'equity' doit être numérique fini")

    extra_cb_keys = (
        set(cb)
        - set(_CB_BOOL_FIELDS)
        - set(_CB_TS_OR_NONE_FIELDS)
        - set(_CB_OPTIONAL_FIELDS)
        - {"consecutive_losses"}
    )
    if extra_cb_keys:
        _fail(f"circuit_breakers : champ(s) inattendu(s) {sorted(extra_cb_keys)}")

    history = _require(state, "trade_history_for_breakers", list)
    for i, entry in enumerate(history):
        if not isinstance(entry, dict):
            _fail(f"trade_history_for_breakers[{i}] : doit être un objet")
        for fld in ("ts", "symbol", "realized_pnl_usd"):
            if fld not in entry:
                _fail(f"trade_history_for_breakers[{i}] : champ manquant '{fld}'")
        if not isinstance(entry["ts"], str) or not entry["ts"]:
            _fail(f"trade_history_for_breakers[{i}] : 'ts' doit être une chaîne non vide")
        if not isinstance(entry["symbol"], str) or not entry["symbol"]:
            _fail(f"trade_history_for_breakers[{i}] : 'symbol' doit être une chaîne non vide")
        pnl = entry["realized_pnl_usd"]
        if isinstance(pnl, bool) or not isinstance(pnl, (int, float)) or not math.isfinite(pnl):
            _fail(f"trade_history_for_breakers[{i}] : 'realized_pnl_usd' doit être numérique fini")


def load_state(path: str | None = None) -> dict:
    """Charge et valide `state.json`.

    - Fichier absent (tout premier run de l'histoire du dépôt) : retourne `init_state()`,
      c'est le SEUL cas de valeur par défaut silencieuse, et il est explicitement documenté
      (ARCHITECTURE.md §5.4) — ce n'est pas une donnée corrompue, c'est l'absence légitime
      d'historique.
    - Fichier présent mais JSON invalide, ou schéma incomplet/mal typé : lève
      `StateValidationError`. Ne JAMAIS retomber sur des valeurs par défaut dans ce cas.
    """
    path = path or cfg.STATE_JSON
    if not os.path.exists(path):
        return init_state()

    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail_msg = f"state.json corrompu : JSON invalide à {path} ({exc})"
        raise StateValidationError(_fail_msg) from exc

    validate_schema(state)
    return state


def save_state(state: dict, path: str | None = None) -> None:
    """Écriture atomique de `state` dans `path` (tmp + os.replace, même filesystem).

    Valide le schéma avant d'écrire quoi que ce soit sur disque : on ne persiste jamais un
    état invalide, même transitoirement.
    """
    validate_schema(state)
    path = path or cfg.STATE_JSON
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    tmp_path = f"{path}.tmp"
    canonical = json.dumps(state, sort_keys=True, indent=2, ensure_ascii=False)
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(canonical)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, path)


def compute_state_hash(state: dict) -> str:
    """sha256 hex du JSON canonique de `state` (clés triées, séparateurs compacts).

    Utilisé pour la chaîne d'intégrité : chaque nouvel état porte dans `state_hash_prev` le
    hash de l'état tel qu'il existait au tout début du run qui l'a produit (voir
    `bot.persist.audit.verify_chain` pour l'audit rétroactif via l'historique git).
    """
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_run_already_done(state: dict, run_id: str) -> bool:
    """True si `state["last_run_id"] == run_id` (ARCHITECTURE.md §4.2, règle d'idempotence).

    Le runner doit appeler ceci juste après `load_state()`, avant tout appel réseau ou
    construction de signal, et sortir immédiatement (code 0) si True.
    """
    return state.get("last_run_id") == run_id
