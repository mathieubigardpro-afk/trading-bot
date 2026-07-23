"""bot/strategies/dual_momentum_etf.py — "dual momentum multi-classes ETF", la stratégie
retenue pour la poche ETF des wallets prudent 🛡️ et équilibré ⚖️ (SPEC :
`docs/config-strategies.json` -> `strategy_definitions.dual_momentum_multiclasse_etf` ;
justifications et réserves : `docs/SELECTION-FINALE.md` §1.2 et §5).

--------------------------------------------------------------------------------------------
RÉSERVE MAJEURE (copiée verbatim depuis les documents de décision — à l'attention du
superviseur, cf. consigne de mission) :
--------------------------------------------------------------------------------------------
`docs/config-strategies.json` -> `strategy_definitions.dual_momentum_multiclasse_etf` :
  "status": "retenue_avec_reserve"
  "oos_backtest_reference.statistical_confidence": "Jobson-Korkie/Memmel vs 60/40 : p = 0.069
  -0.089 (journalier/mensuel) -- ecart de Sharpe NON etabli significativement, et dans le sens
  DEFAVORABLE a la strategie (benchmark 60/40 meilleur sur Sharpe/CAGR/MaxDD)."
  "oos_backtest_reference.honest_caveat": "Ne bat statistiquement AUCUN benchmark teste sur
  cette fenetre. Retenue pour sa diversification structurelle (8 classes d'actifs vs 2 pour le
  60/40) et sa faible correlation recente au BTC (0.13 quotidien / 0.31 mensuel, 2022-2026),
  pas pour un edge chiffre prouve. Critere d'echec explicite en docs/SELECTION-FINALE.md
  section 5."

`docs/SELECTION-FINALE.md` §5, critère d'échec chiffré à 3 mois (poche ETF, tous wallets qui
la portent) : "si sur les 3 premiers mois réels le 60/40 SPY/IEF (calculable simplement en
parallèle, sans coût d'implémentation supplémentaire) fait mieux que
`dual_momentum_multiclasse_etf` de plus de 3 points de Sharpe annualisé glissant, considérer
le remplacement par un 60/40 statique — cohérent avec la réserve déjà posée en §1.2 (edge non
prouvé statistiquement dès le départ)." (reformulé de façon identique par wallet dans
`docs/config-strategies.json` -> `wallets.prudent.failure_criteria_3m` : "ETF: si 60/40
SPY/IEF statique bat dual-momentum de plus de 3 points de Sharpe annualise glissant sur 3 mois
-> envisager remplacement par 60/40 statique.")

En clair : cette stratégie est gardée pour sa diversification structurelle (8 classes
d'actifs), PAS parce qu'un edge net a été démontré face à un simple 60/40 SPY/IEF sur la
fenêtre testée. Aucune "amélioration créative" des seuils n'est tentée ici pour compenser
cette réserve — les paramètres ci-dessous sont recopiés à l'identique du SPEC (consigne de
mission #1).

--------------------------------------------------------------------------------------------
Principe (SPEC, aucun seuil "amélioré") — dual momentum multi-classes façon Antonacci/GEM
--------------------------------------------------------------------------------------------
Univers risqué (8 ETF) : SPY, QQQ, IWM, EFA, EEM, VNQ, GLD, DBC.
Actif refuge / bogey de momentum absolu : IEF (`bond_mode="always_ief"` — jamais TLT, cf.
`docs/config-strategies.json`, aucun choix dynamique entre plusieurs refuges).
Paramètres : `top_k=3`, `lookback_months=12` (paramétrage retenu en production, cf.
`docs/SELECTION-FINALE.md` §1.2 : "combinaison la plus STABLE en plein-échantillon [...]
retenue plutôt que la plus souvent choisie en walk-forward" — choix pragmatique d'un jeu de
paramètres FIXE, documenté et assumé, PAS un paramètre par wallet).

  1. **Momentum relatif** — pour chacun des 8 ETF risqués, calcule le rendement total sur
     `lookback_months` mois glissants (clôture de référence / clôture ~12 mois avant - 1),
     classe les 8 par rendement décroissant, retient les `top_k=3` meilleurs.
  2. **Momentum absolu (vs bogey IEF)** — pour chacun des 3 sélectionnés, compare son propre
     rendement `lookback_months` à celui d'IEF sur la MÊME fenêtre (le "bogey"). Si son
     rendement est SUPÉRIEUR à celui d'IEF, le slot reste investi sur cet ETF. Sinon
     ("momentum absolu négatif" au sens de ce document — l'actif sous-performe le bogey),
     le slot bascule intégralement sur IEF (`bond_mode="always_ief"`).
  3. **Pondération** — chacun des `top_k` slots pèse `1/top_k` du poids retourné (poids brut
     0..1 par symbole, cf. `StrategyBase.target_weights`). IEF peut ainsi recevoir de 0 à
     `top_k` slots (jusqu'à 100% si les 3 sélectionnés échouent tous le test de momentum
     absolu — c'est le scénario "bascule refuge").

Contrairement à `bot/strategies/quasi_passif_crypto.py`, ces paramètres (`top_k`,
`lookback_months`, univers, bogey) sont FIXES par le SPEC pour TOUS les wallets qui portent
cette poche (`docs/SELECTION-FINALE.md` §3 : le wallet équilibré utilise "mêmes params que
prudent") — ils ne varient PAS par wallet et ne sont donc PAS lus depuis `profile["risque"]`
(qui ne les définit d'ailleurs pas dans `bot/config.py` actuel, cf. note d'intégration
ci-dessous) : ils sont codés en dur ci-dessous comme constantes de module, en toute fidélité
au SPEC (consigne de mission #1).

--------------------------------------------------------------------------------------------
Wallets concernés par cette poche (`ETF_POCKET_WALLETS`)
--------------------------------------------------------------------------------------------
`docs/SELECTION-FINALE.md` §3 : poche ETF présente pour 🛡️ prudent (55% du capital) et
⚖️ équilibré (25%) — ABSENTE du wallet 🔥 agressif ("Pas de poche ETF pour ce profil --
dual-momentum ETF ne bat pas statistiquement un 60/40 [...] et n'apporte pas de plus-value
évidente au profil le plus risqué."). `target_weights()` retourne `{}` pour tout autre
`profile["id"]` (agressif compris) — jamais d'extrapolation, même logique défensive que
`quasi_passif_crypto.SPEC_UNIVERSE_BY_WALLET`. Le dimensionnement exact par la part de
capital de la poche (`capital_alloc_pct`, absent du schéma `bot/config.py` actuel — cf. note
d'intégration `docs/config-strategies.json._meta.changements_proposes_vs_config_actuel`)
n'est PAS appliqué ici : cette stratégie retourne un poids brut 0..1 "plein pot" (comme
`quasi_passif_crypto`), le dimensionnement par poche restant hors périmètre de cette mission
(limitée à `bot/strategies/` et `bot/tests/`).

--------------------------------------------------------------------------------------------
Note d'intégration — source des bougies journalières (à l'attention de l'agent qui câble
cette stratégie dans `bot/runner.py`)
--------------------------------------------------------------------------------------------
`StrategyBase.target_weights` est une fonction PURE (aucun appel réseau, aucune écriture
disque, cf. sa docstring) — cette stratégie ne fait donc AUCUN appel à
`bot.feeds.get_daily_history()` elle-même. Elle s'attend à ce que `history[symbol]` (pour
chaque symbole de `RISKY_UNIVERSE + [BOND_BOGEY]`) soit déjà un DataFrame de bougies
JOURNALIÈRES CLÔTURÉES (colonnes `open/high/low/close/volume`, index `ts` croissant, JAMAIS
la bougie du jour en cours), au format retourné par `bot.feeds.get_daily_history(symbol,
n_days, asset_class="etf")` — c'est précisément l'interface que `bot/feeds/daily.py` documente
comme "attendue par bot.strategies.* pour tout signal calculé sur des clôtures quotidiennes".
C'est au runner (hors périmètre de cette mission) de peupler ce dict via
`bot.feeds.prefetch_daily_history()` + `bot.feeds.get_daily_history()` AVANT d'appeler
`combine_strategies()`, exactement comme il peuple aujourd'hui le dict horaire pour
`quasi_passif_crypto` via `bot.feeds.get_history()`.

--------------------------------------------------------------------------------------------
Rebalance MENSUEL persisté, sans aucun champ d'état supplémentaire, sans look-ahead
--------------------------------------------------------------------------------------------
Le SPEC impose une décision mensuelle ("dernier jour de bourse US du mois"), exécutée à
l'ouverture du jour de bourse suivant, PAS un recalcul quotidien du signal (qui trahirait la
cadence mensuelle même si les poids résultants ne changeraient que rarement d'un jour à
l'autre). Choix d'implémentation documenté, même principe que
`quasi_passif_crypto._daily_closes` (qui n'utilise QUE des jours calendaires complets, sans
jamais lire l'horloge système) mais à la granularité du MOIS plutôt que du jour :

`_last_confirmed_month_end()` ne retient une date `d` de l'historique d'IEF comme "dernier
jour de bourse du mois" QUE si une bougie plus récente d'un mois calendaire DIFFÉRENT est déjà
présente dans l'historique reçu. Un mois en cours (dont on n'a encore vu aucune bougie du mois
suivant) n'est donc JAMAIS retenu comme référence — la référence de calcul ("reference_date")
reste celle du dernier mois déjà confirmé, EXACTEMENT stable durant tout le mois suivant, et
n'avance QUE le jour où la toute première bougie du mois d'après apparaît dans l'historique
(ce qui correspond bien à "exécution à l'ouverture du jour de bourse US suivant" : la toute
première bougie journalière confirmant le nouveau mois est celle du jour d'exécution). Aucun
champ `state` dédié n'est donc nécessaire pour obtenir ce comportement — `state` est accepté
par `target_weights()` pour respecter l'interface `StrategyBase` mais n'est jamais lu ici.

Cette construction garantit également l'absence de look-ahead : `reference_date` ne peut
jamais être une date pour laquelle il resterait encore, dans la réalité, des séances de
bourse à venir dans le même mois — puisqu'elle n'est validée qu'après coup, par l'apparition
effective d'une bougie du mois suivant.

--------------------------------------------------------------------------------------------
Warmup (300 jours de bourse et plus, cf. consigne de mission)
--------------------------------------------------------------------------------------------
`MIN_WARMUP_TRADING_DAYS = 300` : un symbole (y compris IEF) n'est exploité pour un calcul de
rendement `lookback_months` QUE s'il dispose d'au moins 300 bougies journalières clôturées
jusqu'à `reference_date` inclus (12 mois ≈ 252 séances + marge). En-dessous de ce seuil, le
symbole est traité comme NON ÉLIGIBLE ce cycle (jamais une fenêtre de lookback raccourcie ou
approximée) — posture pessimiste identique à `quasi_passif_crypto`. Si IEF (le bogey) lui-même
n'a pas ce warmup, ou si `reference_date` ne peut pas être déterminée (historique IEF trop
court), AUCUN signal n'est émis pour aucun symbole (poids tous à 0) : le bogey est la seule
source de vérité pour `reference_date`, un momentum absolu ne peut jamais être évalué sans lui.

--------------------------------------------------------------------------------------------
Posture défensive (réseau bloqué en développement, ARCHITECTURE.md §0.2/§0.3)
--------------------------------------------------------------------------------------------
Comme `quasi_passif_crypto` : toute donnée insuffisante ou incohérente (moins de `top_k`
actifs risqués éligibles, IEF absent/insuffisant, `profile` incomplet, wallet hors
`ETF_POCKET_WALLETS`) se traduit TOUJOURS par une posture non investie (poids 0) plutôt que
par une extrapolation ou un remplacement créatif d'une valeur manquante. Si moins de `top_k`
actifs risqués sont éligibles (mais au moins un), les slots disponibles se partagent 1.0
également (`1 / nombre_de_slots_reellement_utilises`) plutôt que de laisser un reliquat de
capital inventé arbitrairement — décision d'implémentation documentée pour un cas dégénéré non
couvert explicitement par le SPEC (qui suppose implicitement les 8 ETF disponibles), PAS une
réinterprétation des paramètres `top_k`/`lookback_months` eux-mêmes.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

import pandas as pd

from bot.strategies import StrategyBase

# --- SPEC fixe (docs/config-strategies.json -> dual_momentum_multiclasse_etf) --------------
RISKY_UNIVERSE: List[str] = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "GLD", "DBC"]
BOND_BOGEY = "IEF"
TOP_K = 3
LOOKBACK_MONTHS = 12
MIN_WARMUP_TRADING_DAYS = 300  # cf. docstring module, "warmup 300j+" (consigne de mission)

# --- Wallets portant la poche ETF (docs/SELECTION-FINALE.md §3) ----------------------------
ETF_POCKET_WALLETS = {"prudent", "equilibre"}

__all__ = [
    "DualMomentumETF",
    "RISKY_UNIVERSE",
    "BOND_BOGEY",
    "TOP_K",
    "LOOKBACK_MONTHS",
    "MIN_WARMUP_TRADING_DAYS",
    "ETF_POCKET_WALLETS",
]


# ------------------------------------------------------------------------------------------
# Helpers purs (testés isolément)
# ------------------------------------------------------------------------------------------


def _daily_closes(history_df: Optional[pd.DataFrame]) -> pd.Series:
    """Extrait une série de clôtures journalières propre (triée, dédupliquée, positives
    uniquement) depuis un DataFrame `history[symbol]` au format `bot.feeds.get_daily_history`
    (cf. note d'intégration en tête de module). Ne fait AUCUNE agrégation horaire->journalière
    (contrairement à `quasi_passif_crypto._daily_closes`) : l'entrée est déjà journalière."""
    if history_df is None or "close" not in getattr(history_df, "columns", []):
        return pd.Series(dtype=float)
    closes = history_df["close"].astype(float)
    closes = closes[closes > 0]
    closes = closes.sort_index()
    closes = closes[~closes.index.duplicated(keep="last")]
    return closes.dropna()


def _last_confirmed_month_end(dates: pd.Index) -> Optional[pd.Timestamp]:
    """Retourne la plus récente date de `dates` (index trié croissant, sans doublon — contrat
    de `_daily_closes`) confirmée comme "dernier jour de bourse du mois" : une date `d` est
    confirmée SEULEMENT SI la date immédiatement suivante DANS `dates` appartient à un mois
    calendaire différent. Retourne `None` si aucune transition de mois n'est encore observable
    (moins de 2 dates, ou toutes les dates appartiennent au même mois calendaire) — cf.
    docstring module pour la justification (pas de look-ahead, rebalance mensuel persisté)."""
    idx = pd.DatetimeIndex(dates)
    if len(idx) < 2:
        return None
    # `to_period("M")` sur un index tz-aware émet un avertissement (perte de tz, sans impact
    # ici : seule la date calendaire du mois nous intéresse) -- normalisation explicite pour
    # rester silencieux plutôt que de laisser fuiter un avertissement à chaque appel.
    idx_naive = idx.tz_localize(None) if idx.tz is not None else idx
    months = idx_naive.to_period("M")
    is_month_end = months[:-1] != months[1:]
    candidates = idx[:-1][is_month_end]
    if len(candidates) == 0:
        return None
    return candidates.max()


def _total_return_asof(
    closes: pd.Series, reference_date: pd.Timestamp, months_back: int
) -> Optional[float]:
    """Rendement total `closes[reference_date] / closes[reference_date - months_back mois] -
    1`, ou `None` si `reference_date` n'est pas une clôture réellement disponible pour ce
    symbole, si moins de `MIN_WARMUP_TRADING_DAYS` bougies sont disponibles jusqu'à
    `reference_date` inclus, ou si aucune clôture n'existe au plus tôt à la date de départ
    visée (historique trop court pour couvrir tout le lookback) — jamais de fenêtre
    raccourcie/approximée en silence."""
    if closes is None or closes.empty:
        return None

    until_ref = closes[closes.index <= reference_date]
    if len(until_ref) < MIN_WARMUP_TRADING_DAYS:
        return None
    if until_ref.index[-1] != reference_date:
        # Ce symbole n'a pas de clôture exactement à reference_date (trou de données) :
        # posture pessimiste, on ne l'utilise pas ce cycle plutôt que d'approx via la clôture
        # disponible la plus proche.
        return None
    end_price = float(until_ref.iloc[-1])

    start_target = reference_date - pd.DateOffset(months=months_back)
    until_start = closes[closes.index <= start_target]
    if until_start.empty:
        return None
    start_price = float(until_start.iloc[-1])

    if not math.isfinite(start_price) or start_price <= 0:
        return None
    if not math.isfinite(end_price) or end_price <= 0:
        return None

    return end_price / start_price - 1.0


# ------------------------------------------------------------------------------------------
# Stratégie
# ------------------------------------------------------------------------------------------


class DualMomentumETF(StrategyBase):
    """Dual momentum multi-classes (8 ETF risqués + refuge IEF), rebalance mensuel persisté.
    Voir docstring de module pour le détail complet de l'algorithme, la réserve majeure
    documentée (SELECTION-FINALE.md §5) et les choix d'implémentation défensifs.
    """

    name = "dual_momentum_etf"
    # cf. docs/config-strategies.json -> dual_momentum_multiclasse_etf.market_hours_only :
    # décision/exécution alignées sur le calendrier de bourse US — attribut informatif pour
    # l'intégration runner (hors périmètre de cette mission), pas encore consommé ailleurs.
    market_hours_only = True

    def target_weights(
        self,
        history: Dict[str, pd.DataFrame],
        state: dict,
        profile: Optional[dict] = None,
    ) -> Dict[str, float]:
        profile = profile or {}
        history = history or {}
        wallet_id = profile.get("id")

        weights: Dict[str, float] = {sym: 0.0 for sym in RISKY_UNIVERSE + [BOND_BOGEY]}

        if wallet_id not in ETF_POCKET_WALLETS:
            # Wallet sans poche ETF (agressif) ou wallet inconnu : pas d'extrapolation.
            return {}

        # --- reference_date, ancrée sur IEF (seule source de vérité temporelle ici) --------
        ief_closes = _daily_closes(history.get(BOND_BOGEY))
        reference_date = _last_confirmed_month_end(ief_closes.index)
        if reference_date is None:
            return weights

        bogey_return = _total_return_asof(ief_closes, reference_date, LOOKBACK_MONTHS)
        if bogey_return is None:
            return weights

        # --- momentum relatif : rendement lookback_months de chaque actif risqué éligible ---
        returns: Dict[str, float] = {}
        for symbol in RISKY_UNIVERSE:
            closes = _daily_closes(history.get(symbol))
            ret = _total_return_asof(closes, reference_date, LOOKBACK_MONTHS)
            if ret is not None:
                returns[symbol] = ret

        if not returns:
            return weights

        ranked = sorted(
            returns.items(),
            key=lambda kv: (-kv[1], RISKY_UNIVERSE.index(kv[0])),
        )
        selected = ranked[:TOP_K]
        num_slots = len(selected)
        if num_slots == 0:
            return weights
        slot_weight = 1.0 / num_slots

        # --- momentum absolu vs bogey IEF : bascule refuge si l'actif sous-performe IEF ----
        for symbol, ret in selected:
            if ret > bogey_return:
                weights[symbol] += slot_weight
            else:
                weights[BOND_BOGEY] += slot_weight

        return weights
