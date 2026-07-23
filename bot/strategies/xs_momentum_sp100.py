"""bot/strategies/xs_momentum_sp100.py — momentum CROSS-SECTIONNEL S&P100, la stratégie
actions retenue pour les poches "equities" des wallets équilibré ⚖️ et agressif 🔥 (SPEC :
`docs/config-strategies.json` -> `strategy_definitions.xs_momentum_sp100` ; justifications et
réserves : `docs/SELECTION-FINALE.md` §1.1, §3, §5 ; implémentation de référence AUDITÉE dont ce
module reproduit fidèlement le comportement : `bt-final/xs-momentum-sp100/strategy.py`, réglage
de production figé = `lookback_months=6, rebalance_freq=monthly, weighting=equal`, cf.
`params_note` du SPEC).

Aucun seuil n'est "amélioré" ici par rapport au SPEC / à l'implémentation auditée — fidélité
absolue exigée par la mission. Là où le SPEC (texte, `docs/config-strategies.json`) est ambigu
ou silencieux sur un détail d'implémentation, ce module suit l'implémentation AUDITÉE
(`bt-final/xs-momentum-sp100/strategy.py`) qui a produit les métriques OOS citées en référence
(Sharpe 0.82, DSR 0.92) — jamais une interprétation alternative, aussi raisonnable soit-elle.

--------------------------------------------------------------------------------------------
Algorithme (repris à l'identique de `bt-final/xs-momentum-sp100/strategy.py`)
--------------------------------------------------------------------------------------------
  1. **Filtre de régime marché** — long autorisé seulement si le dernier close JOURNALIER
     CLÔTURÉ de SPY est strictement supérieur à sa SMA200 (moyenne des 200 dernières clôtures
     journalières). Si le filtre est "off" : 0% sur TOUTE la poche actions (cash), pas
     seulement un blocage des nouvelles entrées — cf. point dur (2) ci-dessous pour la
     justification de ce choix face à un texte SPEC un peu ambigu sur ce point précis.
  2. **Rebalancement mensuel, ancré sur le dernier jour de bourse US du mois** — le classement
     (quels titres, quels poids) n'est recalculé qu'au premier cycle où un nouveau mois-fin
     confirmé est disponible, et reste identique (figé) tout le reste du mois — cf. point dur
     (1) ci-dessous pour le mécanisme exact (dérivation pure, sans état persistant).
  3. **Classement momentum 6-1** — parmi les titres ÉLIGIBLES (assez d'historique réel, cf.
     ci-dessous), score = `close[t-skip] / close[t-skip-lookback] - 1` avec `skip=21` jours de
     bourse, `lookback=126` jours de bourse (convention ~21 jours de bourse/mois, `6*21=126`,
     reprise à l'identique de `LOOKBACK_DAYS_OF[6]` dans l'implémentation auditée). Top 10
     (`top_k`, FIXE) par score décroissant ; parmi ce top 10, on NE GARDE que les titres à
     momentum STRICTEMENT positif (pas de long forcé sur un perdant relatif) — aucun candidat
     positif -> 100% cash sur la poche.
  4. **Pondération équipondérée** parmi les survivants (`weighting="equal"`, réglage de
     production figé du SPEC — la branche `inv_vol`/`vol_lookback_days=63` documentée par le
     SPEC pour la grille de recherche n'est PAS implémentée ici, volontairement : elle ne fait
     pas partie du réglage retenu en production, cf. `params_note` du SPEC).

--------------------------------------------------------------------------------------------
Point dur (1) — rebalancement mensuel SANS état persistant (dérivation pure du calendrier)
--------------------------------------------------------------------------------------------
La consigne de mission envisage de persister la date du dernier rebalancement dans l'état du
wallet (champ `strategy_state`). Ce module choisit délibérément une alternative équivalente,
plus robuste et strictement dans le périmètre d'écriture autorisé (`bot/strategies/` +
`bot/tests/` uniquement — `bot/runner.py` et `bot/persist/state.py` construisent aujourd'hui
`new_state` avec un jeu de clés explicite et NE PROPAGENT PAS de champ `strategy_state`
arbitraire d'un cycle à l'autre ; y ajouter un tel champ nécessiterait de modifier ces deux
fichiers, hors périmètre de cette tâche) :

  - `_decision_date()` détermine, PUREMENT à partir du calendrier NYSE (`bot.feeds.calendar`,
    aucune dépendance aux données de prix disponibles CE cycle), le dernier jour de bourse déjà
    CONFIRMÉ comme dernier jour de bourse de son mois civil. "Confirmé" ici ne veut PAS dire
    "on a vu au moins un jour du mois suivant apparaître dans les données" (ce qui introduirait
    un jour de retard artificiel) mais "le calendrier NYSE lui-même atteste qu'aucun autre jour
    de bourse n'existe plus tard dans le même mois civil" — un calcul de calendrier pur, valable
    dès la clôture du jour même, cf. `_is_last_trading_day_of_month()`.
  - Tant que ce `_decision_date()` ne change pas d'un cycle à l'autre (i.e. tant qu'on est dans
    le même mois), le classement (quels titres, quels poids) est recalculé à l'IDENTIQUE à
    chaque appel, car il ne dépend QUE des clôtures antérieures ou égales à `_decision_date()`
    (`.loc[:decision_date]`) — toute donnée plus récente apparue en cours de mois est ignorée
    pour cette étape. Résultat strictement identique à une valeur persistée et relue, sans
    jamais avoir besoin d'écrire quoi que ce soit dans `state` — même principe déjà utilisé par
    `bot/strategies/quasi_passif_crypto.py` pour son "une décision par jour" (cf. sa docstring
    module, section "Fréquence de décision").
  - Le filtre de régime (étape 1) n'est PAS figé de cette façon : il est réévalué à CHAQUE appel
    avec la clôture SPY la plus RÉCENTE disponible (pas seulement à `_decision_date()`) — c'est
    un choix délibéré, pas un oubli, cf. point dur (2).
  - Si `bot/runner.py`/`bot/persist/state.py` sont étendus dans une future phase d'intégration
    pour propager un champ `state["strategy_state"]` générique d'un cycle à l'autre, ce module
    n'a besoin d'AUCUNE modification pour en bénéficier : le paramètre `state` de
    `target_weights()` reste accepté (signature `StrategyBase`) et pourrait alors servir de
    cache d'optimisation (éviter de re-scanner tout l'univers chaque cycle), mais ce n'est PAS
    requis pour la CORRECTION du comportement, seulement une optimisation de performance
    éventuelle — documenté ici pour le prochain agent d'intégration.

--------------------------------------------------------------------------------------------
Point dur (2) — filtre marché SPY>SMA200 : cash immédiat, réévalué CHAQUE cycle
--------------------------------------------------------------------------------------------
Le texte de `docs/config-strategies.json` ("pas de nouvelle position long si filtre off") peut
se lire comme "bloque seulement les nouvelles entrées". CE N'EST PAS le comportement de
l'implémentation auditée : `bt-final/xs-momentum-sp100/strategy.py` calcule
`out[i, :] = current * regime[i]` où `regime` est vérifié CHAQUE JOUR DE BOURSE (pas seulement
aux dates de rebalancement) — un régime "off" met la poche ENTIÈRE à 0 (liquidation complète),
pas seulement un gel des renforcements. C'est aussi explicitement ce que demande le point dur
(2) de la mission ("cibles actions = 0 (cash)"). Ce module suit l'implémentation auditée : le
filtre de régime est réévalué à CHAQUE appel de `target_weights()` avec la clôture SPY la plus
récente disponible dans `history["SPY"]` (pas figée à `_decision_date()`), donnant un
coupe-circuit immédiat dès que le régime bascule, sans attendre le rebalancement mensuel
suivant — fidélité au comportement réellement AUDITÉ, qui est la seule source de vérité pour
les métriques OOS citées en référence.

--------------------------------------------------------------------------------------------
Point dur (3) — uniquement des bougies JOURNALIÈRES CLÔTURÉES, aucun appel réseau ici
--------------------------------------------------------------------------------------------
`StrategyBase.target_weights()` est une fonction PURE (aucun appel réseau, aucune écriture
disque, cf. `bot/strategies/__init__.py`) : ce module ne récupère JAMAIS lui-même de données —
il suppose que `history[symbol]` (pour chaque symbole de `UNIVERSE_SP100` ET pour `"SPY"`) lui
est fourni par l'appelant DÉJÀ AGRÉGÉ EN BOUGIES JOURNALIÈRES CLÔTURÉES (colonnes
`open/high/low/close/volume`, index de dates croissant, AUCUNE bougie du jour en cours de
formation) — exactement le contrat de `bot.feeds.daily.get_daily_history()` /
`prefetch_daily_history()` (cf. docstring de ce module : "Interface publique attendue par
`bot.strategies.*` pour tout signal calculé sur des clôtures quotidiennes (filtre SMA200
crypto, momentum cross-sectionnel S&P100, dual-momentum ETF...)").

**Constat d'intégration important (audité, pas supposé)** : à la date de cette mission,
`bot/runner.py:process_wallet()` ne construit `history` qu'à partir de l'UNION des univers
CRYPTO des 3 wallets (`bot.feeds.get_history`, bougies HORAIRES) — il n'appelle PAS
`bot.feeds.daily.prefetch_daily_history()`/`get_daily_history()` pour les 103 tickers S&P100 ni
pour `"SPY"`, et ne route donc aujourd'hui AUCUNE donnée actions vers `combine_strategies()`.
Câbler cette récupération (et la fusionner dans le dict `history` passé aux stratégies) est un
travail d'intégration dans `bot/runner.py`, explicitement HORS PÉRIMÈTRE de cette tâche (limitée
à `bot/strategies/` + `bot/tests/`) — documenté ici pour le prochain agent d'intégration, avec
le rappel du warmup nécessaire : `WARMUP_BUFFER_DAYS = 400` jours de bourse (>
`skip_days + lookback_days` = 147, et > `SMA_DAYS` = 200, avec marge), cohérent avec
`bot.feeds.daily.MIN_WARMUP_DAYS = 400`.

--------------------------------------------------------------------------------------------
Point dur (4) — market_hours_only : déjà spécifié au niveau runner, PAS de ce module
--------------------------------------------------------------------------------------------
`docs/ARCHITECTURE.md` §5.1 (`is_us_market_open()`), §6 étape 3 et §7 documentent explicitement
que c'est le RUNNER (`bot/runner.py`), pas les stratégies, qui décide si un symbole action est
envoyé au pipeline de décision/exécution ce cycle ("Aucun ordre n'est jamais généré pour un
symbole action quand `is_us_market_open(now) == False`... Les positions actions existantes sont
conservées telles quelles"). C'est un choix d'architecture cohérent avec `StrategyBase` : une
stratégie reste une fonction PURE de `(history, state, profile)`, indépendante de l'heure
d'appel — exactement comme `quasi_passif_crypto` (actif 24/7) ne teste jamais l'horaire de
marché lui-même. `target_weights()` de ce module ne teste donc PAS `is_us_market_open()` — ce
serait une responsabilité dupliquée et hors de la portée d'une fonction pure.

**Vérification effectuée (pas supposée)** : `bot.feeds.calendar.is_us_market_open()` existe et
est correctement implémenté (jours fériés NYSE 2026/2027, séance 09:30-16:00 America/New_York).
**Mais, comme pour le point dur (3), `bot/runner.py:process_wallet()` ne l'utilise nulle part
aujourd'hui** (le cycle multi-wallets actuel est 100% crypto, cf. constat ci-dessus) — le
mécanisme de gating "marché fermé -> aucun ordre actions" décrit par ARCHITECTURE.md §7 N'EST
PAS ENCORE câblé dans le cycle réel. C'est un gap d'intégration identique à celui du point dur
(3), pas une lacune de ce module de stratégie — documenté ici pour le prochain agent
d'intégration, qui devra reproduire pour les actions le même motif déjà utilisé par le code
crypto (`market_open` dans `decisions.jsonl`, positions conservées hors séance).

--------------------------------------------------------------------------------------------
Univers restreint aux wallets qui le portent (SPEC §3 : équilibré 35%, agressif 30%)
--------------------------------------------------------------------------------------------
`docs/SELECTION-FINALE.md` §3 exclut explicitement `xs_momentum_sp100` du wallet prudent
("MaxDD historique 50.3%, incompatible avec l'objectif de préservation de capital... Un choix
de moins est un choix"). `bot/runner.py` combine aujourd'hui TOUTES les stratégies découvertes
pour TOUS les wallets sans distinction de poche (`combine_strategies` "placeholder" documenté,
cf. `bot/strategies/__init__.py`) : à défaut d'un mécanisme d'allocation par poche déjà câblé
(absent de `bot/config.py` -> `WALLETS[*]` aujourd'hui, cf. `_meta.changements_proposes` du
SPEC), ce module applique lui-même le filtrage par wallet, à l'identique de la technique déjà
utilisée par `quasi_passif_crypto.SPEC_UNIVERSE_BY_WALLET` : `profile["id"] not in
SPEC_EQUITIES_WALLETS` -> aucune cible émise (`{}`), jamais un `0.0` explicite qui purgerait à
tort une position existante sur un wallet qui ne devrait simplement jamais en détenir.

--------------------------------------------------------------------------------------------
Limitation connue, partagée avec `quasi_passif_crypto` : pas de scaling par poche de capital
--------------------------------------------------------------------------------------------
Les poids retournés somment à 1.0 au maximum parmi les titres sélectionnés (fraction de "la
poche actions"), PAS à `capital_alloc_pct` (35%/30% du SPEC) fraction du capital TOTAL du
wallet — `bot/config.py:WALLETS` ne définit aujourd'hui aucune notion de poche/allocation par
classe d'actif (uniquement `univers_crypto`/`risque`, cf. bandeau `_meta` du SPEC : "à ajouter
au schéma de config du bot"). Tant que ce schéma n'est pas étendu (hors périmètre de cette
tâche), `combine_strategies()` traite le poids retourné comme une fraction de l'ÉQUITY TOTALE
du wallet, pas de la seule poche actions — comportement partagé et déjà documenté par
`quasi_passif_crypto` pour la poche crypto, pas une régression propre à ce module.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

from bot.feeds.calendar import NYSE_HOLIDAYS
from bot.strategies import StrategyBase

# --- Univers SPEC (docs/config-strategies.json -> strategy_definitions.xs_momentum_sp100) ---
UNIVERSE_SP100: List[str] = [
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "AIG", "AMD", "AMGN", "AMT", "AMZN",
    "AVGO", "AXP", "BA", "BAC", "BKNG", "BLK", "BMY", "BRK.B", "C", "CAT",
    "CHTR", "CL", "CMCSA", "COF", "COP", "COST", "CRM", "CSCO", "CVS", "CVX",
    "DE", "DHR", "DIS", "DOW", "DUK", "EMR", "F", "FDX", "GD", "GE",
    "GILD", "GM", "GOOG", "GOOGL", "GS", "HD", "HON", "IBM", "INTC", "INTU",
    "ISRG", "JNJ", "JPM", "KHC", "KMI", "KO", "LIN", "LLY", "LMT", "LOW",
    "MA", "MCD", "MDLZ", "MDT", "MET", "META", "MMM", "MO", "MRK", "MS",
    "MSFT", "NEE", "NFLX", "NKE", "NVDA", "ORCL", "PEP", "PFE", "PG", "PM",
    "PYPL", "QCOM", "RTX", "SBUX", "SCHW", "SO", "SPG", "T", "TGT", "TJX",
    "TMO", "TMUS", "TSLA", "TXN", "UNH", "UNP", "UPS", "USB", "V", "VZ",
    "WFC", "WMT", "XOM",
]
assert len(UNIVERSE_SP100) == 103, f"attendu 103 tickers, trouvé {len(UNIVERSE_SP100)}"

MARKET_FILTER_SYMBOL = "SPY"

# --- Paramètres FIXES de production (docs/config-strategies.json -> params, params_note) ---
TOP_K = 10
SKIP_DAYS = 21
LOOKBACK_MONTHS = 6
TRADING_DAYS_PER_MONTH = 21  # convention académique reprise à l'identique de l'implémentation
# auditée (`LOOKBACK_DAYS_OF = {12: 252, 6: 126}` dans bt-final/xs-momentum-sp100/strategy.py).
LOOKBACK_DAYS = LOOKBACK_MONTHS * TRADING_DAYS_PER_MONTH  # 126
VOL_LOOKBACK_DAYS = 63  # documenté par le SPEC pour la pondération "inv_vol" (grille de
# recherche) ; NON utilisé en production (weighting="equal" figé, cf. docstring module) — gardé
# ici uniquement pour traçabilité avec le SPEC, jamais lu par le code ci-dessous.
SMA_DAYS = 200
WARMUP_BUFFER_DAYS = 400

# Wallets qui portent effectivement cette poche (docs/SELECTION-FINALE.md §3) — le wallet
# prudent 🛡️ exclut explicitement les actions individuelles.
SPEC_EQUITIES_WALLETS = {"equilibre", "agressif"}

__all__ = [
    "XsMomentumSp100",
    "UNIVERSE_SP100",
    "MARKET_FILTER_SYMBOL",
    "TOP_K",
    "SKIP_DAYS",
    "LOOKBACK_DAYS",
    "SMA_DAYS",
    "WARMUP_BUFFER_DAYS",
    "SPEC_EQUITIES_WALLETS",
]


# ------------------------------------------------------------------------------------------
# Helpers purs (testés isolément) — calendrier NYSE
# ------------------------------------------------------------------------------------------


def _is_nyse_trading_day(d: date) -> bool:
    """Jour de bourse NYSE (jour ouvré, hors jours fériés `bot.feeds.calendar.NYSE_HOLIDAYS`).
    Réutilise la SEULE source de vérité du calendrier déjà maintenue par le projet — aucune
    liste de jours fériés dupliquée ici."""
    if d.weekday() >= 5:  # 5=samedi, 6=dimanche
        return False
    return d not in NYSE_HOLIDAYS


def _is_last_trading_day_of_month(d: date) -> bool:
    """True si `d` est lui-même le DERNIER jour de bourse NYSE de son mois civil — calcul de
    calendrier PUR (aucune dépendance aux données de prix disponibles), donc "confirmé" dès la
    clôture du jour même, sans attendre qu'un jour du mois suivant apparaisse dans les données
    (cf. point dur (1) de la docstring module)."""
    probe = d + timedelta(days=1)
    while probe.month == d.month and probe.year == d.year:
        if _is_nyse_trading_day(probe):
            return False
        probe += timedelta(days=1)
    return True


# ------------------------------------------------------------------------------------------
# Helpers purs (testés isolément) — données
# ------------------------------------------------------------------------------------------


def _daily_closes(df: Optional[pd.DataFrame]) -> pd.Series:
    """Série `close` triée, dédoublonnée (dernière valeur conservée), sans NaN, à partir d'un
    DataFrame de bougies JOURNALIÈRES déjà clôturées (contrat `bot.feeds.daily.get_daily_
    history`, cf. point dur (3)). Ne fait AUCUNE agrégation horaire->journalière ici (contrairement
    à `quasi_passif_crypto._daily_closes`) : l'entrée est supposée DÉJÀ journalière."""
    if df is None or "close" not in getattr(df, "columns", []):
        return pd.Series(dtype=float)
    closes = pd.to_numeric(df["close"], errors="coerce").dropna()
    if closes.empty:
        return pd.Series(dtype=float)
    closes = closes.sort_index()
    closes = closes[~closes.index.duplicated(keep="last")]
    return closes


