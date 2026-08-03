"""backtest/risk_overlay.py — surcouche de risque (bande de non-négociation + vol targeting)
appliquée PAR DÉFAUT par `backtest/engine.py:simulate_segment()` à toute matrice de poids déjà
DÉCIDÉS par une stratégie (`weights_decided`), AVANT simulation du portefeuille.

--------------------------------------------------------------------------------------------
Correctif audit 2026-07-27 (chantier 3, gouvernance backtest/) — pourquoi ce module existe
--------------------------------------------------------------------------------------------
Le moteur (base héritée du 22/07, complétée par la session hebdo du 27/07 dans
`backtest/engine.py`) exécutait `weights_decided` BRUT, sans aucune des deux protections que
`bot/risk/manager.py` applique à CHAQUE cycle en production. Un moteur de gouvernance qui
ignore les DEUX rend systématiquement le backtest PLUS optimiste que la réalité :

  1. **Bande de non-négociation** (`no_trade_band`) : un écart de poids trop petit ne déclenche
     aucun ordre en production (le bruit est absorbé, cf. `bot/risk/manager.py` étape 6). Sans
     elle, le backtest exécute un ordre CHAQUE fois que la stratégie recalcule un poids
     légèrement différent — l'audit a mesuré 16,3 fills par transition de signal réellement
     voulue avec la bande à 0, soit environ 0,30 point de Sharpe de pénalité artificielle en
     coûts de transaction qui ne se produirait jamais en production.
  2. **Vol targeting portefeuille** : en production, `bot/risk/manager.py` RÉDUIT
     systématiquement l'exposition brute quand la vol réalisée dépasse la cible
     (`bot.config.VOL_TARGET_ANNUALIZED`). Un backtest qui l'ignore simule un portefeuille
     STRUCTURELLEMENT PLUS GROS que celui réellement risqué en production — l'audit a mesuré un
     MaxDD sous-estimé d'un facteur ~2 sans cette correction.

Ce module ne réimplémente PAS `bot/risk/manager.py` intégralement (circuit breakers, caps par
actif, gel d'entrées, cap d'exposition brute totale... hors périmètre de CE correctif, cf.
mission de l'audit : "les DEUX corrections exigées", pas une refonte complète) : il réutilise
TEL QUEL la logique de calcul du vol scalar de `bot.risk.vol_targeting` (mêmes fonctions, mêmes
constantes `bot.config`) pour que le sizing du backtest suive la MÊME formule que la
production, adaptée à la fréquence QUOTIDIENNE de ce moteur (vs horaire en production) :
  - `periods_per_year=252` (jours de bourse) au lieu de 8760 (heures/an, cf. `backtest/
    metrics.py` qui fait déjà ce choix pour Sharpe/Sortino/CAGR — cohérence interne à ce
    moteur) ;
  - `halflife_days = bot.config.VOL_EWMA_HALFLIFE_HOURS / 24 = 2.5` jours (même demi-vie
    PHYSIQUE que la production — 60 heures —, seulement ré-exprimée dans l'unité de la série
    journalière utilisée ici).

Toute divergence RESTANTE avec `bot/risk/manager.py` (circuit breakers, caps par actif, gel des
nouvelles entrées, cap d'exposition brute totale, bande PAR POCHE) reste un écart CONNU et
documenté, pas un bug caché — cf. la ligne de tête ajoutée à `docs/RESEARCH-BACKLOG.md` par ce
même correctif, qui demande explicitement un re-audit adversarial de ce moteur avant de s'en
servir pour juger une nouvelle candidate.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

import pandas as pd

from bot import config as bot_cfg
from bot.risk import compute_vol_scalar

# --- Défauts alignés production (bot/config.py — calibrage AGRESSIF unique, cf. bandeau de
# tête de ce fichier dans bot/config.py) -------------------------------------------------------
DEFAULT_NO_TRADE_BAND = float(bot_cfg.NO_TRADE_BAND)  # 0.05 -- correctif audit 2026-07-27
DEFAULT_VOL_TARGET_ANNUALIZED = float(bot_cfg.VOL_TARGET_ANNUALIZED)  # 0.275
DEFAULT_VOL_COLDSTART_MIN_POINTS = int(bot_cfg.VOL_COLDSTART_MIN_POINTS)  # 30
DEFAULT_VOL_COLDSTART_SCALAR = float(bot_cfg.VOL_COLDSTART_SCALAR)  # 0.5
# 60h de production -> 2.5 jours (même demi-vie physique, cf. docstring module).
# ATTENTION (audit 2026-08-03, F2) : `halflife` est passé tel quel à pandas.ewm(), qui compte
# en LIGNES de la série, jamais en temps réel. Ces défauts ne sont donc corrects QUE pour des
# séries à 1 ligne = 1 jour. Pour des données HORAIRES, utiliser les constantes HOURLY_*
# ci-dessous — `backtest/engine.py:simulate_segment` refuse désormais (ValueError) un
# calendrier intra-journalier combiné aux défauts quotidiens.
DEFAULT_VOL_EWMA_HALFLIFE_DAYS = float(bot_cfg.VOL_EWMA_HALFLIFE_HOURS) / 24.0
DEFAULT_VOL_PERIODS_PER_YEAR = 252.0  # jours de bourse -- cf. backtest/metrics.py, pas 8760

# --- Équivalents HORAIRES (audit 2026-08-03, F2) : à passer EXPLICITEMENT à simulate_segment
# pour toute candidate sur bougies horaires (crypto). Mêmes valeurs physiques que la
# production (`bot/risk/vol_targeting.py` : halflife 60 observations horaires, annualisation
# sqrt(8760)) — ici exprimées dans l'unité "lignes de la série" attendue par pandas.ewm().
HOURLY_VOL_EWMA_HALFLIFE_PERIODS = float(bot_cfg.VOL_EWMA_HALFLIFE_HOURS)  # 60 lignes horaires
HOURLY_VOL_PERIODS_PER_YEAR = 8760.0  # heures/an, comme bot/risk/vol_targeting.py


def precompute_vol_stats(
    closes: pd.DataFrame,
    halflife_days: float = DEFAULT_VOL_EWMA_HALFLIFE_DAYS,
    periods_per_year: float = DEFAULT_VOL_PERIODS_PER_YEAR,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Précalcule, pour TOUT le calendrier de `closes` (une seule fois, vectorisé), la vol EWMA
    annualisée par symbole (`vol_annual`) et le nombre cumulé de rendements valides par symbole
    (`valid_count`, sert au cold-start, cf. `bot.risk.vol_targeting` point 4). Causal par
    construction : `vol_annual.iloc[i]` / `valid_count.iloc[i]` n'utilisent que
    `closes.iloc[:i+1]` (le rendement du jour `i` compare la clôture `i` à la clôture `i-1`,
    jamais une donnée future) — sûr à appeler une seule fois sur le calendrier complet puis à
    indexer par position dans la boucle de simulation, comme le fait déjà `simulate_segment`
    pour `opens`/`closes` eux-mêmes."""
    returns = closes.pct_change()
    ewma_std = returns.ewm(halflife=float(halflife_days), adjust=False).std(bias=False)
    vol_annual = ewma_std * math.sqrt(float(periods_per_year))
    valid_count = returns.notna().cumsum()
    return vol_annual, valid_count


