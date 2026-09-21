"""backtest/strategies/pairs_ratio.py — signal "retour à la moyenne sur le ratio ETH/BTC"
(version dégradée long-only) pour la candidate `pairs_ethbtc_ratio_rotation`
(`backtest/results/pairs_ethbtc_ratio_rotation/SPEC.md`, pré-enregistrée 2026-09-21, backlog
P1#5, session hebdomadaire #8). Long-only, univers FIXE {BTC, ETH} (2 actifs), rotation
d'allocation entre les deux selon un z-score de retour à la moyenne calculé sur le LOG-RATIO
ETH/BTC — jamais sur les prix absolus (structurellement différent du RSI2 déjà rejeté). AUCUN
paramètre ci-dessous n'est une liberté d'implémentation : la grille, les seuils et les fenêtres
sont ceux de la SPEC ; toute interprétation ambiguë est documentée ci-dessous et tranchée de
façon CONSERVATRICE (§0.2 `ARCHITECTURE.md`), jamais silencieusement en faveur de la candidate.

--------------------------------------------------------------------------------------------
Signal (cf. SPEC.md §3) — rappel exact
--------------------------------------------------------------------------------------------
  1. Log-ratio : `x_t = ln(close_ETH,t / close_BTC,t)`.
  2. Z-score CAUSAL : `z_t = (x_t - SMA_L(x)_t) / STD_L(x)_t`, fenêtre glissante de `L` heures
     (grille), fenêtre PLEINE requise (`min_periods=L`, `NaN` avant) ; `STD` ÉCHANTILLON
     (`ddof=1`) ; si `STD_L = 0` (fenêtre pleine mais log-ratio parfaitement constant dessus)
     ⇒ `z` traité comme `0` (aucune position tiltée sur une vol nulle, SPEC.md §3.2 explicite).
  3. Machine à états CAUSALE (état initial ET warm-up : NEUTRE = 50/50, SPEC.md §3.3) :
       - NEUTRE, `z_t >= +theta` ⇒ TILT_BTC (`w = {BTC:1, ETH:0}`, décidé à `t`, exécuté à
         `t+1` par le moteur commun) ;
       - NEUTRE, `z_t <= -theta` ⇒ TILT_ETH (`w = {BTC:0, ETH:1}`) ;
       - TILT_BTC ou TILT_ETH, `|z_t| <= theta/2` (hystérésis FIXE) ⇒ retour NEUTRE 50/50 ;
       - TILT_BTC, `z_t <= -theta` ⇒ bascule DIRECTE vers TILT_ETH (et symétriquement TILT_ETH,
         `z_t >= +theta` ⇒ bascule directe vers TILT_BTC) — cas rare, sans ambiguïté possible
         avec la sortie par hystérésis : `|z_t| <= theta/2 < theta` et `|z_t| >= theta` sont
         disjoints par construction (`theta > 0`), donc l'ORDRE dans lequel ces deux conditions
         sont testées ci-dessous n'a AUCUN effet sur le résultat.
  4. Poids DÉCIDÉS (SPEC.md §3.4) : NEUTRE = `{BTC: 0.5, ETH: 0.5}` ; tilt = rotation TOTALE
     (100/0) vers l'actif « bon marché » relatif — zéro paramètre d'intensité de tilt. Long-only
     strict, toujours investi à 100 % AVANT la surcouche de risque du moteur commun
     (`sum(w) == 1.0` sur toutes les lignes, jamais de `NaN`).

Le signal ne lit QUE les closes de BTC et ETH — jamais l'équity, le PnL ou les positions de la
stratégie (garde structurelle habituelle contre l'« equity curve trading », vérifiée par
introspection dans `backtest/tests/test_pairs_ratio.py`, même convention que
`backtest/strategies/protective_vol_gate.py::test_signature_anti_equity_curve_trading`).

--------------------------------------------------------------------------------------------
Interprétations tranchées (ambiguïtés de la SPEC, documentées comme demandé par la mission —
choisies pour ne JAMAIS surestimer silencieusement le bénéfice de la candidate)
--------------------------------------------------------------------------------------------
  1. **Warm-up / `z_t` `NaN`** : la SPEC dit "état initial et warm-up : NEUTRE = 50/50" sans
     préciser explicitement si un `NaN` qui réapparaîtrait APRÈS le warm-up initial (ex. un trou
     de données non comblé par le `ffill` borné à 3h de `backtest/data_hourly.py`) doit aussi
     forcer NEUTRE. Choisi : la règle s'applique à CHAQUE `t` où `z_t` est `NaN`, pas seulement
     au tout début de la série (même interprétation, documentée pour la même raison, que
     `backtest/strategies/protective_vol_gate.py::run_state_machine` — ne JAMAIS propager un
     état tilté décidé sur une donnée indéfinie). Sans conséquence pratique attendue ici :
     `backtest/data_hourly.py` documente 0 trou réel sur l'univers 6-majors (BTC/ETH inclus) sur
     la période couverte — cette garde reste un filet de sécurité défensif, jamais activement
     sollicité sur ce jeu de données.
  2. **`STD_L = 0`** : la SPEC est explicite ("z traité comme 0"). Implémenté par un `.mask()`
     APRÈS calcul de `(x - SMA_L(x)) / STD_L(x)` (qui produirait sinon `0/0 = NaN` ou `±inf`
     selon le numérateur) — un `STD_L` `NaN` (fenêtre pas encore pleine, warm-up) n'est PAS
     confondu avec un `STD_L` EXACTEMENT nul (`NaN == 0` est `False` en Python/numpy/pandas,
     donc le masque ne touche jamais les lignes de warm-up, qui restent `NaN` et tombent dans
     l'interprétation §1 ci-dessus).
  3. **Ordre des vérifications dans la machine à états** : sans ambiguïté (cf. point 3 du rappel
     de signal ci-dessus, domaines disjoints par construction) — documenté explicitement pour
     qu'un audit ne le découvre pas comme une zone grise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np
import pandas as pd

__all__ = [
    "PairsRatioParams",
    "HYSTERESIS_FRAC",
    "UNIVERSE",
    "WEIGHT_NEUTRAL",
    "STATE_NEUTRAL",
    "STATE_TILT_BTC",
    "STATE_TILT_ETH",
    "PARAM_GRID",
    "log_ratio",
    "zscore",
    "run_state_machine",
    "generate_state",
    "generate_weight_decisions",
]

# Univers FIXE (SPEC.md §2) : BTC, ETH, dans cet ordre — jamais un autre symbole, jamais une
# taille d'univers dynamique (contrairement à `protective_vol_gate.py`, réutilisé par 6 candidats
# différents avec des univers variables : cette candidate est structurellement à 2 actifs).
UNIVERSE = ["BTC", "ETH"]
WEIGHT_NEUTRAL = 0.5  # base 50/50 (SPEC.md §3.4), zéro degré de liberté

# Hystérésis de sortie FIXE (SPEC.md §4 : "hystérésis de sortie theta/2", "convention
# funding_carry_6majors") — fraction de theta, jamais une valeur absolue indépendante.
HYSTERESIS_FRAC = 0.5

# États de la machine (SPEC.md §3.3) -----------------------------------------------------------
STATE_NEUTRAL = 0
STATE_TILT_BTC = 1
STATE_TILT_ETH = 2

# Grille pré-enregistrée EXACTE (SPEC.md §4) — 4 combinaisons, RIEN d'autre.
# `backtest/run_pairs_ethbtc.py` importe CETTE constante (jamais une valeur ad hoc).
PARAM_GRID = [
    {"l_hours": 720, "theta": 1.5},
    {"l_hours": 720, "theta": 2.0},
    {"l_hours": 2160, "theta": 1.5},
    {"l_hours": 2160, "theta": 2.0},
]


@dataclass(frozen=True)
class PairsRatioParams:
    """Un point de la grille. `l_hours` (L) et `theta` sont les DEUX seuls axes de variation
    autorisés par la SPEC (`PARAM_GRID` ci-dessus) ; cette dataclass reste volontairement
    générique (pas de validation qui figerait `l_hours`/`theta` aux seules valeurs de la grille)
    car elle est aussi réutilisée par les tests synthétiques de
    `backtest/tests/test_pairs_ratio.py` sur des fixtures minuscules — la discipline "jamais
    hors grille" est appliquée au niveau du SCRIPT D'ORCHESTRATION (`run_pairs_ethbtc.py`
    n'importe et n'itère QUE sur `PARAM_GRID`), pas ici."""

    l_hours: int
    theta: float
    hysteresis_frac: float = HYSTERESIS_FRAC


def log_ratio(closes: pd.DataFrame) -> pd.Series:
    """`x_t = ln(close_ETH,t / close_BTC,t)` (SPEC.md §3.1) — strictement causal, `x.iloc[t]` ne
    dépend que de `closes.iloc[t]` (jamais une donnée future ni passée au-delà de `t`)."""
    return np.log(closes["ETH"] / closes["BTC"])


def zscore(x: pd.Series, l_hours: int) -> pd.Series:
    """`z_t = (x_t - SMA_L(x)_t) / STD_L(x)_t` (SPEC.md §3.2), fenêtre glissante CAUSALE de `L`
    heures, fenêtre PLEINE requise (`min_periods=l_hours` — `NaN` avant), `STD` ÉCHANTILLON
    (`ddof=1`). `STD_L = 0` (fenêtre pleine, log-ratio parfaitement constant dessus) ⇒ `z`
    traité comme `0` (interprétation §2 de la docstring module) — un `STD_L` `NaN` (warm-up)
    n'est jamais confondu avec un `STD_L` nul (`NaN == 0` est `False`, cf. docstring module)."""
    sma = x.rolling(l_hours, min_periods=l_hours).mean()
    std = x.rolling(l_hours, min_periods=l_hours).std(ddof=1)
    z = (x - sma) / std
    return z.mask(std == 0, 0.0)


