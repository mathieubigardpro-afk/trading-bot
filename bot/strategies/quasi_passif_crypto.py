"""bot/strategies/quasi_passif_crypto.py — "quasi-passif crypto vol-targeté", la stratégie
crypto retenue pour les 3 wallets (SPEC : `docs/config-strategies.json` ->
`strategy_definitions.crypto_quasi_passif_vol_targete` ; justifications et réserves :
`docs/SELECTION-FINALE.md` §2 et §5).

Principe (repris À L'IDENTIQUE du SPEC, aucun seuil ni formule "amélioré") : détention
long/flat vol-targetée avec filtre de tendance SMA200 journalier très lent.

  1. **Filtre de tendance** — pour chaque actif de l'univers du wallet, éligible ("on") si
     la dernière clôture JOURNALIÈRE CLÔTURÉE (agrégée depuis l'historique horaire) est
     strictement supérieure à la SMA200 des clôtures journalières.
  2. **Vol réalisée du panier** — EWMA (demi-vie `vol_ewma_halflife_hours`, 60h pour les 3
     wallets) des rendements HORAIRES du panier équipondéré des seuls actifs "on",
     annualisée par `sqrt(8760)`.
  3. **Sizing brut portefeuille** —
     `poids_brut_portefeuille = min(gross_exposure_max, vol_target_annualized / vol_réalisée)`.
  4. **Répartition par actif** — chaque actif "on" reçoit une part égale de
     `poids_brut_portefeuille` (`poids_brut_portefeuille / nombre_actifs_on`), plafonnée à
     `cap_per_asset`. Les actifs "off" (ou sans historique exploitable) reçoivent 0.

Tous les paramètres numériques (`vol_target_annualized`, `gross_exposure_max`,
`cap_per_asset`, `vol_ewma_halflife_hours`) sont lus depuis `profile["risque"]` (le profil du
wallet courant tel que construit par `bot.config.WALLETS`, transmis par
`bot.strategies.combine_strategies(..., profile=wallet_cfg)`) — ils y sont déjà alignés,
valeur pour valeur, sur les variantes du SPEC (`prudent_btc_eth`, `equilibre_6majors`,
`agressif_12diversifie`). Cette stratégie ne les recopie JAMAIS en dur : le lire depuis
`profile` est la seule source de vérité, exactement comme documenté dans
`docs/ARCHITECTURE.md` §2.

--------------------------------------------------------------------------------------------
Univers par wallet — panier resserré du wallet "agressif" (choix documenté)
--------------------------------------------------------------------------------------------
`docs/SELECTION-FINALE.md` §3 recommande de restreindre l'univers crypto du wallet agressif
aux 30 cryptos actuelles de `bot.config.WALLETS[agressif]["univers_crypto"]` à un panier
resserré de 12 actifs diversifiés (`agressif_12diversifie` du SPEC) — changement PROPOSÉ mais
explicitement NON appliqué automatiquement à `bot/config.py` par ce document (il faudrait une
modification hors du périmètre de cette mission, limité à `bot/strategies/` et `bot/tests/`).

Cette stratégie applique donc la restriction ICI, au niveau du signal, sans toucher à
`bot/config.py` : `SPEC_UNIVERSE_BY_WALLET` fixe, pour chaque wallet, EXACTEMENT l'univers de
la variante SPEC correspondante (prudent = BTC/ETH, équilibré = 6 majors, agressif = panier de
12). Pour le wallet agressif, les 18 cryptos supplémentaires que `bot/config.py` fait par
ailleurs suivre (prix/historique récupérés, journalisés dans `decisions.jsonl`) ne reçoivent
simplement jamais de cible de cette stratégie — elles restent à 0 (ou à leur position
existante si une autre pièce du pipeline en détenait une, ce qui ne devrait jamais arriver en
usage normal puisque cette stratégie est la seule à émettre des cibles crypto). C'est une
intersection défensive avec `profile["univers_crypto"]`, jamais une extrapolation au-delà :
si un wallet ne suit pas un symbole de sa variante SPEC (ne devrait jamais arriver au vu de
`bot/config.py` actuel), ce symbole est silencieusement écarté plutôt que ciblé "à l'aveugle".

--------------------------------------------------------------------------------------------
Fréquence de décision — une fois par jour, au premier cycle horaire après minuit UTC
--------------------------------------------------------------------------------------------
Le SPEC impose une décision quotidienne (pas horaire) : "les autres cycles horaires ne
changent rien (la no-trade band absorbe le bruit)". Choix d'implémentation documenté : AUCUN
état supplémentaire n'est nécessaire pour obtenir ce comportement, par construction pure de
`_daily_closes()` ci-dessous.

`bot.feeds.get_history()` ne renvoie QUE des bougies horaires strictement CLÔTURÉES (jamais la
bougie en cours de formation, ARCHITECTURE.md §0.4) — indexées par l'heure d'OUVERTURE de la
bougie (UTC). `_daily_closes()` agrège ces bougies horaires en clôtures journalières UTC en ne
retenant QUE les jours calendaires COMPLETS (24 heures distinctes 00h-23h présentes). Le jour
courant ("aujourd'hui") ne peut, par construction, jamais atteindre 24 heures tant que sa
dernière bougie horaire (23h-24h UTC) n'a pas encore clôturé — ce qui n'arrive qu'au tout
premier cycle horaire lancé APRÈS 00:00 UTC le lendemain (le cycle qui tourne peu après minuit
UTC, ex. cron "minute 7" -> 00:07 UTC, voit pour la première fois la bougie 23h-24h UTC de la
veille comme clôturée). Le dernier jour calendaire complet disponible — et donc le signal
(clôture vs SMA200, vol du panier) — reste par conséquent IDENTIQUE à chaque cycle horaire
d'une même journée UTC, et change automatiquement une seule fois par jour, exactement au
cycle attendu par le SPEC, sans qu'aucun compteur/horodatage de "dernière décision" ne doive
être lu ou écrit dans `state` (qui, de toute façon, ne persiste aucun champ propre à une
stratégie individuelle — cf. `bot/runner.py:process_wallet`, le schéma de `state.json` est
entièrement reconstruit à chaque cycle). Cette propriété est vérifiée explicitement par
`bot/tests/test_quasi_passif_crypto.py::test_no_trade_intraday_same_day_gives_identical_weights`.

--------------------------------------------------------------------------------------------
Posture défensive (réseau bloqué en développement, ARCHITECTURE.md §0.2/§0.3)
--------------------------------------------------------------------------------------------
Aucun appel réseau ni écriture disque ici (fonction pure, comme l'exige `StrategyBase`).
En cas de donnée insuffisante ou incohérente pour un calcul fiable — moins de 200 jours
calendaires complets pour un actif (SMA200 non calculable), moins de 2 rendements horaires
communs aux actifs "on" (vol de panier non calculable), profil de risque incomplet — la
règle est TOUJOURS de traiter l'actif/le wallet comme non éligible ce cycle (poids 0, jamais
une valeur extrapolée ou une "meilleure estimation" créative), conformément au principe
pessimiste cardinal du projet.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

import pandas as pd

from bot.strategies import StrategyBase

# --- Univers SPEC par wallet (docs/config-strategies.json -> variants) ---------------------
SPEC_UNIVERSE_BY_WALLET: Dict[str, List[str]] = {
    "prudent": ["BTC", "ETH"],
    "equilibre": ["BTC", "ETH", "SOL", "DOGE", "LINK", "AVAX"],
    "agressif": [
        "BTC", "ETH", "SOL", "BNB", "XRP",
        "TRX", "XLM", "HBAR", "ICP", "OP", "UNI", "FIL",
    ],
}

REGIME_SMA_DAYS = 200
HOURS_PER_COMPLETE_DAY = 24
PERIODS_PER_YEAR_HOURLY = 8760  # bougies horaires 24/7/365, cf. bot/risk/vol_targeting.py

__all__ = ["QuasiPassifCrypto", "SPEC_UNIVERSE_BY_WALLET", "REGIME_SMA_DAYS"]


# ------------------------------------------------------------------------------------------
# Helpers purs (testés isolément)
# ------------------------------------------------------------------------------------------


def _daily_closes(history: Optional[pd.DataFrame]) -> pd.Series:
    """Agrège une history horaire (index = heure d'ouverture UTC des bougies CLÔTURÉES,
    colonnes `bot.feeds.get_history()`) en clôtures journalières UTC, en ne retenant que les
    jours calendaires COMPLETS (24 heures distinctes 00h-23h présentes pour ce jour). Le jour
    en cours (forcément incomplet, cf. docstring module) est ainsi exclu par construction,
    sans logique de date "aujourd'hui" explicite — donc sans risque de dérive avec l'horloge
    système. Retourne une Series vide si aucun jour complet n'est disponible.
    """
    if history is None or "close" not in getattr(history, "columns", []):
        return pd.Series(dtype=float)
    closes = history["close"].astype(float).sort_index()
    if closes.empty:
        return pd.Series(dtype=float)

    idx = pd.DatetimeIndex(closes.index)
    dates = idx.date
    frame = pd.DataFrame({"date": dates, "hour": idx.hour, "close": closes.values})

    hours_per_date = frame.groupby("date")["hour"].nunique()
    complete_dates = set(hours_per_date[hours_per_date >= HOURS_PER_COMPLETE_DAY].index)
    if not complete_dates:
        return pd.Series(dtype=float)

    frame = frame[frame["date"].isin(complete_dates)]
    # Clôture du jour = clôture de la dernière heure disponible de ce jour (23h en usage
    # normal), après tri par heure croissante au sein du jour.
    daily = frame.sort_values(["date", "hour"]).groupby("date")["close"].last()
    daily.index = pd.to_datetime(daily.index)
    return daily.sort_index()


def _is_trend_on(daily_closes: pd.Series, sma_days: int = REGIME_SMA_DAYS) -> Optional[bool]:
    """True/False si `sma_days` clôtures journalières complètes sont disponibles (SMA200
    fiable), sinon `None` (donnée insuffisante -> à traiter comme "non éligible" par
    l'appelant, jamais comme un SMA approximé sur une fenêtre plus courte)."""
    if daily_closes is None or len(daily_closes) < sma_days:
        return None
    window = daily_closes.iloc[-sma_days:]
    sma = float(window.mean())
    if math.isnan(sma):
        return None
    last_close = float(daily_closes.iloc[-1])
    if math.isnan(last_close):
        return None
    return last_close > sma


def _hourly_returns(history: Optional[pd.DataFrame]) -> pd.Series:
    """Rendements simples horaires (bougies clôturées, triées par index)."""
    if history is None or "close" not in getattr(history, "columns", []):
        return pd.Series(dtype=float)
    closes = history["close"].astype(float).sort_index()
    if len(closes) < 2:
        return pd.Series(dtype=float)
    return closes.pct_change().dropna()


def _basket_vol_annualized(
    eligible: List[str],
    history: Dict[str, pd.DataFrame],
    halflife_hours: float,
) -> Optional[float]:
    """Vol EWMA annualisée des rendements horaires du panier ÉQUIPONDÉRÉ des actifs
    `eligible`, ou `None` si moins de 2 rendements communs (index horaire aligné, jointure
    stricte sur les timestamps partagés par TOUS les actifs éligibles) ne sont disponibles."""
    if not eligible:
        return None

    returns_by_symbol = {s: _hourly_returns(history.get(s)) for s in eligible}
    if any(r.empty for r in returns_by_symbol.values()):
        return None

    aligned = pd.concat(returns_by_symbol, axis=1, join="inner")
    if len(aligned) < 2:
        return None

    basket_returns = aligned.mean(axis=1)
    ewm_std = basket_returns.ewm(halflife=float(halflife_hours), adjust=False).std(bias=False)
    if ewm_std.empty:
        return None
    vol_hourly = ewm_std.iloc[-1]
    if vol_hourly is None or (isinstance(vol_hourly, float) and math.isnan(vol_hourly)):
        return None
    if vol_hourly <= 0:
        return None
    return float(vol_hourly) * math.sqrt(PERIODS_PER_YEAR_HOURLY)


# ------------------------------------------------------------------------------------------
# Stratégie
# ------------------------------------------------------------------------------------------


class QuasiPassifCrypto(StrategyBase):
    """Détention crypto long/flat vol-targetée avec filtre de tendance SMA200 journalier.
    Voir docstring de module pour le détail complet de l'algorithme et des choix documentés.
    """

    name = "quasi_passif_crypto"

    def target_weights(
        self,
        history: Dict[str, pd.DataFrame],
        state: dict,
        profile: Optional[dict] = None,
    ) -> Dict[str, float]:
        profile = profile or {}
        history = history or {}
        wallet_id = profile.get("id")

        risque = profile.get("risque") or {}
        vol_target = risque.get("vol_target_annualized")
        gross_exposure_max = risque.get("gross_exposure_max")
        cap_per_asset = risque.get("cap_per_asset")
        halflife_hours = risque.get("vol_ewma_halflife_hours")

        if None in (vol_target, gross_exposure_max, cap_per_asset, halflife_hours):
            # Profil de risque incomplet (config manquante/malformée) : aucune cible plutôt
            # qu'une valeur par défaut inventée (principe pessimiste).
            return {}

        spec_universe = SPEC_UNIVERSE_BY_WALLET.get(wallet_id)
        if not spec_universe:
            # Wallet inconnu du SPEC crypto quasi-passif : pas d'extrapolation créative.
            return {}

        configured_universe = profile.get("univers_crypto")
        if configured_universe:
            configured_set = set(configured_universe)
            universe = [s for s in spec_universe if s in configured_set]
        else:
            universe = list(spec_universe)

        if not universe:
            return {}

        # --- 1. filtre de tendance SMA200 journalière, par actif -----------------------
        eligible: List[str] = []
        for symbol in universe:
            daily = _daily_closes(history.get(symbol))
            if _is_trend_on(daily) is True:
                eligible.append(symbol)

        weights: Dict[str, float] = {symbol: 0.0 for symbol in universe}
        if not eligible:
            return weights

        # --- 2. vol EWMA annualisée du panier équipondéré des actifs "on" --------------
        vol_annualized = _basket_vol_annualized(eligible, history, halflife_hours)
        if vol_annualized is None:
            # Vol de panier non estimable de façon fiable : posture la plus prudente
            # possible (aucune exposition ce cycle), jamais de vol inventée.
            return weights

        # --- 3. sizing brut portefeuille -------------------------------------------------
        poids_brut_portefeuille = min(float(gross_exposure_max), float(vol_target) / vol_annualized)
        poids_brut_portefeuille = max(0.0, poids_brut_portefeuille)

        # --- 4. répartition équipondérée entre actifs "on", cap par actif ----------------
        per_asset_raw = poids_brut_portefeuille / len(eligible)
        cap = float(cap_per_asset)
        for symbol in eligible:
            weights[symbol] = min(per_asset_raw, cap)

        return weights
