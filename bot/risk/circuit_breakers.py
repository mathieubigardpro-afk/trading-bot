"""Circuit breakers — calibrage AGRESSIF (voir `config_fallback.py` pour la justification des
seuils recalibrés). Quatre mécanismes, tous INCHANGÉS dans leur EXISTENCE (l'agressivité du
profil passe par la taille des positions et le vol targeting, jamais par la suppression ou
l'affaiblissement d'un garde-fou) :

  1. Perte 24h glissante > `CB_DAILY_LOSS_FREEZE_PCT` (4%) -> gel des nouvelles entrées 24h
     (les réductions/sorties de position restent toujours autorisées).
  2. `CB_CONSECUTIVE_LOSSES_TRIGGER` (5) pertes réalisées consécutives -> cooldown
     `CB_COOLDOWN_HOURS` (24h), même traitement que le gel ci-dessus côté sizing.
  3. Drawdown > `CB_DD_HALF_SIZE_PCT` (20%) depuis le pic -> tailles cibles réduites de moitié.
  4. Drawdown > `CB_DD_FLATTEN_PCT` (30%) -> flatten total + `manual_review_required=True`,
     que **seul un humain** éditant `state.json` peut repasser à `False` (ARCHITECTURE.md §3.1)
     — ce module ne lève JAMAIS lui-même `flatten_mode`/`manual_review_required` une fois activés.

Frontière de responsabilité explicite : `consecutive_losses` (compteur de pertes réalisées
consécutives) est incrémenté/remis à zéro par le module qui applique les fills après passage
d'ordres — hors du périmètre de `bot/risk` : `RiskManager.apply()` est appelé AVANT tout ordre
du cycle courant et n'a connaissance d'aucun fill. Ce module se contente de LIRE ce compteur
(persisté par un run précédent dans `state.json`) pour décider d'ouvrir/lever le cooldown
associé — voir docstring de `RiskManager` dans `manager.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

DEFAULT_CB_CONFIG = {
    "CB_DAILY_LOSS_FREEZE_PCT": 0.04,
    "CB_DAILY_LOSS_FREEZE_HOURS": 24,
    "CB_CONSECUTIVE_LOSSES_TRIGGER": 5,
    "CB_COOLDOWN_HOURS": 24,
    "CB_DD_HALF_SIZE_PCT": 0.20,
    "CB_DD_FLATTEN_PCT": 0.30,
}


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def default_breaker_state() -> dict:
    return {
        "flatten_mode": False,
        "manual_review_required": False,
        "daily_loss_freeze_until": None,
        "cooldown_until": None,
        "consecutive_losses": 0,
        "dd_half_size_active": False,
        "equity_window_24h": [],
    }


def compute_drawdown_pct(equity_now: float, equity_peak: float) -> float:
    """`(pic - équity) / pic`, borné à [0, 1]. 0.0 si pas de pic valide (>0) encore connu."""
    if equity_peak is None or equity_peak <= 0:
        return 0.0
    dd = (float(equity_peak) - float(equity_now)) / float(equity_peak)
    return max(0.0, min(1.0, dd))


def update_equity_window(cb_state: dict, now: datetime, equity_now: float, window_hours: float) -> list:
    """Ajoute le point d'équity courant à la fenêtre glissante et purge tout point plus vieux
    que `window_hours`. Cette fenêtre est le mécanisme interne qui permet à ce module de
    calculer une perte 24h glissante SANS dépendre d'une série d'équity externe non fournie à
    `apply()` (voir docstring de `vol_targeting.py` point 2 pour la même contrainine côté vol)."""
    window = list(cb_state.get("equity_window_24h") or [])
    window.append({"ts": now.isoformat(), "equity": float(equity_now)})
    cutoff = now - timedelta(hours=window_hours)
    pruned = []
    for entry in window:
        ts = _parse_ts(entry.get("ts"))
        if ts is not None and ts >= cutoff:
            pruned.append(entry)
    return pruned


def compute_daily_loss_pct(window: list, equity_now: float) -> float:
    """Perte glissante sur la fenêtre : compare l'équity actuelle à la plus ancienne valeur
    encore présente dans la fenêtre (proxy de "il y a ~24h" avec des runs horaires). Retourne
    0.0 si la fenêtre a moins de 2 points (pas assez d'historique pour juger — pessimiste au
    sens où on ne bloque rien sans preuve, mais on ne peut pas non plus inventer un point)."""
    if not window or len(window) < 2:
        return 0.0
    oldest = window[0].get("equity")
    if oldest is None or oldest <= 0:
        return 0.0
    return (float(oldest) - float(equity_now)) / float(oldest)


def evaluate_breakers(
    cb_state_in: dict,
    now: datetime,
    equity_now: float,
    equity_peak: float,
    daily_loss_freeze_pct: float,
    daily_loss_freeze_hours: float,
    consecutive_losses_trigger: int,
    cooldown_hours: float,
    dd_half_size_pct: float,
    dd_flatten_pct: float,
) -> Tuple[dict, dict]:
    """Calcule le nouvel état persistant des breakers pour ce cycle, à partir de l'équity
    courante/du pic et de l'état chargé depuis `state.json`. Retourne `(cb_state, flags)` où
    `flags` = `{"freeze_entries", "half_size", "flatten", "manual_review", "daily_loss_pct",
    "drawdown_pct", "cooldown_active"}`, à appliquer immédiatement au sizing de CE cycle par
    `RiskManager.apply()`.

    Seuils déclenchés strictement AU-DESSUS (`>`), jamais à l'égalité — "se déclenche au bon
    seuil et pas avant" au sens strict.
    """
    cb = dict(cb_state_in or {})
    for key, default in default_breaker_state().items():
        cb.setdefault(key, default)

    # --- 1) fenêtre glissante d'équity + perte 24h -> gel des nouvelles entrées ---
    window = update_equity_window(cb, now, equity_now, window_hours=daily_loss_freeze_hours)
    cb["equity_window_24h"] = window
    daily_loss_pct = compute_daily_loss_pct(window, equity_now)

    if daily_loss_pct > daily_loss_freeze_pct:
        freeze_until = now + timedelta(hours=daily_loss_freeze_hours)
        current_until = _parse_ts(cb.get("daily_loss_freeze_until"))
        if current_until is None or freeze_until > current_until:
            cb["daily_loss_freeze_until"] = freeze_until.isoformat()

    freeze_until_dt = _parse_ts(cb.get("daily_loss_freeze_until"))
    freeze_entries = freeze_until_dt is not None and now < freeze_until_dt
    if freeze_until_dt is not None and now >= freeze_until_dt:
        cb["daily_loss_freeze_until"] = None

    # --- 2) cooldown pertes consécutives (compteur lu, jamais calculé ici) ---
    # Expiration vérifiée EN PREMIER : un cooldown déjà expiré doit purger le compteur avant
    # d'évaluer un nouveau déclenchement, sinon un compteur externe resté à sa valeur de
    # déclenchement (jamais remis à zéro par le module de fills, ex. absence de nouveau trade)
    # relancerait indéfiniment un cooldown déjà écoulé.
    existing_cooldown_until_dt = _parse_ts(cb.get("cooldown_until"))
    if existing_cooldown_until_dt is not None and now >= existing_cooldown_until_dt:
        cb["cooldown_until"] = None
        cb["consecutive_losses"] = 0

    consecutive_losses = int(cb.get("consecutive_losses", 0))
    if consecutive_losses >= consecutive_losses_trigger:
        cooldown_until = now + timedelta(hours=cooldown_hours)
        current_cd = _parse_ts(cb.get("cooldown_until"))
        if current_cd is None or cooldown_until > current_cd:
            cb["cooldown_until"] = cooldown_until.isoformat()

    cooldown_until_dt = _parse_ts(cb.get("cooldown_until"))
    cooldown_active = cooldown_until_dt is not None and now < cooldown_until_dt

    # --- 3) & 4) drawdown depuis le pic ---
    dd_pct = compute_drawdown_pct(equity_now, equity_peak)
    half_size_now = dd_pct > dd_half_size_pct
    flatten_now = dd_pct > dd_flatten_pct

    cb["dd_half_size_active"] = bool(half_size_now or flatten_now)
    if flatten_now:
        cb["flatten_mode"] = True
        cb["manual_review_required"] = True
    else:
        # flatten_mode / manual_review_required sont STICKY : une fois activés, seul un humain
        # les repasse à False en éditant state.json (ARCHITECTURE.md §3.1) — on préserve donc
        # la valeur déjà persistée plutôt que de la recalculer depuis le drawdown courant.
        cb["flatten_mode"] = bool(cb.get("flatten_mode", False))
        cb["manual_review_required"] = bool(cb.get("manual_review_required", False))

    flags = {
        "freeze_entries": bool(freeze_entries or cooldown_active),
        "half_size": bool(cb["dd_half_size_active"]),
        "flatten": bool(cb["flatten_mode"]),
        "manual_review": bool(cb["manual_review_required"]),
        "daily_loss_pct": daily_loss_pct,
        "drawdown_pct": dd_pct,
        "cooldown_active": cooldown_active,
    }
    return cb, flags
