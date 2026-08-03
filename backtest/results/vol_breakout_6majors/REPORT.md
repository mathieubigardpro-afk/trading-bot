# RAPPORT — `vol_breakout_6majors` (backlog P0#2) — ÉCHEC PORTE 1

*Session hebdomadaire #2, 2026-08-03. Spec pré-enregistrée et committée AVANT exécution
(`SPEC.md`, commit 59647d3). Moteur commun `backtest/engine.py` avec correctifs d'audit
F1/F2/F3 du jour. Contre-audit adversarial indépendant : `isSound: true` (reproduction
bit-à-bit du results.json depuis les données brutes, attaque de causalité sur données
réelles, recalcul indépendant du DSR/K_total/seuils).*

## Verdict : ÉCARTÉE — 2 seuils sur 5 de PROMOTION-RULES.md §1.2 manqués, marges larges

| Critère §1.2 | Seuil | Mesuré | Verdict |
|---|---|---|---|
| Sharpe OOS net (8760) | ≥ 0,70 | **0,434** | ❌ (−38%) |
| Profit factor OOS | > 1,15 | 1,209 | ✅ |
| Trades OOS clos | ≥ 80 | 302 | ✅ |
| MaxDD OOS relatif | ≤ 1,5× benchmark | 0,348× (23,3% vs 66,8%) | ✅ |
| DSR (K_total=66) | ≥ 0,50 | **0,058** | ❌ (−88%) |

## Protocole exécuté

- Univers 6 majors V1, bougies horaires 2022-01 → 2026-06 (39 407 h, 0 trou de données).
- Walk-forward 9m IS / 3m OOS / pas 3m → **14 fenêtres**, 30 671 heures OOS concaténées.
- Grille pré-enregistrée 4 combos (W∈{55,110} × P∈{0,20, 0,35}), sélection IS par Sharpe,
  sim_kwargs horaires (halflife 60 lignes, √8760) identiques IS/OOS.
- Coûts 25 bps/côté uniforme (palier "mids", pessimiste pour BTC/ETH/SOL).
- Overlay production par défaut : vol targeting 0,275 + bande de non-négociation 0,05.
- K_total = 10 (lignes du registre au jour du test) + 14×4 = **66** (§1.3).

## Résultats détaillés (OOS concaténé, net de coûts)

Candidate : Sharpe 0,434 ; Sortino 0,617 ; PF 1,209 ; MaxDD 23,3% ; CAGR 4,4% ;
exposition moyenne 8,3% ; 302 trades clos.
Benchmark (buy & hold équipondéré, aligné, sans coûts ni overlay) : Sharpe 0,653 ;
MaxDD 66,8% ; CAGR 23,6%.

La candidate est SOUS son benchmark en Sharpe (0,434 vs 0,653) — même famille d'échec que
`donchian_ensemble_6majors` (0,31 vs 0,55) et `ema_momentum_6majors` (0,24 vs 0,55) : sur cet
univers et cette période, les signaux techniques actifs ne battent pas la simple détention.

## Fragilités supplémentaires (chacune suffirait à douter, ensemble elles condamnent)

- **Stress de coûts** : PF 1,209 → 0,771 à 3× → 0,521 à 5×. Sous 1,0 dès 3× le coût nominal
  (le précédent xs_momentum_sp100 tenait 1,50 à 5×).
- **Instabilité temporelle** : Sharpe 2022-2023 = **−0,58** vs 2024-2026 = **+0,95**. Tout
  l'edge apparent vient de la seconde moitié — exactement le motif « ça a l'air de marcher
  récemment » contre lequel le walk-forward existe. Ne pas retester une variante sur la seule
  foi de la sous-période récente (§3.3 : compterait dans K_total).
- **Sélection IS instable** : W=110 choisi sur 12 fenêtres/14, mais P oscille (9× 0,20,
  3× 0,35) ; fenêtre 0 en fallback (IS Sharpe NaN — aucun trade IS).
- **DSR quasi nul** (0,058) : Sharpe horaire brut ≈ 0,0046 sur 30 671 obs. avec kurtosis
  excess 89 (70,5% de rendements exactement nuls — stratégie flat 92% du temps) — signal
  indiscernable du bruit une fois déflaté de 66 essais.

## Analyses d'honnêteté (spec)

- 299 épisodes de squeeze distincts pour 302 trades (~1:1) — l'échec ne vient PAS d'un
  manque d'indépendance des observations.
- Corrélation aux rendements du proxy quasi-passif du même panier : 0,45 (< seuil d'alerte
  0,7) — la diversification aurait été réelle si l'edge avait existé.
- Déviations d'implémentation : toutes tranchées en défaveur de la candidate (ddof=1,
  percentile inclusif causal, benchmark sans overlay), documentées dans results.json.

## Finding du contre-audit (cosmétique, sans impact décisionnel)

`engine.select_params_via_is` annualise le Sharpe IS d'affichage à √252 au lieu de √8760
(facteur d'échelle constant : ne change pas l'argmax de la sélection, aucun chiffre de
décision affecté). À corriger à l'occasion (champ informatif `is_sharpe` de results.json
sous-évalué ~5,9×).

## Effets

- Entrée n°11 au registre (`statut: "ecartee"`), K_total de la prochaine candidate = 11
  lignes de registre + sa grille interne.
- Aucune incubation. Labo toujours vide (0/3).
