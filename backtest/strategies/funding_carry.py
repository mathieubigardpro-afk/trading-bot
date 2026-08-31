"""backtest/strategies/funding_carry.py — signal « funding carry delta-neutre » pour la
candidate `funding_carry_6majors` (`backtest/results/funding_carry_6majors/SPEC.md`,
pré-enregistrée 2026-08-31, backlog P0#1). Long spot / short perp de même notionnel par symbole,
piloté par le funding rate annualisé glissant. AUCUN paramètre ci-dessous n'est une liberté
d'implémentation : la grille, les seuils et les poids sont ceux de la SPEC ; toute interprétation
ambiguë est documentée et tranchée en défaveur de la candidate (ARCHITECTURE.md §0.2).

--------------------------------------------------------------------------------------------
Signal (cf. SPEC.md §"Signal") — rappel exact
--------------------------------------------------------------------------------------------
Par symbole, à la clôture horaire `t` :
  - `carry_ann(t)` = Σ des funding rates réglés dans la fenêtre `(t − D jours, t]` × `365/D`
    (annualisation par la durée réelle de la fenêtre `D`, PAS par le nombre de règlements —
    robuste aux épisodes d'intervalle 4h/2h sans aucun ré-échantillonnage : qu'un symbole règle
    toutes les 8h ou toutes les 4h pendant une partie de la fenêtre, la somme des rates réglés
    est simplement plus dense, la formule d'annualisation ne change pas).
  - Entrée (état actif) si `carry_ann(t) > θ_in` ; sortie (état flat) si
    `carry_ann(t) < θ_out = θ_in/2` ; entre les deux, état précédent conservé (hystérésis).
  - Poids actif : spot `+w`, perp `−w`, `w = 0.10` (fixé, non optimisé). Flat = 0 sur les deux
    jambes. Jamais de `NaN` : flat pendant le warm-up (fenêtre `D` incomplète) ou quand le
    perp/funding (ou le spot, cf. interprétation §3 ci-dessous) du symbole est indisponible.

--------------------------------------------------------------------------------------------
Interprétations tranchées (ambiguïtés de la SPEC, documentées comme demandé)
--------------------------------------------------------------------------------------------
  1. **Fenêtre glissante « durée réelle »** : implémentée via `DataFrame.rolling(f"{D}D",
     closed="right")` (fenêtre basée sur le TEMPS, pas sur un nombre fixe de lignes) plutôt que
     `rolling(D*24)` (nombre de lignes) — strictement équivalent sur un calendrier horaire
     parfaitement contigu, mais correct AUSSI en présence d'un trou de calendrier ponctuel (ex.
     l'heure DST 2023-03-24 absente de l'univers 6 majors, cf. `backtest/data_hourly.py`) :
     `rolling(D*24)` sur un calendrier avec un trou engloberait alors D jours + 1h de temps réel,
     `rolling(f"{D}D")` reste exact. `closed="right"` (défaut de pandas pour une fenêtre à offset
     temporel, vérifié empiriquement) donne exactement l'intervalle `(t−D jours, t]` de la SPEC
     (borne gauche exclue, `t` inclus) — aucun paramètre à forcer, mais rendu explicite ici pour
     qu'un audit n'ait pas à le redécouvrir dans la documentation pandas.
  2. **Warm-up** : `carry_ann(t)` est mis à `NaN` (donc `entry_signal`/`exit_signal` valent
     `False`, jamais `True` — toute comparaison avec `NaN` vaut `False` en pandas) tant que
     `t < calendar[0] + D jours` (le premier point du calendrier fourni à cette fonction) — le
     rolling à fenêtre temporelle de pandas produirait sinon une somme PARTIELLE non nulle dès
     la première bougie (fenêtre tronquée à gauche par le début de la série), ce qui n'est PAS
     "la fenêtre D entièrement disponible" au sens de la SPEC. Forcé explicitement, pas laissé au
     comportement par défaut de `rolling` (qui ne renvoie JAMAIS `NaN` pour une fenêtre à offset
     temporel, contrairement à une fenêtre à `N` lignes avec `min_periods`).
  3. **« Funding/perp indisponible »** : la SPEC (§"Signal") ne mentionne explicitement que
     perp/funding, mais un spot indisponible (`NaN`) rendrait la jambe couverte incohérente de la
     même façon (`+w` sur un prix inconnu) — interprétation étendue PAR PRUDENCE (ambiguïté
     tranchée en défaveur de la candidate, jamais en sa faveur) : un symbole est "disponible" à
     `t` seulement si SPOT, PERP (close) ET FUNDING sont tous les trois non-`NaN` à `t`. Sur le
     calendrier réellement utilisé par `run_funding_carry.py` (restreint à 2022-04-03→2026-07-31
     pour éviter les trous SOL-PERP, cf. SPEC.md) cette condition est TOUJOURS vraie — cette
     branche ne joue donc aucun rôle sur le run nominal, elle est testée séparément sur données
     synthétiques (`backtest/tests/test_funding_carry.py`).
  4. **Indisponibilité ET état** : quand un symbole devient indisponible, la position est forcée
     FLAT immédiatement (jamais de position maintenue sur donnée manquante) ET l'état interne de
     la machine à états est réinitialisé à `False` — au retour de la donnée, une nouvelle entrée
     franche (`carry_ann(t) > θ_in`) est requise, jamais une reprise automatique d'une position
     qui aurait traversé le trou sans being tradée (cohérent avec `engine.simulate_segment`, qui
     de toute façon lève `ValueError` sur un prix/funding perp `NaN` avec un poids cible non nul
     — cette strategie ne doit jamais produire un tel poids).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

import numpy as np
import pandas as pd

from backtest import perp as bt_perp

__all__ = [
    "FundingCarryParams",
    "PARAM_GRID",
    "WEIGHT_PER_SYMBOL",
    "DAYS_PER_YEAR",
    "compute_carry_ann",
    "generate_weight_decisions",
]

# Poids fixe non optimisé (SPEC.md §"Signal" — justification chiffrée de la contrainte de
# faisabilité du moteur : 6 * 0.10 * 1.5 = 0.90 <= 1, marge de 10%).
WEIGHT_PER_SYMBOL = 0.10

DAYS_PER_YEAR = 365.0

# Grille pré-enregistrée EXACTE (SPEC.md §"Grille pré-enregistrée") — 4 combinaisons, rien
# d'autre. `backtest/run_funding_carry.py` importe CETTE constante (jamais une valeur ad hoc).
PARAM_GRID = [
    {"window_days": 7, "theta_in": 0.05},
    {"window_days": 7, "theta_in": 0.10},
    {"window_days": 30, "theta_in": 0.05},
    {"window_days": 30, "theta_in": 0.10},
]


@dataclass(frozen=True)
class FundingCarryParams:
    """Un point de la grille. `window_days` (`D`) et `theta_in` sont les DEUX seuls axes de
    variation autorisés par la SPEC (`PARAM_GRID` ci-dessus). `theta_out` est DÉRIVÉ (fixé à la
    moitié de `theta_in`, non optimisé — SPEC.md §"Signal"), jamais un axe de grille indépendant."""

    window_days: int
    theta_in: float

    @property
    def theta_out(self) -> float:
        return self.theta_in / 2.0


def compute_carry_ann(funding: pd.DataFrame, window_days: int) -> pd.DataFrame:
    """`carry_ann(t)` causal par colonne (chaque colonne = un symbole perp `<SYM>-PERP`,
    indépendant des autres) : somme des taux de `funding` (matrice alignée, valeur à la ligne `i`
    = réglé À LA CLÔTURE de la bougie `i`, cf. `backtest/perp.py:align_funding_to_calendar`) sur
    la fenêtre TEMPORELLE `(t − window_days jours, t]`, multipliée par `365/window_days` (SPEC.md
    §"Signal", interprétation §1 de la docstring module pour le choix d'une fenêtre à offset
    temporel plutôt qu'à nombre de lignes fixe).

    Causalité stricte : `carry_ann.loc[t]` ne dépend que de `funding.loc[:t]` — un `rolling`
    pandas ne regarde jamais au-delà de la position courante, et la fenêtre à offset temporel
    ne peut PAR CONSTRUCTION pas inclure d'index `> t` (bornes `(t-D, t]`, cf. vérification
    empirique dans `backtest/tests/test_funding_carry.py::test_causality_...`).

    `NaN` (donc flat après comparaison, interprétation §2) tant que `t < funding.index[0] +
    window_days jours` (fenêtre `D` pas entièrement disponible depuis le tout début de la série
    fournie — warm-up, SPEC.md §"Signal")."""
    if window_days <= 0:
        raise ValueError(f"window_days doit être > 0, reçu {window_days!r}")
    offset = f"{window_days}D"
    rolling_sum = funding.rolling(offset, closed="right").sum()
    carry_ann = rolling_sum * (DAYS_PER_YEAR / float(window_days))

    if len(funding.index) > 0:
        warm_cutoff = funding.index[0] + pd.Timedelta(days=window_days)
        warm_mask = funding.index < warm_cutoff
        carry_ann = carry_ann.copy()
        carry_ann.loc[warm_mask, :] = np.nan
    return carry_ann


def _positions_from_carry(
    entry_signal: pd.DataFrame, exit_signal: pd.DataFrame, available: pd.DataFrame
) -> pd.DataFrame:
    """Machine à états CAUSALE, itération séquentielle sur les LIGNES du calendrier (vectorisée
    sur les COLONNES/symboles à chaque pas, même style que
    `backtest/strategies/vol_breakout.py::_positions_from_signals`) :

      `pos[t] = True`  si (`pos[t-1] == False` ET `entry_signal[t]` ET `available[t]`) OU
                          (`pos[t-1] == True` ET NON `exit_signal[t]` ET `available[t]`)
      `pos[t] = False` sinon (y compris FORCÉ si `available[t]` est `False` — interprétations
      §3-4 de la docstring module : une donnée indisponible force flat ET réinitialise l'état,
      une entrée n'est jamais évaluée sur donnée indisponible).

    `pos[t]` ne dépend que de `entry_signal[<=t]`/`exit_signal[<=t]`/`available[<=t]` (récurrence
    causale par construction)."""
    index = entry_signal.index
    columns = entry_signal.columns
    entry_arr = entry_signal.to_numpy(dtype=bool)
    exit_arr = exit_signal.to_numpy(dtype=bool)
    avail_arr = available.to_numpy(dtype=bool)
    n, m = entry_arr.shape
    pos = np.zeros((n, m), dtype=bool)
    state = np.zeros(m, dtype=bool)
    for i in range(n):
        avail_i = avail_arr[i]
        entry_i = entry_arr[i] & avail_i
        exit_i = exit_arr[i] | (~avail_i)
        entry_mask = (~state) & entry_i
        exit_mask = state & exit_i
        new_state = state.copy()
        new_state[entry_mask] = True
        new_state[exit_mask] = False
        new_state = new_state & avail_i
        pos[i] = new_state
        state = new_state
    return pd.DataFrame(pos, index=index, columns=columns)


def generate_weight_decisions(
    spot_closes: pd.DataFrame,
    perp_closes: pd.DataFrame,
    funding: pd.DataFrame,
    symbols: Sequence[str],
    params: FundingCarryParams,
) -> pd.DataFrame:
    """Construit `weights_decided` (index = calendrier commun de `spot_closes`/`perp_closes`/
    `funding`, DEUX colonnes par symbole : `<SYM>` spot et `<SYM>-PERP` perp) attendu par
    `backtest/engine.py::simulate_segment`.

    `spot_closes` : colonnes >= `symbols` (clôtures spot, `<SYM>`).
    `perp_closes` : colonnes >= `<SYM>-PERP` pour chaque symbole (clôtures perp, utilisées
        UNIQUEMENT pour détecter la disponibilité du symbole, interprétation §3 — le signal
        lui-même ne dépend que du funding, SPEC.md §"Signal").
    `funding` : matrice alignée (`backtest/perp.py:build_aligned_perp_matrices`), colonnes
        `<SYM>-PERP`, valeur = taux réglé à la clôture de chaque bougie (jamais `NaN` par
        construction de ce loader, mais cette fonction reste défensive si un appelant de test
        fournit une matrice avec des `NaN` explicites).

    Toutes les trois DOIVENT partager le même index (calendrier) — aucune réindexation n'est
    faite ici (responsabilité de l'appelant, cf. `backtest/run_funding_carry.py`)."""
    symbols = list(symbols)
    calendar = spot_closes.index
    perp_cols = [bt_perp.perp_column_name(s) for s in symbols]

    carry_ann = compute_carry_ann(funding[perp_cols], params.window_days)
    entry_signal = carry_ann > params.theta_in
    exit_signal = carry_ann < params.theta_out

    spot_avail = spot_closes[symbols].notna().to_numpy()
    perp_avail = perp_closes[perp_cols].notna().to_numpy()
    funding_avail = funding[perp_cols].notna().to_numpy()
    available = pd.DataFrame(
        spot_avail & perp_avail & funding_avail, index=calendar, columns=symbols
    )
    entry_signal.columns = symbols
    exit_signal.columns = symbols

    positions = _positions_from_carry(entry_signal, exit_signal, available)

    spot_weights = positions.astype(float) * WEIGHT_PER_SYMBOL
    perp_weights = -positions.astype(float) * WEIGHT_PER_SYMBOL
    perp_weights.columns = perp_cols

    weights = pd.concat([spot_weights, perp_weights], axis=1)
    # Garde défensive explicite (jamais de NaN en sortie, cf. docstring + audit F3 de
    # `backtest/engine.py`) : par construction ci-dessus il ne peut pas y en avoir (positions
    # est un bool, jamais NaN), un `fillna(0.0)` couvre silencieusement toute régression future
    # sans jamais introduire de poids non voulu (0.0 = flat, la valeur la plus prudente).
    return weights.fillna(0.0)
