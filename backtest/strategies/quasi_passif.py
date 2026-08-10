"""backtest/strategies/quasi_passif.py — adaptateur walk-forward de la candidate
`quasi_passif_crypto_wf_retest` (`backtest/results/quasi_passif_crypto_wf_retest/SPEC.md`,
pré-enregistrée 2026-08-10, backlog P2#13). Moteur commun `backtest/engine.py` uniquement
(`docs/PROMOTION-RULES.md` §1.1) : ce module ne fait qu'assembler la matrice `weights_decided`
(index = calendrier HORAIRE, colonnes = univers) attendue par `engine.simulate_segment`, en
réutilisant TEL QUEL (jamais réimplémentés) les helpers purs de production
`bot.strategies.quasi_passif_crypto` : `_daily_closes`, `_is_trend_on`,
`_basket_vol_annualized`, `SPEC_UNIVERSE_BY_WALLET`.

--------------------------------------------------------------------------------------------
Décision quotidienne — cadence et causalité (SPEC.md §"Stratégie testée", point 5)
--------------------------------------------------------------------------------------------
Le signal ne change qu'UNE fois par jour UTC, au premier timestamp horaire après minuit (heure
0 UTC) — moment où la journée civile précédente devient, pour la première fois, un jour
calendaire COMPLET (24 heures distinctes) au sens de `_daily_closes`. Entre deux décisions
quotidiennes, le poids décidé reste rigoureusement constant (propagation horaire).

Convention causale retenue (alignée sur le calage RÉEL des cycles de production, cf. docstring
`bot/strategies/quasi_passif_crypto.py` "Fréquence de décision") : à la décision prise au
timestamp `t` (= heure 0 UTC du jour D+1, `calendar` étant indexé par l'heure d'OUVERTURE des
bougies horaires CLÔTURÉES), la bougie EXACTEMENT indexée `t` n'est PAS encore close (elle
couvre `[t, t+1h)`) — seules les bougies d'index STRICTEMENT `< t` sont utilisées, aussi bien
pour l'agrégation journalière (`_daily_closes`) que pour la vol de panier
(`_basket_vol_annualized`). C'est exactement équivalent au calage réel de production : au tout
premier cycle après minuit UTC, la dernière bougie horaire disponible est celle ouverte à 23h
la veille (close à 00h), jamais celle qui vient de s'ouvrir à 00h.

--------------------------------------------------------------------------------------------
Vectorisation pour la performance — `_daily_closes` pré-calculée UNE FOIS par symbole
--------------------------------------------------------------------------------------------
Appeler les 3 helpers de production une fois PAR JOUR DE DÉCISION (~1600 jours sur 2022-2026)
et non par heure (~39 000 lignes) est la seule contrainte de performance explicite de la
mission ; ce module va plus loin pour `_daily_closes` (agrégation journalière) : cette fonction
n'effectue qu'un groupby LOCAL à chaque jour calendaire (jamais de fenêtre glissante qui
engloberait plusieurs jours), donc agréger l'historique COMPLET d'un symbole UNE SEULE FOIS
(`_daily_closes(raw[s])`) puis, à chaque jour de décision `t`, NE GARDER que les lignes du
résultat déjà agrégé dont l'index de jour est STRICTEMENT ANTÉRIEUR à `t`
(`daily_closes_full[s][daily_closes_full[s].index < t]`) produit EXACTEMENT le même résultat
que ré-agréger `raw[s]` tronqué à `index < t` à chaque jour — aucune information provenant de
jours `>= t` n'entre jamais dans un jour `< t` par construction de l'agrégation (le respect de
cette égalité est vérifié explicitement par
`backtest/tests/test_quasi_passif_backtest.py::test_vectorized_daily_matches_production_helper`
sur un échantillon de dates de données réelles). La vol de panier (`_basket_vol_annualized`),
elle, reste appelée SANS raccourci : l'ensemble des actifs "on" change de jour en jour (pas de
série "panier" stable à précalculer une fois pour toutes), le helper de production est donc
invoqué directement sur l'historique brut tronqué (`raw[s][raw[s].index < t]`) à chaque jour de
décision — fidélité totale, aucune approximation numérique introduite pour cette étape.

--------------------------------------------------------------------------------------------
Actif sans SMA200 calculable (ex. OP avant ~2023-01) — politique de backtest (SPEC.md)
--------------------------------------------------------------------------------------------
`_is_trend_on` renvoie `None` tant que 200 jours calendaires complets ne sont pas disponibles
pour le symbole : ce module traite alors ce symbole comme simplement INÉLIGIBLE (poids 0.0),
jamais "gelé" — la politique "donnée manquante -> gel 24 cycles" (correctif ARCHITECTURE.md
§12.4, `apply_missing_data_policy`) est une robustesse d'EXPLOITATION temps réel documentée
comme explicitement NON reproduite en backtest sur données statiques alignées (SPEC.md
§"Différences assumées backtest vs production").
"""

