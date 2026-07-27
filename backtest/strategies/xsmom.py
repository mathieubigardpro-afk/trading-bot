"""backtest/strategies/xsmom.py — VERSION BACKTEST de `bot/strategies/xs_momentum_sp100.py`,
consommée par `backtest/engine.py`. Reproduit fidèlement l'algorithme de production (mêmes
règles, mêmes seuils, cf. docstring du module de production pour la justification complète) et
AJOUTE le paramètre `weighting` ("equal" | "inv_vol") requis par la candidate P0#3 du backlog
(`docs/RESEARCH-BACKLOG.md` idée #3) — "equal" reproduit EXACTEMENT le réglage de production
(sert de contrôle pour valider ce moteur, cf. `backtest/run_xsmom_invvol.py`).

Les constantes SPEC (`UNIVERSE_SP100`, `TOP_K`, `SKIP_DAYS`, `LOOKBACK_DAYS`, `SMA_DAYS`,
`VOL_LOOKBACK_DAYS`, `MARKET_FILTER_SYMBOL`) sont IMPORTÉES depuis le module de production
(`bot.strategies.xs_momentum_sp100`, lecture seule, jamais dupliquées par valeur) — seule
source de vérité, pour ne jamais risquer une divergence de transcription entre les deux univers.

--------------------------------------------------------------------------------------------
Différences volontaires avec le module de production (toutes des adaptations de PERFORMANCE
pour un calcul vectorisé sur ~30 ans d'historique, jamais des changements de RÈGLE) :
--------------------------------------------------------------------------------------------
  - Le module de production est appelé une fois par cycle (horaire, `target_weights()` pur,
    stateless) et recalcule tout à chaque appel. Ici, l'historique complet est connu à l'avance
    (backtest), donc le calcul du score momentum et de la volatilité réalisée est VECTORISÉ une
    fois pour toutes les dates (via `Series.shift()`/`Series.rolling()`, strictement causal :
    la valeur à la date `t` ne dépend que de données `<= t`, mathématiquement identique à
    l'appel répété de `_momentum_as_of()`/`_market_regime_on()` du module de production à
    chaque date). Le classement (`top_k` puis filtre momentum strictement positif) n'est en
    revanche recalculé que sur les VRAIES dates de décision mensuelles (~400 sur toute la
    période, cf. mission), pas 8000+ fois par jour — inutile puisque le résultat est identique
    tant que la date de décision ne change pas (cf. point dur (1) du module de production).
  - Le filtre de régime SPY>SMA200 reste réévalué CHAQUE jour de bourse (comme en production,
    point dur (2)) : dès qu'il repasse "off", la poche entière repasse cash IMMÉDIATEMENT, pas
    seulement au prochain mois-fin.
  - Pondération `inv_vol` : la volatilité réalisée (63 jours de bourse, `VOL_LOOKBACK_DAYS`,
    documentée mais non utilisée par le réglage de production figé) est évaluée À LA DATE DE
    DÉCISION du mois (comme le classement momentum lui-même — même principe de gel mensuel que
    le point dur (1) de la production, pour une cohérence interne : le poids ne doit pas
    "dériver" en cours de mois sur une pondération recalculée quotidiennement alors que le
    classement, lui, reste gelé). Un titre gagnant du classement mais SANS 63 jours de bourse
    d'historique RÉEL à la date de décision (cas structurellement rare : le seuil d'éligibilité
    momentum, 148 jours, dépasse toujours le seuil de volatilité, 64 jours de rendements requis
    -- donc si un titre est éligible au momentum il l'est presque toujours aussi à la vol) est
    EXCLU de la pondération inv_vol ce mois-là (poids 0, capital réparti au prorata de 1/sigma
    parmi les autres gagnants valides) — si AUCUN gagnant n'a de vol valide (cas dégénéré,
    jamais observé sur ce jeu de données mais gardé par prudence défensive), repli explicite sur
    l'équipondération pour ce mois-là plutôt que de laisser la poche à tort en cash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
import pandas as pd

from bot.strategies.xs_momentum_sp100 import (
    LOOKBACK_DAYS,
    MARKET_FILTER_SYMBOL,
    SKIP_DAYS,
    SMA_DAYS,
    TOP_K,
    UNIVERSE_SP100,
    VOL_LOOKBACK_DAYS,
)

__all__ = [
    "XsMomParams",
    "generate_weight_decisions",
    "UNIVERSE_SP100",
    "MARKET_FILTER_SYMBOL",
]


@dataclass(frozen=True)
class XsMomParams:
    """Paramètres du backtest — valeurs par défaut = réglages de production FIGÉS (SPEC), cf.
    `bot/strategies/xs_momentum_sp100.py`. `weighting` est le seul axe de variation couvert par
    la candidate P0#3 ; tout le reste est un import direct des constantes de production, jamais
    retouché ("aucun seuil n'est amélioré ici", même exigence que le module de production)."""

    weighting: str = "equal"  # "equal" | "inv_vol"
    top_k: int = TOP_K
    skip_days: int = SKIP_DAYS
    lookback_days: int = LOOKBACK_DAYS
    sma_days: int = SMA_DAYS
    vol_lookback_days: int = VOL_LOOKBACK_DAYS

    def __post_init__(self):
        if self.weighting not in ("equal", "inv_vol"):
            raise ValueError(f"weighting invalide: {self.weighting!r}")


def _momentum_matrix(
    raw_closes: Dict[str, pd.Series],
    calendar: pd.DatetimeIndex,
    skip_days: int,
    lookback_days: int,
) -> pd.DataFrame:
    """Score momentum 6-1 causal, vectorisé par titre sur SA PROPRE série brute (jamais
    réindexée avant ce calcul, cf. docstring module), puis aligné sur le calendrier canonique.
    `NaN` avant que `skip_days + lookback_days` observations réelles ne soient disponibles —
    exactement l'éligibilité de `bot.strategies.xs_momentum_sp100._momentum_as_of`."""
    cols = {}
    for sym, s in raw_closes.items():
        cols[sym] = s.shift(skip_days) / s.shift(skip_days + lookback_days) - 1.0
    df = pd.DataFrame(cols)
    return df.reindex(calendar)


