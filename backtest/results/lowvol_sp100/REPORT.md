# Rapport de recherche — `lowvol_sp100` (Porte 1)

*Candidate : « low volatility » long-only sur le S&P100 (bottom-N par volatilité réalisée, équipondéré, rebalance mensuel). SPEC pré-enregistrée : `backtest/LOWVOL-SP100-SPEC.md` (session #11, commit 35df9b5, committée AVANT exécution). Moteur : `backtest/engine.py`. Signal : `backtest/strategies/lowvol.py`. Orchestration : `backtest/run_lowvol_sp100.py`. Résultats bruts : `backtest/results/lowvol_sp100/results.json`.*

**Ce document n'est PAS l'audit adversarial indépendant (`docs/PROMOTION-RULES.md` §1.4, verdict `isSound`) : c'est le backtest walk-forward lui-même (Porte 1, §1.1 à §1.3). Aucun paramètre n'a été ajusté après observation de l'OOS ; aucun re-run avec d'autres valeurs.**

---

## 0. Anomalies et ambiguïtés constatées à l'exécution (lire en premier)

- **A1_BK_absent_de_UNIVERSE_SP100** — La SPEC (§1) et la mission supposent BK (indisponible) à retirer de UNIVERSE_SP100 pour un univers de 102 titres. UNIVERSE_SP100 NE CONTIENT PAS de ticker 'BK' (103 titres, dont BKNG, tous 103 chargés avec succès) : l'exclusion est un no-op et l'univers effectif de ce run est de 103 titres, pas 102. Aucune substitution/ajout/retrait n'a été fait.
- **A2_interpretation_eligibilite_252_jours** — « ≥ 252 jours de bourse d'historique réel » est implémenté comme ≥ 252 RENDEMENTS quotidiens réels (253 clôtures) : seule lecture rendant l'éligibilité identique pour L=63 et L=252 (vérifié : eligibility_independent_of_L). Écart d'un jour d'historique pour un titre à son entrée dans l'univers, sans effet matériel attendu.
- **A3_bande_de_non_negociation_vs_poids_1_sur_N** — Avec no_trade_band=0,05 (SPEC §2, sémantique S1 du moteur) comparée PAR TITRE aux poids cibles : N=30 (1/30=0,033) et le contrôle (~1/100 = 0,01) sont SOUS la bande — le moteur ne passe JAMAIS d'ordre d'entrée depuis flat (hold = |cible-courant| < 0,05). Conséquences constatées : N=30 jamais investi (Sharpe IS NaN, jamais sélectionnable), CONTRÔLE APPARIÉ JAMAIS INVESTI (Sharpe/Sortino NaN, comparaison et domination NON évaluables). N=20 est exactement à la limite (1/20 = 0,05, ordre émis car inégalité stricte) ; mais les titres sortis du top-N dont le poids dérivé est < 0,05 ne sont jamais vendus (cible 0, |Δ| < 0,05 => hold) : des positions résiduelles s'accumulent et l'exposition brute dépasse 1 (levier implicite, cash négatif non financé). Corollaire : ces positions perdantes ne sont presque jamais RÉALISÉES (seules les lignes dont le poids dérivé est ≥ 0,05 sont vendues), ce qui gonfle mécaniquement le profit factor (gains réalisés / pertes rarement réalisées) et fausse le décompte de trades clos. Voir structural_diagnostics. AUCUN paramètre n'a été modifié : les chiffres officiels sont ceux de la SPEC à la lettre ; la SPEC n'avait pas anticipé ce mécanisme (cf. piège « poids sous 0,05 n'entre jamais », BAND-FIDELITY-SPEC §1.3).

Exposition brute moyenne mesurée : candidate **1,248** (max 1,798, 91,91 % des jours > 1,05) ; contrôle **0,000** (100,00 % des jours à exposition nulle). Nombre moyen de titres détenus (réplica S1) : 44,4 (max 62) pour une cible de 20. Fenêtres IS dont la combinaison avait un Sharpe NaN (jamais investie) : 60 couples (fenêtre, combo) sur 120.

---

## 1. Résumé exécutif (OOS concaténé)

| | Sharpe | Sortino | Profit factor | MaxDD | CAGR | Trades clos | Expo. moy. |
|---|---|---|---|---|---|---|---|
| **`lowvol_sp100` (combo walk-forward)** | 0,813 | 1,167 | 11,124 | 51,88 % | 14,95 % | 371 | 1,248 |
| Contrôle (éligibles équipondérés) | n/a | n/a | n/a | 0,00 % | 0,00 % | 0 | 0,000 |
| Benchmark SPY B&H | 0,607 | 0,863 | n/a | 54,85 % | 10,36 % | 0 | 1,000 |

*30 fenêtres walk-forward (36m IS / 12m OOS / pas 12m), OOS 1996-01-29 → 2026-01-28, univers effectif 103 titres, coûts 5 bps/côté (SPY 3), `apply_vol_targeting=False`, bande 0,05, portage inter-fenêtres actif.*

**Verdict automatique (SPEC §5) : `indetermine_controle_degenere_domination_non_evaluable`** — sous réserve de l'audit adversarial §1.4 (non exécuté) et de l'arbitrage `band_fragile`.

---

## 2. Seuils `docs/PROMOTION-RULES.md` §1.2 — S1 (sémantique du moteur)

| Critère | Seuil | Valeur | Verdict |
|---|---|---|---|
| Sharpe OOS net de coûts | ≥ 0,70 | **0,813** | PASS |
| Profit factor OOS | > 1,15 | **11,124** | PASS |
| Trades OOS clos | ≥ 80 | **371** | PASS |
| MaxDD ≤ 1,5× MaxDD SPY aligné | ratio ≤ 1,50 | **0,946** (51,88 % / 54,85 %) | PASS |
| DSR (K_total = 166) | ≥ 0,50 | **0,9567** | PASS |

`promotion_rules_1_2_all_pass : true`.

**DSR** : `K_total = 16 + 30×4 + 30×1 = 166` (lignes du registre à l'exécution : 16). Bailey & López de Prado 2014 sur 7549 rendements quotidiens OOS concaténés : Sharpe quotidien 0,05120, skew -0,093, kurtosis excess 11,373, SR0 = 0,03133, DSR = 0,9567. Rappel d'honnêteté (#12b) : à n ≈ 7 500 observations quotidiennes le seuil DSR ≥ 0,50 est peu discriminant — aucun argument « DSR élevé » ne compense un autre seuil manqué.

### Stress de coûts (combinaisons IS déjà choisies, portage actif)

| Coût/côté | Profit factor | Sharpe |
|---|---|---|
| 5 bps (nominal) | 11,124 | 0,813 |
| 15 bps (3×) | 10,607 | 0,806 |
| 25 bps (5×) | 10,561 | 0,808 |

---

## 3. Contrôle apparié et comparaison

Contrôle = univers éligible entier équipondéré, mêmes dates de rebalance. Sharpe n/a, Sortino n/a, MaxDD 0,00 % ; candidate : Sharpe 0,813, Sortino 1,167, MaxDD 51,88 % ; Information Ratio candidate vs contrôle 0,813.

**Le contrôle est DÉGÉNÉRÉ (jamais investi : poids ~0,01 sous la bande de 0,05, cf. anomalie A3).** La comparaison candidate/contrôle et la domination sont donc **NON ÉVALUABLES** (`candidate_dominated_by_control = null`) : cela ne vaut PAS non-domination. La sémantique SPEC §5 (« non dominée par le contrôle ») ne peut pas être appliquée mécaniquement ; la décision est renvoyée à l'orchestrateur.

---

## 4. Analyses d'honnêteté obligatoires (consignées, jamais critères de sauvetage)

### Sous-périodes (Sharpe OOS)

| Période | Candidate | Contrôle | SPY B&H | n jours |
|---|---|---|---|---|
| avant_2010-01-01 | 0,655 | n/a | 0,380 | 3507 |
| depuis_2010-01-01 | 0,988 | n/a | 0,862 | 4042 |
| depuis_2020-01-01 | 0,741 | n/a | 0,797 | 1526 |

### Corrélations (rendements quotidiens OOS)

Candidate/contrôle n/a ; candidate/SPY 0,895 ; contrôle/SPY n/a.

### Turnover mensuel moyen

Signal (poids cibles, fraction remplacée) : candidate **13,40 %** par mois (360 dates de décision OOS), contrôle 0,09 %. Réalisé (moteur, après bande, one-way) : candidate 6,06 % par mois, contrôle 0,00 %.

### Combinaisons sélectionnées en IS (nombre de fenêtres)

- `L=252, N=20` : 16 / 30
- `L=63, N=20` : 14 / 30

---

## 5. Annexe `band_fidelity` (BAND-FIDELITY-SPEC §3.2)

Run S2 apparié : même moteur, mêmes fenêtres, MÊME combinaison retenue par fenêtre que S1 (sélection IS non rejouée), poids décidés a·W (a=0.3), no_trade_band = 0,05·a = 0.0150, mêmes coûts, portage inter-fenêtres actif ; métriques reconverties en unités de poche par fenêtre via P = 1 + (E−1)/a puis concaténées. Hypothèse : reste du wallet en cash (borne documentée, BAND-FIDELITY-SPEC §4).

| Seuil §1.2 | S1 | S2 (unités de poche) | Bascule |
|---|---|---|---|
| Sharpe ≥ 0,70 | 0,813 (PASS) | 0,770 (PASS) | False |
| PF > 1,15 | 11,124 (PASS) | 141,126 (PASS) | False |
| Trades clos ≥ 80 | 371 (PASS) | 716 (PASS) | False |
| MaxDD/SPY ≤ 1,5 | 0,946 (PASS) | 1,047 (PASS) | False |
| DSR ≥ 0,50 | 0,9567 (PASS) | 0,9339 (PASS) | False |

`a = 0.3` ; `sharpe_s1 = 0,813` ; `sharpe_s2 = 0,770` (Δ = -0,042) ; **`band_fragile = false`**. Barres de décisions divergentes (sur 7549 barres OOS) : D1 mêmes états = **2631** (4941 cellules titre-barre sur 335634 actives), D2 chemins indépendants = 619 (1059 cellules) ; ordres S1 801 / S2 1468. Réplica vs moteur : écart d'équity max S1 1.3e-15, S2 4.4e-16 (`replica_matches_engine = true`).

Poids cible 1/N : N=20 donne exactement 0,05 = la bande (condition de hold stricte `|Δ|<bande` => ordre émis à la limite, en S1 comme en S2 puisque a·(1/20) et 0,05·a sont le même flottant) ; N=30 (0,033) et le contrôle (~0,01) sont SOUS la bande : aucun ordre d'entrée n'est jamais émis depuis flat (cf. structural_diagnostics).

### Bloc `cb` — exposition aux circuit breakers du wallet LABO

Seuils labo (`bot/config.py`, lecture seule) : half-size 15,0 %, flatten 25,0 %, gel d'entrées sur perte journalière > 3,0 %. MaxDD de poche S2 (référence) : 57,41 %.

| Lecture de l'équity wallet | MaxDD wallet | Jours > half-size | Épisodes half-size | Jours > flatten | Épisodes flatten | 1er flatten | Épisodes gel perte jour. |
|---|---|---|---|---|---|---|---|
| E = 1 + a·(P−1) (SPEC, P concaténée) | 45,98 % | 493 | 35 | 204 | 7 | 2008-10-09 | 56 |
| a ré-équilibré à chaque fenêtre | 20,72 % | 53 | 4 | 0 | 0 | — | 2 |

MaxDD wallet ≈ a × MaxDD poche ≈ 11-14 %, sous le seuil half-size labo (15 %) — À VÉRIFIER (SPEC §1). Constat : a × MaxDD poche = 15,56 % (S1) / 17,22 % (S2), soit AU-DESSUS du seuil half-size labo (15,0 %) : l'attente a priori de la SPEC (MaxDD de poche 35-45 %) est RÉFUTÉE (MaxDD de poche mesuré 51,88 % en S1). La première lecture (formule SPEC sur l'équity de poche concaténée) laisse la part de la poche dériver bien au-delà de a (le reste du wallet est figé en cash alors que la poche compose sur 30 ans) : elle SURESTIME l'exposition d'un wallet réel ; la seconde (a ré-équilibré chaque année) est plus proche d'un wallet vivant. Circuit breaker « pertes consécutives » (cooldown) non modélisé ; flatten production STICKY (une fois déclenché, revue manuelle) — n_episodes_flatten est un nombre de franchissements simulés, pas de flattens réels.

---

## 6. Lecture honnête des chiffres (ne remplace ni l'audit §1.4 ni l'arbitrage de l'orchestrateur)

- Les 5 seuils §1.2 sont tous passés en S1 et en S2, **mais le chemin d'exécution du moteur n'est pas celui d'un portefeuille « 20 titres équipondérés, long-only, non levier »** : exposition brute moyenne 1,248 (max 1,798), 44,4 titres détenus en moyenne (max 62) au lieu de 20.
- Le profit factor (11,12) est mécaniquement gonflé : 307 évènements de réalisation gagnants contre 64 perdants (Σ gains 2,971 vs Σ pertes -0,267) — une position dont le poids dérivé est tombé sous 0,05 n'est jamais vendue, donc ses pertes ne sont pas réalisées. Le seuil PF > 1,15 est franchi de très loin pour cette raison, pas par la robustesse de l'edge. Le décompte de trades clos est faussé dans le même sens.
- Le Sharpe est 0,813 contre 0,607 pour SPY, mais corrélation 0,895 avec SPY et MaxDD de poche 51,88 % (SPY 54,85 %) : fenêtres OOS à Sharpe négatif : #6 (2002, -0,56), #12 (2008, -0,48), #22 (2018, -0,36) (cf. §8).
- Sous-périodes : Sharpe 0,655 avant 2010, 0,988 depuis 2010, 0,741 depuis 2020 (SPY : 0,797 depuis 2020).
- Le contrôle apparié, adversaire obligatoire de la SPEC, n'est pas investi par le moteur : la question « la sélection low-vol bat-elle l'équipondéré large ? » reste **sans réponse** dans ce run. Aucune variante (bande 0, poids rééchelonnés, etc.) n'a été exécutée : la SPEC §5 impose qu'une variante soit un nouvel id avec un nouveau K_total.

---

## 7. Réserves méthodologiques (inchangées par ce run)

- **Biais du survivant** : constituants ACTUELS du S&P100 (réserve documentée par la SPEC).
- **Pas de taux sans risque** soustrait des ratios ; pas de financement du levier implicite.
- **Moteur** : pas de circuit breakers, caps ni vol targeting ; bande par TITRE (cf. A3).
- **Contrôle** : voir §3 — son rôle d'adversaire (« équipondéré large ») n'est pas rempli si le moteur ne l'investit jamais.
- **Audit adversarial §1.4** (points SIZING-FIDELITY §3.4 et BAND-FIDELITY §3.7) : à mener par une session distincte ; `isSound` non renseigné.

---

## 8. Walk-forward fenêtre par fenêtre (candidate)

| # | OOS | combo retenue (L, N) | Sharpe IS | Sharpe OOS | Trades clos |
|---|---|---|---|---|---|
| 0 | 1996-01-29 → 1997-01-28 | (63, 20) | 2,363 | 2,272 | 33 |
| 1 | 1997-01-29 → 1998-01-28 | (63, 20) | 2,553 | 2,013 | 29 |
| 2 | 1998-01-29 → 1999-01-28 | (63, 20) | 2,544 | 1,121 | 19 |
| 3 | 1999-01-29 → 2000-01-28 | (252, 20) | 1,687 | 0,237 | 8 |
| 4 | 2000-01-31 → 2001-01-26 | (63, 20) | 1,208 | 1,344 | 22 |
| 5 | 2001-01-29 → 2002-01-28 | (63, 20) | 0,957 | 0,091 | 13 |
| 6 | 2002-01-29 → 2003-01-28 | (63, 20) | 0,608 | -0,561 | 23 |
| 7 | 2003-01-29 → 2004-01-28 | (252, 20) | 0,332 | 1,934 | 10 |
| 8 | 2004-01-29 → 2005-01-28 | (252, 20) | 0,609 | 1,064 | 6 |
| 9 | 2005-01-31 → 2006-01-27 | (252, 20) | 0,618 | 0,821 | 5 |
| 10 | 2006-01-30 → 2007-01-26 | (63, 20) | 1,353 | 2,273 | 10 |
| 11 | 2007-01-29 → 2008-01-28 | (252, 20) | 1,395 | 0,194 | 8 |
| 12 | 2008-01-29 → 2009-01-28 | (252, 20) | 0,789 | -0,480 | 9 |
| 13 | 2009-01-29 → 2010-01-28 | (63, 20) | 0,015 | 0,974 | 16 |
| 14 | 2010-01-29 → 2011-01-28 | (252, 20) | 0,016 | 1,154 | 7 |
| 15 | 2011-01-31 → 2012-01-27 | (252, 20) | 0,298 | 0,954 | 3 |
| 16 | 2012-01-30 → 2013-01-28 | (252, 20) | 1,210 | 1,868 | 4 |
| 17 | 2013-01-29 → 2014-01-28 | (252, 20) | 1,480 | 1,708 | 4 |
| 18 | 2014-01-29 → 2015-01-28 | (63, 20) | 1,563 | 1,734 | 19 |
| 19 | 2015-01-29 → 2016-01-28 | (252, 20) | 1,725 | 0,021 | 3 |
| 20 | 2016-01-29 → 2017-01-27 | (252, 20) | 0,896 | 2,241 | 2 |
| 21 | 2017-01-30 → 2018-01-26 | (63, 20) | 1,033 | 3,701 | 20 |
| 22 | 2018-01-29 → 2019-01-28 | (63, 20) | 1,508 | -0,364 | 18 |
| 23 | 2019-01-29 → 2020-01-28 | (63, 20) | 1,098 | 2,364 | 17 |
| 24 | 2020-01-29 → 2021-01-28 | (252, 20) | 1,224 | 0,487 | 15 |
| 25 | 2021-01-29 → 2022-01-28 | (252, 20) | 0,496 | 1,823 | 10 |
| 26 | 2022-01-31 → 2023-01-27 | (252, 20) | 0,912 | 0,218 | 5 |
| 27 | 2023-01-30 → 2024-01-26 | (63, 20) | 0,647 | 0,806 | 13 |
| 28 | 2024-01-29 → 2025-01-28 | (252, 20) | 0,759 | 1,856 | 6 |
| 29 | 2025-01-29 → 2026-01-28 | (63, 20) | 0,926 | 0,815 | 14 |
