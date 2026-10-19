"""backtest/strategies/lowvol.py — signal « low volatility » de la candidate `lowvol_sp100`
(`backtest/LOWVOL-SP100-SPEC.md` §1, pré-enregistrée en session #11, committée AVANT toute
exécution — la SPEC fait foi en cas de divergence avec cette docstring).

Règles (SPEC §1, aucune liberté ajoutée ici) :
  1. Dates de décision = DERNIER jour de bourse de chaque mois civil du calendrier réel. Les
     poids sont décidés à la clôture `t`, exécutés à l'open `t+1` par le moteur (convention
     `backtest/engine.py`, jamais gérée ici) et GELÉS jusqu'à la décision suivante.
  2. Éligibilité (UNIFORME pour toutes les combinaisons de la grille — l'univers effectif ne
     dépend jamais de `L`) : au moins `MIN_HISTORY_DAYS` = 252 jours de bourse d'historique RÉEL
     (rendements quotidiens observés dans la série PROPRE du titre, jamais une barre synthétique
     ni un backfill) à la date de décision. Pas de point-in-time des constituants (biais du
     survivant documenté dans la SPEC).
     INTERPRÉTATION (signalée, pas résolue silencieusement) : « 252 jours d'historique réel »
     est compté en RENDEMENTS quotidiens (253 clôtures). C'est la seule lecture qui rend
     l'éligibilité indépendante de `L` : avec `L = 252`, l'écart-type exige 252 rendements
     complets ; compter 252 clôtures (251 rendements) rendrait la combinaison `L = 252`
     structurellement plus restrictive que `L = 63`, contredisant la SPEC.
  3. Volatilité réalisée = écart-type ÉCHANTILLON (ddof=1) des rendements quotidiens (clôtures
     ajustées) sur les `L` derniers jours de bourse du titre, annualisé (× √252).
  4. Portefeuille = les `N` titres éligibles de PLUS FAIBLE volatilité, poids égaux 1/N. Égalités
     de volatilité tranchées par ordre alphabétique du symbole (déterministe). Si moins de `N`
     titres sont éligibles (dégénéré, non attendu après 1996) : tous les éligibles équipondérés
     (1/n_éligibles). Aucun éligible : ligne de poids nuls (cash).
  5. AUCUN filtre de régime (pas de SMA200 SPY, pas de vol gate) : la candidate est toujours
     investie dès qu'elle a des titres éligibles.

Contrôle apparié (SPEC §2) : `generate_control_weights` — univers éligible ENTIER équipondéré
(aucune sélection), MÊMES dates de rebalance, MÊME éligibilité.

Causalité : la valeur à `t` ne dépend que de clôtures `<= t` (`pct_change`/`rolling` strictement
rétrospectifs, `ffill` borné vers l'avant uniquement). Le masque des dates de décision ne dépend
que du CALENDRIER (jamais des prix), cf. `decision_day_mask` (même convention que
`backtest/strategies/xsmom.py:_decision_day_mask`, y compris l'artefact documenté « la toute
dernière date du calendrier est marquée décision »).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from bot.strategies.xs_momentum_sp100 import UNIVERSE_SP100

__all__ = [
    "LowVolParams",
    "PARAM_GRID",
    "VOL_WINDOWS",
    "N_HOLDINGS",
    "MIN_HISTORY_DAYS",
    "TRADING_DAYS_PER_YEAR",
    "EXCLUDED_TICKERS",
    "effective_universe",
    "decision_day_mask",
    "compute_signal_panels",
    "generate_weight_decisions",
    "generate_control_weights",
    "signal_turnover_one_way",
]

VOL_WINDOWS = (63, 252)
N_HOLDINGS = (20, 30)
MIN_HISTORY_DAYS = 252  # SPEC §1 signal 1 (compté en rendements, cf. docstring module)
TRADING_DAYS_PER_YEAR = 252
MAX_FFILL_DAYS = 5  # même borne que backtest/data.py:align_to_calendar (filet de sécurité)

# SPEC §1 : « BK est indisponible dans market-data » (backlog #20).
# ANOMALIE CONSTATÉE À L'EXÉCUTION (signalée dans results.json/REPORT.md) : `UNIVERSE_SP100`
# ne contient AUCUN ticker `BK` (103 titres, dont BKNG, tous disponibles) — l'exclusion est donc
# sans effet et l'univers effectif chargé est de 103 titres, pas 102.
EXCLUDED_TICKERS = ("BK",)


@dataclass(frozen=True)
class LowVolParams:
    """Un point de la grille pré-enregistrée (SPEC §1) : `vol_window` = L, `n_holdings` = N."""

    vol_window: int = 63
    n_holdings: int = 20

    def __post_init__(self):
        if self.vol_window not in VOL_WINDOWS:
            raise ValueError(f"vol_window {self.vol_window!r} hors grille SPEC {VOL_WINDOWS}")
        if self.n_holdings not in N_HOLDINGS:
            raise ValueError(f"n_holdings {self.n_holdings!r} hors grille SPEC {N_HOLDINGS}")

    def to_dict(self) -> dict:
        return {"vol_window": self.vol_window, "n_holdings": self.n_holdings}


# Grille PRÉ-ENREGISTRÉE (SPEC §1) : L ∈ {63, 252} × N ∈ {20, 30}, rien d'autre.
PARAM_GRID: List[dict] = [
    {"vol_window": L, "n_holdings": N} for L in VOL_WINDOWS for N in N_HOLDINGS
]


def effective_universe(universe: Optional[Iterable[str]] = None) -> List[str]:
    """`UNIVERSE_SP100` privé de `EXCLUDED_TICKERS`, trié alphabétiquement (ordre utilisé pour
    départager les égalités de volatilité)."""
    base = list(UNIVERSE_SP100) if universe is None else list(universe)
    return sorted(t for t in base if t not in EXCLUDED_TICKERS)


def decision_day_mask(calendar: pd.DatetimeIndex) -> np.ndarray:
    """`True` pour chaque DERNIER jour de bourse de son mois civil dans `calendar`. Dépend du
    calendrier seul. Le dernier élément est `True` par construction (pas d'information sur un
    éventuel jour suivant non inclus) — artefact sans effet sur les métriques walk-forward."""
    cal = pd.DatetimeIndex(calendar)
    n = len(cal)
    mask = np.zeros(n, dtype=bool)
    if n == 0:
        return mask
    months = cal.to_period("M")
    mask[:-1] = months.values[:-1] != months.values[1:]
    mask[-1] = True
    return mask


def compute_signal_panels(
    raw_closes: Dict[str, pd.Series],
    calendar: pd.DatetimeIndex,
    universe: Sequence[str],
    vol_windows: Sequence[int] = VOL_WINDOWS,
) -> Dict[str, pd.DataFrame]:
    """Panneaux causaux (index = `calendar`, colonnes = `universe`) :
      - `n_returns` : nombre de rendements quotidiens RÉELS disponibles à `t` dans la série
        propre du titre (NaN avant la première cotation / si la donnée manque plus de
        `MAX_FFILL_DAYS` lignes) ;
      - `vol_<L>` : volatilité annualisée sur `L` jours (NaN tant que `L` rendements réels ne
        sont pas disponibles — `min_periods = L`).
    Chaque série est calculée sur SA PROPRE histoire (jamais réindexée avant le calcul), puis
    alignée sur le calendrier par `reindex` + `ffill(limit=MAX_FFILL_DAYS)` (propagation vers
    l'avant uniquement)."""
    universe = list(universe)
    n_ret_cols: Dict[str, pd.Series] = {}
    vol_cols: Dict[int, Dict[str, pd.Series]] = {L: {} for L in vol_windows}
    for sym in universe:
        s = raw_closes[sym].dropna()
        rets = s.pct_change()
        n_ret = pd.Series(np.arange(len(s), dtype=float), index=s.index)  # 0 pour la 1re clôture
        n_ret_cols[sym] = n_ret.reindex(calendar).ffill(limit=MAX_FFILL_DAYS)
        for L in vol_windows:
            v = rets.rolling(L, min_periods=L).std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)
            vol_cols[L][sym] = v.reindex(calendar).ffill(limit=MAX_FFILL_DAYS)
    panels: Dict[str, pd.DataFrame] = {"n_returns": pd.DataFrame(n_ret_cols, index=calendar)[universe]}
    for L in vol_windows:
        panels[f"vol_{L}"] = pd.DataFrame(vol_cols[L], index=calendar)[universe]
    return panels


