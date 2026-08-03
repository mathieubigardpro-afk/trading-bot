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
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from backtest import metrics as bt_metrics
from backtest import risk_overlay

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

    def n_trades_closed(self) -> int:
        return len(self.trades_closed)


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
    `docs/RESEARCH-BACKLOG.md`."""
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

    shares = pd.Series(0.0, index=universe)
    avg_cost = pd.Series(0.0, index=universe)
    open_date: Dict[str, pd.Timestamp] = {}
    cash = float(initial_capital)
    cost_rate = float(cost_bps) / 10000.0

    n = end_idx - start_idx + 1
    equity = np.empty(n)
    exposure = np.empty(n)
    trades_closed: List[dict] = []
    realized_events: List[dict] = []

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

        raw_w = weights_decided.iloc[i - 1]
        if apply_vol_targeting:
            vol_scalar = risk_overlay.compute_portfolio_vol_scalar(
                raw_w,
                vol_annual_full.iloc[i - 1],
                valid_count_full.iloc[i - 1],
                target_vol_annualized=vol_target_annualized,
                coldstart_min_points=vol_coldstart_min_points,
                coldstart_scalar=vol_coldstart_scalar,
            )
            scaled_w = raw_w * vol_scalar
        else:
            scaled_w = raw_w

        equity_before_trade = cash + float((shares * open_price).sum())
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

        trade_shares = target_shares - shares

        changed = trade_shares[trade_shares.abs() > 1e-9]
        turnover_dollars = float((changed.abs() * open_price.reindex(changed.index)).sum())
        cost = turnover_dollars * cost_rate

        for sym, d_shares in changed.items():
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
            if old_sh <= 1e-9 and new_sh > 1e-9:
                avg_cost[sym] = price * (1.0 + cost_rate)
                open_date[sym] = date
            elif old_sh > 1e-9 and new_sh <= 1e-9:
                sell_price_net = price * (1.0 - cost_rate)
                pnl = old_sh * (sell_price_net - avg_cost[sym])
                realized_events.append({"date": date, "symbol": sym, "pnl": pnl, "closes_line": True})
                trades_closed.append(
                    {
                        "symbol": sym,
                        "open_date": open_date.get(sym, date),
                        "close_date": date,
                        "pnl": pnl,
                    }
                )
                avg_cost[sym] = 0.0
                open_date.pop(sym, None)
            elif new_sh > old_sh > 1e-9:
                added = new_sh - old_sh
                buy_price_gross = price * (1.0 + cost_rate)
                avg_cost[sym] = (old_sh * avg_cost[sym] + added * buy_price_gross) / new_sh
            elif 0 < new_sh < old_sh:
                sold = old_sh - new_sh
                sell_price_net = price * (1.0 - cost_rate)
                pnl = sold * (sell_price_net - avg_cost[sym])
                realized_events.append({"date": date, "symbol": sym, "pnl": pnl, "closes_line": False})

        cash = cash - float((trade_shares * open_price).sum()) - cost
        shares = target_shares

        equity[j] = cash + float((shares * close_price).sum())
        exposure[j] = (
            float((shares.abs() * close_price).sum()) / equity[j] if equity[j] != 0 else float("nan")
        )

    dates = calendar[start_idx : end_idx + 1]
    equity_series = pd.Series(equity, index=dates)
    returns = equity_series.pct_change()
    returns.iloc[0] = equity[0] / float(initial_capital) - 1.0
    exposure_series = pd.Series(exposure, index=dates)

    return SegmentResult(
        dates=dates,
        equity=equity_series,
        returns=returns,
        gross_exposure=exposure_series,
        trades_closed=trades_closed,
        realized_events=realized_events,
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


def concatenate_segments(segments: Sequence[SegmentResult]) -> ConcatenatedOosResult:
    if not segments:
        return ConcatenatedOosResult(
            returns=pd.Series(dtype=float),
            equity_curve=pd.Series(dtype=float),
            trades_closed=[],
            realized_events=[],
            gross_exposure=pd.Series(dtype=float),
        )
    returns = pd.concat([s.returns for s in segments])
    equity_curve = (1.0 + returns).cumprod()
    equity_curve = pd.concat([pd.Series([1.0]), equity_curve])
    trades_closed = [t for s in segments for t in s.trades_closed]
    realized_events = [e for s in segments for e in s.realized_events]
    gross_exposure = pd.concat([s.gross_exposure for s in segments])
    return ConcatenatedOosResult(
        returns=returns,
        equity_curve=equity_curve,
        trades_closed=trades_closed,
        realized_events=realized_events,
        gross_exposure=gross_exposure,
    )


def summarize_segment(seg: "SegmentResult | ConcatenatedOosResult") -> dict:
    """Bloc de métriques standard (backtest/metrics.py) appliqué à un segment (fenêtre unique)
    ou à un résultat concaténé multi-fenêtres — même fonction pour garantir que les métriques
    "par fenêtre" et "concaténées" sont calculées de façon strictement identique."""
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
    }