def _decision_date(available_dates) -> Optional[pd.Timestamp]:
    """Le plus RÉCENT timestamp de `available_dates` (index/itérable trié croissant de dates
    disponibles) qui est CONFIRMÉ comme dernier jour de bourse de son mois civil (calendrier
    NYSE pur, cf. `_is_last_trading_day_of_month`). Retourne `None` si aucune date disponible
    n'est un tel mois-fin confirmé (historique trop court)."""
    dates = list(available_dates)
    for ts in reversed(dates):
        d = pd.Timestamp(ts).date()
        if _is_last_trading_day_of_month(d):
            return ts
    return None


def _momentum_as_of(
    closes: pd.Series, decision_date: pd.Timestamp, skip_days: int, lookback_days: int
) -> Optional[float]:
    """`close[t-skip] / close[t-skip-lookback] - 1`, où `t` = position de `decision_date` dans
    `closes` restreinte à `.loc[:decision_date]` (aucune donnée postérieure à `decision_date`
    n'intervient — c'est ce qui garantit le gel du classement entre deux rebalancements, cf.
    point dur (1)). Retourne `None` (jamais une valeur approximée) si moins de
    `skip_days + lookback_days + 1` clôtures réelles ne sont disponibles jusqu'à
    `decision_date` inclus — exactement le seuil d'éligibilité de l'implémentation auditée
    (`threshold_days = lookback_days + skip_days`, cf. `bt-final/xs-momentum-sp100/strategy.py`)."""
    closes_upto = closes.loc[:decision_date]
    needed = skip_days + lookback_days + 1
    if len(closes_upto) < needed:
        return None
    price_recent = float(closes_upto.iloc[-1 - skip_days])
    price_past = float(closes_upto.iloc[-1 - skip_days - lookback_days])
    if price_past == 0 or math.isnan(price_recent) or math.isnan(price_past):
        return None
    return price_recent / price_past - 1.0


