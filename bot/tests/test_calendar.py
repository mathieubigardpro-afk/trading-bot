"""Tests de `bot.feeds.calendar.is_us_market_open` — cas connus, aucun
appel réseau (le calendrier est 100% en dur)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.feeds.calendar import NYSE_HOLIDAYS, is_us_market_open

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def ny(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=NY)


def test_tuesday_during_session_is_open():
    # Mardi 21 juillet 2026, 14:00 heure de New York — jour ouvré normal.
    assert datetime(2026, 7, 21).weekday() == 1  # mardi
    assert is_us_market_open(ny(2026, 7, 21, 14, 0)) is True


def test_tuesday_14h_et_open_matches_task_reference_case():
    # Cas explicitement demandé dans le brief: "un mardi 14h ET".
    dt = ny(2026, 7, 21, 14, 0)
    assert is_us_market_open(dt) is True


def test_saturday_is_closed():
    # Samedi générique, hors tout jour férié.
    d = ny(2026, 8, 1, 12, 0)
    assert d.weekday() == 5
    assert is_us_market_open(d) is False


def test_july_4th_2026_saturday_is_closed():
    # Cas explicitement demandé: "4 juillet". En 2026 le 4 juillet tombe un
    # samedi -> fermé par le week-end (et le jour férié observé est le 3).
    d = ny(2026, 7, 4, 12, 0)
    assert d.weekday() == 5
    assert is_us_market_open(d) is False


def test_independence_day_observed_2026_is_closed():
    # Vendredi 3 juillet 2026 = jour férié NYSE observé (4 juillet -> samedi).
    d = ny(2026, 7, 3, 12, 0)
    assert d.weekday() == 4  # vendredi
    assert is_us_market_open(d) is False


def test_independence_day_observed_2027_is_closed():
    # 2027: le 4 juillet tombe un dimanche -> observé le lundi 5 juillet.
    d = ny(2027, 7, 5, 12, 0)
    assert d.weekday() == 0  # lundi
    assert is_us_market_open(d) is False
    # Le dimanche 4 lui-même est fermé de toute façon (week-end).
    assert is_us_market_open(ny(2027, 7, 4, 12, 0)) is False


def test_juneteenth_observed_2027_is_closed():
    # 19 juin 2027 tombe un samedi -> observé le vendredi 18.
    d = ny(2027, 6, 18, 12, 0)
    assert d.weekday() == 4
    assert is_us_market_open(d) is False


def test_christmas_observed_2027_is_closed():
    # 25 décembre 2027 tombe un samedi -> observé le vendredi 24.
    d = ny(2027, 12, 24, 12, 0)
    assert d.weekday() == 4
    assert is_us_market_open(d) is False


def test_thanksgiving_2026_is_closed():
    d = ny(2026, 11, 26, 12, 0)
    assert d.weekday() == 3  # jeudi
    assert is_us_market_open(d) is False


def test_good_friday_2026_is_closed():
    d = ny(2026, 4, 3, 12, 0)
    assert is_us_market_open(d) is False


def test_boundary_open_at_0930_inclusive():
    assert is_us_market_open(ny(2026, 7, 21, 9, 30)) is True


def test_boundary_closed_before_0930():
    assert is_us_market_open(ny(2026, 7, 21, 9, 29)) is False


def test_boundary_closed_at_1600_exclusive():
    assert is_us_market_open(ny(2026, 7, 21, 16, 0)) is False


def test_boundary_open_just_before_1600():
    assert is_us_market_open(ny(2026, 7, 21, 15, 59)) is True


def test_utc_input_converted_correctly_to_ny_time():
    # 18:00 UTC en été (EDT, UTC-4) = 14:00 heure de New York -> ouvert.
    d = datetime(2026, 7, 21, 18, 0, tzinfo=UTC)
    assert is_us_market_open(d) is True
    # 13:00 UTC = 09:00 ET -> fermé (avant l'ouverture).
    d2 = datetime(2026, 7, 21, 13, 0, tzinfo=UTC)
    assert is_us_market_open(d2) is False


def test_naive_datetime_raises_value_error():
    with pytest.raises(ValueError):
        is_us_market_open(datetime(2026, 7, 21, 14, 0))


def test_holidays_set_is_non_empty_and_covers_both_years():
    years = {d.year for d in NYSE_HOLIDAYS}
    assert years == {2026, 2027}
    assert len(NYSE_HOLIDAYS) == 20  # 10 jours fériés/an * 2 ans
