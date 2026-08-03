# SPEC pré-enregistrée — `vol_breakout_6majors` (backlog P0#2)

*Session hebdomadaire #2, 2026-08-03. Ce document est committé AVANT toute exécution du
backtest (pré-enregistrement au sens de `docs/PROMOTION-RULES.md` §0) : la grille, les seuils
et toutes les conventions ci-dessous sont figés avant d'avoir vu le moindre résultat. Toute
déviation constatée entre cette spec et l'implémentation doit être traitée comme un finding
d'audit, pas comme une liberté d'implémentation.*

## Hypothèse (backlog P0#2)

Les expansions de range après compression de volatilité ("squeeze" de Bollinger) précèdent
des mouvements directionnels en crypto. Long-only, complémentaire du trend-following déjà
testé (entrée sur l'expansion, pas après l'établissement de la tendance).

## Univers, données, coûts

- Univers : `bot.config.SYMBOLS_CRYPTO` = BTC, ETH, SOL, DOGE, LINK, AVAX (6 majors V1 —
  même univers et même benchmark que `donchian_ensemble_6majors`/`ema_momentum_6majors`,
  convention du registre).
- Données : bougies HORAIRES de la branche `market-data` (2022-01-01 → 2026-06-30),
  colonnes `timestamp,open,high,low,close,volume`.
- Coûts : **25 bps/côté uniforme** = palier le plus défavorable présent dans l'univers
  (DOGE/LINK/AVAX sont "mids" : 15 fee + 10 slippage, cf. `bot.config.COST_TIER_*`) —
  volontairement PESSIMISTE pour BTC/ETH/SOL (palier majors réel : 15 bps/côté), ambiguïté
  tranchée en défaveur du bot (`ARCHITECTURE.md` §0.2). Stress de coûts §1.4 : 3× et 5×.

## Signal (long-only, par symbole, sur clôtures horaires)

- Bandes de Bollinger : fenêtre `W` heures, k = 2,0. `middle = SMA(close, W)`,
  `bandwidth = (upper − lower) / middle`.
- Squeeze actif à t : rank-percentile de `bandwidth(t)` sur fenêtre glissante de 2160 h
  (90 jours, aligné `REGIME_ATR_PERCENTILE_WINDOW_DAYS`) ≤ `P`.
- Entrée (poids 1/6) si les 3 conditions sont vraies à la clôture t :
  1. squeeze actif à t ou à au moins une des 24 heures précédentes ;
  2. `close(t) > upper_band(t)` (cassure haussière) ;
  3. filtre de régime : `close(t) > SMA(close, 4800 h)` (équivalent horaire du SMA 200 jours
     de production, `REGIME_SMA_DAYS`).
- Sortie (poids 0) : `close(t) < middle_band(t)`.
- Entre entrée et sortie, le poids reste 1/6. Jamais de NaN (0.0 = flat, y compris pendant
  le warm-up de 4800 h).

## Grille pré-enregistrée (4 combinaisons — AUCUNE autre valeur ne sera testée)

- `W ∈ {55, 110}` (heures) — repris des lookbacks Donchian/EMA déjà utilisés par le projet,
  pas optimisés pour cette idée.
- `P ∈ {0.20, 0.35}` — percentiles de compression a priori raisonnables, fixés avant tout test.
- Sélection IS par Sharpe via `engine.select_params_via_is` (jamais l'OOS), avec `sim_kwargs`
  horaires identiques à la simulation OOS.

## Walk-forward et moteur

- 9 mois IS / 3 mois OOS, pas 3 mois (`PROMOTION-RULES.md` §1.1, convention crypto horaire).
- Moteur commun `backtest/engine.py` tel quel (correctifs d'audit F1/F2/F3 du 2026-08-03
  inclus), overlay par défaut (vol targeting ON + bande 0,05) avec
  `vol_ewma_halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS` (60) et
  `vol_periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR` (8760).
- Métriques : équity OOS concaténée, `periods_per_year=8760` passé explicitement aux
  fonctions de `backtest/metrics.py`.

## Benchmark et seuils (Porte 1 §1.2 — tous obligatoires)

- Benchmark : buy & hold équipondéré des 6 majors, mêmes fenêtres OOS alignées, sans coûts
  ni overlay (convention vague 1 du registre).
- Sharpe OOS net ≥ 0,70 ; PF OOS > 1,15 ; trades OOS clos ≥ 80 ; MaxDD OOS ≤ 1,5× MaxDD OOS
  benchmark aligné ; DSR ≥ 0,50.
- DSR : `K_total = 10 (lignes du registre au 2026-08-03) + n_fenêtres × 4 combinaisons`
  (§1.3 — le nombre exact de fenêtres sera celui produit par le générateur, attendu ≈ 15).

## Analyses d'honnêteté obligatoires dans le rapport

- Nombre d'épisodes de squeeze DISTINCTS par symbole (risque n°1 du backlog : 80 trades sur
  peu d'épisodes indépendants ne sont pas 80 observations) ;
- Corrélation des rendements OOS de la candidate avec le quasi-passif crypto déployé (proxy :
  buy & hold vol-targeté du même univers) — si > 0,7-0,8, intérêt marginal faible même si
  standalone correct (backlog P0#2, risque n°2) ;
- Sous-périodes 2022-2023 vs 2024-2026 (dégradation temporelle).
