# backtest/ — moteur commun de backtest (gouvernance de la recherche)

Moteur vectorisé (pandas/numpy), imposé par `docs/PROMOTION-RULES.md` §1.1 : **toute**
candidate qui prétend passer la Porte 1 (labo) doit être backtestée avec CE moteur, jamais un
script ad hoc parallèle non versionné (c'est l'absence de ce module qui constituait le finding
critique de l'audit du 27/07 — la seule instance auparavant était un script externe hors dépôt,
non ré-exécutable ni auditable).

> **AVERTISSEMENT — toute modification de ce moteur doit être re-auditée adversarialement
> avant de juger une nouvelle candidate.** `engine.py`/`metrics.py`/`risk_overlay.py` sont la
> RÈGLE DU JEU commune à toutes les décisions de promotion (§1.1-§1.3) : un changement
> silencieux ici (formule de coût, fenêtres walk-forward, sizing, DSR...) peut faire basculer
> une décision passée OU future sans que personne ne s'en rende compte si le changement n'est
> pas explicitement revu par un audit indépendant (même principe que §1.4 pour les candidates
> elles-mêmes). Le correctif du 2026-07-27 (`risk_overlay.py`, ce commit) n'a PAS reçu cet audit
> séparé — budget de la session insuffisant, dette explicite consignée en tête de
> `docs/RESEARCH-BACKLOG.md` plutôt que passée sous silence.

## Fichiers

| Fichier | Rôle |
|---|---|
| `data.py` | Chargement CSV.gz par ticker (branche orpheline `market-data`), série brute (signal) vs alignée sur calendrier canonique SPY (simulation portefeuille), sans backfill. |
| `engine.py` | `simulate_segment()` : simulation vectorisée long-only, signal décidé à la clôture `t`, exécuté à l'**open** de `t+1`, coûts bps/côté sur le turnover dollar réel. `generate_walk_forward_windows()`, `select_params_via_is()` (sélection IS-only), `concatenate_segments()` (équity OOS unique), `summarize_segment()` (bloc de métriques standard). |
| `risk_overlay.py` | Surcouche de risque appliquée par défaut par `simulate_segment()` (correctif audit 2026-07-27) : bande de non-négociation + vol targeting, alignées `bot/risk/manager.py`. Voir §"Surcouche de risque" ci-dessous. |
| `metrics.py` | Sharpe, Sortino, profit factor, max drawdown (pic-à-creux), CAGR, ratio d'information, exposition moyenne, Deflated Sharpe Ratio / PSR (Bailey & López de Prado 2014). |
| `strategies/xsmom.py` | Version backtest vectorisée de `bot/strategies/xs_momentum_sp100.py` (constantes SPEC importées, jamais dupliquées). |
| `run_xsmom_invvol.py` | Script d'exécution de bout en bout (exemple complet, cf. `backtest/results/xs_momentum_invvol_sp100/`). |
| `tests/` | Preuves pytest : anti-look-ahead, coûts proportionnels au turnover, bornes walk-forward, cas limites DSR, bande de non-négociation par défaut, vol targeting. |

## Règles non négociables implémentées

1. **Aucun look-ahead** : `weights_decided.loc[t]` doit être calculable uniquement à partir de
   données de clôture `<= t` (responsabilité de la couche stratégie) — le moteur exécute
   TOUJOURS à `opens.iloc[i+1]`, jamais au prix ayant servi à la décision. Testé explicitement
   (`test_lookahead_cheat_collapses_...`).
2. **Long-only** : les poids négatifs ne sont pas gérés par ce moteur (cohérent avec les 4
   stratégies de production, toutes long/flat).
3. **Coûts systématiques sur turnover dollar réel** (`cost_bps`, points de base PAR CÔTÉ) —
   jamais de backtest "sans coûts" comme chiffre de décision (`docs/PROMOTION-RULES.md` §1.1).
4. **Walk-forward IS/OOS, sélection IS-only, métriques sur l'OOS concaténé** — jamais sur une
   fenêtre isolée ni sur la période complète non découpée (§1.1/§1.4).
5. **Surcouche de risque alignée production, activée par défaut** (voir section dédiée
   ci-dessous) — un moteur qui ignore le sizing réel de `bot/risk/` est structurellement PLUS
   généreux que ce qui se passerait en production.

## Surcouche de risque (`risk_overlay.py`, correctif audit 2026-07-27)

Avant ce correctif, `simulate_segment()` exécutait les poids décidés BRUTS. L'audit a mesuré
deux conséquences concrètes :

- **Bande de non-négociation à 0** : 16,3 fills exécutés par transition de signal *réellement
  voulue* (le moteur ré-exécutait un ordre à chaque micro-variation du poids recalculé) — environ
  0,30 point de Sharpe de pénalité artificielle en coûts de transaction qui ne se produirait
  jamais en production (`bot/risk/manager.py` absorbe ce bruit, étape 6 de son pipeline).
- **Absence de vol targeting** : le backtest simule un portefeuille structurellement plus gros
  (donc plus risqué) que celui réellement dimensionné en production — MaxDD sous-estimé d'un
  facteur ~2.

`simulate_segment()` applique désormais, **par défaut**, la même logique que
`bot/risk/manager.py` (vol targeting puis bande, dans cet ordre) :