def _market_regime_on(spy_closes: pd.Series, sma_days: int = SMA_DAYS) -> Optional[bool]:
    """True/False si `sma_days` clôtures SPY sont disponibles (SMA200 fiable), sinon `None`
    (donnée insuffisante -> l'appelant doit traiter comme "filtre off", jamais une SMA
    approximée sur une fenêtre plus courte, principe pessimiste cardinal du projet). Utilise
    la clôture SPY la PLUS RÉCENTE disponible (pas figée à la date de décision mensuelle),
    cf. point dur (2)."""
    if spy_closes is None or len(spy_closes) < sma_days:
        return None
    window = spy_closes.iloc[-sma_days:]
    sma = float(window.mean())
    last_close = float(spy_closes.iloc[-1])
    if math.isnan(sma) or math.isnan(last_close):
        return None
    return last_close > sma


def _rank_and_select(
    universe: List[str],
    history: Dict[str, pd.DataFrame],
    decision_date: pd.Timestamp,
    top_k: int = TOP_K,
    skip_days: int = SKIP_DAYS,
    lookback_days: int = LOOKBACK_DAYS,
) -> List[Tuple[str, float]]:
    """Classement momentum cross-sectionnel à `decision_date`, restreint aux titres ÉLIGIBLES
    (assez d'historique réel). Retourne les gagnants retenus (top `top_k`, puis momentum
    strictement positif uniquement) triés par momentum décroissant — liste vide si aucun
    candidat éligible n'a un momentum positif."""
    candidates: List[Tuple[str, float]] = []
    for symbol in universe:
        closes = _daily_closes(history.get(symbol))
        if closes.empty:
            continue
        mom = _momentum_as_of(closes, decision_date, skip_days, lookback_days)
        if mom is not None:
            candidates.append((symbol, mom))

    if not candidates:
        return []

    # Tri par momentum décroissant, tie-break alphabétique (déterminisme, absent du texte du
    # SPEC mais nécessaire pour un résultat reproductible en cas d'égalité exacte).
    candidates.sort(key=lambda t: (-t[1], t[0]))
    top = candidates[:top_k]
    return [(sym, mom) for sym, mom in top if mom > 0.0]