def compute_portfolio_vol_scalar(
    raw_weights: "pd.Series",
    vol_annual_row: "pd.Series",
    valid_count_row: "pd.Series",
    target_vol_annualized: float = DEFAULT_VOL_TARGET_ANNUALIZED,
    coldstart_min_points: int = DEFAULT_VOL_COLDSTART_MIN_POINTS,
    coldstart_scalar: float = DEFAULT_VOL_COLDSTART_SCALAR,
) -> float:
    """Reproduit `bot.risk.vol_targeting.portfolio_vol_annualized` + `compute_vol_scalar` (même
    proxy PESSIMISTE : somme pondérée des valeurs absolues des poids bruts x vol EWMA annualisée
    de chaque actif à poids non nul -- borne supérieure de la vraie vol de portefeuille, cf.
    docstring de `bot/risk/vol_targeting.py` point 2), à partir de séries DÉJÀ précalculées par
    `precompute_vol_stats` (une ligne = un jour) plutôt que d'un dict `history` complet par
    cycle -- forme adaptée à ce moteur vectorisé, formule IDENTIQUE à la production (même
    fonction `bot.risk.compute_vol_scalar`, réutilisée telle quelle, jamais réimplémentée)."""
    portfolio_vol = 0.0
    coldstart = False
    for sym, w in raw_weights.items():
        if w is None:
            continue
        wf = float(w)
        if math.isnan(wf):
            # Audit 2026-08-03 (F3) : NaN n'est ni filtré par `abs(w) < eps` (toute
            # comparaison avec NaN est False) ni propagé jusqu'au scalar final --
            # `min(1.0, nan)` renvoie 1.0 en Python, donc UN SEUL poids NaN désactivait
            # silencieusement le vol targeting pour TOUT le portefeuille. Échec bruyant
            # obligatoire (principe pessimiste ARCHITECTURE.md §0.2).
            raise ValueError(
                f"compute_portfolio_vol_scalar: poids NaN pour {sym!r} -- un poids invalide "
                "ne doit jamais neutraliser silencieusement le vol targeting (audit F3)."
            )
        if abs(wf) < 1e-12:
            continue
        vc = valid_count_row.get(sym) if valid_count_row is not None else None
        if vc is None or (isinstance(vc, float) and math.isnan(vc)) or vc < coldstart_min_points:
            coldstart = True
        va = vol_annual_row.get(sym) if vol_annual_row is not None else None
        if va is None or (isinstance(va, float) and math.isnan(va)):
            va = 0.0
            coldstart = True
        portfolio_vol += abs(float(w)) * float(va)
    return compute_vol_scalar(portfolio_vol, target_vol_annualized, coldstart, coldstart_scalar)
