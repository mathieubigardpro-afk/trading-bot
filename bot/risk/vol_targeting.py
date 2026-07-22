"""Vol targeting PORTEFEUILLE — cible 25-30% annualisée (défaut 27.5%), EWMA demi-vie 60h.

Choix documenté (demande explicite : "documente ton choix") :

1. **Niveau d'application** : le vol targeting est calculé et appliqué au niveau PORTEFEUILLE
   (un seul scalaire multiplicatif appliqué à TOUTES les cibles brutes), pas actif par actif.
   C'est la recommandation du rapport de recherche (§3E) : les corrélations inter-crypto majors
   sont très élevées (0.85-0.99, BTC-SOL jusqu'à 0.99) et le book doit être traité "comme un pari
   unique corrélé", pas comme N paris indépendants — sous peine de sous-estimer le VaR agrégé si
   on vol-target chaque actif séparément puis qu'on additionne des tailles individuellement
   "safe" mais collectivement corrélées.

2. **Proxy de la vol portefeuille** : `state.json` ne conserve PAS de série temporelle
   d'équity (seulement le pic `equity_peak_usd`/`equity_peak_ts`, voir ARCHITECTURE.md §3.1) —
   et chaque run repart d'un conteneur vierge, donc une EWMA calculée directement sur les
   rendements d'équity réalisée n'est pas reconstructible à partir des seuls arguments de
   `apply()` (`state`, `prices`, `history`) sans changer le schéma d'état d'un autre module.
   On utilise donc le PROXY explicitement autorisé par la consigne : la somme pondérée
   (valeur absolue des poids cibles bruts) des vols EWMA annualisées de chaque actif, calculées
   sur les rendements horaires de `history`.

   Ce proxy est délibérément PESSIMISTE, pas une simple approximation de confort : pour toute
   matrice de corrélation valide Σ (coefficients dans [-1, 1]), l'inégalité
   `sqrt(w^T Σ w) <= sum(|w_i| * sigma_i)` est TOUJOURS vraie (cas d'égalité seulement si
   corrélation parfaite = 1 entre tous les actifs pondérés). Notre proxy est donc une borne
   SUPÉRIEURE de la vraie vol de portefeuille — on ne peut jamais sous-estimer le risque avec
   cette formule, seulement le surestimer, ce qui va dans le sens du principe cardinal
   pessimiste du projet (le vol_scalar résultant ne peut être que plus prudent, jamais plus
   agressif, que si on avait pu utiliser la vraie matrice de corrélation).

3. **Annualisation** : `sqrt(8760)` (24h x 365j), conformément au tranchage explicite du
   rapport de recherche (§7, "choisir √8760 sur rendements horaires pour cohérence avec la
   fréquence native du bot") — appliqué ici uniformément (crypto et actions), documenté comme
   tel : la vol des actions basée sur des rendements horaires disponibles uniquement pendant
   les heures de marché US serait sous-estimée par ce facteur si on l'annualisait sur son
   propre nombre d'heures de trading réel (bien moins que 8760/an) ; utiliser 8760 uniformément
   revient à SOUS-annualiser (donc SOUS-estimer) la vol actions relativement à sa vraie
   fréquence native — mais comme ce proxy sert seulement de plancher conservateur (voir point 2,
   la vraie vol de portefeuille agrégée est de toute façon bornée par le proxy total, où les
   actions ne pèsent qu'une fraction du book), et que la cible reste la même constante
   `VOL_TARGET_ANNUALIZED` pour tout le portefeuille, ce choix reste cohérent et est documenté
   ici pour transparence plutôt que dissimulé.

4. **Cold-start** : sous `VOL_COLDSTART_MIN_POINTS` (30) points de rendements horaires pour AU
   MOINS UN actif à poids non nul, le scalaire final est multiplié par
   `VOL_COLDSTART_SCALAR` (0.5) — indépendamment de la valeur de vol calculée — car une EWMA sur
   moins de 30 points est statistiquement peu fiable (haute variance de l'estimateur) et le
   principe pessimiste impose de ne pas lui faire confiance pour un sizing agressif.
"""

from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