# ------------------------------------------------------------------------------------------
# Stratégie
# ------------------------------------------------------------------------------------------


class XsMomentumSp100(StrategyBase):
    """Momentum cross-sectionnel S&P100 (top 10, skip 21j, lookback 6 mois, rebalancement
    mensuel au dernier jour de bourse confirmé, équipondéré, filtre SPY>SMA200 réévalué à
    chaque cycle). Voir docstring de module pour l'algorithme complet et les points durs."""

    name = "xs_momentum_sp100"

    def target_weights(
        self,
        history: Dict[str, pd.DataFrame],
        state: dict,
        profile: Optional[dict] = None,
    ) -> Dict[str, float]:
        profile = profile or {}
        history = history or {}

        wallet_id = profile.get("id")
        if wallet_id not in SPEC_EQUITIES_WALLETS:
            # Wallet qui ne porte pas cette poche (prudent 🛡️, ou wallet inconnu) : aucune
            # cible émise, jamais un 0.0 qui purgerait à tort une position existante sur un
            # wallet qui ne devrait simplement jamais en détenir (cf. docstring module).
            return {}

        weights: Dict[str, float] = {symbol: 0.0 for symbol in UNIVERSE_SP100}

        spy_closes = _daily_closes(history.get(MARKET_FILTER_SYMBOL))
        if spy_closes.empty:
            # Aucune donnée SPY exploitable : ni le filtre de régime ni la date de décision ne
            # sont calculables -> aucune décision informée ce cycle (positions existantes
            # conservées par le RiskManager, cf. bot/risk/manager.py : "aucune cible brute
            # fournie -> poids conservé"), PAS un 0.0 explicite qui forcerait une liquidation.
            return {}

        # --- 1. filtre de régime marché, réévalué CHAQUE cycle (point dur 2) -----------------
        regime_on = _market_regime_on(spy_closes)
        if regime_on is not True:
            # `None` (SMA200 non calculable, historique SPY insuffisant) ou `False` (régime
            # baissier confirmé) -> posture la plus prudente : 100% cash sur la poche actions.
            return weights

        # --- 2. date de décision = dernier jour de bourse confirmé du mois (point dur 1) -----
        decision_date = _decision_date(spy_closes.index)
        if decision_date is None:
            # Aucun mois-fin encore confirmé dans l'historique SPY disponible (warmup
            # insuffisant) -> cash, jamais un rebalancement anticipé sur une date incertaine.
            return weights

        # --- 3+4. classement + pondération équipondérée, figés jusqu'au prochain mois-fin ----
        winners = _rank_and_select(UNIVERSE_SP100, history, decision_date)
        if not winners:
            return weights

        w = 1.0 / len(winners)
        for symbol, _mom in winners:
            weights[symbol] = w

        return weights