from __future__ import annotations

from typing import Dict, List, Sequence

import numpy as np
import pandas as pd

from bot.strategies.quasi_passif_crypto import (  # noqa: F401 -- ré-exportés pour les tests/runner
    REGIME_SMA_DAYS,
    SPEC_UNIVERSE_BY_WALLET,
    _basket_vol_annualized,
    _daily_closes,
    _is_trend_on,
)

__all__ = [
    "generate_weight_decisions",
    "decision_positions",
    "REGIME_SMA_DAYS",
    "SPEC_UNIVERSE_BY_WALLET",
]


def decision_positions(calendar: pd.DatetimeIndex) -> np.ndarray:
    """Positions (entiers, dans `calendar`) des instants de décision quotidienne : le premier
    timestamp horaire de chaque jour calendaire UTC (heure 0), cf. docstring module."""
    hours = pd.DatetimeIndex(calendar).hour
    return np.flatnonzero(hours == 0)


def generate_weight_decisions(
    raw: Dict[str, pd.DataFrame],
    calendar: pd.DatetimeIndex,
    universe: Sequence[str],
    risk_profile: dict,
) -> pd.DataFrame:
    """Construit `weights_decided` (index = `calendar`, colonnes = `universe`) selon
    l'algorithme EXACT de `bot.strategies.quasi_passif_crypto.QuasiPassifCrypto.target_weights`
    (filtre de tendance -> vol de panier EWMA -> sizing brut -> répartition égale plafonnée),
    décidé une fois par jour UTC et propagé heure par heure (constant entre deux décisions).

    `raw` : historique HORAIRE BRUT (non aligné à un calendrier commun, non "ffillé") par
    symbole, tel que retourné par `backtest.data_hourly.load_universe_raw` -- fidèle à la
    production, qui interroge chaque symbole indépendamment (`bot.feeds.get_history()`), jamais
    une vue pré-alignée/rebouchée sur l'univers complet (un tel alignement pourrait injecter des
    heures "fantômes" -- ffill -- dans l'agrégation journalière d'un symbole à cause d'un trou
    d'un AUTRE symbole du calendrier union, cf. `backtest/data_hourly.py`).

    `risk_profile` : `bot.config.WALLETS[i]["risque"]` de la variante -- lu tel quel, jamais
    recopié en dur (SPEC.md).
    """
    universe = list(universe)
    vol_target = float(risk_profile["vol_target_annualized"])
    gross_max = float(risk_profile["gross_exposure_max"])
    cap_per_asset = float(risk_profile["cap_per_asset"])
    halflife_hours = float(risk_profile["vol_ewma_halflife_hours"])

    calendar = pd.DatetimeIndex(calendar)
    n = len(calendar)
    positions = decision_positions(calendar)

    values = np.zeros((n, len(universe)), dtype=float)
    if len(positions) == 0:
        return pd.DataFrame(values, index=calendar, columns=universe)

    # --- pré-calcul journalier, une fois par symbole (cf. docstring module) -----------------
    daily_closes_full: Dict[str, pd.Series] = {s: _daily_closes(raw.get(s)) for s in universe}

    col_idx = {s: i for i, s in enumerate(universe)}
    last_row = np.zeros(len(universe), dtype=float)

    for k, pos in enumerate(positions):
        t = calendar[pos]

        # --- 1. filtre de tendance SMA200, par actif (helper de production, causal < t) ----
        eligible: List[str] = []
        for s in universe:
            daily = daily_closes_full[s]
            daily_causal = daily[daily.index < t]
            if _is_trend_on(daily_causal, REGIME_SMA_DAYS) is True:
                eligible.append(s)

        row = np.zeros(len(universe), dtype=float)
        if eligible:
            # --- 2. vol EWMA annualisée du panier "on" (helper de production, causal < t) --
            history_cutoff = {
                s: raw[s][raw[s].index < t] for s in eligible if s in raw and raw[s] is not None
            }
            vol_annualized = _basket_vol_annualized(eligible, history_cutoff, halflife_hours)
            if vol_annualized is not None:
                # --- 3. sizing brut portefeuille -----------------------------------------
                poids_brut = max(0.0, min(gross_max, vol_target / vol_annualized))
                # --- 4. répartition égale entre actifs "on", cap par actif ----------------
                per_asset = poids_brut / len(eligible)
                per_asset_capped = min(per_asset, cap_per_asset)
                for s in eligible:
                    row[col_idx[s]] = per_asset_capped

        last_row = row
        end_pos = positions[k + 1] if k + 1 < len(positions) else n
        values[pos:end_pos, :] = last_row

    return pd.DataFrame(values, index=calendar, columns=universe)