def run_state_machine(z: np.ndarray, theta: float, hysteresis_frac: float = HYSTERESIS_FRAC) -> np.ndarray:
    """Machine à états CAUSALE (SPEC.md §3.3), boucle Python simple sur un tableau numpy 1D (~40
    000 valeurs, 4 combos — la CLARTÉ/causalité priment sur la vitesse, même choix que
    `protective_vol_gate.py::run_state_machine`). `z[t]` ne doit dépendre que de
    `closes[<=t]` (responsabilité de l'appelant, cf. `zscore`) — cette fonction elle-même ne
    regarde jamais `z[t+1:]` pour décider de `state[t]` (récurrence strictement causale par
    construction : chaque itération `t` ne lit que `z[t]` et l'état hérité de `t-1`).

    États (SPEC.md §3.3) :
      - NEUTRE : `z_t >= +theta` -> TILT_BTC ; `z_t <= -theta` -> TILT_ETH ; sinon reste NEUTRE.
      - TILT_BTC : `z_t <= -theta` -> bascule DIRECTE TILT_ETH ; `|z_t| <= theta*hysteresis_frac`
        -> retour NEUTRE ; sinon reste TILT_BTC (domaines disjoints par construction, cf.
        docstring module — aucune ambiguïté d'ordre).
      - TILT_ETH : symétrique (`z_t >= +theta` -> bascule directe TILT_BTC ;
        `|z_t| <= theta*hysteresis_frac` -> retour NEUTRE ; sinon reste TILT_ETH).
      - Avertissement (`z[t]` est `NaN`, warm-up SPEC.md §3.3) : état forcé NEUTRE à CHAQUE `t`
        concerné (pas seulement au tout début, interprétation §1 de la docstring module).

    Retourne `state`, tableau numpy de même longueur que `z`."""
    n = len(z)
    state_arr = np.empty(n, dtype=np.int8)
    theta = float(theta)
    exit_band = theta * float(hysteresis_frac)

    state = STATE_NEUTRAL
    for t in range(n):
        zt = z[t]
        if np.isnan(zt):
            state = STATE_NEUTRAL
        elif state == STATE_NEUTRAL:
            if zt >= theta:
                state = STATE_TILT_BTC
            elif zt <= -theta:
                state = STATE_TILT_ETH
            # sinon reste NEUTRE
        elif state == STATE_TILT_BTC:
            if zt <= -theta:
                state = STATE_TILT_ETH
            elif abs(zt) <= exit_band:
                state = STATE_NEUTRAL
            # sinon reste TILT_BTC
        else:  # STATE_TILT_ETH
            if zt >= theta:
                state = STATE_TILT_BTC
            elif abs(zt) <= exit_band:
                state = STATE_NEUTRAL
            # sinon reste TILT_ETH
        state_arr[t] = state
    return state_arr


