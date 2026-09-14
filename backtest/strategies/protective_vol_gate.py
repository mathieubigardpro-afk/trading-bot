"""backtest/strategies/protective_vol_gate.py — signal "protective put synthétique" pour la
candidate `protective_vol_gate_6majors` (`backtest/results/protective_vol_gate_6majors/SPEC.md`,
pré-enregistrée 2026-09-14, backlog P1#6, session hebdomadaire #7). Long-only, panier
équipondéré des 6 majors, sur clôtures HORAIRES. AUCUN paramètre ci-dessous n'est une liberté
d'implémentation : la grille, les seuils et les fenêtres sont ceux de la SPEC ; toute
interprétation ambiguë est documentée ci-dessous et tranchée de façon CONSERVATRICE (§0.2
`ARCHITECTURE.md`, rappelé par la mission) -- jamais silencieusement en faveur de la candidate.

--------------------------------------------------------------------------------------------
Signal (cf. SPEC.md §3) -- rappel exact
--------------------------------------------------------------------------------------------
  1. Rendement du panier : `r_t` = moyenne équipondérée des `pct_change` des closes des 6
     symboles à `t` (NaN si un symbole a un close manquant à `t` ou `t-1` -- `pandas.mean`
     ignore silencieusement les NaN d'une ligne, cohérent avec l'esprit "panier équipondéré"
     tant qu'au moins un symbole a un rendement défini).
  2. Vol réalisée : écart-type glissant de `r` sur `V` heures (grille), fenêtre PLEINE requise
     (`min_periods=V` -- `NaN` avant, jamais une vol calculée sur un échantillon partiel).
  3. Rang percentile CAUSAL : fraction des valeurs de vol des 4320 dernières heures (180 j)
     strictement inférieures ou égales à la vol courante, calculée sur `rolling(4320,
     min_periods=2160).rank(pct=True)` -- `NaN` tant que 2160 observations RÉELLES (non-NaN)
     de vol ne sont pas disponibles dans la fenêtre (warm-up, cf. interprétation §1 ci-dessous).
  4. Machine à états causale (état initial : investi, `g=1`) -- cf. `_run_state_machine` pour
     l'implémentation exacte et l'interprétation §2 (timing de la rampe) ci-dessous.
  5. Poids décidés : `w_t(sym) = g_t / 6` pour chacun des 6 symboles (long-only, `g_t in [0,1]`
     donc `sum(w_t) = g_t <= 1` -- jamais de sizing interne au-delà, cf. SPEC.md §3.5).

--------------------------------------------------------------------------------------------
Interprétations tranchées (ambiguïtés de la SPEC, documentées comme demandé par la mission --
choisies pour ne JAMAIS surestimer silencieusement le bénéfice de la candidate)
--------------------------------------------------------------------------------------------
  1. **Fenêtre du rang percentile** : la SPEC dit "fraction des valeurs de vol des 4320
     dernières heures ... strictement inférieures ou égales à la vol courante" avec un minimum
     de 2160 observations VALIDES. Interprété comme `vol.rolling(4320, min_periods=2160)
     .rank(pct=True)` : le dénominateur du percentile est le nombre RÉEL d'observations de vol
     non-NaN dans la fenêtre glissante (`<=4320`, jamais complétée artificiellement à 4320) --
     vérifié empiriquement que `pandas.Series.rolling(window, min_periods=k).rank(pct=True)`
     calcule bien le rang PARMI les valeurs valides du sous-échantillon (pas parmi `window`
     positions dont certaines seraient `NaN`), donc `t` inclus dans son propre percentile
     (définition standard, strictement causale : aucune donnée `> t` n'est utilisée). Warm-up
     (< 2160 observations valides) -> `NaN` -> état forcé "investi" (SPEC.md §3.3, explicite).
  2. **Timing de la rampe de redéploiement** : la SPEC dit "g remonte LINÉAIREMENT de 0 à 1 en
     R = 72h, incrément 1/72 par heure" sans préciser si l'heure de déclenchement
     (`percentile_t <= p_out`, transition protégé -> redéploiement) compte comme le premier
     incrément ou comme l'instant `g=0` de départ de la rampe. Choisi : L'HEURE DE DÉCLENCHEMENT
     a `g=0` (c'est l'instant `0` de la rampe "de 0 à 1 en 72h"), le premier incrément
     (`g=1/72`) a lieu à l'heure SUIVANTE -- `g` atteint exactement `1.0` 72 heures PLEINES
     après le déclenchement (72 incréments consommés). C'est l'interprétation la plus
     CONSERVATRICE des deux lectures naturelles (l'alternative -- incrément dès l'heure de
     déclenchement -- redéploierait une heure plus tôt, donc gagnerait potentiellement plus de
     rebond) : elle retarde d'une heure la ré-exposition, jamais l'inverse. Symétrique et
     indépendante de la coupure (SPEC.md §3.4, "pas de rampe à la coupure : la protection est
     immédiate par construction") : la coupure `g=0` s'applique elle-même IMMÉDIATEMENT à
     l'heure où `percentile_t >= p_in` est observé (aucune interprétation possible ici, la SPEC
     est explicite), sans ambiguïté symétrique côté coupure.
  3. **Écart-type de la vol réalisée** : `ddof` non précisé par la SPEC. Choisi : écart-type
     ÉCHANTILLON (`ddof=1`, défaut de `pandas.Series.rolling().std()`, même choix documenté que
     `backtest/strategies/vol_breakout.py` §1) -- aucun paramètre à surcharger, cohérence
     interne du dépôt.
  4. **Simultanéité coupure/rang percentile égal aux deux seuils** : `p_in > p_out` toujours
     par construction de la grille (0.95/0.98 > 0.80 fixé) -- aucun chevauchement possible entre
     les deux conditions de seuil, pas d'ambiguïté à trancher ici.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np
import pandas as pd

__all__ = [
    "ProtectiveVolGateParams",
    "P_OUT",
    "R_HOURS",
    "PCT_WINDOW_HOURS",
    "MIN_VALID_VOL_OBS",
    "WEIGHT_PER_SYMBOL",
    "STATE_INVESTED",
    "STATE_PROTECTED",
    "STATE_REDEPLOY",
    "PARAM_GRID",
    "basket_return",
    "realized_vol",
    "percentile_rank",
    "run_state_machine",
    "generate_state_and_g",
    "generate_weight_decisions",
]

# Fixés par la SPEC (zéro degré de liberté supplémentaire, SPEC.md §4) -----------------------
P_OUT = 0.80
R_HOURS = 72
PCT_WINDOW_HOURS = 4320  # 180 jours
MIN_VALID_VOL_OBS = 2160  # 90 jours
WEIGHT_PER_SYMBOL = 1.0 / 6.0  # univers fixe des 6 majors (SPEC.md §2)

# États de la machine (SPEC.md §3.4) ----------------------------------------------------------
STATE_INVESTED = 0
STATE_PROTECTED = 1
STATE_REDEPLOY = 2

# Grille pré-enregistrée EXACTE (SPEC.md §4) -- 4 combinaisons, rien d'autre.
# `backtest/run_protective_vol_gate.py` importe CETTE constante (jamais une valeur ad hoc).
PARAM_GRID = [
    {"vol_window_hours": 24, "p_in": 0.95},
    {"vol_window_hours": 24, "p_in": 0.98},
    {"vol_window_hours": 72, "p_in": 0.95},
    {"vol_window_hours": 72, "p_in": 0.98},
]


@dataclass(frozen=True)
class ProtectiveVolGateParams:
    """Un point de la grille. `vol_window_hours` (V) et `p_in` sont les DEUX seuls axes de
    variation autorisés par la SPEC (`PARAM_GRID` ci-dessus) ; cette dataclass reste
    volontairement générique (pas de validation qui figerait `vol_window_hours`/`p_in` aux
    seules valeurs de la grille) car elle est aussi réutilisée par les tests synthétiques de
    `backtest/tests/test_protective_vol_gate.py` sur des fixtures minuscules -- la discipline
    "jamais hors grille" est appliquée au niveau du SCRIPT D'ORCHESTRATION
    (`run_protective_vol_gate.py` n'importe et n'itère QUE sur `PARAM_GRID`), pas ici."""

    vol_window_hours: int
    p_in: float
    p_out: float = P_OUT
    r_hours: int = R_HOURS
    pct_window_hours: int = PCT_WINDOW_HOURS
    min_valid_vol_obs: int = MIN_VALID_VOL_OBS


def basket_return(closes: pd.DataFrame) -> pd.Series:
    """Rendement du panier équipondéré (SPEC.md §3.1) : moyenne, ligne par ligne, des
    `pct_change()` de chaque colonne (symbole) -- `pandas.DataFrame.mean(axis=1)` ignore les
    `NaN` par défaut (`skipna=True`), donc reste défini tant qu'au moins un symbole a un
    rendement calculable à `t`. Strictement causal : `pct_change().iloc[t]` ne compare que
    `closes.iloc[t]` à `closes.iloc[t-1]`, jamais une donnée future."""
    return closes.pct_change().mean(axis=1)


def realized_vol(returns: pd.Series, vol_window_hours: int) -> pd.Series:
    """Écart-type glissant CAUSAL de `returns` sur `vol_window_hours` heures, fenêtre PLEINE
    requise (`min_periods=vol_window_hours`, SPEC.md §3.2) -- `ddof=1` (défaut pandas,
    interprétation §3 de la docstring module)."""
    return returns.rolling(vol_window_hours, min_periods=vol_window_hours).std()


def percentile_rank(
    vol: pd.Series, pct_window_hours: int = PCT_WINDOW_HOURS, min_valid_vol_obs: int = MIN_VALID_VOL_OBS
) -> pd.Series:
    """Rang percentile CAUSAL de `vol.iloc[t]` parmi les `pct_window_hours` dernières heures
    (`t` inclus), `NaN` tant que `min_valid_vol_obs` observations RÉELLES de vol ne sont pas
    disponibles dans cette fenêtre (SPEC.md §3.3, interprétation §1 de la docstring module).
    `rolling(window, min_periods=k).rank(pct=True)` : vérifié (cf. docstring module) que pandas
    calcule bien le rang parmi les valeurs VALIDES du sous-échantillon, jamais parmi `window`
    positions dont certaines seraient `NaN`."""
    return vol.rolling(pct_window_hours, min_periods=min_valid_vol_obs).rank(pct=True)


def run_state_machine(
    percentile: np.ndarray, p_in: float, p_out: float = P_OUT, r_hours: int = R_HOURS
) -> Tuple[np.ndarray, np.ndarray]:
    """Machine à états CAUSALE (SPEC.md §3.4), boucle Python simple sur un tableau numpy 1D
    (~40 000 valeurs, 4 combos -- la CLARTÉ/causalité priment sur la vitesse, cf. mission).
    `percentile[t]` ne doit dépendre que de `closes[<=t]` (responsabilité de l'appelant, cf.
    `percentile_rank`) -- cette fonction elle-même ne regarde jamais `percentile[t+1:]` pour
    décider de `state[t]`/`g[t]` (récurrence strictement causale par construction : chaque
    itération `t` ne lit que `percentile[t]` et l'état `(state, g)` hérité de `t-1`).

    États (SPEC.md §3.4) :
      - INVESTIE (`g=1`) : `percentile_t >= p_in` -> PROTÉGÉE, `g=0` IMMÉDIAT à `t` (pas de
        rampe à la coupure, SPEC.md explicite) ; sinon reste investie, `g=1`.
      - PROTÉGÉE (`g=0`) : `percentile_t <= p_out` -> REDÉPLOIEMENT, `g=0` à `t` (instant `0`
        de la rampe, interprétation §2 de la docstring module -- le premier incrément a lieu à
        `t+1`) ; sinon reste protégée, `g=0`.
      - REDÉPLOIEMENT : `percentile_t >= p_in` -> retour IMMÉDIAT à PROTÉGÉE, `g=0` (SPEC.md
        §3.4, "re-coupure pendant la rampe") ; sinon `g` incrémenté de `1/r_hours` par rapport
        à `g` de `t-1` (plafonné à 1.0) -- `g==1.0` atteint -> transition vers INVESTIE (la
        rampe est terminée, `g` reste `1.0` tant que `percentile_t < p_in`).
      - Avertissement (`percentile[t]` est `NaN`, warm-up SPEC.md §3.3) : état forcé INVESTIE,
        `g=1`, à CHAQUE `t` concerné (pas seulement au tout début -- règle appliquée
        uniformément, jamais seulement "au démarrage").

    Retourne `(state, g)`, deux tableaux numpy de même longueur que `percentile`."""
    n = len(percentile)
    state_arr = np.empty(n, dtype=np.int8)
    g_arr = np.empty(n, dtype=np.float64)
    r_hours_int = int(r_hours)

    # Correctif dérive flottante : `g` de la rampe est dérivé d'un COMPTEUR ENTIER
    # `ramp_hours_elapsed` (`g = min(1.0, ramp_hours_elapsed / r_hours)`), jamais d'une
    # accumulation répétée `g = g + 1/r_hours` -- une somme de 72 additions de `1/72` en
    # flottant double NE VAUT PAS exactement `1.0` (`0.9999999999999983` mesuré empiriquement),
    # ce qui retarderait indéfiniment (ou d'au moins une heure) la transition
    # REDÉPLOIEMENT -> INVESTIE via un test `g >= 1.0` jamais satisfait. Le compteur entier
    # rend cette transition EXACTE, quel que soit `r_hours`.
    state = STATE_INVESTED
    g = 1.0
    ramp_hours_elapsed = 0
    for t in range(n):
        p = percentile[t]
        if np.isnan(p):
            state = STATE_INVESTED
            g = 1.0
        elif state == STATE_INVESTED:
            if p >= p_in:
                state = STATE_PROTECTED
                g = 0.0
            else:
                g = 1.0
        elif state == STATE_PROTECTED:
            if p <= p_out:
                state = STATE_REDEPLOY
                ramp_hours_elapsed = 0
                g = 0.0
            else:
                g = 0.0
        else:  # STATE_REDEPLOY
            if p >= p_in:
                state = STATE_PROTECTED
                g = 0.0
            else:
                ramp_hours_elapsed += 1
                if ramp_hours_elapsed >= r_hours_int:
                    state = STATE_INVESTED
                    g = 1.0
                else:
                    g = ramp_hours_elapsed / r_hours_int
        state_arr[t] = state
        g_arr[t] = g
    return state_arr, g_arr


def generate_state_and_g(closes: pd.DataFrame, params: ProtectiveVolGateParams) -> pd.DataFrame:
    """Pipeline complet du signal (SPEC.md §3, étapes 1-4), calculé UNE SEULE FOIS sur le
    calendrier complet fourni (`closes`), indexé par position -- même approche que
    `backtest/run_vol_breakout.py`. Retourne un DataFrame indexé comme `closes` avec les
    colonnes `r` (rendement panier), `vol`, `percentile`, `state`, `g` -- exposé séparément de
    `generate_weight_decisions` pour les analyses d'honnêteté du runner (épisodes de
    protection, cf. SPEC.md §7.1) qui ont besoin de `state`/`g`, pas seulement des poids
    finaux."""
    r = basket_return(closes)
    vol = realized_vol(r, params.vol_window_hours)
    pct = percentile_rank(vol, params.pct_window_hours, params.min_valid_vol_obs)
    state_arr, g_arr = run_state_machine(pct.to_numpy(), params.p_in, params.p_out, params.r_hours)
    return pd.DataFrame(
        {"r": r, "vol": vol, "percentile": pct, "state": state_arr, "g": g_arr}, index=closes.index
    )


def generate_weight_decisions(closes: pd.DataFrame, params: ProtectiveVolGateParams) -> pd.DataFrame:
    """Construit `weights_decided` (index=calendrier horaire commun, colonnes=`closes.columns`)
    attendu par `backtest/engine.py::simulate_segment` : `weights_decided.loc[t]` = poids
    DÉCIDÉS à la clôture de `t` (exécutés à `open[t+1]` par le moteur), calculés UNIQUEMENT à
    partir de `closes.loc[:t]` (cf. `generate_state_and_g`, strictement causal).

    GARDE ANTI « EQUITY CURVE TRADING » (SPEC.md §1, structurellement vérifiable) : la
    signature de cette fonction n'accepte QUE `closes` (un DataFrame de PRIX) et `params` (une
    dataclass de paramètres) -- AUCUN paramètre d'équity, de PnL ou de position n'existe dans
    cette signature ni n'est utilisé nulle part dans ce module (cf. `backtest/tests/
    test_protective_vol_gate.py::test_signature_anti_equity_curve_trading`, qui vérifie cela
    par introspection). Valeurs : `g_t / 6` (`g_t in [0,1]`) -- jamais de `NaN` (warm-up ->
    `g=1`, cf. `run_state_machine`)."""
    universe = list(closes.columns)
    sg = generate_state_and_g(closes, params)
    g = sg["g"]
    weights = pd.DataFrame({sym: g * WEIGHT_PER_SYMBOL for sym in universe}, index=closes.index)
    # Garde défensive explicite (jamais de NaN en sortie, cf. audit F3 de `backtest/engine.py`) :
    # `g` ne devrait jamais être NaN par construction (warm-up -> g=1.0 forcé), mais un
    # `fillna(0.0)` couvre silencieusement toute régression future sans jamais introduire de
    # poids > 0 non voulu (0.0 = flat, la valeur la plus prudente possible).
    return weights.fillna(0.0)