def _vol_matrix(
    raw_closes: Dict[str, pd.Series], calendar: pd.DatetimeIndex, vol_lookback_days: int
) -> pd.DataFrame:
    """Volatilité réalisée = écart-type des rendements quotidiens sur `vol_lookback_days` jours
    de bourse (fenêtre glissante causale, `min_periods` par défaut = fenêtre entière -> `NaN`
    tant que `vol_lookback_days` rendements réels ne sont pas disponibles, cf. docstring module)."""
    cols = {}
    for sym, s in raw_closes.items():
        rets = s.pct_change()
        cols[sym] = rets.rolling(vol_lookback_days).std()
    df = pd.DataFrame(cols)
    return df.reindex(calendar)


def _regime_series(spy_raw_close: pd.Series, calendar: pd.DatetimeIndex, sma_days: int) -> pd.Series:
    """`True` si la dernière clôture SPY disponible (causale) est strictement au-dessus de sa
    SMA `sma_days`. `NaN` (warmup insuffisant) traité comme régime "off" par défaut PRUDENT
    (aucune position tant que la SMA200 n'est pas fiable — cohérent avec le comportement final
    de `bot.strategies.xs_momentum_sp100._market_regime_on` : `None` -> poche gelée -> cash
    tant qu'aucune position n'a jamais été prise, ce qui est le cas ici en tout début
    d'historique)."""
    sma = spy_raw_close.rolling(sma_days).mean()
    regime = spy_raw_close > sma
    regime = regime.reindex(calendar)
    return regime.fillna(False)


