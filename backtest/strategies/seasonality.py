"""backtest/strategies/seasonality.py — signal "saisonnalité horaire BTC 21h-23h UTC" pour la
candidate `btc_seasonality_2123utc` (`backtest/SEASONALITY-BTC-SPEC.md` §1, backlog P1#4).
Long-only, univers BTC SEUL, ZÉRO paramètre libre : long pendant les bougies horaires ouvrant à
21h et 22h UTC, flat tout le reste du temps. AUCUNE variante d'heure, AUCUN filtre additionnel
(spec §1 : "INTERDIT ABSOLU").

--------------------------------------------------------------------------------------------
Signal (SPEC.md §1) -- rappel exact
--------------------------------------------------------------------------------------------
Sur la convention du moteur (poids DÉCIDÉS à la clôture de la bougie i, EXÉCUTÉS à l'open de la
bougie i+1, cf. `backtest/engine.py`) :

    poids_brut(bougie i) = 1.0 si heure_UTC(open(bougie i+1)) in {21, 22}, sinon 0.0.

Concrètement, décidé aux clôtures des bougies 20h et 21h, exécuté (entrée) à l'open de la bougie
21h puis maintenu à l'open de la bougie 22h, sorti à l'open de la bougie 23h -- position tenue
EXACTEMENT 2 bougies horaires (21h-22h UTC puis 22h-23h UTC), jamais à cheval sur minuit UTC.

--------------------------------------------------------------------------------------------
Convention de timestamp de `_data/crypto/BTC.csv.gz` -- VÉRIFIÉE avant implémentation (mission)
--------------------------------------------------------------------------------------------
Vérification empirique faite avant d'écrire ce module (`python3` sur le fichier brut) : la ligne
`timestamp=2022-01-01T00:00:00+00:00` porte `open=46216.93` -- la colonne `timestamp` est bien
l'heure d'OUVERTURE de la bougie (pas sa clôture), cohérente avec `backtest/data_hourly.py` qui
charge `timestamp` comme index SANS aucun décalage. `backtest/engine.py::simulate_segment` exécute
`weights_decided.iloc[i-1]` à `opens.iloc[i]` -- donc `calendar[i]` EST bien l'heure UTC
d'ouverture de la bougie `i`, et "heure_UTC(open(bougie i+1))" de la SPEC est directement
`calendar[i + 1].hour` : AUCUNE conversion/décalage supplémentaire n'est nécessaire ici.

--------------------------------------------------------------------------------------------
Causalité et robustesse aux trous de calendrier (mission point 7)
--------------------------------------------------------------------------------------------
Le poids décidé à la position `i` du calendrier suit l'heure de la bougie RÉELLEMENT présente à
la position `i+1` du `calendar` FOURNI (jamais une heure "théorique" `calendar[i].hour + 1`, qui
divergerait dès qu'une heure manque du calendrier commun -- cf. `backtest/data_hourly.py`, pas
de trou réel constaté sur BTC seul mais le principe doit rester correct en général). Si la
bougie 21h est absente du calendrier, la bougie qui décide à `i` (dont la bougie SUIVANTE réelle
ouvre à 22h) reçoit un poids de 1.0 -- comportement voulu : le signal suit l'horloge RÉELLE des
bougies disponibles, jamais un calendrier hypothétique. C'est trivialement causal : `poids[i]` ne
dépend QUE de `calendar[i+1]`, une métadonnée d'horloge connue à l'avance pour tout calendrier
fixé avant l'exécution -- jamais d'un `close`/`open` futur (l'interdiction de fuite de
`backtest/engine.py` porte sur les PRIX futurs, jamais sur les TIMESTAMPS, structurellement
connus tout le long d'un calendrier déjà construit)."""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd

__all__ = ["TARGET_OPEN_HOURS_UTC", "PARAM_GRID", "generate_weight_decisions"]

TARGET_OPEN_HOURS_UTC = frozenset({21, 22})  # SPEC.md §1 -- AUCUNE variante, AUCUN paramètre.

# Grille pré-enregistrée (SPEC.md §1 : "Grille de paramètres : AUCUNE (1 seule combinaison, zéro
# degré de liberté)") -- un seul dict vide, importé TEL QUEL par `backtest/run_btc_seasonality.py`
# (jamais une valeur ad hoc), cohérent avec le format `PARAM_GRID` des autres candidates
# (`vol_breakout.PARAM_GRID`, `funding_carry.PARAM_GRID`) pour que
# `backtest/engine.py::select_params_via_is` s'utilise SANS branche spéciale (`len(grid) <= 1` ->
# combinaison unique retournée directement, `is_sharpe=NaN` documentant "non applicable").
PARAM_GRID = [{}]


def generate_weight_decisions(calendar: pd.DatetimeIndex, symbols: Sequence[str] = ("BTC",)) -> pd.DataFrame:
    """Construit `weights_decided` (index=`calendar`, colonnes=`symbols`) attendu par
    `backtest/engine.py::simulate_segment` : `weights_decided.iloc[i]` = poids DÉCIDÉ à la
    clôture de `calendar[i]` (exécuté par le moteur à `opens.iloc[i+1]`). Ne dépend QUE de
    `calendar` (aucun prix, aucun paramètre) -- cf. docstring module pour la causalité et la
    robustesse aux trous de calendrier.

    Dernière ligne (`i == len(calendar) - 1`) : aucune bougie `i+1` n'existe dans `calendar` ;
    poids conventionnellement fixé à `0.0` -- valeur JAMAIS consommée par `simulate_segment`
    (une fenêtre simulée exécute au plus `weights_decided.iloc[end_idx - 1]` pour sa dernière
    bougie `end_idx`, jamais `iloc[end_idx]` lui-même, cf. docstring `simulate_segment`) ; ce
    choix documente explicitement l'absence de fuite/`IndexError`, pas une nécessité
    fonctionnelle."""
    symbols = list(symbols)
    n = len(calendar)
    if n == 0:
        return pd.DataFrame(columns=symbols)
    hours = np.asarray(calendar.hour)
    next_hours = np.empty(n, dtype=hours.dtype)
    if n > 1:
        next_hours[:-1] = hours[1:]
    # Sentinelle -- jamais dans TARGET_OPEN_HOURS_UTC (0-23), jamais consommée (cf. docstring).
    next_hours[-1] = -1
    raw_weight = np.isin(next_hours, list(TARGET_OPEN_HOURS_UTC)).astype(float)
    data = {sym: raw_weight for sym in symbols}
    return pd.DataFrame(data, index=calendar)
