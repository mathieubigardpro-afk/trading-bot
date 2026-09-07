"""backtest/tests/test_seasonality_signal.py — tests unitaires du générateur de signal de
`backtest/strategies/seasonality.py` (candidate `btc_seasonality_2123utc`,
`backtest/SEASONALITY-BTC-SPEC.md` §1, mission point 7).

Deux propriétés vérifiées :
  1. Mapping heure -> poids correct sur un calendrier RÉGULIER (sans trou) : `poids[i] == 1.0`
     ssi `calendar[i+1].hour in {21, 22}`, `0.0` sinon (y compris la toute dernière ligne, jamais
     consommée par le moteur -- cf. docstring `generate_weight_decisions`).
  2. Causalité triviale + suivi de l'HORLOGE RÉELLE, pas d'une heure théorique : sur un calendrier
     SYNTHÉTIQUE comportant un TROU de données exactement à 21h un jour donné, le poids décidé à
     la bougie qui PRÉCÈDE le trou doit suivre l'heure de la bougie i+1 RÉELLEMENT présente dans
     le calendrier fourni (22h, la bougie suivante réelle) et non une heure "21h" hypothétique
     qui n'existe pas dans les données -- démontrant que `generate_weight_decisions` ne fait
     JAMAIS `calendar[i].hour + 1` en arithmétique d'horloge, mais lit toujours `calendar[i+1]`
     positionnellement dans le calendrier RÉELLEMENT fourni (le seul sens dans lequel la
     "causalité" a un contenu non trivial ici, le signal ne dépendant d'aucun prix -- cf.
     docstring module).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtest.strategies import seasonality  # noqa: E402


def _regular_calendar(n_hours: int, start: str = "2023-06-01 00:00:00") -> pd.DatetimeIndex:
    return pd.date_range(start=start, periods=n_hours, freq="h")


def test_target_open_hours_are_exactly_21_and_22():
    # Fige la définition exacte (SPEC.md §1) -- toute régression qui élargirait/rétrécirait
    # l'ensemble d'heures cibles doit faire échouer ce test immédiatement.
    assert seasonality.TARGET_OPEN_HOURS_UTC == frozenset({21, 22})


def test_param_grid_is_a_single_empty_combo():
    # SPEC.md §1 : "Grille de paramètres : AUCUNE (1 seule combinaison, zéro degré de liberté)".
    assert seasonality.PARAM_GRID == [{}]


def test_weight_mapping_on_regular_calendar_no_gap():
    """Calendrier régulier sur 3 jours pleins (72h, sans trou) : `poids[i] == 1.0` ssi
    `calendar[i+1].hour in {21, 22}` pour TOUTE position `i` sauf la dernière (convention
    documentée, jamais consommée) -- vérifié heure par heure, pas seulement échantillonné."""
    cal = _regular_calendar(72)
    w = seasonality.generate_weight_decisions(cal, symbols=("BTC",))
    assert list(w.columns) == ["BTC"]
    assert len(w) == len(cal)

    values = w["BTC"].to_numpy()
    for i in range(len(cal) - 1):
        expected = 1.0 if cal[i + 1].hour in (21, 22) else 0.0
        assert values[i] == expected, f"i={i} calendar[i]={cal[i]} calendar[i+1]={cal[i+1]}"
    # Dernière ligne : convention documentée, jamais consommée par le moteur.
    assert values[-1] == 0.0


def test_weight_mapping_entry_and_exit_timestamps_exact():
    """Sur une seule journée complète, vérifie EXACTEMENT les timestamps de décision attendus
    (SPEC.md §1 : "décidé aux clôtures des bougies 20h et 21h, sorti à l'open de la bougie 23h") :
    poids=1.0 décidé à 20h ET 21h uniquement, 0.0 à toutes les autres heures de la journée."""
    cal = _regular_calendar(24, start="2023-06-01 00:00:00")
    w = seasonality.generate_weight_decisions(cal, symbols=("BTC",))
    active_hours = sorted(t.hour for t in cal[w["BTC"].to_numpy() == 1.0])
    assert active_hours == [20, 21]


def test_weight_is_zero_one_only_never_nan():
    cal = _regular_calendar(200)
    w = seasonality.generate_weight_decisions(cal, symbols=("BTC",))
    assert not w.isna().any().any()
    assert set(np.unique(w["BTC"].to_numpy())) <= {0.0, 1.0}


def test_multiple_symbols_columns_identical_and_ordered():
    cal = _regular_calendar(48)
    w = seasonality.generate_weight_decisions(cal, symbols=("BTC", "ETH"))
    assert list(w.columns) == ["BTC", "ETH"]
    assert (w["BTC"] == w["ETH"]).all()


def test_empty_calendar_returns_empty_frame_with_columns():
    cal = pd.DatetimeIndex([])
    w = seasonality.generate_weight_decisions(cal, symbols=("BTC",))
    assert len(w) == 0
    assert list(w.columns) == ["BTC"]


# --------------------------------------------------------------------------------------------
# Mission point 7 : causalité + suivi de l'HEURE RÉELLE de la bougie i+1 du calendrier fourni,
# jamais d'une heure théorique, sur un calendrier synthétique comportant un trou à 21h.
# --------------------------------------------------------------------------------------------


def _calendar_with_21h_gap(day: str = "2023-06-01") -> pd.DatetimeIndex:
    """Calendrier d'une seule journée, HEURE 21h ABSENTE (simule un trou de données réel comme
    celui documenté dans `backtest/data_hourly.py`) : ..., 19h, 20h, 22h, 23h, ... -- la bougie
    21h n'existe simplement pas dans ce calendrier, comme un trou non comblé par `align_to_
    calendar` (au-delà du ffill borné) le ferait disparaître du calendrier canonique lui-même."""
    hours = [h for h in range(24) if h != 21]
    return pd.DatetimeIndex([pd.Timestamp(f"{day} {h:02d}:00:00") for h in hours])


def test_signal_follows_real_next_candle_not_theoretical_hour_across_data_gap():
    """Coeur du test anti-régression (mission point 7) : avec 21h ABSENTE du calendrier,
      - la bougie décidant à 20h a pour bougie SUIVANTE RÉELLE 22h (pas 21h, qui n'existe pas) ->
        22h in {21,22} -> poids DÉCIDÉ À 20h doit être 1.0 (le signal "voit" que la prochaine
        bougie réelle ouvre à 22h, et s'active en conséquence -- suivi de l'horloge RÉELLE) ;
      - la bougie décidant à 22h a pour bougie suivante réelle 23h -> 23h not in {21,22} -> poids
        décidé à 22h doit être 0.0 (comportement IDENTIQUE au calendrier sans trou : la sortie a
        toujours lieu "à la bougie qui précède 23h", que cette bougie s'appelle 21h ou 22h) ;
      - la bougie décidant à 19h a pour bougie suivante réelle 20h -> pas dans {21,22} -> 0.0
        (aucun effet du trou en amont du trou lui-même, le signal reste strictement local à
        `calendar[i+1]`).
    Une implémentation BUGUÉE qui calculerait `calendar[i].hour + 1` en arithmétique d'horloge
    plutôt que de lire positionnellement `calendar[i + 1]` donnerait ICI un résultat IDENTIQUE
    pour la bougie 20h (20+1=21 in {21,22} -> 1.0, par coïncidence) mais DIVERGERAIT sur un
    calendrier où le trou couvre 20h ET 21h (cf. test suivant) -- ce test isole donc le cas le
    plus simple ET documente pourquoi il ne suffit pas seul, tandis que le suivant tranche sans
    ambiguïté entre les deux implémentations."""
    cal = _calendar_with_21h_gap()
    w = seasonality.generate_weight_decisions(cal, symbols=("BTC",))
    by_hour = {t.hour: w["BTC"].loc[t] for t in cal}

    assert by_hour[19] == 0.0
    assert by_hour[20] == 1.0  # bougie suivante réelle = 22h (21h absente) -> in {21,22}
    assert by_hour[22] == 0.0  # bougie suivante réelle = 23h -> not in {21,22}


def test_signal_follows_real_next_candle_when_gap_covers_both_target_hours():
    """Cas SANS ambiguïté entre "lit `calendar[i+1]` positionnellement" et "calcule
    `calendar[i].hour + 1` en arithmétique d'horloge" : 21h ET 22h sont TOUTES DEUX absentes du
    calendrier (trou de 2h). La bougie suivante RÉELLE de 20h est alors 23h (not in {21,22}) ->
    poids décidé à 20h DOIT être 0.0. Une implémentation buguée en arithmétique d'horloge
    (`20+1=21 in {21,22}`) donnerait 1.0 ici -- ce test la ferait échouer explicitement, et la
    bonne implémentation (lecture positionnelle de `calendar[i+1]`) réussit."""
    hours = [h for h in range(24) if h not in (21, 22)]
    cal = pd.DatetimeIndex([pd.Timestamp(f"2023-06-01 {h:02d}:00:00") for h in hours])
    w = seasonality.generate_weight_decisions(cal, symbols=("BTC",))
    by_hour = {t.hour: w["BTC"].loc[t] for t in cal}

    assert by_hour[20] == 0.0  # bougie suivante réelle = 23h (21h et 22h absentes)
    assert by_hour[23] == 0.0  # bougie suivante réelle = 00h du jour suivant -> absente du calendrier d'un jour, jamais testée ici sauf via un calendrier multi-jours ; ce jour seul confirme déjà la non-régression ciblée.


def test_signal_multi_day_gap_still_activates_correctly_around_the_hole():
    """Calendrier de 2 jours avec le trou de 21h uniquement le PREMIER jour (le second jour est
    régulier) : le second jour doit suivre le mapping standard (20h/21h actifs), confirmant que
    le trou d'un jour ne contamine pas le jour suivant (le signal reste purement LOCAL à
    `calendar[i+1]`, jamais un état global mémorisé entre jours)."""
    day1_hours = [h for h in range(24) if h != 21]
    day1 = [pd.Timestamp(f"2023-06-01 {h:02d}:00:00") for h in day1_hours]
    day2 = [pd.Timestamp(f"2023-06-02 {h:02d}:00:00") for h in range(24)]
    cal = pd.DatetimeIndex(day1 + day2)
    w = seasonality.generate_weight_decisions(cal, symbols=("BTC",))

    assert w["BTC"].loc[pd.Timestamp("2023-06-01 20:00:00")] == 1.0  # trou jour 1 : 22h réel suit
    assert w["BTC"].loc[pd.Timestamp("2023-06-01 22:00:00")] == 0.0
    # Jour 2, régulier : mapping standard.
    assert w["BTC"].loc[pd.Timestamp("2023-06-02 20:00:00")] == 1.0
    assert w["BTC"].loc[pd.Timestamp("2023-06-02 21:00:00")] == 1.0
    assert w["BTC"].loc[pd.Timestamp("2023-06-02 22:00:00")] == 0.0
