"""backtest/metrics.py — métriques de décision du moteur commun (backtest/engine.py).

Toutes les métriques ci-dessous sont volontairement calculées sur des RENDEMENTS QUOTIDIENS
(barres journalières, cf. mission), annualisées avec la convention `sqrt(252)` (252 jours de
bourse/an, convention académique standard — PAS `sqrt(8760)` utilisé ailleurs dans ce dépôt
pour les rendements HORAIRES crypto de `bot/risk/vol_targeting.py`, à ne pas confondre).

Le Deflated Sharpe Ratio (DSR) suit Bailey & López de Prado, *The Deflated Sharpe Ratio:
Correcting for Selection Bias, Backtest Overfitting and Non-Normality*, Journal of Portfolio
Management, 2014 — cf. `docs/PROMOTION-RULES.md` §1.3 pour l'exigence de méthode côté projet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd
from scipy import stats

PERIODS_PER_YEAR_DAILY = 252
EULER_MASCHERONI = 0.5772156649015329


def sharpe_ratio(returns: Sequence[float], periods_per_year: int = PERIODS_PER_YEAR_DAILY) -> float:
    """Sharpe annualisé (rendements quotidiens, écart-type ÉCHANTILLON `ddof=1`, sans taux sans
    risque soustrait — argent fictif, pas de proxy de taux sans risque documenté dans ce projet
    pour la poche actions ; cf. réserve dans REPORT.md). `NaN` si moins de 2 observations ou
    écart-type nul (série constante, ex. 100% cash tout du long)."""
    r = pd.Series(returns, dtype=float).dropna()
    if len(r) < 2:
        return float("nan")
    sd = r.std(ddof=1)
    if sd == 0 or math.isnan(sd):
        return float("nan")
    return float(r.mean() / sd * math.sqrt(periods_per_year))


def sortino_ratio(returns: Sequence[float], periods_per_year: int = PERIODS_PER_YEAR_DAILY) -> float:
    """Sortino annualisé : dénominateur = déviation semi-carrée (MAR=0) sur TOUTES les
    observations (rendements positifs comptés comme un écart nul, convention standard),
    `NaN` si aucun rendement négatif (déviation de downside nulle -> ratio non défini, jamais
    forcé à +inf) ou moins de 2 observations."""
    r = pd.Series(returns, dtype=float).dropna()
    if len(r) < 2:
        return float("nan")
    downside_sq = np.where(r < 0, r**2, 0.0)
    downside_dev = math.sqrt(float(np.mean(downside_sq)))
    if downside_dev == 0 or math.isnan(downside_dev):
        return float("nan")
    return float(r.mean() / downside_dev * math.sqrt(periods_per_year))


def profit_factor(realized_pnls: Iterable[float]) -> float:
    """Profit factor = somme des gains RÉALISÉS / |somme des pertes RÉALISÉES|, sur des
    évènements de réalisation de PnL (fermetures complètes ET partielles de ligne, cf.
    convention documentée dans `backtest/engine.py::simulate_segment` — "un trade" au sens
    `n_trades_closed` est distinct de "un évènement de réalisation" au sens profit factor).
    `NaN` si aucun évènement. `inf` si aucune perte réalisée (toutes les réalisations gagnantes)."""
    pnls = [float(p) for p in realized_pnls]
    if not pnls:
        return float("nan")
    gains = sum(p for p in pnls if p > 0)
    losses = sum(-p for p in pnls if p < 0)
    if losses == 0:
        return float("inf") if gains > 0 else float("nan")
    return gains / losses


def max_drawdown(equity_curve: Sequence[float]) -> float:
    """MaxDD en fraction POSITIVE (0.503 = -50.3%), calculé sur la courbe d'équity fournie
    (typiquement reconstruite par `cumprod(1+rendements concaténés)`, cf. `backtest/engine.py`).
    `NaN` si moins de 2 points."""
    eq = pd.Series(equity_curve, dtype=float).dropna()
    if len(eq) < 2:
        return float("nan")
    running_max = eq.cummax()
    drawdown = eq / running_max - 1.0
    return float(-drawdown.min())


def cagr(equity_curve: Sequence[float], periods_per_year: int = PERIODS_PER_YEAR_DAILY) -> float:
    """CAGR à partir du nombre de PÉRIODES (jours de bourse, pas jours calendaires) de la
    courbe d'équity fournie -- cohérent avec l'annualisation `sqrt(252)` utilisée partout
    ailleurs dans ce module (pas une conversion en jours calendaires qui introduirait une
    incohérence entre le CAGR et le Sharpe rapportés côte à côte)."""
    eq = pd.Series(equity_curve, dtype=float).dropna()
    if len(eq) < 2 or eq.iloc[0] <= 0:
        return float("nan")
    n_periods = len(eq) - 1
    if n_periods <= 0:
        return float("nan")
    total_return = eq.iloc[-1] / eq.iloc[0]
    if total_return <= 0:
        return float("nan")
    years = n_periods / periods_per_year
    if years <= 0:
        return float("nan")
    return float(total_return ** (1.0 / years) - 1.0)


def information_ratio(
    returns_a: Sequence[float], returns_b: Sequence[float], periods_per_year: int = PERIODS_PER_YEAR_DAILY
) -> float:
    """Ratio d'information annualisé de `returns_a` (candidate) contre `returns_b` (référence,
    ex. equal-weight), sur les rendements EXCÉDENTAIRES jour par jour (mêmes dates, cf. appelant
    pour l'alignement) -- `mean(excess)/std(excess) * sqrt(periods_per_year)`."""
    a = pd.Series(returns_a, dtype=float).reset_index(drop=True)
    b = pd.Series(returns_b, dtype=float).reset_index(drop=True)
    n = min(len(a), len(b))
    excess = (a.iloc[:n] - b.iloc[:n]).dropna()
    if len(excess) < 2:
        return float("nan")
    sd = excess.std(ddof=1)
    if sd == 0 or math.isnan(sd):
        return float("nan")
    return float(excess.mean() / sd * math.sqrt(periods_per_year))


def average_exposure(gross_exposure: Sequence[float]) -> float:
    """Exposition brute moyenne (fraction de l'équity investie, 0..1 pour une stratégie
    long-only sans levier) sur la période fournie."""
    e = pd.Series(gross_exposure, dtype=float).dropna()
    if len(e) == 0:
        return float("nan")
    return float(e.mean())


# ------------------------------------------------------------------------------------------
# Deflated Sharpe Ratio (Bailey & López de Prado, 2014)
# ------------------------------------------------------------------------------------------


def _sharpe_std_error(n: int, skew: float, kurtosis_excess: float, sr_hat: float) -> float:
    """Écart-type de l'estimateur du Sharpe (formule non-i.i.d. gaussienne, eq. (7) du papier),
    corrigée de l'asymétrie (`skew`) et de l'aplatissement EN EXCÈS (`kurtosis_excess = kurt -
    3`, convention `scipy.stats.kurtosis(fisher=True)`)."""
    if n <= 1:
        return float("nan")
    kurt_pearson = kurtosis_excess + 3.0  # le papier utilise le kurtosis "Pearson" (non-excess)
    variance = (1.0 - skew * sr_hat + (kurt_pearson - 1.0) / 4.0 * sr_hat**2) / (n - 1)
    if variance < 0:
        # Garde-fou numérique (échantillon très court / skew extrême) : jamais de variance
        # négative, on la plafonne à 0 plutôt que de propager un NaN silencieux.
        variance = 0.0
    return math.sqrt(variance)


def expected_max_sharpe(trials_k: int, sr_std: float) -> float:
    """`SR0` = espérance du MAXIMUM de Sharpe observé sur `trials_k` essais indépendants dont le
    Sharpe VRAI est nul (formule asymptotique eq. (10) du papier, approximation de Gumbel via la
    constante d'Euler-Mascheroni). Cas limite `trials_k <= 1` : AUCUNE correction multi-essais
    à appliquer (un seul essai = pas de sélection parmi plusieurs -> `SR0 = 0`, le DSR se réduit
    alors exactement au PSR contre un Sharpe nul, cf. `deflated_sharpe_ratio` et les tests
    synthétiques `backtest/tests/test_engine.py`) — la formule asymptotique elle-même diverge
    numériquement à `trials_k == 1` (`Phi^-1(1 - 1/1) = Phi^-1(0) = -inf`), d'où ce cas séparé,
    documenté plutôt que masqué par une valeur bricolée."""
    if trials_k <= 1:
        return 0.0
    if sr_std <= 0 or math.isnan(sr_std):
        return 0.0
    z1 = stats.norm.ppf(1.0 - 1.0 / trials_k)
    z2 = stats.norm.ppf(1.0 - 1.0 / (trials_k * math.e))
    return float(sr_std * ((1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2))


def probabilistic_sharpe_ratio(
    sr_hat: float, sr_benchmark: float, n: int, skew: float, kurtosis_excess: float
) -> float:
    """PSR(SR*) = Phi( (SR_hat - SR*) * sqrt(n-1) / sigma_SR ), cf. eq. (8) du papier — probabilité
    que le VRAI Sharpe dépasse `sr_benchmark`, étant donné l'estimateur `sr_hat` sur `n`
    observations (skew/kurtosis en excès de la série de rendements)."""
    if n <= 1 or math.isnan(sr_hat):
        return float("nan")
    sr_std = _sharpe_std_error(n, skew, kurtosis_excess, sr_hat)
    if sr_std == 0 or math.isnan(sr_std):
        return float("nan")
    z = (sr_hat - sr_benchmark) / sr_std
    return float(stats.norm.cdf(z))


@dataclass
class DsrResult:
    sharpe_hat_period: float  # Sharpe NON annualisé, sur la fréquence des rendements fournis
    n_obs: int
    skew: float
    kurtosis_excess: float
    trials_k: int
    sr0_benchmark: float
    dsr: float

    def to_dict(self) -> dict:
        return {
            "sharpe_hat_period": self.sharpe_hat_period,
            "n_obs": self.n_obs,
            "skew": self.skew,
            "kurtosis_excess": self.kurtosis_excess,
            "trials_k": self.trials_k,
            "sr0_benchmark_expected_max_sharpe": self.sr0_benchmark,
            "dsr": self.dsr,
        }


def deflated_sharpe_ratio(returns: Sequence[float], trials_k: int) -> DsrResult:
    """DSR complet (Bailey & López de Prado 2014) sur une série de rendements PAR PÉRIODE
    (typiquement les rendements quotidiens OOS concaténés — le Sharpe utilisé en interne est
    donc le Sharpe NON annualisé de cette fréquence, cohérent en interne avec `n = len(returns)`
    observations ; annualiser le Sharpe SANS changer `n` en conséquence biaiserait le calcul).

    `trials_k` = nombre TOTAL d'essais/combinaisons à déflater (`K_total` au sens
    `docs/PROMOTION-RULES.md` §1.3 : lignes du registre + combinaisons internes à la candidate).
    `trials_k <= 1` -> `SR0 = 0`, DSR == PSR(0) (cf. `expected_max_sharpe`)."""
    r = pd.Series(returns, dtype=float).dropna()
    n = len(r)
    if n < 3:
        return DsrResult(float("nan"), n, float("nan"), float("nan"), trials_k, float("nan"), float("nan"))
    sr_hat = float(r.mean() / r.std(ddof=1)) if r.std(ddof=1) != 0 else float("nan")
    skew = float(stats.skew(r, bias=False))
    kurt_excess = float(stats.kurtosis(r, fisher=True, bias=False))
    sr_std = _sharpe_std_error(n, skew, kurt_excess, sr_hat)
    sr0 = expected_max_sharpe(trials_k, sr_std)
    dsr = probabilistic_sharpe_ratio(sr_hat, sr0, n, skew, kurt_excess)
    return DsrResult(sr_hat, n, skew, kurt_excess, trials_k, sr0, dsr)
