"""backtest/strategies/vol_breakout.py — signal "squeeze de Bollinger puis cassure" pour la
candidate `vol_breakout_6majors` (`backtest/results/vol_breakout_6majors/SPEC.md`, pré-enregistré
2026-08-03). Long-only, par symbole, sur clôtures HORAIRES. AUCUN paramètre ci-dessous n'est une
liberté d'implémentation : la grille, les seuils et les fenêtres sont ceux de la SPEC ; toute
interprétation ambiguë est documentée et tranchée en défaveur de la stratégie (§0.2
`ARCHITECTURE.md`, rappelé par la mission).

--------------------------------------------------------------------------------------------
Signal (cf. SPEC.md §"Signal") -- rappel exact
--------------------------------------------------------------------------------------------
  - Bandes de Bollinger : fenêtre `W` heures, k = 2,0. `middle = SMA(close, W)`,
    `upper = middle + k*std`, `lower = middle - k*std`, `bandwidth = (upper-lower)/middle`.
  - Squeeze actif à t : rank-percentile CAUSAL de `bandwidth(t)` sur les 2160 heures glissantes
    (90 jours) se terminant à t (INCLUS) est `<= P`.
  - Entrée (poids 1/6) si, à la clôture t : (1) squeeze actif à t OU à au moins une des 24
    heures précédentes ; (2) `close(t) > upper_band(t)` ; (3) `close(t) > SMA(close, 4800h)`.
  - Sortie (poids 0) : `close(t) < middle_band(t)`.
  - Entre entrée et sortie, poids = 1/6 constant. Sinon 0.0 -- JAMAIS de `NaN`, y compris
    pendant le warm-up (4800h, le plus long des lookbacks).

--------------------------------------------------------------------------------------------
Interprétations tranchées (ambiguïtés de la SPEC, documentées comme demandé -- toutes
défavorables à la candidate)
--------------------------------------------------------------------------------------------
  1. **Écart-type des bandes de Bollinger** : la SPEC ne précise pas `ddof` (population vs
     échantillon). Choisi : écart-type ÉCHANTILLON (`ddof=1`, défaut de `pandas.Series.rolling
     .std()` -- aucun paramètre à surcharger). Un `ddof=1` produit un écart-type légèrement PLUS
     GRAND qu'un `ddof=0` pour la même fenêtre (`sqrt(W/(W-1))` plus grand), donc des bandes plus
     LARGES : `close > upper_band` est mathématiquement PLUS DUR à satisfaire -- interprétation
     la plus défavorable au nombre d'entrées et donc au Sharpe/PF de la candidate.
  2. **Fenêtre du rank-percentile** : "sur fenêtre glissante de 2160h" est interprétée comme les
     2160 heures se terminant à `t` INCLUS (`rolling(2160)`, jamais `rolling(2160).shift(1)` qui
     exclurait `t` -- exclure `t` de son propre percentile serait un choix, mais l'inclusion est
     la définition standard d'un rank-percentile "à la date t" et est STRICTEMENT CAUSALE de la
     même façon : aucune donnée `> t` n'est utilisée). Le percentile est `NaN` tant que 2160
     observations réelles ne sont pas disponibles -> squeeze considéré INACTIF (pas une licence
     à entrer) pendant ce warm-up, jamais l'inverse.
  3. **"24 heures précédentes"** : interprété comme un OR sur une fenêtre de 25 heures glissante
     se terminant à `t` inclus (`t`, `t-1`, ..., `t-24`), jamais 24h *après* `t` (aucune lecture
     alternative causale n'existe ici).
  4. **Simultanéité entrée/sortie** : si `close(t)` satisfait à la fois une condition de sortie
     (`< middle`) alors que le symbole est déjà FLAT (donc la sortie ne s'applique à rien) et une
     condition d'entrée le même jour, l'entrée est évaluée normalement (les deux conditions ne
     sont, par construction du signal, jamais logiquement contradictoires pour un même symbole
     au même instant : la sortie ne s'applique qu'à une position déjà ouverte, l'entrée qu'à une
     position fermée -- cf. `_positions_from_signals`).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = [
    "VolBreakoutParams",
    "BOLLINGER_K",
    "SQUEEZE_LOOKBACK_HOURS",
    "ENTRY_SQUEEZE_LOOKBACK_HOURS",
    "REGIME_SMA_HOURS",
    "PARAM_GRID",
    "generate_weight_decisions",
]

BOLLINGER_K = 2.0
SQUEEZE_LOOKBACK_HOURS = 2160  # 90 jours, aligné REGIME_ATR_PERCENTILE_WINDOW_DAYS (bot/config.py)
ENTRY_SQUEEZE_LOOKBACK_HOURS = 24  # "à t ou dans les 24h précédentes" -> fenêtre de 25h (t inclus)
REGIME_SMA_HOURS = 4800  # équivalent horaire de REGIME_SMA_DAYS=200 (bot/config.py) : 200*24
PORTFOLIO_WEIGHT_PER_SYMBOL = 1.0 / 6.0  # univers fixe des 6 majors (SPEC.md)

# Grille pré-enregistrée EXACTE (SPEC.md §"Grille pré-enregistrée") -- 4 combinaisons, rien
# d'autre. `backtest/run_vol_breakout.py` importe CETTE constante (jamais une valeur ad hoc) pour
# garantir qu'aucune combinaison hors grille n'est testée.
PARAM_GRID = [
    {"window_hours": 55, "squeeze_percentile": 0.20},
    {"window_hours": 55, "squeeze_percentile": 0.35},
    {"window_hours": 110, "squeeze_percentile": 0.20},
    {"window_hours": 110, "squeeze_percentile": 0.35},
]


@dataclass(frozen=True)
class VolBreakoutParams:
    """Un point de la grille. `window_hours` (`W`) et `squeeze_percentile` (`P`) sont les DEUX
    seuls axes de variation autorisés par la SPEC (`PARAM_GRID` ci-dessus) ; cette dataclass reste
    volontairement générique (pas de validation qui figerait `window_hours`/`squeeze_percentile`
    aux seules valeurs de la grille) car elle est aussi réutilisée par les tests synthétiques de
    `backtest/tests/test_vol_breakout.py` sur des fixtures minuscules -- la discipline "jamais
    hors grille" est appliquée au niveau du SCRIPT D'ORCHESTRATION (`run_vol_breakout.py`
    n'importe et n'itère QUE sur `PARAM_GRID`), pas ici."""

    window_hours: int
    squeeze_percentile: float
    k: float = BOLLINGER_K
    squeeze_lookback_hours: int = SQUEEZE_LOOKBACK_HOURS
    entry_squeeze_lookback_hours: int = ENTRY_SQUEEZE_LOOKBACK_HOURS
    regime_sma_hours: int = REGIME_SMA_HOURS


def _bollinger_bands(closes: pd.DataFrame, window_hours: int, k: float):
    """Bandes de Bollinger CAUSALES par colonne (chaque colonne = un symbole, indépendant des
    autres) : `middle.iloc[t]`/`upper.iloc[t]`/`lower.iloc[t]`/`bandwidth.iloc[t]` ne dépendent
    que de `closes.iloc[t-window_hours+1 : t+1]` (fenêtre `rolling`, jamais de `shift` négatif ni
    de fenêtre centrée). `NaN` tant que `window_hours` observations réelles ne sont pas
    disponibles (warm-up), cf. interprétation §1 de la docstring module pour `ddof`."""
    middle = closes.rolling(window_hours).mean()
    std = closes.rolling(window_hours).std()  # ddof=1 (défaut pandas) -- interprétation §1
    upper = middle + k * std
    lower = middle - k * std
    bandwidth = (upper - lower) / middle
    return middle, upper, lower, bandwidth


def _squeeze_active(bandwidth: pd.DataFrame, lookback_hours: int, percentile: float) -> pd.DataFrame:
    """`True` à `t` si le rank-percentile CAUSAL de `bandwidth.iloc[t]` sur les `lookback_hours`
    heures se terminant à `t` (inclus) est `<= percentile` (`rolling(...).rank(pct=True)`,
    strictement causal : la fonction `rolling` de pandas ne regarde jamais au-delà de la position
    courante). `NaN` (warm-up, moins de `lookback_hours` observations réelles) -> comparaison
    `NaN <= percentile` vaut `False` en pandas -> squeeze considéré INACTIF (interprétation §2)."""
    pct_rank = bandwidth.rolling(lookback_hours).rank(pct=True)
    return pct_rank <= percentile


def _squeeze_recent(squeeze_active: pd.DataFrame, lookback_hours: int) -> pd.DataFrame:
    """`True` à `t` si `squeeze_active` est `True` à `t` OU à au moins une des `lookback_hours`
    heures PRÉCÉDENTES (fenêtre de `lookback_hours + 1` heures se terminant à `t` inclus,
    interprétation §3 de la docstring module) -- strictement causal (`rolling`, jamais de
    donnée `> t`)."""
    window = lookback_hours + 1
    recent_max = squeeze_active.astype(float).rolling(window, min_periods=1).max()
    return recent_max.fillna(0.0) > 0.5


def _regime_filter(closes: pd.DataFrame, regime_sma_hours: int) -> pd.DataFrame:
    """`True` à `t` si `close(t) > SMA(close, regime_sma_hours)(t)` (SMA CAUSALE, `rolling`).
    `NaN` (warm-up) -> comparaison `False` -> régime "off" par défaut PRUDENT (aucune position
    tant que le filtre n'est pas fiable, même convention que `backtest/strategies/xsmom.py
    ._regime_series`)."""
    sma = closes.rolling(regime_sma_hours).mean()
    return closes > sma


def _positions_from_signals(entry_signal: pd.DataFrame, exit_signal: pd.DataFrame) -> pd.DataFrame:
    """Machine à états CAUSALE, par colonne (symbole), appliquée en une seule passe vectorisée
    sur les COLONNES (une itération Python par LIGNE du calendrier, ~40k lignes -- rapide, pas de
    boucle imbriquée par symbole) :

      `pos[t] = True`  si (`pos[t-1] == False` et `entry_signal[t] == True`) OU
                          (`pos[t-1] == True` et `exit_signal[t] == False`)
      `pos[t] = False` sinon.

    C'est-à-dire : une entrée n'est évaluée QUE si le symbole est flat au tick précédent, une
    sortie QUE s'il est en position au tick précédent -- exactement "le poids reste 1/6 entre
    entrée et sortie" de la SPEC. `pos[t]` ne dépend que de `entry_signal[<=t]`/`exit_signal[<=t]`
    (récurrence causale par construction), donc de `closes[<=t]` en amont -- aucune fuite."""
    index = entry_signal.index
    columns = entry_signal.columns
    entry_arr = entry_signal.to_numpy(dtype=bool)
    exit_arr = exit_signal.to_numpy(dtype=bool)
    n, m = entry_arr.shape
    pos = np.zeros((n, m), dtype=bool)
    state = np.zeros(m, dtype=bool)
    for i in range(n):
        entry_mask = (~state) & entry_arr[i]
        exit_mask = state & exit_arr[i]
        new_state = state.copy()
        new_state[entry_mask] = True
        new_state[exit_mask] = False
        pos[i] = new_state
        state = new_state
    return pd.DataFrame(pos, index=index, columns=columns)


def generate_weight_decisions(closes: pd.DataFrame, params: VolBreakoutParams) -> pd.DataFrame:
    """Construit `weights_decided` (index=calendrier horaire commun, colonnes=`closes.columns`)
    attendu par `backtest/engine.py::simulate_segment` : `weights_decided.loc[t]` = poids DÉCIDÉS
    à la clôture de `t` (exécutés à `open[t+1]` par le moteur), calculés UNIQUEMENT à partir de
    `closes.loc[:t]` (toutes les fonctions internes -- `_bollinger_bands`/`_squeeze_active`/
    `_squeeze_recent`/`_regime_filter`/`_positions_from_signals` -- sont strictement causales,
    cf. leurs docstrings). Valeurs : `1/6` (en position) ou `0.0` (flat) -- jamais de `NaN`, y
    compris pendant le warm-up (`closes.rolling(...)` produit du `NaN` en amont, mais toute
    comparaison avec `NaN` vaut `False` en pandas, donc `entry_signal`/`exit_signal` sont
    `False` -- jamais `NaN` -- pendant le warm-up, et la position reste `False` -> poids `0.0`,
    cf. `_positions_from_signals`)."""
    universe = list(closes.columns)
    middle, upper, _lower, bandwidth = _bollinger_bands(closes, params.window_hours, params.k)
    squeeze_active = _squeeze_active(bandwidth, params.squeeze_lookback_hours, params.squeeze_percentile)
    squeeze_recent = _squeeze_recent(squeeze_active, params.entry_squeeze_lookback_hours)
    regime_ok = _regime_filter(closes, params.regime_sma_hours)

    entry_signal = squeeze_recent & (closes > upper) & regime_ok
    exit_signal = closes < middle

    positions = _positions_from_signals(entry_signal, exit_signal)
    weights = positions.astype(float) * PORTFOLIO_WEIGHT_PER_SYMBOL
    weights = weights.reindex(columns=universe)
    # Garde défensive explicite (jamais de NaN en sortie, cf. docstring + audit F3 de
    # `backtest/engine.py`) : par construction ci-dessus il ne peut pas y en avoir, mais un
    # `fillna(0.0)` couvre silencieusement toute régression future sans jamais introduire de
    # poids > 0 non voulu (0.0 = flat, la valeur la plus prudente possible).
    return weights.fillna(0.0)