def _eligible_mask(panels: Dict[str, pd.DataFrame], vol_window: int) -> pd.DataFrame:
    n_ret = panels["n_returns"]
    vol = panels[f"vol_{vol_window}"]
    return (n_ret >= MIN_HISTORY_DAYS) & vol.notna()


def _weights_from_panels(
    panels: Dict[str, pd.DataFrame],
    calendar: pd.DatetimeIndex,
    universe: Sequence[str],
    vol_window: int,
    n_holdings: Optional[int],
) -> pd.DataFrame:
    """Cœur commun candidate (`n_holdings = N`) / contrôle (`n_holdings = None` : aucun
    classement, tous les éligibles). Éligibilité identique pour les deux et pour tout `L`
    (assertée dans `generate_weight_decisions` par les tests)."""
    universe = list(universe)
    decision_mask = decision_day_mask(calendar)
    elig = _eligible_mask(panels, vol_window)
    vol = panels[f"vol_{vol_window}"]
    elig_vals = elig.to_numpy()
    vol_vals = vol.to_numpy()
    n_cols = len(universe)
    rows = {}
    for i in np.flatnonzero(decision_mask):
        e = elig_vals[i]
        row = np.zeros(n_cols)
        n_e = int(e.sum())
        if n_e > 0:
            if n_holdings is None or n_e <= n_holdings:
                row[e] = 1.0 / n_e  # contrôle, ou repli dégénéré (< N éligibles) : tous équipondérés
            else:
                idx_e = np.flatnonzero(e)
                # tri stable : à vol égale, l'ordre des colonnes (alphabétique) départage
                order = np.argsort(vol_vals[i, idx_e], kind="stable")
                chosen = idx_e[order[:n_holdings]]
                row[chosen] = 1.0 / n_holdings
        rows[calendar[i]] = row
    if rows:
        monthly = pd.DataFrame.from_dict(rows, orient="index", columns=universe).sort_index()
    else:
        monthly = pd.DataFrame(columns=universe, dtype=float)
    # gel jusqu'à la décision suivante : poids[t] = dernière ligne de décision <= t (causal)
    return monthly.reindex(calendar).ffill().fillna(0.0)[universe]