```python
no_trade_band: float = risk_overlay.DEFAULT_NO_TRADE_BAND          # 0.05  (bot.config.NO_TRADE_BAND)
apply_vol_targeting: bool = True
vol_target_annualized: float = risk_overlay.DEFAULT_VOL_TARGET_ANNUALIZED   # 0.275
vol_ewma_halflife_days: float = risk_overlay.DEFAULT_VOL_EWMA_HALFLIFE_DAYS # 2.5j (60h de prod)
vol_coldstart_min_points: int = risk_overlay.DEFAULT_VOL_COLDSTART_MIN_POINTS  # 30
vol_coldstart_scalar: float = risk_overlay.DEFAULT_VOL_COLDSTART_SCALAR        # 0.5
```

`risk_overlay.py` **réutilise** `bot.risk.compute_vol_scalar` (jamais réimplémenté) — seule la
fréquence d'annualisation change (252 jours de bourse ici, contre 8760 heures en production ;
même demi-vie physique de 60h, ré-exprimée en 2,5 jours). Pour désactiver explicitement l'une
ou l'autre correction (ex. pour isoler une autre propriété du moteur en test, cf.
`backtest/tests/test_engine.py`) : `no_trade_band=0.0`, `apply_vol_targeting=False`.

**Écart connu, assumé, documenté** : cette surcouche ne réimplémente PAS `bot/risk/manager.py`
intégralement — pas de circuit breakers, pas de caps par actif, pas de bande par poche, pas de
cap d'exposition brute totale. Un backtest qui active un breaker de drawdown sévère en
production continuerait donc de trader normalement ici. Ligne de tête de
`docs/RESEARCH-BACKLOG.md` : re-audit demandé pour la prochaine session hebdomadaire.

## Convention temporelle interne

```
target_w[t]      = poids DÉCIDÉ à la clôture de calendar[t] (fourni par la stratégie)
poids_exécuté[t] = surcouche de risque(target_w[t-1], historique <= t-1)   (risk_overlay.py)
position[t]      = poids_exécuté[t] détenu pendant open[t] -> open[t+1]
ret_période[t]   = close[t] / open[t] - 1 (côté détention) ; exécution au prix open[t]
équity[t]        = cash[t] + shares[t] * close[t]
coût[t]          = |Δshares[t]| * open[t] * cost_bps / 10000
```

La toute dernière ligne du segment ne génère pas de rendement réalisé supplémentaire (pas de
`open[n+1]` disponible) — documenté, pas un bug.

## Exemple d'usage minimal

```python
import pandas as pd
from backtest import engine, metrics

calendar = pd.bdate_range("2015-01-05", periods=500)
opens = pd.DataFrame({"X": ...}, index=calendar)   # prix d'ouverture
closes = pd.DataFrame({"X": ...}, index=calendar)  # prix de clôture

# Poids DÉCIDÉS par la stratégie à la clôture de chaque jour (sans look-ahead : ne doit
# utiliser que closes.loc[:t]).
weights_decided = pd.DataFrame({"X": ...}, index=calendar)

seg = engine.simulate_segment(
    calendar, weights_decided, opens, closes,
    start_idx=1, end_idx=len(calendar) - 1,
    cost_bps=5.0,  # actions/ETF : 5 bps/côté ; crypto : cf. bot/config.py COST_TIER_*
    # no_trade_band / apply_vol_targeting / vol_target_annualized : défauts alignés production,
    # inutile de les préciser sauf pour désactiver explicitement une correction en test.
)

print(metrics.sharpe_ratio(seg.returns), metrics.max_drawdown(seg.equity))

# Walk-forward multi-fenêtres + DSR : cf. backtest/run_xsmom_invvol.py pour l'exemple complet
# (chargement data.py, generate_walk_forward_windows, concatenate_segments, deflated_sharpe_ratio
# avec K_total = lignes du registre + fenêtres × combinaisons de grille, docs/PROMOTION-RULES.md
# §1.3).
```

## Lancer les tests

```bash
cd trading-bot   # racine du dépôt
python3 -m pytest backtest/tests/ -v
```

Preuves couvertes : anti-look-ahead, coûts proportionnels au turnover réel, bornes des fenêtres
walk-forward (IS/OOS jamais chevauchantes), cas limites du DSR (K=1 ≈ PSR(0), K croissant ->
DSR décroissant), bande de non-négociation par défaut = 0,05 (micro-rebalance filtré,
rebalance réel exécuté), vol targeting qui réduit bien l'exposition moyenne et le MaxDD par
rapport à une exécution sans surcouche.

## Limites connues (assumées, pas des bugs)

- Long-only strict, pas de levier ni de vente à découvert.
- Pas de taux sans risque soustrait dans les ratios (argent fictif, aucun proxy documenté pour
  la poche actions de ce projet).
- La surcouche de risque (`risk_overlay.py`) ne couvre QUE bande + vol targeting (cf. section
  dédiée ci-dessus) — circuit breakers/caps/cap d'exposition brute totale non simulés.
- `data.py` ne connaît que des barres QUOTIDIENNES (actions/ETF) — pas encore d'équivalent
  horaire pour les stratégies crypto (`quasi_passif_crypto`), hors périmètre de ce correctif.