import pandas as pd

PERIODS_PER_YEAR = 8760  # bougies horaires 24/7/365 — voir point 3 de la docstring module


def hourly_returns(history: Optional[pd.DataFrame]) -> pd.Series:
    """Rendements simples entre clôtures horaires successives, à partir d'une history au
    format `bot.feeds.get_history()` (colonnes open/high/low/close/volume, bougies clôturées
    uniquement — jamais la bougie en cours, cf. ARCHITECTURE.md §0.4)."""
    if history is None or "close" not in getattr(history, "columns", []):
        return pd.Series(dtype=float)
    closes = history["close"].astype(float)
    if len(closes) < 2:
        return pd.Series(dtype=float)
    return closes.pct_change().dropna()


def ewma_vol_per_period(returns: pd.Series, halflife_hours: float) -> Optional[float]:
    """Écart-type EWMA (demi-vie en heures) du dernier point de la série de rendements, ou
    None si pas assez de points pour produire une estimation (< 2 rendements)."""
    if returns is None or len(returns) < 2:
        return None
    ewm_std = returns.ewm(halflife=float(halflife_hours), adjust=False).std(bias=False)
    if ewm_std.empty:
        return None
    val = ewm_std.iloc[-1]
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    return float(val)


def annualize_vol(vol_per_period: float, periods_per_year: float = PERIODS_PER_YEAR) -> float:
    return float(vol_per_period) * math.sqrt(periods_per_year)


def portfolio_vol_annualized(
    history: Optional[Dict[str, pd.DataFrame]],
    weights: Dict[str, float],
    halflife_hours: float,
    min_points: int,
) -> Tuple[float, bool]:
    """Proxy PESSIMISTE de la vol de portefeuille annualisée : somme pondérée (valeurs
    absolues des poids `weights`, typiquement les cibles brutes de ce cycle) des vols EWMA
    annualisées de chaque actif — voir point 2 de la docstring du module pour la justification
    mathématique du biais pessimiste. Retourne `(vol_annualisee, coldstart)` où `coldstart` est
    True si au moins un actif à poids non nul manque de profondeur d'historique
    (< `min_points` rendements) pour une estimation EWMA fiable.
    """
    history = history or {}
    total = 0.0
    coldstart = False
    for symbol, w in (weights or {}).items():
        if w is None or abs(w) < 1e-12:
            continue
        df = history.get(symbol)
        returns = hourly_returns(df)
        if len(returns) < min_points:
            coldstart = True
        vol_period = ewma_vol_per_period(returns, halflife_hours)
        if vol_period is None:
            # Pas assez de données pour estimer une vol : hypothèse neutre (contribution nulle
            # à la somme), compensée par le flag coldstart qui déclenche le scalaire prudent
            # global — on ne veut PAS traiter un actif sans historique comme "sans risque" de
            # façon silencieuse, d'où le marquage coldstart systématique dans ce cas.
            vol_period = 0.0
            coldstart = True
        total += abs(w) * annualize_vol(vol_period)
    return total, coldstart


def compute_vol_scalar(
    portfolio_vol_annual: float,
    target_vol_annual: float,
    coldstart: bool,
    coldstart_scalar: float,
) -> float:
    """`scalar = min(1, cible / vol_réalisée)`, jamais > 1 (le vol targeting ne fait que
    RÉDUIRE l'exposition quand la vol dépasse la cible, jamais l'amplifier au-delà des cibles
    brutes déjà décidées par les stratégies). Multiplié par `coldstart_scalar` si l'estimation
    de vol n'est pas fiable (voir point 4 de la docstring module)."""
    if portfolio_vol_annual is None or portfolio_vol_annual <= 1e-12:
        # Vol non mesurable (pas d'exposition ciblée, ou historique totalement plat) : pas de
        # base pour réduire — mais le cold-start scalar reste appliqué séparément ci-dessous.
        scalar = 1.0
    else:
        scalar = float(target_vol_annual) / float(portfolio_vol_annual)
    scalar = min(1.0, scalar)
    if coldstart:
        scalar *= float(coldstart_scalar)
    return max(0.0, scalar)