def generate_weight_decisions(
    raw_closes: Dict[str, pd.Series],
    calendar: pd.DatetimeIndex,
    params: LowVolParams,
    universe: Optional[Sequence[str]] = None,
    panels: Optional[Dict[str, pd.DataFrame]] = None,
) -> pd.DataFrame:
    """Matrice `weights_decided` (index = `calendar`, colonnes = `universe`) attendue par
    `backtest/engine.py:simulate_segment` pour la candidate à `params` (L, N). `panels` permet
    de réutiliser `compute_signal_panels` entre combinaisons (aucun effet sur le résultat)."""
    universe = effective_universe(universe)
    if panels is None:
        panels = compute_signal_panels(raw_closes, calendar, universe, (params.vol_window,))
    return _weights_from_panels(panels, calendar, universe, params.vol_window, params.n_holdings)


def generate_control_weights(
    raw_closes: Dict[str, pd.Series],
    calendar: pd.DatetimeIndex,
    universe: Optional[Sequence[str]] = None,
    panels: Optional[Dict[str, pd.DataFrame]] = None,
) -> pd.DataFrame:
    """Contrôle apparié (SPEC §2) : univers éligible ENTIER équipondéré aux MÊMES dates de
    rebalance. L'éligibilité est celle de la grille (≥ 252 rendements) ; le masque de
    disponibilité de la volatilité (`vol_<L>` non NaN) est redondant avec elle (vérifié par les
    tests), on utilise ici L = 63 sans conséquence sur l'ensemble éligible."""
    universe = effective_universe(universe)
    if panels is None:
        panels = compute_signal_panels(raw_closes, calendar, universe, (min(VOL_WINDOWS),))
    return _weights_from_panels(panels, calendar, universe, min(VOL_WINDOWS), None)


def signal_turnover_one_way(
    weights_decided: pd.DataFrame, calendar: pd.DatetimeIndex, start_idx: int, end_idx: int
) -> List[float]:
    """Turnover du SIGNAL à chaque date de décision tombant dans `[start_idx, end_idx]` :
    fraction du portefeuille remplacée = 0,5 · Σ|w_nouveau − w_précédent| (« one-way »), le
    précédent étant la ligne de décision immédiatement antérieure de la MÊME matrice (la première
    décision du calendrier n'a pas de précédent et est ignorée). C'est le turnover THÉORIQUE des
    poids cibles, avant bande de non-négociation (qui retient une partie des ordres)."""
    mask = decision_day_mask(calendar)
    dec_idx = np.flatnonzero(mask)
    vals = weights_decided.to_numpy()
    out: List[float] = []
    for k in range(1, len(dec_idx)):
        i = int(dec_idx[k])
        if i < start_idx or i > end_idx:
            continue
        prev = vals[int(dec_idx[k - 1])]
        out.append(float(0.5 * np.abs(vals[i] - prev).sum()))
    return out
