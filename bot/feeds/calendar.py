"""Calendrier NYSE simplifié — aucun appel réseau.

`is_us_market_open(ts)` : True si `ts` (tz-aware) tombe un jour ouvré NYSE,
entre 09:30 et 16:00 America/New_York, hors jours fériés NYSE. Les demi-séances
(veille de Thanksgiving, veille de Noël quand elle tombe un jour ouvré, etc.)
sont IGNORÉES par choix explicite du projet : ces jours sont traités comme des
séances pleines 09:30-16:00, jamais comme fermées ou raccourcies. Documenté ici
pour éviter toute confusion future.

Liste des jours fériés maintenue en dur, mise à jour manuelle annuelle
attendue (couvre 2026 et 2027 à ce jour). Les règles d'observation standard
sont déjà appliquées dans les dates ci-dessous (ex. jour férié tombant un
samedi -> observé le vendredi précédent ; tombant un dimanche -> observé le
lundi suivant).
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")

MARKET_OPEN_TIME = time(9, 30)
MARKET_CLOSE_TIME = time(16, 0)

# --- 2026 ---
NYSE_HOLIDAYS_2026 = {
    date(2026, 1, 1),   # New Year's Day (jeudi)
    date(2026, 1, 19),  # Martin Luther King Jr. Day (3e lundi de janvier)
    date(2026, 2, 16),  # Washington's Birthday / Presidents Day (3e lundi de février)
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day (dernier lundi de mai)
    date(2026, 6, 19),  # Juneteenth (vendredi, jour même)
    date(2026, 7, 3),   # Independence Day observé (4 juillet tombe un samedi -> observé le vendredi 3)
    date(2026, 9, 7),   # Labor Day (1er lundi de septembre)
    date(2026, 11, 26), # Thanksgiving Day (4e jeudi de novembre)
    date(2026, 12, 25), # Christmas Day (vendredi, jour même)
}

# --- 2027 ---
NYSE_HOLIDAYS_2027 = {
    date(2027, 1, 1),   # New Year's Day (vendredi, jour même)
    date(2027, 1, 18),  # Martin Luther King Jr. Day
    date(2027, 2, 15),  # Washington's Birthday / Presidents Day
    date(2027, 3, 26),  # Good Friday
    date(2027, 5, 31),  # Memorial Day
    date(2027, 6, 18),  # Juneteenth observé (19 juin tombe un samedi -> observé le vendredi 18)
    date(2027, 7, 5),   # Independence Day observé (4 juillet tombe un dimanche -> observé le lundi 5)
    date(2027, 9, 6),   # Labor Day
    date(2027, 11, 25), # Thanksgiving Day
    date(2027, 12, 24), # Christmas observé (25 décembre tombe un samedi -> observé le vendredi 24)
}

NYSE_HOLIDAYS = NYSE_HOLIDAYS_2026 | NYSE_HOLIDAYS_2027


def is_us_market_open(ts: datetime) -> bool:
    """True si `ts` tombe dans une séance régulière NYSE (09:30-16:00
    America/New_York, jour ouvré, hors jours fériés listés ci-dessus).

    `ts` DOIT être tz-aware (UTC ou toute autre timezone valide) — une
    datetime naïve est une erreur de programmation en amont, pas un cas à
    deviner silencieusement ; on lève ValueError plutôt que de supposer UTC.
    """
    if ts.tzinfo is None:
        raise ValueError(
            "is_us_market_open() requiert une datetime tz-aware (naive datetime reçue)"
        )

    ts_ny = ts.astimezone(NY_TZ)

    if ts_ny.weekday() >= 5:  # 5=samedi, 6=dimanche
        return False

    if ts_ny.date() in NYSE_HOLIDAYS:
        return False

    t = ts_ny.time()
    return MARKET_OPEN_TIME <= t < MARKET_CLOSE_TIME
