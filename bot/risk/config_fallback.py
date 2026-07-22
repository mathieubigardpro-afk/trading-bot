"""Repli local des constantes de risque nécessaires à `bot.risk`.

`bot/config.py` (module central, construit potentiellement en parallèle par un autre agent)
est la source de vérité documentée dans `docs/ARCHITECTURE.md` §2 pour l'univers d'actifs,
les coûts, et les valeurs "génériques" du vol-targeting/circuit breakers. Ce fichier permet à
`bot.risk` de fonctionner et d'être testé de façon autonome si `bot/config.py` n'existe pas
encore (comme `bot/feeds/_config_fallback.py` le fait déjà pour `bot.feeds`) — ET fixe les
valeurs du CALIBRAGE AGRESSIF explicitement mandaté pour ce module, qui diffèrent
volontairement des valeurs "génériques" citées dans `docs/rapport-recherche.md` §3C et reprises
telles quelles dans le tableau de constantes de `docs/ARCHITECTURE.md` §2 :

  - perte 24h glissante > **4%** (pas 3%) -> gel des nouvelles entrées 24h ;
  - drawdown > **20%** (pas 15%) depuis le pic -> tailles cibles réduites de moitié ;
  - drawdown > **30%** (pas 25%) -> flatten total + revue manuelle obligatoire (flag state.json).

Ces trois seuils sont EXPLICITEMENT mandatés par la spécification de ce module ("Circuit
breakers NON NÉGOCIABLES ... l'agressivité passe par la taille, pas par leur suppression") pour
le profil AGRESSIF du projet, et priment donc ici sur les valeurs génériques du rapport — les
breakers eux-mêmes (leur existence, pas leur seuil) restent inchangés, conformément à la
consigne. Le compteur de pertes consécutives (5 -> cooldown 24h) n'est pas mentionné
explicitement dans la liste "NON NÉGOCIABLE" mais fait partie du schéma `state.json` documenté
dans ARCHITECTURE.md (`consecutive_losses`, `cooldown_until`) et du framework §3C du rapport :
implémenté ici avec les valeurs du rapport (non recalibrées, faute d'instruction contraire).

Le cap par actif diffère aussi par classe d'actif (25% crypto / 15% action), alors que
`docs/ARCHITECTURE.md` §2 ne prévoit qu'un seul `CAP_PER_ASSET` unique (25%) — repris ici comme
`CAP_PER_ASSET_CRYPTO` / `CAP_PER_ASSET_EQUITY` distincts, également mandatés explicitement par
la spécification de ce module.

Priorité : si `bot/config.py` définit un jour l'un de ces attributs, sa valeur est utilisée à
la place du défaut ci-dessous (source de vérité centrale) — tant qu'il ne le fait pas (ou pas
encore, le module central étant construit en parallèle), les défauts agressifs s'appliquent.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

_DEFAULTS = {
    # --- Univers (pour classer crypto vs action lors du cap par actif) ---
    "SYMBOLS_CRYPTO": ["BTC", "ETH", "SOL", "DOGE", "LINK", "AVAX"],
    "SYMBOLS_EQUITY": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META"],

    # --- Vol targeting portefeuille ---
    "VOL_TARGET_ANNUALIZED": 0.275,          # point médian de la fourchette 25-30% (rapport §3A)
    "VOL_EWMA_HALFLIFE_HOURS": 60,           # milieu de la fourchette 48-72h — voir manager.py
    "VOL_COLDSTART_MIN_POINTS": 30,
    "VOL_COLDSTART_SCALAR": 0.5,

    # --- Caps et exposition ---
    "CAP_PER_ASSET_CRYPTO": 0.25,
    "CAP_PER_ASSET_EQUITY": 0.15,
    "GROSS_EXPOSURE_MAX": 0.80,
    "NO_TRADE_BAND": 0.05,

    # --- Circuit breakers (calibrage AGRESSIF explicite — voir docstring ci-dessus) ---
    "CB_DAILY_LOSS_FREEZE_PCT": 0.04,
    "CB_DAILY_LOSS_FREEZE_HOURS": 24,
    "CB_CONSECUTIVE_LOSSES_TRIGGER": 5,
    "CB_COOLDOWN_HOURS": 24,
    "CB_DD_HALF_SIZE_PCT": 0.20,
    "CB_DD_FLATTEN_PCT": 0.30,
}


def _load() -> SimpleNamespace:
    try:
        real_config = importlib.import_module("bot.config")
    except ImportError:
        real_config = None

    values = dict(_DEFAULTS)
    if real_config is not None:
        for key in _DEFAULTS:
            if hasattr(real_config, key):
                values[key] = getattr(real_config, key)
    return SimpleNamespace(**values)


# Résolu une fois à l'import, même pattern que bot/feeds/_config_fallback.py. Les tests qui
# veulent forcer le repli peuvent recharger ce module après avoir masqué bot.config.
cfg = _load()