def generate_state(closes: pd.DataFrame, params: PairsRatioParams) -> pd.DataFrame:
    """Pipeline complet du signal (SPEC.md §3, étapes 1-3), calculé UNE SEULE FOIS sur le
    calendrier complet fourni (`closes`), indexé par position — même approche que
    `protective_vol_gate.generate_state_and_g`. Retourne un DataFrame indexé comme `closes` avec
    les colonnes `x` (log-ratio), `z`, `state` — exposé séparément de `generate_weight_decisions`
    pour les analyses d'honnêteté du runner (épisodes de tilt, SPEC.md §8.1) qui ont besoin de
    `state`, pas seulement des poids finaux."""
    x = log_ratio(closes)
    z = zscore(x, params.l_hours)
    state_arr = run_state_machine(z.to_numpy(), params.theta, params.hysteresis_frac)
    return pd.DataFrame({"x": x, "z": z, "state": state_arr}, index=closes.index)


def generate_weight_decisions(closes: pd.DataFrame, params: PairsRatioParams) -> pd.DataFrame:
    """Construit `weights_decided` (index=calendrier horaire commun, colonnes=`UNIVERSE` =
    `["BTC", "ETH"]`) attendu par `backtest/engine.py::simulate_segment` : `weights_decided.loc[t]`
    = poids DÉCIDÉS à la clôture de `t` (exécutés à `open[t+1]` par le moteur), calculés
    UNIQUEMENT à partir de `closes.loc[:t]` (cf. `generate_state`, strictement causal).

    GARDE ANTI « EQUITY CURVE TRADING » (SPEC.md §3, structurellement vérifiable) : la signature
    de cette fonction n'accepte QUE `closes` (un DataFrame de PRIX) et `params` (une dataclass de
    paramètres) — AUCUN paramètre d'équity, de PnL ou de position n'existe dans cette signature
    ni n'est utilisé nulle part dans ce module (cf. `backtest/tests/test_pairs_ratio.py::
    test_signature_anti_equity_curve_trading`, qui vérifie cela par introspection). Poids :
    NEUTRE = `{BTC: 0.5, ETH: 0.5}`, TILT_BTC = `{BTC: 1.0, ETH: 0.0}`, TILT_ETH =
    `{BTC: 0.0, ETH: 1.0}` — jamais de `NaN` (warm-up -> NEUTRE forcé, cf. `run_state_machine`),
    somme toujours exactement `1.0` (long-only, toujours investi à 100 % avant overlay)."""
    sg = generate_state(closes, params)
    state = sg["state"].to_numpy()
    w_btc = np.where(state == STATE_TILT_BTC, 1.0, np.where(state == STATE_TILT_ETH, 0.0, WEIGHT_NEUTRAL))
    w_eth = np.where(state == STATE_TILT_ETH, 1.0, np.where(state == STATE_TILT_BTC, 0.0, WEIGHT_NEUTRAL))
    weights = pd.DataFrame({"BTC": w_btc, "ETH": w_eth}, index=closes.index)
    # Garde défensive explicite (jamais de NaN en sortie, cf. audit F3 de `backtest/engine.py`) :
    # `state` ne devrait jamais être NaN par construction (warm-up -> NEUTRE forcé), mais un
    # `fillna` couvre silencieusement toute régression future sans jamais introduire un poids
    # non voulu (NEUTRE = 50/50, la valeur la plus prudente possible — jamais 0.0/0.0 qui
    # laisserait le portefeuille flat, contrairement à `protective_vol_gate` où 0.0 est la valeur
    # prudente : ici la SPEC exige "toujours investi à 100 %", donc la valeur prudente par défaut
    # est NEUTRE, pas flat).
    return weights.reindex(columns=UNIVERSE).fillna(WEIGHT_NEUTRAL)