def _decision_day_mask(calendar: pd.DatetimeIndex) -> np.ndarray:
    """`True` pour chaque date qui est le DERNIER jour de bourse de son mois civil dans
    `calendar` — reproduction, à partir du calendrier RÉEL, de
    `bot.strategies.xs_momentum_sp100._is_last_trading_day_of_month` (résultat identique : un
    jour est "confirmé dernier jour de bourse du mois" si et seulement si aucun jour de bourse
    plus tard dans le même mois civil n'existe dans le calendrier réel). Note : le tout DERNIER
    jour du calendrier fourni est toujours marqué `True` par construction (pas d'information sur
    un éventuel jour de bourse suivant non inclus dans les données) — artefact sans effet sur les
    métriques walk-forward, qui n'utilisent jamais la toute fin non couverte par une fenêtre OOS
    complète (cf. `backtest/engine.py:generate_walk_forward_windows`)."""
    cal = pd.DatetimeIndex(calendar)
    n = len(cal)
    mask = np.zeros(n, dtype=bool)
    if n == 0:
        return mask
    months = cal.to_period("M")
    mask[:-1] = months.values[:-1] != months.values[1:]
    mask[-1] = True
    return mask


def _rank_and_select(mom_row: pd.Series, top_k: int):
    """Reproduction exacte de `bot.strategies.xs_momentum_sp100._rank_and_select` : tri par
    momentum décroissant, tie-break alphabétique, top `top_k`, puis filtre momentum strictement
    positif. Retourne une `pd.Series` (index = symboles gagnants, valeurs = momentum), vide si
    aucun gagnant."""
    elig = mom_row.dropna()
    if elig.empty:
        return pd.Series(dtype=float)
    tmp = elig.rename("mom").reset_index()
    tmp.columns = ["symbol", "mom"]
    tmp = tmp.sort_values(by=["mom", "symbol"], ascending=[False, True], kind="mergesort")
    top = tmp.iloc[:top_k]
    winners = top[top["mom"] > 0.0]
    return winners.set_index("symbol")["mom"]


def generate_weight_decisions(
    raw_closes: Dict[str, pd.Series],
    spy_raw_close: pd.Series,
    calendar: pd.DatetimeIndex,
    params: XsMomParams,
) -> pd.DataFrame:
    """Construit la matrice `weights_decided` (index=`calendar`, colonnes=`UNIVERSE_SP100`)
    attendue par `backtest/engine.py::simulate_segment` : `weights_decided.loc[t]` = poids
    DÉCIDÉS à la clôture de `t` (à exécuter à l'ouverture de `t+1`), calculés UNIQUEMENT à
    partir de clôtures `<= t` (aucune colonne de ce DataFrame ne dépend d'une donnée future,
    cf. `_momentum_matrix`/`_vol_matrix`/`_regime_series`, toutes strictement causales).
    Convention de calendrier des dates de décision et de gel mensuel : cf. docstring module."""
    universe = list(UNIVERSE_SP100)
    mom = _momentum_matrix(raw_closes, calendar, params.skip_days, params.lookback_days)
    vol = _vol_matrix(raw_closes, calendar, params.vol_lookback_days) if params.weighting == "inv_vol" else None
    regime = _regime_series(spy_raw_close, calendar, params.sma_days)
    decision_mask = _decision_day_mask(calendar)

    decision_rows = {}
    for i, is_decision in enumerate(decision_mask):
        if not is_decision:
            continue
        date = calendar[i]
        mom_row = mom.loc[date, universe]
        winners = _rank_and_select(mom_row, params.top_k)
        row = pd.Series(0.0, index=universe)
        if not winners.empty:
            if params.weighting == "equal":
                row[winners.index] = 1.0 / len(winners)
            else:  # inv_vol
                vol_row = vol.loc[date, winners.index]
                valid = vol_row[(vol_row.notna()) & (vol_row > 0.0)]
                if valid.empty:
                    # Repli défensif documenté (jamais observé empiriquement, cf. docstring
                    # module) : équipondération plutôt qu'une poche à tort vide.
                    row[winners.index] = 1.0 / len(winners)
                else:
                    inv = 1.0 / valid
                    w = inv / inv.sum()
                    row[valid.index] = w
                    # titres gagnants sans vol valide : poids explicitement 0 (exclus), capital
                    # déjà intégralement réparti ci-dessus parmi les gagnants valides.
        decision_rows[date] = row

    if decision_rows:
        monthly = pd.DataFrame(decision_rows).T
        monthly = monthly.reindex(columns=universe).sort_index()
    else:
        monthly = pd.DataFrame(columns=universe)

    weights = monthly.reindex(calendar).ffill().fillna(0.0)
    weights = weights.multiply(regime.astype(float), axis=0)
    return weights[universe]
