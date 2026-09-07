"""backtest/engine.py — MOTEUR COMMUN de backtest walk-forward (docs/PROMOTION-RULES.md §1.1).

Ce module ne connaît RIEN d'une stratégie particulière : il consomme une matrice de poids déjà
DÉCIDÉS jour par jour (`weights_decided`, cf. ci-dessous) et simule un portefeuille long-only sur
barres quotidiennes. La logique spécifique à une stratégie (ex. `xs_momentum_sp100` en variante
"backtest", cf. `backtest/strategies/xsmom.py`) construit cette matrice ; ce module ne fait que
l'EXÉCUTER honnêtement.

--------------------------------------------------------------------------------------------
Principe non négociable n°1 — aucun look-ahead
--------------------------------------------------------------------------------------------
`weights_decided.loc[t]` doit être calculable UNIQUEMENT à partir de données de clôture `<= t`
(c'est la responsabilité de la couche stratégie, cf. `backtest/strategies/xsmom.py`). Ce moteur
applique une règle stricte et non contournable : la ligne `weights_decided.loc[t]` est exécutée
à l'OUVERTURE du jour de bourse SUIVANT `t` dans le calendrier (`open[t+1]`), jamais à la
clôture de `t` elle-même ni à une clôture antérieure. `backtest/tests/test_engine.py` contient
un test construit spécifiquement pour détecter la régression inverse (un moteur qui exécuterait
au même prix que celui utilisé pour décider verrait sa performance s'effondrer une fois le
décalage correctement appliqué, cf. docstring de ce test).

--------------------------------------------------------------------------------------------
Principe non négociable n°2 — coûts sur turnover réel
--------------------------------------------------------------------------------------------
`cost_bps` (points de base PAR CÔTÉ) est appliqué au dollar-turnover RÉEL de chaque rebalance
(somme des valeurs absolues des variations de position en dollars, valorisées au prix
d'exécution) — achats ET ventes payent chacun `cost_bps`, jamais un coût symétrique compté une
seule fois pour l'aller-retour.

--------------------------------------------------------------------------------------------
Principe non négociable n°3 — walk-forward IS/OOS, sélection IS-only, métriques OOS concaténées
--------------------------------------------------------------------------------------------
`generate_walk_forward_windows()` construit des fenêtres glissantes non chevauchantes (IS puis
OOS immédiatement adjacente). Si une grille de paramètres (`param_grid`) est fournie,
`select_params_via_is()` choisit les paramètres UNIQUEMENT sur la performance mesurée dans la
fenêtre IS (jamais en regardant l'OOS, cf. `docs/PROMOTION-RULES.md` §1.4 dernier point). Les
métriques de décision (`backtest/metrics.py`) sont ensuite calculées sur la CONCATÉNATION des
rendements quotidiens OOS de toutes les fenêtres — jamais sur une seule fenêtre isolée ni sur la
période complète non découpée.

--------------------------------------------------------------------------------------------
Convention "un trade" vs "un évènement de réalisation" (n_trades_closed / profit factor)
--------------------------------------------------------------------------------------------
Pour chaque symbole, ce moteur maintient un coût de revient moyen pondéré (`avg_cost`) et un
nombre d'actions détenues (`shares`) déduits de `weights_decided` (aucun état "trade" explicite
n'existe ailleurs dans le projet à reproduire ici — convention propre à ce moteur, documentée
explicitement comme demandé) :
  - **Ouverture** : `shares` passe de 0 à un nombre positif -> nouvelle "ligne" ouverte
    (`open_date` mémorisée), aucune réalisation de PnL.
  - **Renforcement** : `shares` augmente sans repasser par 0 -> `avg_cost` mis à jour (moyenne
    pondérée), toujours aucune réalisation.
  - **Réduction partielle** (`shares` diminue sans atteindre 0) : le PnL des actions VENDUES est
    RÉALISÉ immédiatement (`(prix_vente - avg_cost) * actions_vendues`) et compte comme un
    **évènement de réalisation** dans le pool utilisé par `profit_factor()` (gain ou perte) —
    mais ne compte PAS comme une unité supplémentaire de `n_trades_closed` : la "ligne" reste
    ouverte tant que `shares > 0`. C'est le sens précis de "les rebalances partiels comptent
    proportionnellement" (mission) : leur PnL réalisé pèse dans le profit factor au prorata des
    actions effectivement vendues, sans gonfler artificiellement le nombre de trades clos.
  - **Fermeture complète** (`shares` retombe à 0) : le PnL résiduel est réalisé, la ligne est
    comptée dans `n_trades_closed` (ET son PnL alimente aussi `profit_factor()`, comme toute
    autre réalisation).
  - Une position encore ouverte à la fin de la fenêtre simulée n'est PAS forcée à se fermer et
    n'est PAS comptée dans `n_trades_closed` (conforme à `docs/PROMOTION-RULES.md` §1.2 :
    "`n_trades_closed`, pas `n_trades_total` qui inclut les positions encore ouvertes").
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from backtest import metrics as bt_metrics
from backtest import risk_overlay

# ------------------------------------------------------------------------------------------
# Extension short/perpétuels + funding (backtest/PERP-EXTENSION-SPEC.md) -- clés du dict
# `pnl_breakdown` de `SegmentResult`, toujours les MÊMES 6 clés (même sans perp -- valeurs
# perp à 0.0 -- pour que `concatenate_segments` puisse sommer terme à terme sans jamais avoir
# à gérer un dict partiel).
# ------------------------------------------------------------------------------------------
PNL_BREAKDOWN_KEYS = (
    "spot_pnl",
    "perp_variation",
    "funding_received",
    "costs_spot",
    "costs_perp",
    "liquidation_fees",
)


def _empty_pnl_breakdown() -> dict:
    return {k: 0.0 for k in PNL_BREAKDOWN_KEYS}


def _sum_pnl_breakdowns(breakdowns: Sequence[dict]) -> dict:
    total = _empty_pnl_breakdown()
    for bd in breakdowns:
        for k in PNL_BREAKDOWN_KEYS:
            total[k] += float(bd.get(k, 0.0))
    return total

# ------------------------------------------------------------------------------------------
# Extension portage inter-fenêtres (backtest/CARRY-EXTENSION-SPEC.md) -- structures de données
# UNIQUEMENT (la logique de reconstruction/packaging vit dans le corps de `simulate_segment`,
# cf. commentaires dédiés plus bas). Ces deux dataclasses sont intégralement OPTIONNELLES :
# `CarryState` n'est jamais construite ni consommée si l'appelant ne passe pas `carry_in` et
# n'utilise pas `SegmentResult.carry_out` -- aucun impact sur le chemin historique (spec §1.1).
# ------------------------------------------------------------------------------------------


@dataclass
class CarryLine:
    """Métadonnées d'UNE ligne (spot ou perp) encore ouverte à la fin d'un segment, destinée à
    être portée vers la fenêtre OOS suivante si le RUNNER active l'extension et que la
    contiguïté calendaire est vérifiée (CARRY-EXTENSION-SPEC.md §1.3/§2/§4).

    `pnl_accum`/`cost_accum` sont NORMALISÉS : fraction de L'ÉQUITY DE CLÔTURE DU SEGMENT
    (`equity.iloc[-1]`, PAS `initial_capital`) DE CHAQUE FENÊTRE où ils ont été accrus, sommée
    fenêtre après fenêtre -- jamais un montant dollar brut (spec §2/§4.2, cohérent avec
    `concatenate_segments` qui concatène déjà des RENDEMENTS fractionnaires d'une fenêtre à
    l'autre, jamais des dollars absolus).
    Correctif audit adversarial F1 (CRITIQUE, cf. amendement CARRY-EXTENSION-SPEC.md §7 et
    `backtest/README.md` section "Portage inter-fenêtres") : la base de normalisation DOIT être
    la MÊME que celle utilisée pour reconstruire les `shares` au segment suivant
    (`weights_at_last_close[s] = shares_final[s]*last_close[s] / equity_final` -- déjà une
    fraction de `equity_final`, jamais de `initial_capital`). Normaliser `pnl_accum` par
    `initial_capital` (l'ancienne convention, MATHÉMATIQUEMENT PROUVÉE FAUSSE dès que le segment
    ne rend pas exactement 0% -- `equity_final != initial_capital` presque toujours) introduisait
    un facteur d'échelle DIFFÉRENT de celui des `shares` reconstruites (`initial_capital_suivant
    / equity_final` pour les shares, contre `initial_capital_suivant / initial_capital` pour le
    PnL porté, les deux ne coïncidant QUE si `equity_final == initial_capital`) : le PnL non
    réalisé "manquant" par action (`pnl_accum_$ / shares`) divergeait alors du véritable coût de
    revient moyen implicite, et cette erreur se propageait -- gonflée ou dégonflée selon le sens
    du rendement du segment précédent -- à CHAQUE réalisation ultérieure (réduction partielle OU
    fermeture complète), y compris SANS aucun renforcement post-reconstruction (démontré par
    `backtest/tests/test_carry.py::test_anti_gaming_profit_factor_matches_continuous_simulation_
    across_seeds`, cas à une seule réduction). Avec `equity_final` comme base, le même facteur
    d'échelle s'applique aux DEUX quantités (`shares` et `pnl_accum` en dollars du segment
    suivant) et la reconstruction préserve EXACTEMENT le ratio "PnL non réalisé par action" --
    la comptabilité par ligne (`avg_cost` + `_spot_prior_pnl`/`perp_pnl_accum`) redevient alors
    l'exact télescopage d'une simulation continue, réduction partielle ou renforcement compris
    (cf. dérivation complète dans le rapport d'implémentation). `pnl_accum` reste NET de coûts
    (il inclut déjà l'effet de `cost_accum`) -- à la fermeture réelle de la ligne, la contribution
    de la fenêtre COURANTE (déjà nette de SES PROPRES coûts par la mécanique existante du moteur)
    est simplement ADDITIONNÉE à `pnl_accum` porté, jamais recombinée une seconde fois avec
    `cost_accum`. `cost_accum` est une décomposition INFORMATIVE (audit/transparence : "combien
    cette ligne a-t-elle payé de coûts cumulés sur toute sa vie, toutes fenêtres confondues") --
    monotone croissante, jamais réinjectée dans un calcul de PnL (pour ne jamais compter un coût
    deux fois) -- normalisée par la MÊME base (`equity_final`) uniquement par cohérence de
    représentation avec `pnl_accum`, sa valeur n'entrant elle-même jamais dans une formule de
    PnL."""

    symbol: str
    leg: str  # "spot" ou "perp" -- jamais autre chose (miroir de `perp_symbols` du segment)
    line_id: str  # stable tant que la ligne reste ouverte -- `f"{symbol}:{leg}"` (au plus UNE
    # ligne ouverte par (symbole, jambe) à la fois dans ce moteur -- cf. docstring module)
    pnl_accum: float
    cost_accum: float
    entry_ts: "pd.Timestamp"  # timestamp d'entrée D'ORIGINE, jamais réécrit par un portage
    carried_windows: int  # nombre de frontières de fenêtre déjà traversées par cette ligne


@dataclass
class CarryState:
    """État de fin de segment nécessaire à la reconstruction du début du segment suivant
    (CARRY-EXTENSION-SPEC.md §2-3). Produit TOUJOURS par `simulate_segment` (même sans
    `carry_in`, cf. spec §2) -- c'est au RUNNER de décider de le rebrancher ou non en
    `carry_in` de l'appel suivant."""

    last_idx: int  # position ENTIÈRE dans le calendrier de la DERNIÈRE bougie simulée (spec §1.3)
    weights_at_last_close: pd.Series  # poids RÉELLEMENT détenus, marqués à `last_close`, SIGNÉS
    last_close: pd.Series  # prix de clôture par symbole porté (spec §3, reconstruction)
    open_lines: List[CarryLine] = field(default_factory=list)


# ------------------------------------------------------------------------------------------
# Simulation de portefeuille sur un segment de calendrier donné
# ------------------------------------------------------------------------------------------


@dataclass
class SegmentResult:
    dates: pd.DatetimeIndex
    equity: pd.Series  # valeur de clôture du portefeuille chaque jour du segment
    returns: pd.Series  # rendements quotidiens (le premier = retour du jour 0 vs capital initial)
    gross_exposure: pd.Series  # fraction (0..1 en long-only) de l'équity investie, en clôture
    trades_closed: List[dict] = field(default_factory=list)
    realized_events: List[dict] = field(default_factory=list)
    # --- Extension perp (PERP-EXTENSION-SPEC.md §2-4), champs optionnels rétro-compatibles :
    # listes/dict VIDES par défaut -- un appelant historique qui ignore ces champs (n'existaient
    # pas avant cette extension) ne voit STRICTEMENT rien changer.
    liquidations: List[dict] = field(default_factory=list)  # évènements de liquidation perp
    pnl_breakdown: dict = field(default_factory=_empty_pnl_breakdown)  # PnL par jambe (spec §4)
    # --- Extension portage inter-fenêtres (CARRY-EXTENSION-SPEC.md §2) : `None` par défaut --
    # un appelant historique (qui ignore ce champ, n'existait pas avant cette extension) ne voit
    # STRICTEMENT rien changer. TOUJOURS renseigné par `simulate_segment` (même sans `carry_in`)
    # SAUF si aucune position n'est ouverte en fin de segment (spec §2, dernier point).
    carry_out: Optional["CarryState"] = None

    def n_trades_closed(self) -> int:
        return len(self.trades_closed)

    def n_liquidations(self) -> int:
        return len(self.liquidations)


def simulate_segment(
    calendar: pd.DatetimeIndex,
    weights_decided: pd.DataFrame,
    opens: pd.DataFrame,
    closes: pd.DataFrame,
    start_idx: int,
    end_idx: int,
    cost_bps: float,
    initial_capital: float = 1.0,
    no_trade_band: float = risk_overlay.DEFAULT_NO_TRADE_BAND,
    apply_vol_targeting: bool = True,
    vol_target_annualized: float = risk_overlay.DEFAULT_VOL_TARGET_ANNUALIZED,
    vol_ewma_halflife_days: float = risk_overlay.DEFAULT_VOL_EWMA_HALFLIFE_DAYS,
    vol_coldstart_min_points: int = risk_overlay.DEFAULT_VOL_COLDSTART_MIN_POINTS,
    vol_coldstart_scalar: float = risk_overlay.DEFAULT_VOL_COLDSTART_SCALAR,
    vol_periods_per_year: float = risk_overlay.DEFAULT_VOL_PERIODS_PER_YEAR,
    # --- Extension short/perpétuels + funding (PERP-EXTENSION-SPEC.md §2), TOUS optionnels --
    # `perp_symbols` à `None` (défaut) : AUCUNE des branches ci-dessous ne s'exécute, le moteur
    # reproduit EXACTEMENT le comportement historique (exigence de rétro-compatibilité bit à
    # bit, cf. docstring ci-dessous et `backtest/tests/test_perp.py`).
    perp_symbols: Optional[Iterable[str]] = None,
    funding: Optional[pd.DataFrame] = None,
    highs: Optional[pd.DataFrame] = None,
    lows: Optional[pd.DataFrame] = None,
    perp_cost_bps: Optional[float] = None,
    perp_initial_margin_frac: float = 0.50,
    perp_maintenance_margin_frac: float = 0.025,
    perp_liquidation_fee_bps: float = 100.0,
    # --- Extension portage inter-fenêtres (CARRY-EXTENSION-SPEC.md §2), optionnelle --
    # `carry_in=None` (défaut) : AUCUNE des branches ci-dessous ne s'exécute, comportement
    # historique bit-identique (spec §1.1, cf. `backtest/tests/test_carry.py`).
    carry_in: Optional["CarryState"] = None,
) -> SegmentResult:
    """Simule le portefeuille sur `calendar[start_idx:end_idx+1]`, capital remis à
    `initial_capital` au tout début du segment (nécessaire pour produire des fenêtres OOS
    indépendantes dont les RENDEMENTS peuvent être concaténés, cf. docstring module — la
    remise à zéro du capital ne réintroduit AUCUN look-ahead : la décision exécutée le premier
    jour du segment (`weights_decided.iloc[start_idx-1]`) a été calculée causalement, souvent
    bien avant `start_idx`, ce qui est attendu et correct, pas une fuite IS->OOS).

    `weights_decided.iloc[i]` = poids DÉCIDÉS à la clôture de `calendar[i]`, EXÉCUTÉS à
    `opens.iloc[i+1]` (ouverture du jour de bourse suivant) — jamais au même prix que celui
    ayant servi à la décision.

    --------------------------------------------------------------------------------------
    Surcouche de risque (correctif audit 2026-07-27, `backtest/risk_overlay.py`)
    --------------------------------------------------------------------------------------
    AVANT exécution, `weights_decided.iloc[i-1]` (le poids brut DÉCIDÉ par la stratégie) passe
    par la MÊME surcouche que `bot/risk/manager.py` applique en production (vol targeting PUIS
    bande de non-négociation, dans cet ordre — cf. `bot/risk/manager.py` étapes 3 et 6) :

      - `apply_vol_targeting` (défaut `True`) : le poids brut est multiplié par un scalaire
        `<= 1` qui réduit l'exposition quand la vol EWMA du portefeuille dépasse
        `vol_target_annualized` (défaut `bot.config.VOL_TARGET_ANNUALIZED`) — jamais
        l'inverse. Mettre `False` neutralise cette étape (utile pour isoler d'autres tests du
        moteur, cf. `backtest/tests/test_engine.py`).
      - `no_trade_band` (défaut `bot.config.NO_TRADE_BAND` = 0,05, ALIGNÉ PRODUCTION) : si
        l'écart entre le poids scalé et le poids RÉELLEMENT PORTÉ ce matin (position dérivée,
        marquée au prix d'ouverture — `poids_actuel` au sens de `bot/risk/manager.py` étape 6,
        jamais le poids brut du signal) est strictement inférieur à cette bande, AUCUN ordre
        n'est émis pour ce symbole : ses `shares` restent STRICTEMENT inchangées et la position
        dérive librement avec le prix. Correctif audit 2026-08-03 (F1) : l'implémentation
        précédente figeait le POIDS puis reprojetait les shares sur ce poids à chaque barre
        (equity et prix bougeant) — 96,9% des bougies BTC généraient un ordre malgré un signal
        constant, vidant la bande de son effet et créant coûts + rebalancing premium fantômes.
        Mettre `0.0` désactive cette étape (reprojection continue assumée).

    Cette surcouche est volontairement PLUS SIMPLE que `bot/risk/manager.py` (pas de circuit
    breakers, pas de caps par actif, pas de bande par poche, pas de cap d'exposition brute
    totale) — écart connu et documenté, cf. `backtest/risk_overlay.py` et la ligne de tête de
    `docs/RESEARCH-BACKLOG.md`.

    --------------------------------------------------------------------------------------
    Extension short/perpétuels + funding (`backtest/PERP-EXTENSION-SPEC.md`, pré-enregistrée --
    cette spec prime en cas de divergence avec cette docstring)
    --------------------------------------------------------------------------------------
    `perp_symbols` désigne le sous-ensemble des colonnes de `weights_decided` (et donc de
    `opens`/`closes`, qui doivent déjà contenir CES colonnes -- l'appelant fusionne
    spot+perp AVANT d'appeler cette fonction, cf. `backtest/perp.py:build_aligned_perp_matrices`)
    traité comme des PERPÉTUELS : poids SIGNÉS autorisés (short = poids négatif), marge
    (jamais le spot comme collatéral), liquidation intra-bougie, funding périodique. Les
    colonnes NON listées dans `perp_symbols` restent long-only strict (poids négatif -> `ValueError`
    -- règle inconditionnelle, indépendante de `perp_symbols`, cf. spec §2).

    `funding`/`highs`/`lows` sont OBLIGATOIRES dès que `perp_symbols` est non vide (`ValueError`
    sinon) -- alignées sur le MÊME calendrier que `opens`/`closes` (une ligne = une bougie,
    même position `i`), colonnes = au moins `perp_symbols`. `perp_cost_bps` défaut = `cost_bps`
    (coût par côté sur le turnover dollar de la jambe perp, séparé du spot). `perp_initial_margin_frac`
    (défaut 0,50 = levier 2 max), `perp_maintenance_margin_frac` (défaut 0,025) et
    `perp_liquidation_fee_bps` (défaut 100 = 1 %) suivent `PERP-EXTENSION-SPEC.md` §2.

    Comptabilité PAR BOUGIE (spec §3, DANS CET ORDRE, jamais réordonnée) :
      1. Surcouche de risque (vol targeting PUIS bande, inchangée) -- SEULE différence : la vol
         de portefeuille utilisée pour le vol targeting est estimée sur `|w|` (poids en valeur
         absolue) quand `perp_symbols` est non vide, cf. `risk_overlay.compute_portfolio_vol_scalar`
         (surestime la vol d'une paire couverte spot long / perp short -> DÉ-RISQUE davantage,
         choix pessimiste explicite). NOTE D'IMPLÉMENTATION : `compute_portfolio_vol_scalar`
         applique déjà `abs(w)` en interne pour CHAQUE poids avant de l'agréger (cf. sa
         docstring) -- passer `raw_w` ou `raw_w.abs()` produit donc aujourd'hui EXACTEMENT le
         même résultat numérique. L'appel explicite à `.abs()` est conservé ici tel que prescrit
         par la spec §3.1 (contrat explicite, robuste à une éventuelle évolution future de
         `compute_portfolio_vol_scalar` qui cesserait d'être symétrique en signe), pas parce
         qu'il change quoi que ce soit aujourd'hui -- documenté pour qu'un audit ne le découvre
         pas comme un mystère.
      2. Exécution à l'open (spot inchangé). Perp : `target_shares = w * equity / open` (signé).
         Contrainte de faisabilité AVANT exécution (cf. spec §3.2) : `spot_dollars_cible +
         perp_initial_margin_frac * |notionnel_perp_cible| <= equity_avant_trade * (1+1e-9)` --
         violée -> `ValueError` immédiat (jamais de clipping silencieux). Cette contrainte n'est
         évaluée QUE si `perp_symbols` est non vide (le spot seul n'a jamais été contraint ainsi
         historiquement -- l'introduire inconditionnellement changerait le comportement légitime
         de candidates spot existantes qui somment leurs poids au-delà de 1, cf. rétro-compat).
      3. Coûts : `cost_bps` sur le turnover dollar spot, `perp_cost_bps` sur le turnover dollar
         perp -- déduits séparément, jamais confondus.
      4. Liquidation intra-bougie (perp uniquement) : prix adverse `high` pour un short, `low`
         pour un long, testée par rapport au cash de marge SEUL (jamais le spot comme
         collatéral) -- cf. spec §3.4 pour la formule exacte. Évènement journalisé dans
         `SegmentResult.liquidations`.
      5. Mise au marché à la clôture (perp) -- variation margin réglée en cash chaque bougie.
      6. Funding à la clôture (perp) -- short reçoit si `rate > 0`, paie si `rate < 0` (et
         l'inverse pour un long).
      7. Équity/exposition brute inchangées EN FORMULE (`shares.abs() * close`) mais désormais
         correctes AUTOMATIQUEMENT pour le perp puisque `shares` signé porte déjà |notionnel|
         via `.abs()` -- aucune branche perp séparée nécessaire ici.

    `SegmentResult.trades_closed`/`realized_events` accumulent les lignes PERP à côté des lignes
    spot (marquées `"leg": "perp"`) -- `n_trades_closed()`/`profit_factor()` les agrègent donc
    automatiquement (spec §4 : "pas de trade synthétique qui masquerait la contribution de
    chaque jambe"). Le PnL réalisé d'une ligne perp = Σ variation margin + Σ funding − coûts
    (− frais de liquidation le cas échéant) accumulés pendant sa vie -- PAS un `avg_cost` façon
    spot (qui n'a pas de sens pour un instrument marqué au marché chaque bougie), cf.
    commentaires du corps de fonction pour le détail exact de cette comptabilité par ligne.
    `SegmentResult.pnl_breakdown` publie la décomposition Spot/Perp/Funding/Coûts/Liquidation
    du PnL total du segment (toujours présente, à 0.0 sur les clés perp si `perp_symbols` est
    vide -- spec §4, "le rapport DOIT publier le PnL par jambe").

    --------------------------------------------------------------------------------------
    Extension portage inter-fenêtres (`backtest/CARRY-EXTENSION-SPEC.md`, pré-enregistrée --
    cette spec prime en cas de divergence avec cette docstring)
    --------------------------------------------------------------------------------------
    `carry_in` (défaut `None`) : reconstruit, AVANT la première bougie du segment, la position
    RÉELLEMENT détenue à la fin du segment précédent (`CarryState` produite par un appel
    antérieur de cette même fonction, cf. `SegmentResult.carry_out`) -- réplique l'exécution
    CONTINUE de production, modulo la renormalisation du capital à `initial_capital` par
    fenêtre (spec §3). `None` (défaut) : comportement historique bit-identique, aucune des
    branches de reconstruction ne s'exécute (spec §1.1).

    Contiguïté (spec §1.3/§3) VÉRIFIÉE, jamais supposée : `carry_in.last_idx + 1 != start_idx`
    lève `ValueError` immédiatement -- c'est au RUNNER de garantir cet invariant (démarrer à
    plat + journaliser si les fenêtres ne sont pas calendaires-adjacentes), le moteur ne fait
    JAMAIS silencieusement un flat sur un carry_in incohérent (ARCHITECTURE.md §0.2).

    Reconstruction (spec §3, pour chaque symbole `s` de `carry_in.weights_at_last_close` à
    poids non nul) : `shares_0[s] = weights_at_last_close[s] * initial_capital / last_close[s]`
    (signé pour perp), `cash_0 = initial_capital - Σ_spot shares_0 * last_close` (le perp ne
    déduit JAMAIS son notionnel du cash, cohérent avec la comptabilité perp existante). AUCUN
    coût d'entrée sur cette reconstruction (spec §3.2, ce n'est PAS un ordre) -- obtenu
    gratuitement en pré-amorçant `avg_cost`/`perp_ref` à `last_close` : la mécanique PAR BOUGIE
    existante (gap clôture->ouverture, bande, coûts sur `trade_shares` réel) s'applique alors
    SANS AUCUNE modification à la boucle principale, la position reconstruite n'étant jamais vue
    comme une "ouverture" (elle n'est jamais à 0 juste avant). Un prix `NaN` nécessaire à la
    reconstruction d'un symbole à poids non nul lève `ValueError` (spec §3.4, alignée politique
    perp F1 du 2026-08-31) ; un poids exactement `0.0` est simplement omis.

    Anti-gaming (spec §4) : une ligne qui traverse une frontière de fenêtre reste UNE SEULE
    ligne économique -- elle n'entre dans `trades_closed`/`n_trades_closed()` qu'à sa VRAIE
    sortie (jamais artificiellement à la frontière), avec un `pnl` total = somme NORMALISÉE de
    ses contributions à travers toutes les fenêtres traversées (`carried_windows`/`entry_ts`
    ajoutés en champs ADDITIFS sur cette entrée de `trades_closed`, uniquement si la ligne a
    réellement été portée au moins une fois -- jamais présents sinon, schéma historique
    inchangé). En fin de segment, toute ligne encore ouverte est publiée dans
    `SegmentResult.carry_out.open_lines` (jamais forcée à se fermer, comportement historique
    inchangé) -- c'est au RUNNER de choisir de rebrancher `carry_out` en `carry_in` de la
    fenêtre suivante ou non (le moteur n'a pas la notion de "dernière fenêtre du walk-forward")."""
    universe = list(weights_decided.columns)
    if start_idx <= 0:
        raise ValueError(
            "start_idx doit être >= 1 : la décision exécutée le premier jour du segment "
            "provient de weights_decided.iloc[start_idx-1] (warmup requis avant toute fenêtre)."
        )
    if end_idx < start_idx:
        raise ValueError("end_idx doit être >= start_idx")

    # --- Gardes défensives (audit adversarial 2026-08-03) --------------------------------
    # F3 : un NaN dans les poids décidés désactivait silencieusement le vol targeting de tout
    # le portefeuille (cf. risk_overlay.compute_portfolio_vol_scalar). Refus bruyant en amont,
    # une seule vérification vectorisée sur les lignes réellement consommées par ce segment.
    w_slice = weights_decided.iloc[start_idx - 1 : end_idx]
    if bool(w_slice.isna().any().any()):
        bad_cols = [c for c in w_slice.columns if bool(w_slice[c].isna().any())]
        raise ValueError(
            f"weights_decided contient des NaN (colonnes {bad_cols}) sur le segment demandé -- "
            "la stratégie doit produire des poids définis (0.0 = flat), jamais NaN (audit F3)."
        )
    # F2 : les défauts de vol targeting (halflife 2.5 « jours » = 2.5 LIGNES, sqrt(252)) ne
    # sont valides que sur des barres quotidiennes. Sur un calendrier intra-journalier ils
    # sous-estiment la vol d'un facteur ~12 (mesuré sur BTC horaire) et neutralisent le
    # dérisking. Refus explicite : l'appelant doit passer les constantes HOURLY_* de
    # risk_overlay pour des bougies horaires.
    if apply_vol_targeting and len(calendar) >= 3:
        _steps_s = np.diff(calendar.values).astype("timedelta64[s]").astype(float)
        _median_step_hours = float(np.median(_steps_s)) / 3600.0
        if _median_step_hours <= 2.0 and float(vol_periods_per_year) <= 1000.0:
            raise ValueError(
                f"Calendrier intra-journalier détecté (pas médian ≈ {_median_step_hours:.2f}h) "
                f"mais paramètres de vol targeting quotidiens (vol_periods_per_year="
                f"{vol_periods_per_year}, halflife={vol_ewma_halflife_days} lignes). Passer "
                "vol_ewma_halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS et "
                "vol_periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR (audit F2)."
            )
    # Règle inconditionnelle (spec §2) : un poids négatif sur une colonne NON perp n'est
    # JAMAIS valide (pas de short spot simulé par ce moteur) -- vérifié que `perp_symbols` soit
    # renseigné ou non, cf. docstring "règle inconditionnelle". N'affecte AUCUN appelant
    # existant (toutes les stratégies de production/backtest actuelles sont long-only, poids
    # >= 0 par construction) -- seul un poids invalide qui n'aurait jamais dû être produit se
    # met désormais à lever une erreur bruyante plutôt que d'être silencieusement exécuté.
    perp_set = set(perp_symbols) if perp_symbols else set()
    _spot_cols_for_check = [c for c in w_slice.columns if c not in perp_set]
    if _spot_cols_for_check:
        _neg_mask = w_slice[_spot_cols_for_check] < -1e-9
        if bool(_neg_mask.any().any()):
            _bad_cols = [c for c in _spot_cols_for_check if bool(_neg_mask[c].any())]
            raise ValueError(
                f"weights_decided contient des poids négatifs sur des colonnes NON perp "
                f"{_bad_cols} -- long-only strict sur le spot ; un short n'est autorisé QUE "
                "sur une colonne déclarée via perp_symbols (PERP-EXTENSION-SPEC.md §2)."
            )

    # --- Validation perp (spec §2) : funding/highs/lows obligatoires dès que perp_symbols est
    # non vide, colonnes perp présentes partout où elles sont nécessaires -- refus bruyant
    # immédiat, jamais un calcul partiel silencieux (principe pessimiste ARCHITECTURE.md §0.2).
    if perp_set:
        _missing_in_weights = [s for s in perp_set if s not in weights_decided.columns]
        if _missing_in_weights:
            raise ValueError(
                f"perp_symbols {_missing_in_weights} absent(s) des colonnes de weights_decided."
            )
        if funding is None or highs is None or lows is None:
            raise ValueError(
                "perp_symbols non vide exige funding, highs ET lows (spec §2) -- fournir les "
                "trois matrices alignées sur le même calendrier que opens/closes, cf. "
                "backtest/perp.py:build_aligned_perp_matrices."
            )
        for _label, _df in (("opens", opens), ("closes", closes), ("funding", funding), ("highs", highs), ("lows", lows)):
            _missing_cols = [s for s in perp_set if s not in _df.columns]
            if _missing_cols:
                raise ValueError(f"{_label} ne contient pas les colonnes perp {_missing_cols}.")

    shares = pd.Series(0.0, index=universe)
    avg_cost = pd.Series(0.0, index=universe)
    open_date: Dict[str, pd.Timestamp] = {}
    cash = float(initial_capital)
    cost_rate = float(cost_bps) / 10000.0

    # --- État dédié à la comptabilité perp (spec §3-4), inerte si perp_set est vide ----------
    perp_cost_rate = float(perp_cost_bps if perp_cost_bps is not None else cost_bps) / 10000.0
    perp_open_date: Dict[str, pd.Timestamp] = {}
    perp_pnl_accum: Dict[str, float] = {}  # PnL couru de la ligne perp OUVERTE par symbole
    perp_ref: Dict[str, float] = {}  # dernier prix de mise au marché par symbole (spec §3.4)
    liquidations: List[dict] = []
    spot_cols = [c for c in universe if c not in perp_set]
    pnl_breakdown = _empty_pnl_breakdown()
    # `trades_closed`/`realized_events` déclarés ICI (et non plus loin, juste avant la boucle
    # principale comme avant cette extension) : le garde MINEUR de reconstruction ci-dessous
    # (poids porté ~0 avec PnL accumulé substantiel -> clôture synthétique à la frontière) doit
    # pouvoir y écrire AVANT que la boucle principale ne démarre -- même listes, aucun changement
    # de sémantique pour le chemin historique (`carry_in=None` : ces deux lignes restent vides
    # jusqu'à la boucle principale exactement comme avant, seul leur point de DÉCLARATION bouge).
    trades_closed: List[dict] = []
    realized_events: List[dict] = []

    # --- État dédié au portage inter-fenêtres (CARRY-EXTENSION-SPEC.md §4), inerte si
    # `carry_in` est `None` -- ces dicts restent VIDES dans ce cas, aucune branche ci-dessous ni
    # dans la boucle principale n'a d'effet observable (spec §1.1, rétro-compat bit-à-bit).
    _spot_prior_pnl: Dict[str, float] = {}  # PnL net-de-coûts des fenêtres PRÉCÉDENTES (spot
    # uniquement -- le perp réutilise directement `perp_pnl_accum`, pré-amorcé ci-dessous, cf.
    # docstring : sa mécanique de fermeture/réduction partielle est DÉJÀ exactement celle dont
    # le portage a besoin, aucune duplication de logique n'est nécessaire côté perp).
    _carry_cost_accum: Dict[str, float] = {}  # coûts cumulés informatifs (spot ET perp), $ de
    # CETTE fenêtre + coût déjà porté des fenêtres précédentes -- monotone, jamais soustrait
    # d'un PnL (spec §2 : "coûts accumulés", champ séparé et purement informatif).
    _carry_windows_count: Dict[str, int] = {}  # nb de frontières déjà traversées AVANT cette
    # fenêtre (0 = ligne ouverte pour la première fois DANS cette fenêtre) -- n'est incrémenté
    # qu'au moment de la publication dans `carry_out.open_lines` en fin de segment, jamais ici.
    _carry_entry_ts: Dict[str, pd.Timestamp] = {}  # timestamp d'entrée D'ORIGINE (spot ET perp)

    if carry_in is not None:
        # Contiguïté défensive (spec §1.3/§3) : le RUNNER doit déjà garantir cet invariant AVANT
        # d'appeler cette fonction (démarrer à plat + journaliser sinon) -- le moteur ne suppose
        # JAMAIS un invariant fourni par l'appelant sans le revérifier lui-même
        # (ARCHITECTURE.md §0.2) : un carry_in non contigu est un bug de l'appelant, jamais une
        # dégradation silencieuse vers un flat.
        if int(carry_in.last_idx) + 1 != start_idx:
            raise ValueError(
                f"carry_in non contigu : carry_in.last_idx={carry_in.last_idx} + 1 != "
                f"start_idx={start_idx} (CARRY-EXTENSION-SPEC.md §1.3) -- le RUNNER doit "
                "démarrer à plat (carry_in=None) et journaliser l'évènement quand les fenêtres "
                "OOS ne sont pas calendaires-adjacentes, jamais appeler simulate_segment avec "
                "un état non contigu."
            )
        _lines_by_key = {(ln.symbol, ln.leg): ln for ln in carry_in.open_lines}
        _total_spot_dollars = 0.0
        for _sym, _w_last in carry_in.weights_at_last_close.items():
            _wf = float(_w_last)
            if abs(_wf) <= 1e-12:
                # Correctif audit (MINEUR) : un poids porté ~0 (`shares[s] > 1e-9` au packaging,
                # mais `weight = shares*price/equity` retombé sous 1e-12, cf. docstring
                # `CarryLine`) était jusqu'ici simplement OMIS -- la ligne `open_lines`
                # correspondante (et TOUT son PnL accumulé, potentiellement non nul) disparaissait
                # sans jamais alimenter `trades_closed`/`realized_events`. Choix retenu (le plus
                # simple et honnête, cf. rapport d'implémentation) : cette ligne est CLOSE ICI --
                # à la frontière, dans le segment qui reconstruit -- avec son PnL accumulé publié
                # normalement (mêmes champs additifs `carried_windows`/`entry_ts` que toute autre
                # ligne portée, spec §4.3), plutôt que de risquer un PnL qui s'évapore. Le poids
                # étant sous 1e-12, AUCUN ordre/coût n'est jamais généré pour cette clôture
                # (spec §3.2 : pas de coût d'entrée sur du portage -- ici c'est une clôture, pas
                # une entrée, et il n'y a de toute façon aucun trade réel à ce prix). Validation
                # (universe/NaN) volontairement PAS appliquée à cette branche (comportement
                # historique inchangé pour un poids réellement nul, cf.
                # `test_reconstruction_zero_weight_symbol_omitted_no_nan_check`) -- seule une
                # ligne connue de `carry_in.open_lines` (donc déjà validée à sa création) peut
                # être close ici, jamais une ligne inconnue.
                _leg0 = "perp" if _sym in perp_set else "spot"
                _line0 = _lines_by_key.get((_sym, _leg0))
                if _line0 is not None and (abs(_line0.pnl_accum) > 0.0 or abs(_line0.cost_accum) > 0.0):
                    _boundary_ts = calendar[int(carry_in.last_idx)]
                    _pnl0 = float(_line0.pnl_accum) * float(initial_capital)
                    _event0 = {"date": _boundary_ts, "symbol": _sym, "pnl": _pnl0, "closes_line": True}
                    _rec0 = {
                        "symbol": _sym,
                        "open_date": _line0.entry_ts,
                        "close_date": _boundary_ts,
                        "pnl": _pnl0,
                        "carried_windows": int(_line0.carried_windows),
                        "entry_ts": _line0.entry_ts,
                    }
                    if _leg0 == "perp":
                        _event0["leg"] = "perp"
                        _rec0["leg"] = "perp"
                    realized_events.append(_event0)
                    trades_closed.append(_rec0)
                continue  # spec §3.4 : poids porté exactement 0 -> jamais reconstruit.
            if _sym not in universe:
                raise ValueError(
                    f"carry_in porte un symbole {_sym!r} absent de l'univers de ce segment "
                    "(weights_decided.columns) -- l'univers doit rester stable d'une fenêtre à "
                    "l'autre pour que le portage ait un sens (CARRY-EXTENSION-SPEC.md §2)."
                )
            _is_perp_sym = _sym in perp_set
            _leg = "perp" if _is_perp_sym else "spot"
            if not _is_perp_sym and _wf < 0.0:
                raise ValueError(
                    f"carry_in porte un poids négatif ({_wf}) sur {_sym!r}, non déclaré perp "
                    "dans CE segment -- incohérence de configuration (leg spot/perp) entre "
                    "fenêtres portées, long-only strict sur le spot (PERP-EXTENSION-SPEC.md §2)."
                )
            _price = carry_in.last_close.get(_sym)
            if _price is None or pd.isna(_price):
                raise ValueError(
                    f"carry_in.last_close manquant/NaN pour {_sym!r} à poids porté non nul -- "
                    "jamais de reconstruction sur un prix manquant (spec §3.4, alignée politique "
                    "perp F1 du 2026-08-31 : jamais un prix 0/NaN silencieux)."
                )
            _price = float(_price)
            _shares0 = _wf * float(initial_capital) / _price
            shares[_sym] = _shares0
            _line = _lines_by_key.get((_sym, _leg))
            # PnL/coûts portés convertis de "fraction de L'ÉQUITY DE CLÔTURE de la fenêtre
            # PRÉCÉDENTE" (spec §3 préambule + correctif audit F1, cf. docstring `CarryLine` --
            # PAS "fraction de son `initial_capital`", c'était le bug F1) vers un montant $ de
            # CETTE fenêtre -- multiplication par LE `initial_capital` COURANT, symétrique de la
            # division par `_equity_final` (PAS `initial_capital`) appliquée à la publication de
            # `carry_out` ci-dessous. C'est EXACTEMENT le même facteur d'échelle
            # (`initial_capital_courant / equity_final_précédente`) que celui implicitement subi
            # par `shares_0` ci-dessus via `_wf` (qui est déjà `shares_final*last_close/
            # equity_final_précédente`, cf. publication `weights_at_last_close` plus bas) --
            # c'est cette COHÉRENCE des deux facteurs d'échelle qui rend le télescopage du PnL
            # porté exact (spec §6.3, `_frac = sold/old_sh` dans la boucle principale reste alors
            # rigoureusement exacte, renforcement post-reconstruction inclus).
            _prior_pnl = float(_line.pnl_accum) * float(initial_capital) if _line is not None else 0.0
            _prior_cost = float(_line.cost_accum) * float(initial_capital) if _line is not None else 0.0
            _entry_ts = _line.entry_ts if _line is not None else calendar[int(carry_in.last_idx)]
            _windows_count = int(_line.carried_windows) if _line is not None else 0
            _carry_cost_accum[_sym] = _prior_cost
            _carry_windows_count[_sym] = _windows_count
            _carry_entry_ts[_sym] = _entry_ts
            if _is_perp_sym:
                perp_open_date[_sym] = _entry_ts
                # Réutilise TEL QUEL le mécanisme perp existant (fermeture complète, réduction
                # partielle proportionnelle, changement de signe) -- `perp_pnl_accum` pré-amorcé
                # au PnL net déjà porté est tout ce qu'il faut, spec §4.2 tombe gratuitement.
                perp_pnl_accum[_sym] = _prior_pnl
                # `perp_ref` = dernière mise au marché connue = `last_close` PORTÉ (spec §3.1,
                # dernier point : "la dérive clôture->ouverture de la première bougie est vécue
                # par la position portée, comme en continu") -- la mécanique de gap existante
                # (`prior_ref`/mise au marché) s'en sert SANS aucune autre modification.
                perp_ref[_sym] = _price
            else:
                open_date[_sym] = _entry_ts
                # Base de coût = `last_close` PORTÉ : isole la contribution PROPRE à CETTE
                # fenêtre dans les calculs `avg_cost` existants (achat/vente), le PnL des
                # fenêtres précédentes étant suivi séparément dans `_spot_prior_pnl` et rajouté
                # explicitement à la fermeture/réduction de la ligne (cf. boucle principale).
                avg_cost[_sym] = _price
                _spot_prior_pnl[_sym] = _prior_pnl
                _total_spot_dollars += _shares0 * _price
        # Spec §3.1 : équity de départ = EXACTEMENT `initial_capital` ; le perp ne déduit JAMAIS
        # son notionnel du cash (marge testée au premier tour comme tout ordre, spec §3.2).
        cash = float(initial_capital) - _total_spot_dollars
    # Équity spot+cash de la borne PRÉCÉDENTE, pour isoler le "gap" de prix (clôture précédente
    # -> open de ce tour) sur le PnL spot -- même logique que le rattrapage perp ci-dessus,
    # nécessaire pour que `pnl_breakdown["spot_pnl"] + pnl_breakdown["perp_variation"] ≈ 0` sur
    # une paire parfaitement couverte (sinon `spot_pnl` sous-compterait silencieusement ce gap,
    # cf. `test_hedged_pair_delta_neutral_...`).
    prev_spot_equity = float(initial_capital)
    ruined = False  # ruine (équity <= 0) -- cf. garde post-audit 2026-08-31 dans la boucle

    n = end_idx - start_idx + 1
    equity = np.empty(n)
    exposure = np.empty(n)
    # `trades_closed`/`realized_events` déclarés plus haut (avant le bloc de reconstruction) --
    # cf. commentaire à leur déclaration.

    # Précalcul vectorisé (une seule fois pour tout le calendrier fourni, cf. docstring de
    # `risk_overlay.precompute_vol_stats`) -- désactivé si `apply_vol_targeting=False`.
    if apply_vol_targeting:
        vol_annual_full, valid_count_full = risk_overlay.precompute_vol_stats(
            closes, halflife_days=vol_ewma_halflife_days, periods_per_year=vol_periods_per_year
        )
    else:
        vol_annual_full = valid_count_full = None

    for j in range(n):
        i = start_idx + j
        date = calendar[i]
        open_price = opens.iloc[i].fillna(0.0)
        close_price = closes.iloc[i].fillna(0.0)
        if perp_set:
            # Correctif audit adversarial 2026-08-31 (CRITIQUE) : le `fillna(0.0)` historique est
            # inoffensif en long-only (prix -> 0 = pénalisant) mais devient un GAIN FICTIF pour un
            # short (démontré sur le trou réel de 69 h de SOL-PERP en février 2022 : +28,9 % de
            # capital en une bougie, ligne « close » à 0, liquidation impossible car NaN < x est
            # False). Règle : un prix perp manquant (open/close/high/low) à une bougie où le
            # symbole est EN POSITION ou a un POIDS CIBLE non nul => refus bruyant. Un symbole
            # flat sans poids cible peut traverser un trou de données sans conséquence (il ne
            # trade pas cette bougie -- ses NaN sont ramenés à 0 comme avant, sans effet).
            _perp_cols_list = list(perp_set)
            _raw_open_perp = opens.iloc[i][_perp_cols_list]
            _raw_close_perp = closes.iloc[i][_perp_cols_list]
            _nan_perp = (
                _raw_open_perp.isna()
                | _raw_close_perp.isna()
                | highs.iloc[i][_perp_cols_list].isna()
                | lows.iloc[i][_perp_cols_list].isna()
                | funding.iloc[i][_perp_cols_list].isna()
            )
            if bool(_nan_perp.any()):
                _raw_w_perp = weights_decided.iloc[i - 1][_perp_cols_list]
                _engaged = (shares[_perp_cols_list].abs() > 1e-9) | (_raw_w_perp.abs() > 1e-9)
                _bad = _nan_perp & _engaged
                if bool(_bad.any()):
                    raise ValueError(
                        f"Prix/funding perp manquant (NaN) à {date} pour "
                        f"{_bad[_bad].index.tolist()} alors que le symbole est en position ou a un "
                        "poids cible non nul -- jamais de trade, de mise au marché ni de liquidation "
                        "sur donnée manquante (ARCHITECTURE.md §0.2, audit 2026-08-31). Restreindre "
                        "le calendrier/l'univers ou mettre la candidate flat AVANT le trou."
                    )

        raw_w = weights_decided.iloc[i - 1]
        if apply_vol_targeting:
            # Spec §3.1 : vol targeting sur |w| dès qu'une jambe perp existe (surestime la vol
            # d'une paire couverte spot long / perp short -> dé-risque davantage, pessimisme
            # explicite). Note : `compute_portfolio_vol_scalar` applique déjà `abs(w)` en
            # interne pour chaque poids (cf. sa docstring) -- ce `.abs()` explicite ne change
            # donc RIEN au résultat aujourd'hui, il documente un contrat attendu par la spec de
            # façon robuste à une évolution future de cette fonction. Conditionné à `perp_set`
            # non vide pour ne RIEN changer au chemin historique (spot pur, même objet `raw_w`
            # passé tel quel qu'auparavant).
            vol_input_w = raw_w.abs() if perp_set else raw_w
            vol_scalar = risk_overlay.compute_portfolio_vol_scalar(
                vol_input_w,
                vol_annual_full.iloc[i - 1],
                valid_count_full.iloc[i - 1],
                target_vol_annualized=vol_target_annualized,
                coldstart_min_points=vol_coldstart_min_points,
                coldstart_scalar=vol_coldstart_scalar,
            )
            scaled_w = raw_w * vol_scalar
        else:
            scaled_w = raw_w

        # Même correction qu'à la clôture (spec §3.7, cf. plus bas) : le perp étant réglé en
        # cash chaque bougie, seul le spot contribue une "valeur de marché" à l'équity servant
        # de base au dimensionnement (`target_dollars` ci-dessous) -- `spot_cols == universe`
        # quand `perp_set` est vide, formule bit-identique au chemin historique dans ce cas.
        equity_before_trade = cash + float((shares[spot_cols] * open_price[spot_cols]).sum())
        safe_open = open_price.replace(0.0, np.nan)
        target_dollars = scaled_w * equity_before_trade
        target_shares = (target_dollars / safe_open).fillna(0.0)

        if no_trade_band and no_trade_band > 0:
            # Correctif audit 2026-08-03 (F1) : la bande compare le poids cible au poids
            # RÉELLEMENT PORTÉ ce matin (position dérivée marquée à l'open — `poids_actuel`
            # de bot/risk/manager.py étape 6). Symbole dans la bande -> AUCUN ordre : shares
            # strictement conservées (la position dérive), jamais reprojetées sur un poids
            # figé (l'ancienne implémentation générait un ordre à quasi chaque barre dès que
            # le prix bougeait, cf. docstring).
            if equity_before_trade > 0:
                current_w = (shares * open_price) / equity_before_trade
            else:
                current_w = pd.Series(0.0, index=universe)
            hold = (scaled_w - current_w).abs() < no_trade_band
            target_shares = target_shares.where(~hold, shares)

        # --- Contrainte de faisabilité de marge (spec §3.2), SEULEMENT si perp_set non vide --
        # jamais appliquée au spot pur (une candidate spot dont les poids somment > 1 n'a
        # jamais été bloquée par ce moteur -- l'introduire inconditionnellement serait un
        # changement de comportement du chemin historique, cf. docstring). Évaluée sur les
        # `target_shares` FINAUX (post bande) -- ce qui sera RÉELLEMENT exécuté cette bougie.
        if perp_set:
            final_target_dollars = target_shares * open_price
            spot_dollars_target = float(final_target_dollars[spot_cols].sum())
            perp_notional_target = float(final_target_dollars[list(perp_set)].abs().sum())
            required_capital = spot_dollars_target + perp_initial_margin_frac * perp_notional_target
            if required_capital > equity_before_trade * (1.0 + 1e-9):
                raise ValueError(
                    "Contrainte de faisabilité de marge violée à "
                    f"{date} : spot_dollars_cible={spot_dollars_target:.6f} + "
                    f"perp_initial_margin_frac*|notionnel_perp_cible|="
                    f"{perp_initial_margin_frac * perp_notional_target:.6f} = "
                    f"{required_capital:.6f} > equity_avant_trade={equity_before_trade:.6f} "
                    "(spec §3.2) -- la stratégie doit dimensionner correctement ses poids, "
                    "jamais de clipping silencieux."
                )

        trade_shares = target_shares - shares

        changed = trade_shares[trade_shares.abs() > 1e-9]
        # Turnover/coût SÉPARÉS spot vs perp (spec §3.3, "coûts par côté sur les DEUX jambes",
        # jamais confondus) -- `perp_changed`/`spot_changed` vides quand perp_set est vide,
        # auquel cas `spot_changed` == `changed` et le calcul ci-dessous reproduit EXACTEMENT
        # la formule historique (une seule variable `cost`, un seul `cost_rate`).
        if perp_set:
            is_perp_changed = changed.index.isin(perp_set)
            spot_changed = changed[~is_perp_changed]
            perp_changed = changed[is_perp_changed]
        else:
            spot_changed = changed
            perp_changed = changed.iloc[0:0]
        spot_turnover_dollars = float((spot_changed.abs() * open_price.reindex(spot_changed.index)).sum())
        spot_cost = spot_turnover_dollars * cost_rate
        perp_turnover_dollars = float((perp_changed.abs() * open_price.reindex(perp_changed.index)).sum())
        perp_cost = perp_turnover_dollars * perp_cost_rate
        cost = spot_cost + perp_cost  # nom historique conservé -- déduit du cash ci-dessous

        for sym, d_shares in changed.items():
            if sym in perp_set:
                continue  # comptabilité perp traitée séparément plus bas (spec §4)
            old_sh = float(shares[sym])
            new_sh = old_sh + float(d_shares)
            price = float(open_price[sym])
            # Le coût (cost_rate, cf. ci-dessus) est ATTRIBUÉ ici au niveau de CHAQUE ligne
            # (prix d'achat gross-up de `cost_rate`, prix de vente net de `cost_rate`) pour que
            # le PnL réalisé par trade -- et donc `profit_factor()` -- soit NET de coûts et
            # sensible à `cost_bps` (cf. test de stress de coûts, `backtest/run_xsmom_invvol.py`).
            # C'est une seconde vue (comptabilité "carnet de trades") du MÊME coût déjà déduit en
            # agrégat de `cash` ci-dessous pour la courbe d'équity -- pas un coût compté deux fois
            # sur l'équity, seulement reflété deux fois dans deux rapports différents (équity
            # globale vs PnL par ligne), pratique standard de reporting de trading.
            # Portage inter-fenêtres (CARRY-EXTENSION-SPEC.md §2, "coûts accumulés") : ce coût
            # ($ de CETTE fenêtre) s'ajoute au ledger informatif, monotone, indépendant de la
            # branche (ouverture/renforcement/réduction/fermeture) -- couvre TOUS les cas d'un
            # seul coup, jamais soustrait d'un PnL (décomposition purement informative).
            _carry_cost_accum[sym] = _carry_cost_accum.get(sym, 0.0) + abs(d_shares) * price * cost_rate
            if old_sh <= 1e-9 and new_sh > 1e-9:
                avg_cost[sym] = price * (1.0 + cost_rate)
                open_date[sym] = date
            elif old_sh > 1e-9 and new_sh <= 1e-9:
                sell_price_net = price * (1.0 - cost_rate)
                pnl = old_sh * (sell_price_net - avg_cost[sym])
                # Anti-gaming (spec §4.1/§4.2) : si cette ligne a été RECONSTRUITE depuis une
                # fenêtre précédente, son PnL total = somme de TOUTES ses contributions
                # (normalisées), ajoutée ICI en une fois car la ligne se ferme ENTIÈREMENT --
                # `_spot_prior_pnl` est vide (pop renvoie 0.0) pour une ligne jamais portée,
                # terme neutre, aucun changement pour le chemin historique.
                pnl += _spot_prior_pnl.pop(sym, 0.0)
                _carried_w = _carry_windows_count.pop(sym, 0)
                _entry_ts_orig = _carry_entry_ts.pop(sym, open_date.get(sym, date))
                realized_events.append({"date": date, "symbol": sym, "pnl": pnl, "closes_line": True})
                _trade_rec = {
                    "symbol": sym,
                    "open_date": open_date.get(sym, date),
                    "close_date": date,
                    "pnl": pnl,
                }
                if _carried_w > 0:
                    # Champs ADDITIFS (spec §4.3) -- UNIQUEMENT présents pour une ligne
                    # réellement portée au moins une fois, schéma historique inchangé sinon.
                    _trade_rec["carried_windows"] = _carried_w
                    _trade_rec["entry_ts"] = _entry_ts_orig
                trades_closed.append(_trade_rec)
                avg_cost[sym] = 0.0
                open_date.pop(sym, None)
                _carry_cost_accum.pop(sym, None)
            elif new_sh > old_sh > 1e-9:
                added = new_sh - old_sh
                buy_price_gross = price * (1.0 + cost_rate)
                avg_cost[sym] = (old_sh * avg_cost[sym] + added * buy_price_gross) / new_sh
            elif 0 < new_sh < old_sh:
                sold = old_sh - new_sh
                sell_price_net = price * (1.0 - cost_rate)
                pnl = sold * (sell_price_net - avg_cost[sym])
                # Réduction partielle d'une ligne PORTÉE (spec §4.2) : la part du PnL des
                # fenêtres précédentes attachée à cette ligne se réalise PROPORTIONNELLEMENT à
                # la fraction vendue (même convention que le spot natif : "les rebalances
                # partiels comptent proportionnellement"), le reste demeure attaché à la portion
                # encore ouverte. Terme neutre (`sym not in _spot_prior_pnl`) pour une ligne
                # jamais portée -- aucun changement pour le chemin historique.
                if sym in _spot_prior_pnl:
                    _frac = sold / old_sh
                    _prior = _spot_prior_pnl[sym]
                    pnl += _frac * _prior
                    _spot_prior_pnl[sym] = _prior * (1.0 - _frac)
                realized_events.append({"date": date, "symbol": sym, "pnl": pnl, "closes_line": False})

        # --- Comptabilité PAR LIGNE perp (spec §4) --------------------------------------------
        # PAS d'`avg_cost` façon spot : un perpétuel est marqué au marché chaque bougie (variation
        # margin + funding, cf. boucle "mise au marché" plus bas), il n'a pas de "prix de revient"
        # au sens spot. Le PnL réalisé d'une ligne = Σ(variation margin + funding − coûts − frais
        # de liquidation) accumulée PENDANT sa vie (`perp_pnl_accum`), réalisée :
        #   - en TOTALITÉ à la fermeture complète (ordre OU liquidation, cf. boucle suivante) ;
        #   - PROPORTIONNELLEMENT à la réduction, sur une réduction partielle (même convention
        #     que le spot : "les rebalances partiels comptent proportionnellement") ;
        #   - un changement de SIGNE est traité comme une fermeture de l'ancienne ligne SUIVIE
        #     d'une ouverture d'une nouvelle (spec §4, jamais un unique "trade" qui masquerait
        #     la sortie du risque précédent).
        # Le coût de CE trade (perp_cost_rate) est imputé à la ligne fermée/réduite (portion
        # concernée) ou à la ligne ouverte/renforcée (portion concernée) -- jamais aux deux à
        # la fois, jamais oublié (la somme des portions égale exactement `perp_cost` ci-dessus).
        for sym in perp_changed.index:
            d_shares = float(perp_changed[sym])
            old_sh = float(shares[sym])
            new_sh = old_sh + d_shares
            price = float(open_price[sym])
            # Portage inter-fenêtres (CARRY-EXTENSION-SPEC.md §2, "coûts accumulés") : ledger
            # informatif monotone, symétrique de l'incrément spot ci-dessus -- jamais soustrait
            # d'un PnL (déjà net de coût via `perp_pnl_accum`), purement pour audit/transparence.
            _carry_cost_accum[sym] = _carry_cost_accum.get(sym, 0.0) + abs(d_shares) * price * perp_cost_rate

            if abs(old_sh) > 1e-9:
                # Rattrapage "clôture précédente -> cet open" sur la position PRÉEXISTANTE,
                # AVANT tout traitement du trade (symétrique à `equity_before_trade`, qui
                # remarque le SPOT au nouvel open avant de recalculer sa cible). Sans cette
                # étape, dès qu'un trade perp a lieu, la variation entre le dernier close et
                # cet open sur les shares TENUES depuis la bougie précédente disparaissait
                # purement et simplement (jamais créditée ni débitée) -- cassant la
                # delta-neutralité d'une paire couverte spot/perp dès que la position perp est
                # réajustée (bug détecté par `test_hedged_pair_delta_neutral_...` en écriture
                # de ce module). `ref` = dernière mise au marché connue (close précédent, ou
                # open lui-même si la ligne vient tout juste d'être ouverte, auquel cas ce
                # terme est nul par construction : `perp_ref` n'existe pas encore pour elle).
                prior_ref = perp_ref.get(sym, price)
                gap_margin = old_sh * (price - prior_ref)
                cash += gap_margin
                perp_pnl_accum[sym] = perp_pnl_accum.get(sym, 0.0) + gap_margin
                pnl_breakdown["perp_variation"] += gap_margin

            if abs(old_sh) <= 1e-9 and abs(new_sh) > 1e-9:
                # Ouverture pure (ligne vide -> non vide).
                perp_open_date[sym] = date
                opening_cost = abs(new_sh) * price * perp_cost_rate
                perp_pnl_accum[sym] = -opening_cost
            elif abs(old_sh) > 1e-9 and abs(new_sh) <= 1e-9:
                # Fermeture complète PAR ORDRE (la fermeture par liquidation est gérée séparément
                # dans la boucle de mise au marché ci-dessous, jamais ici : à ce stade `new_sh`
                # est le résultat d'un ORDRE de la stratégie, pas d'un évènement de liquidation).
                closing_cost = abs(old_sh) * price * perp_cost_rate
                pnl = perp_pnl_accum.get(sym, 0.0) - closing_cost
                realized_events.append({"date": date, "symbol": sym, "pnl": pnl, "closes_line": True, "leg": "perp"})
                # Anti-gaming (spec §4.1/§4.3) : champs additifs UNIQUEMENT si cette ligne a
                # réellement été portée au moins une fois (`_carry_windows_count.pop` -> 0 pour
                # une ligne jamais portée, schéma historique inchangé dans ce cas).
                _carried_w = _carry_windows_count.pop(sym, 0)
                _entry_ts_orig = _carry_entry_ts.pop(sym, perp_open_date.get(sym, date))
                _carry_cost_accum.pop(sym, None)
                _trade_rec = {
                    "symbol": sym,
                    "open_date": perp_open_date.get(sym, date),
                    "close_date": date,
                    "pnl": pnl,
                    "leg": "perp",
                }
                if _carried_w > 0:
                    _trade_rec["carried_windows"] = _carried_w
                    _trade_rec["entry_ts"] = _entry_ts_orig
                trades_closed.append(_trade_rec)
                perp_pnl_accum[sym] = 0.0
                perp_open_date.pop(sym, None)
            elif old_sh * new_sh < 0.0:
                # Changement de signe : fermeture de l'ancienne ligne (au PnL accumulé jusqu'ici,
                # net du coût de clôture de la portion ancienne) PUIS ouverture immédiate d'une
                # nouvelle ligne (net du coût d'ouverture de la portion nouvelle).
                closing_cost = abs(old_sh) * price * perp_cost_rate
                pnl = perp_pnl_accum.get(sym, 0.0) - closing_cost
                realized_events.append({"date": date, "symbol": sym, "pnl": pnl, "closes_line": True, "leg": "perp"})
                # Anti-gaming (spec §4.1/§4.3), même logique que la fermeture par ordre ci-dessus
                # -- la NOUVELLE ligne ouverte juste après (sens opposé) repart intégralement à
                # zéro (`carried_windows`/`entry_ts`/coût informatif jamais réutilisés d'une
                # ligne économiquement différente, même si le symbole est identique).
                _carried_w = _carry_windows_count.pop(sym, 0)
                _entry_ts_orig = _carry_entry_ts.pop(sym, perp_open_date.get(sym, date))
                _carry_cost_accum.pop(sym, None)
                _trade_rec = {
                    "symbol": sym,
                    "open_date": perp_open_date.get(sym, date),
                    "close_date": date,
                    "pnl": pnl,
                    "leg": "perp",
                }
                if _carried_w > 0:
                    _trade_rec["carried_windows"] = _carried_w
                    _trade_rec["entry_ts"] = _entry_ts_orig
                trades_closed.append(_trade_rec)
                perp_open_date[sym] = date
                opening_cost = abs(new_sh) * price * perp_cost_rate
                perp_pnl_accum[sym] = -opening_cost
            elif abs(new_sh) > abs(old_sh):
                # Renforcement (même sens) : pas de réalisation, coût imputé à la ligne en cours.
                added_cost = abs(new_sh - old_sh) * price * perp_cost_rate
                perp_pnl_accum[sym] = perp_pnl_accum.get(sym, 0.0) - added_cost
            else:
                # Réduction partielle (même sens, |new| < |old|, new != 0) : réalisation
                # PROPORTIONNELLE de l'accumulé, coût de la réduction imputé à la seule part
                # réalisée (même logique que le "sell_price_net" du spot ci-dessus).
                frac = (abs(old_sh) - abs(new_sh)) / abs(old_sh)
                reduce_cost = abs(new_sh - old_sh) * price * perp_cost_rate
                prior_accum = perp_pnl_accum.get(sym, 0.0)
                realized_pnl = frac * prior_accum - reduce_cost
                realized_events.append({"date": date, "symbol": sym, "pnl": realized_pnl, "closes_line": False, "leg": "perp"})
                perp_pnl_accum[sym] = prior_accum * (1.0 - frac)

        # Coût spot déduit du cash comme avant (`trade_shares*open_price` limité aux colonnes
        # SPOT -- un perpétuel n'échange AUCUN cash à l'exécution, seule sa marge est vérifiée
        # en amont ; le spot seul, `perp_set` vide, reproduit EXACTEMENT `trade_shares*open_price`
        # sur tout l'univers comme avant, `spot_cols == universe` dans ce cas).
        cash = cash - float((trade_shares[spot_cols] * open_price[spot_cols]).sum()) - cost
        pnl_breakdown["costs_spot"] += spot_cost
        pnl_breakdown["costs_perp"] += perp_cost
        shares = target_shares

        # --- Liquidation intra-bougie / mise au marché / funding (spec §3.4-3.6, perp) --------
        # Exécutée pour TOUTE colonne perp en position (`shares[sym] != 0`), qu'un trade ait eu
        # lieu ou non cette bougie -- inerte (boucle sur ensemble vide) si `perp_set` est vide.
        # Lignes complètes extraites UNE SEULE FOIS (comme `open_price`/`close_price` plus haut)
        # plutôt que dans la boucle par symbole -- même style que le reste de la fonction.
        if perp_set:
            high_row = highs.iloc[i]
            low_row = lows.iloc[i]
            funding_row = funding.iloc[i]
        for sym in perp_set:
            sh = float(shares[sym])
            if abs(sh) <= 1e-9:
                continue
            traded_this_bar = sym in changed.index
            # ref = prix de la DERNIÈRE mise au marché (spec §3.4) : l'open d'exécution si un
            # ordre vient de s'exécuter cette bougie pour ce symbole (ouverture, renforcement,
            # réduction ou changement de signe -- dans tous les cas la position TENUE cette
            # bougie débute à l'open), sinon le close de la bougie précédente (position inchangée
            # depuis, déjà marquée au marché à ce prix la bougie d'avant).
            ref = float(open_price[sym]) if traded_this_bar else float(perp_ref.get(sym, open_price[sym]))
            close_i = float(close_price[sym])
            worst = float(high_row[sym]) if sh < 0 else float(low_row[sym])
            loss_at_worst = sh * (worst - ref)
            maint_threshold = perp_maintenance_margin_frac * abs(sh) * worst
            # Correctif audit adversarial 2026-08-31 (MAJEUR, spec §3.4 amendée) : le funding
            # réglé à la clôture de CETTE bougie entre dans le test de marge s'il est PAYABLE
            # par le bot (montant négatif) -- jamais s'il est favorable (un crédit à venir ne
            # sert pas de collatéral). Évalué au pire prix intra-bougie, cohérent avec `worst`.
            funding_rate_i = float(funding_row[sym])
            funding_payable_at_worst = min(0.0, -sh * worst * funding_rate_i)
            if (cash + loss_at_worst + funding_payable_at_worst) < maint_threshold:
                # Liquidation (spec §3.4) : jambe perp fermée AU PIRE PRIX intra-bougie, frais
                # punitifs en plus de perp_cost_bps, le SPOT n'est PAS touché (la stratégie
                # re-décide au tour suivant). Le cash de marge est le SEUL collatéral testé --
                # la valeur du spot n'entre jamais dans ce test (spec §3.4, dernier point).
                liq_fee = abs(sh * worst) * (perp_cost_rate + float(perp_liquidation_fee_bps) / 10000.0)
                # Correctif audit adversarial 2026-08-31 (MAJEUR) : la perte imputée est
                # PLAFONNÉE au cash de marge disponible (prix de faillite : au-delà, c'est le
                # fonds d'assurance de l'exchange qui absorbe -- le bot perd TOUT son cash, jamais
                # plus). Sans ce plafond, une équity négative inversait le signe économique des
                # rendements suivants dans `summarize_segment` (démontré : -9087 de capital sur
                # un choc synthétique). `bankrupt=True` journalise que le plafond a joué.
                # Contre-audit 2026-08-31 (MAJEUR résiduel) : le funding PAYABLE qui a contribué à
                # déclencher la liquidation est lui aussi DÉBITÉ (l'exchange règle le funding puis
                # réévalue la marge -- le bot n'échappe jamais au paiement qui a motivé sa propre
                # liquidation). Débit total = perte au pire prix + funding payable + frais, borné
                # au cash disponible ; le débit appliqué est ventilé frais d'abord, puis funding,
                # puis variation de prix (libellés du breakdown jamais trompeurs : aucune
                # composante ne peut afficher un « gain » sur une liquidation).
                total_debit = loss_at_worst + funding_payable_at_worst - liq_fee  # <= 0
                applied_debit = max(total_debit, -cash)  # borné au cash (prix de faillite)
                bankrupt = total_debit < -cash
                fee_applied = min(liq_fee, -applied_debit)
                funding_applied = -min(-funding_payable_at_worst, -applied_debit - fee_applied)
                loss_applied = applied_debit + fee_applied - funding_applied  # <= 0
                cash = cash + applied_debit
                pnl = perp_pnl_accum.get(sym, 0.0) + applied_debit
                realized_events.append({"date": date, "symbol": sym, "pnl": pnl, "closes_line": True, "leg": "perp"})
                # Anti-gaming (spec §4.1/§4.3) : la liquidation est une VRAIE sortie -- une ligne
                # portée qui se fait liquider compte comme SA sortie réelle (jamais gonflé par le
                # découpage en fenêtres), champs additifs mêmes règles que les fermetures ci-dessus.
                _carried_w = _carry_windows_count.pop(sym, 0)
                _entry_ts_orig = _carry_entry_ts.pop(sym, perp_open_date.get(sym, date))
                _carry_cost_accum.pop(sym, None)  # ligne fermée (liquidée) -- rien à porter
                _trade_rec = {
                    "symbol": sym,
                    "open_date": perp_open_date.get(sym, date),
                    "close_date": date,
                    "pnl": pnl,
                    "leg": "perp",
                }
                if _carried_w > 0:
                    _trade_rec["carried_windows"] = _carried_w
                    _trade_rec["entry_ts"] = _entry_ts_orig
                trades_closed.append(_trade_rec)
                liquidations.append(
                    {
                        "date": date,
                        "symbol": sym,
                        "side": "short" if sh < 0 else "long",
                        "shares_before": sh,
                        "worst_price": worst,
                        "ref_price": ref,
                        "loss": loss_at_worst,
                        "funding_payable": funding_payable_at_worst,
                        "fee": liq_fee,
                        "fee_applied": fee_applied,
                        "funding_applied": funding_applied,
                        "loss_applied": loss_applied,
                        "applied_debit": applied_debit,
                        "bankrupt": bankrupt,
                    }
                )
                perp_pnl_accum[sym] = 0.0
                perp_open_date.pop(sym, None)
                shares[sym] = 0.0
                pnl_breakdown["perp_variation"] += loss_applied
                pnl_breakdown["funding_received"] += funding_applied
                pnl_breakdown["liquidation_fees"] += fee_applied
                continue  # spec §3 : liquidation (4) avant mise au marché (5) et funding (6) --
                # la ligne est déjà fermée, aucune variation margin ni funding supplémentaire.

            # Pas de liquidation : mise au marché à la clôture (spec §3.5) puis funding (§3.6).
            var_margin = sh * (close_i - ref)
            cash += var_margin
            perp_pnl_accum[sym] = perp_pnl_accum.get(sym, 0.0) + var_margin
            pnl_breakdown["perp_variation"] += var_margin
            perp_ref[sym] = close_i

            funding_cash = -sh * close_i * funding_rate_i
            cash += funding_cash
            perp_pnl_accum[sym] = perp_pnl_accum.get(sym, 0.0) + funding_cash
            pnl_breakdown["funding_received"] += funding_cash

        # Équity (spec §3.7) : `cash + Σ shares_spot × close` UNIQUEMENT -- le perp est
        # INTÉGRALEMENT réglé en cash chaque bougie (variation margin + funding déjà versés
        # dans `cash` ci-dessus), son "notionnel" ne doit JAMAIS être ré-ajouté ici (ce serait
        # compter deux fois la même exposition -- une fois via le cash déjà mouvementé, une
        # fois via une "valeur de marché" fictive que ce moteur ne détient pas réellement).
        # `spot_cols == universe` quand `perp_set` est vide -> reproduit EXACTEMENT la formule
        # historique `shares * close_price` sur tout l'univers.
        equity[j] = cash + float((shares[spot_cols] * close_price[spot_cols]).sum())
        if perp_set and equity[j] <= 0.0:
            # Correctif audit adversarial 2026-08-31 (MAJEUR) : RUINE. Une équity <= 0 rend la
            # série de rendements sémantiquement invalide (signe inversé). Le compte est figé à
            # 0 pour le reste du segment (plus aucun trade possible : `target_dollars` = 0),
            # les rendements suivants valent 0 -- la ruine reste visible dans Sharpe/MaxDD/CAGR
            # (-100 %) sans jamais produire de chiffre absurde. Journalisée dans `liquidations`.
            if not ruined:
                ruined = True
                liquidations.append({"date": date, "symbol": "*", "side": "ruin", "shares_before": 0.0,
                                     "worst_price": float("nan"), "ref_price": float("nan"),
                                     "loss": float(equity[j]), "fee": 0.0, "loss_applied": float(equity[j]),
                                     "bankrupt": True})
            cash = 0.0
            shares[:] = 0.0
            perp_ref.clear()
            equity[j] = 0.0
        # Exposition brute (spec §3.7) : `(Σ spot_dollars + Σ|notionnel perp|) / equity` -- les
        # poids spot étant TOUJOURS >= 0 (long-only strict, vérifié en amont), `shares.abs()`
        # sur les colonnes spot égale déjà `shares` ; sur les colonnes perp, `shares.abs()`
        # donne exactement |notionnel perp|. Une seule expression vectorisée sur TOUT l'univers
        # reproduit donc la formule spec sans branche perp séparée, et reste bit-identique au
        # calcul historique quand `perp_set` est vide.
        exposure[j] = (
            float((shares.abs() * close_price).sum()) / equity[j] if equity[j] != 0 else float("nan")
        )
        # Spot PnL (spec §4, "publier le PnL par jambe") : DEUX composantes, symétriques au
        # rattrapage perp ci-dessus --
        #   (1) le "gap" clôture précédente -> open de ce tour sur les shares TENUES depuis
        #       avant (`equity_before_trade - prev_spot_equity` : ce gap est DÉJÀ implicitement
        #       réalisé dans la comptabilité cash historique du moteur, ce terme ne fait que le
        #       RENDRE VISIBLE dans le breakdown, sans toucher `cash`/`equity`) ;
        #   (2) la tenue de la position post-trade depuis l'open jusqu'à la clôture de cette
        #       bougie (`shares[spot_cols] * (close-open)`, déjà présent avant ce correctif).
        # Sans (1), `spot_pnl` sous-comptait silencieusement toute paire couverte spot/perp
        # dès que le poids spot est réajusté chaque bougie (cf. `test_hedged_pair_delta_
        # neutral_...` -- cette décomposition est PUREMENT informative, `equity`/`returns` ne
        # changent pas).
        pnl_breakdown["spot_pnl"] += equity_before_trade - prev_spot_equity
        pnl_breakdown["spot_pnl"] += float((shares[spot_cols] * (close_price[spot_cols] - open_price[spot_cols])).sum())
        prev_spot_equity = equity[j]

    dates = calendar[start_idx : end_idx + 1]
    equity_series = pd.Series(equity, index=dates)
    returns = equity_series.pct_change()
    returns.iloc[0] = equity[0] / float(initial_capital) - 1.0
    if perp_set and ruined:
        # Après la ruine, équity 0 -> 0 : pct_change donne NaN (0/0) -> rendement nul explicite.
        returns = returns.fillna(0.0)
    exposure_series = pd.Series(exposure, index=dates)

    # --- Publication de `carry_out` (CARRY-EXTENSION-SPEC.md §2) : TOUJOURS calculée (même
    # sans `carry_in`, spec §2), `None` uniquement si aucune position n'est ouverte en fin de
    # segment (ou après ruine, où `shares` est explicitement remis à 0 ci-dessus). N'affecte
    # JAMAIS `equity`/`returns`/`trades_closed` déjà construits -- purement une LECTURE de
    # l'état final `shares`/`avg_cost`/`perp_pnl_accum`/... pour packager l'état à porter.
    carry_out: Optional[CarryState] = None
    _final_close = closes.iloc[end_idx]
    _equity_final = float(equity[-1])
    if _equity_final > 0.0:
        _open_syms = [s for s in universe if abs(float(shares[s])) > 1e-9]
        if _open_syms:
            # Correctif audit F3 (MAJEUR) : un `close` final NaN (mark-to-zero historique,
            # trou de données) sur un symbole ENCORE EN POSITION empoisonnerait silencieusement
            # `CarryState` (poids/`last_close` NaN) -- détecté seulement une fenêtre plus tard,
            # au moment où `carry_in` échouerait sur `_price is None or pd.isna(_price)` (spec
            # §3.4) SANS pointer la bonne fenêtre. Symétrique de ce garde de reconstruction :
            # refus bruyant IMMÉDIAT, ICI, au packaging, avec la fenêtre fautive dans le message.
            _nan_close_syms = [s for s in _open_syms if pd.isna(_final_close[s])]
            if _nan_close_syms:
                raise ValueError(
                    f"Packaging de carry_out impossible : close final NaN à {dates[-1]} "
                    f"(fenêtre [{start_idx}, {end_idx}]) pour {_nan_close_syms} alors que "
                    "le symbole est encore en position en fin de segment -- jamais de "
                    "CarryState empoisonné (poids/last_close NaN) transmis silencieusement à la "
                    "fenêtre suivante (CARRY-EXTENSION-SPEC.md §3.4, symétrique du garde de "
                    "reconstruction 'carry_in.last_close manquant/NaN'). Restreindre le "
                    "calendrier/l'univers ou mettre la candidate flat AVANT le trou de données."
                )
            _weights_out: Dict[str, float] = {}
            _last_close_out: Dict[str, float] = {}
            _open_lines_out: List[CarryLine] = []
            for _s in _open_syms:
                _fc = float(_final_close[_s])
                _sh_final = float(shares[_s])
                _weights_out[_s] = _sh_final * _fc / _equity_final
                _last_close_out[_s] = _fc
                _is_p = _s in perp_set
                _leg2 = "perp" if _is_p else "spot"
                if _is_p:
                    # `perp_pnl_accum` porte déjà le net cumulé (fenêtres précédentes + celle-ci,
                    # cf. reconstruction ci-dessus qui le pré-amorce directement) -- rien à
                    # recalculer, spec §4.2 gratuit.
                    _pnl_dollars = float(perp_pnl_accum.get(_s, 0.0))
                    _entry_ts_s = perp_open_date.get(_s, dates[-1])
                else:
                    # PnL non réalisé de la portion encore ouverte (base de coût `avg_cost`,
                    # qui isole la contribution PROPRE à cette fenêtre) + PnL des fenêtres
                    # PRÉCÉDENTES pas encore réalisé (`_spot_prior_pnl`, 0.0 si jamais porté).
                    _pnl_dollars = _sh_final * (_fc - float(avg_cost[_s])) + _spot_prior_pnl.get(_s, 0.0)
                    _entry_ts_s = open_date.get(_s, dates[-1])
                _cost_dollars = float(_carry_cost_accum.get(_s, 0.0))
                # +1 : cette ligne s'apprête à traverser SA PROCHAINE frontière (celle qui suit
                # la fin de CE segment) -- jamais incrémenté à la reconstruction (spec §4.3,
                # cf. docstring `CarryLine`).
                _windows_out = int(_carry_windows_count.get(_s, 0)) + 1
                _open_lines_out.append(
                    CarryLine(
                        symbol=_s,
                        leg=_leg2,
                        line_id=f"{_s}:{_leg2}",
                        # Correctif audit F1 (CRITIQUE, cf. docstring `CarryLine`) : normalisé par
                        # `_equity_final` (PAS `initial_capital`, l'ancienne convention qui
                        # gonflait/dégonflait le profit factor porté dès que le segment ne rendait
                        # pas exactement 0%) -- MÊME base que `weights_at_last_close` ci-dessus
                        # (`_sh_final*_fc/_equity_final`), condition nécessaire et suffisante pour
                        # que la reconstruction du segment suivant applique le MÊME facteur
                        # d'échelle au PnL porté qu'aux `shares` reconstruites.
                        pnl_accum=_pnl_dollars / _equity_final,
                        cost_accum=_cost_dollars / _equity_final,
                        entry_ts=_entry_ts_s,
                        carried_windows=_windows_out,
                    )
                )
            carry_out = CarryState(
                last_idx=int(end_idx),
                weights_at_last_close=pd.Series(_weights_out),
                last_close=pd.Series(_last_close_out),
                open_lines=_open_lines_out,
            )

    return SegmentResult(
        dates=dates,
        equity=equity_series,
        returns=returns,
        gross_exposure=exposure_series,
        trades_closed=trades_closed,
        realized_events=realized_events,
        liquidations=liquidations,
        pnl_breakdown=pnl_breakdown,
        carry_out=carry_out,
    )


# ------------------------------------------------------------------------------------------
# Fenêtres walk-forward
# ------------------------------------------------------------------------------------------


@dataclass
class WalkForwardWindow:
    index: int
    is_start: pd.Timestamp
    is_end: pd.Timestamp
    oos_start: pd.Timestamp
    oos_end: pd.Timestamp
    is_start_idx: int
    is_end_idx: int
    oos_start_idx: int
    oos_end_idx: int


def generate_walk_forward_windows(
    calendar: pd.DatetimeIndex,
    is_months: int,
    oos_months: int,
    step_months: int,
) -> List[WalkForwardWindow]:
    """Fenêtres IS/OOS glissantes, calées sur des mois CIVILS (cohérent avec
    `docs/PROMOTION-RULES.md` §1.1 : "36 mois IS / 12 mois OOS"), mappées sur les jours de
    bourse RÉELS de `calendar`. Seules des fenêtres COMPLÈTES (IS et OOS entiers) sont retournées
    — jamais de fenêtre finale tronquée qui fausserait la comparaison entre fenêtres. IS et OOS
    ne se chevauchent jamais (OOS commence exactement au jour de bourse suivant la fin de l'IS)."""
    cal = pd.DatetimeIndex(calendar).sort_values()
    if len(cal) == 0:
        return []
    start0 = cal[0]
    cal_end = cal[-1]
    windows: List[WalkForwardWindow] = []
    k = 0
    while True:
        is_start_target = start0 + pd.DateOffset(months=step_months * k)
        oos_start_target = is_start_target + pd.DateOffset(months=is_months)
        oos_end_target = oos_start_target + pd.DateOffset(months=oos_months)
        if oos_end_target > cal_end:
            break
        is_start_pos = int(np.searchsorted(cal.values, is_start_target.to_datetime64(), side="left"))
        oos_start_pos = int(np.searchsorted(cal.values, oos_start_target.to_datetime64(), side="left"))
        oos_end_pos = int(np.searchsorted(cal.values, oos_end_target.to_datetime64(), side="left")) - 1
        if oos_start_pos >= len(cal) or oos_end_pos >= len(cal) or oos_end_pos < oos_start_pos:
            break
        is_end_pos = oos_start_pos - 1
        if is_end_pos < is_start_pos:
            break
        windows.append(
            WalkForwardWindow(
                index=k,
                is_start=cal[is_start_pos],
                is_end=cal[is_end_pos],
                oos_start=cal[oos_start_pos],
                oos_end=cal[oos_end_pos],
                is_start_idx=is_start_pos,
                is_end_idx=is_end_pos,
                oos_start_idx=oos_start_pos,
                oos_end_idx=oos_end_pos,
            )
        )
        k += 1
    return windows


# ------------------------------------------------------------------------------------------
# Sélection de paramètres IS-only (grille, optionnelle)
# ------------------------------------------------------------------------------------------


@dataclass
class ParamSelectionResult:
    chosen_params: dict
    is_sharpe: float
    all_candidates: List[dict]


def select_params_via_is(
    weights_provider: Callable[[dict], pd.DataFrame],
    calendar: pd.DatetimeIndex,
    opens: pd.DataFrame,
    closes: pd.DataFrame,
    cost_bps: float,
    is_start_idx: int,
    is_end_idx: int,
    param_grid: Sequence[dict],
    sim_kwargs: Optional[dict] = None,
) -> ParamSelectionResult:
    """Sélectionne, PARMI `param_grid`, la combinaison de paramètres maximisant le Sharpe mesuré
    sur la fenêtre IS UNIQUEMENT (`docs/PROMOTION-RULES.md` §1.1/§1.4) — jamais l'OOS. Si
    `param_grid` contient 0 ou 1 combinaison, AUCUNE sélection n'a lieu (cette fonction retourne
    directement l'unique combinaison, `is_sharpe=NaN` documentant explicitement "non applicable,
    zéro degré de liberté" plutôt qu'un chiffre qui laisserait croire à une sélection réelle).

    `sim_kwargs` (audit 2026-08-03, F2) : kwargs passés tels quels à `simulate_segment` (ex.
    paramètres de vol targeting HORAIRES pour un calendrier intra-journalier) — la sélection IS
    doit simuler avec EXACTEMENT la même surcouche de risque que l'évaluation OOS, jamais avec
    les défauts quotidiens si l'OOS utilise autre chose."""
    grid = list(param_grid) if param_grid else [{}]
    if len(grid) <= 1:
        params = grid[0]
        return ParamSelectionResult(params, float("nan"), [{"params": params, "is_sharpe": float("nan")}])

    candidates = []
    best_params: Optional[dict] = None
    best_sharpe = float("-inf")
    for params in grid:
        wdf = weights_provider(params)
        seg = simulate_segment(
            calendar, wdf, opens, closes, is_start_idx, is_end_idx, cost_bps, **(sim_kwargs or {})
        )
        sh = bt_metrics.sharpe_ratio(seg.returns)
        candidates.append({"params": params, "is_sharpe": sh})
        if not math.isnan(sh) and sh > best_sharpe:
            best_sharpe = sh
            best_params = params
    if best_params is None:
        best_params = grid[0]
        best_sharpe = float("nan")
    return ParamSelectionResult(best_params, best_sharpe, candidates)


# ------------------------------------------------------------------------------------------
# Concaténation OOS multi-fenêtres
# ------------------------------------------------------------------------------------------


@dataclass
class ConcatenatedOosResult:
    returns: pd.Series  # rendements quotidiens OOS concaténés, dans l'ordre des fenêtres
    equity_curve: pd.Series  # cumprod(1+returns), base 1.0 en tête
    trades_closed: List[dict]
    realized_events: List[dict]
    gross_exposure: pd.Series
    # --- Extension perp (PERP-EXTENSION-SPEC.md) : agrégation simple (concaténation des
    # évènements, somme terme à terme du breakdown) -- défauts vides/nuls, rétro-compatible.
    liquidations: List[dict] = field(default_factory=list)
    pnl_breakdown: dict = field(default_factory=_empty_pnl_breakdown)

    def n_liquidations(self) -> int:
        return len(self.liquidations)


def concatenate_segments(segments: Sequence[SegmentResult]) -> ConcatenatedOosResult:
    if not segments:
        return ConcatenatedOosResult(
            returns=pd.Series(dtype=float),
            equity_curve=pd.Series(dtype=float),
            trades_closed=[],
            realized_events=[],
            gross_exposure=pd.Series(dtype=float),
            liquidations=[],
            pnl_breakdown=_empty_pnl_breakdown(),
        )
    returns = pd.concat([s.returns for s in segments])
    equity_curve = (1.0 + returns).cumprod()
    equity_curve = pd.concat([pd.Series([1.0]), equity_curve])
    trades_closed = [t for s in segments for t in s.trades_closed]
    realized_events = [e for s in segments for e in s.realized_events]
    gross_exposure = pd.concat([s.gross_exposure for s in segments])
    # `getattr(..., [])`/`getattr(..., {})` : défense en profondeur si un appelant construit un
    # `SegmentResult` "à la main" sans les nouveaux champs (ex. test antérieur à cette extension
    # qui instancierait le dataclass directement plutôt que via `simulate_segment`) -- jamais
    # une AttributeError sur un champ qui, par défaut, est justement censé être vide.
    liquidations = [l for s in segments for l in getattr(s, "liquidations", [])]
    pnl_breakdown = _sum_pnl_breakdowns([getattr(s, "pnl_breakdown", {}) for s in segments])
    return ConcatenatedOosResult(
        returns=returns,
        equity_curve=equity_curve,
        trades_closed=trades_closed,
        realized_events=realized_events,
        gross_exposure=gross_exposure,
        liquidations=liquidations,
        pnl_breakdown=pnl_breakdown,
    )


def summarize_segment(seg: "SegmentResult | ConcatenatedOosResult") -> dict:
    """Bloc de métriques standard (backtest/metrics.py) appliqué à un segment (fenêtre unique)
    ou à un résultat concaténé multi-fenêtres — même fonction pour garantir que les métriques
    "par fenêtre" et "concaténées" sont calculées de façon strictement identique.

    Extension perp (PERP-EXTENSION-SPEC.md §4) : `n_liquidations` et `pnl_breakdown` sont
    TOUJOURS présents dans le dict retourné (valeurs nulles si `perp_symbols` n'a jamais été
    utilisé) -- un appelant qui ignore ces deux clés voit le reste du dict inchangé."""
    returns = seg.returns
    pnls = [e["pnl"] for e in seg.realized_events]
    equity = (1.0 + returns).cumprod()
    return {
        "sharpe": bt_metrics.sharpe_ratio(returns),
        "sortino": bt_metrics.sortino_ratio(returns),
        "profit_factor": bt_metrics.profit_factor(pnls),
        "max_drawdown": bt_metrics.max_drawdown(pd.concat([pd.Series([1.0]), equity])),
        "cagr": bt_metrics.cagr(pd.concat([pd.Series([1.0]), equity])),
        "average_exposure": bt_metrics.average_exposure(seg.gross_exposure),
        "n_trades_closed": len(seg.trades_closed),
        "n_days": len(returns),
        "n_liquidations": len(getattr(seg, "liquidations", [])),
        "pnl_breakdown": dict(getattr(seg, "pnl_breakdown", _empty_pnl_breakdown())),
    }
