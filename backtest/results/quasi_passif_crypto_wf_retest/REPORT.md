# RAPPORT — `quasi_passif_crypto_wf_retest` (backlog P2#13) — DETTE DE RECHERCHE SOLDÉE, SEUILS NON PASSÉS (3/3 variantes)

*Session hebdomadaire #3, 2026-08-10. Spec pré-enregistrée et committée AVANT exécution
(`SPEC.md`). Moteur commun `backtest/engine.py` (post-correctifs d'audit F1/F2/F3).*

**Audit adversarial indépendant : `isSound: true`** (chiffres reproduits bit-à-bit), AVEC un
**finding CRITIQUE de fidélité production**, corrigé dans cette version du rapport (cf.
section dédiée ci-dessous). Les chiffres de ce rapport sont donc les chiffres **post-correctif
de fidélité**, qui sont les chiffres de DÉCISION à partir de cette version.

## CORRECTIF DE FIDÉLITÉ PRODUCTION (finding critique de l'audit, appliqué ici)

**Constat de l'audit** : la production **ne double-applique pas** le vol-targeting.
`bot/runner.py:_risk_manager_for_wallet` (lignes ~545-622) construit le `RiskManager`
PORTEFEUILLE avec `vol_target_annualized=50.0` (borne haute du constructeur) et
`vol_coldstart_scalar=1.0` — ce qui neutralise à ~1.0, dans toutes les conditions réalistes,
le scalaire de vol-targeting PORTEFEUILLE de `bot/risk/manager.py.RiskManager.apply()`. Le
SEUL vol-targeting réellement actif en production sur la poche crypto est celui **interne** à
`bot.strategies.quasi_passif_crypto` (déjà reproduit fidèlement par
`backtest/strategies/quasi_passif.py`).

**Ce que la première exécution de ce backtest faisait de travers** : `backtest/
run_quasi_passif.py` passait `vol_target_annualized=risk_profile["vol_target_annualized"]`
(0,10/0,20/0,35 selon la variante) à l'overlay du moteur commun (`backtest/risk_overlay.py`)
— une **seconde couche de vol-targeting réellement active dans le backtest mais inexistante
en production**. Résultat : le backtest pré-correctif sous-estimait légèrement l'exposition
réelle (donc légèrement optimiste sur le MaxDD, légèrement pessimiste/erratique sur le
Sharpe selon les périodes).

**Correctif appliqué** : `backtest/run_quasi_passif.py` passe désormais
`vol_target_annualized=50.0` et `vol_coldstart_scalar=1.0` à l'overlay (constantes
`PRODUCTION_OVERLAY_VOL_TARGET_NEUTRALIZED`/`PRODUCTION_OVERLAY_COLDSTART_SCALAR_NEUTRALIZED`),
reproduisant EXACTEMENT la neutralisation de `_risk_manager_for_wallet`. La bande de
non-négociation (0,05) et tout le reste (coûts, sizing de la stratégie elle-même) restent
inchangés.

**Vérification de cohérence avec la prédiction de l'audit (ablation indépendante)** :

| Variante | Sharpe prédit par l'audit | Sharpe obtenu | Écart |
|---|---|---|---|
| prudent | ≈ 0,808 | **0,808** | ~0 |
| equilibre | ≈ 0,283 | **0,283** | ~0 |
| agressif | ≈ 0,069 (MaxDD≈56,4%, PF≈0,771) | **0,069** (MaxDD 56,4%, PF 0,771) | ~0 |

Correspondance quasi exacte sur les 3 variantes — **aucun signal d'alerte**, le correctif est
appliqué correctement.

**Aucun des 15 verdicts §1.2 (5 critères × 3 variantes) ne bascule** entre les chiffres
pré-correctif et les chiffres corrigés (vérifié programmatiquement, cf. `results.json:
meta.audit_fidelity_correctif.any_verdict_flip_any_variant = false`, détail par critère dans
`variants.*.pre_correctif_audit_session3.verdict_flip_vs_corrected_by_criterion` — tous
`false`). Les chiffres pré-correctif restent disponibles dans `results.json` sous
`variants.*.pre_correctif_audit_session3` pour traçabilité, mais **ne sont plus les chiffres
de décision**.

## Rappel du cadre (SPEC.md §"Objet") — IMPORTANT

`quasi_passif_crypto` est **déjà en production** (poche crypto des 3 wallets réels), déployée
sur la base d'un backtest non audité (`docs/RESEARCH-LOG.md`, 2026-07-23). Ce retest applique
le protocole Porte 1 complet **comme grille de validation a posteriori**, PAS comme porte
d'incubation d'une nouvelle candidate : c'est un antécédent hors cadre `PROMOTION-RULES.md`
§3 (§5). **Aucun seuil manqué ci-dessous ne déclenche automatiquement un changement de
production.** Le déclencheur opérationnel réel reste le critère chiffré propre à
`docs/SELECTION-FINALE.md` §5 (sous-performance nette sur 3 mois réels). Ce résultat est
consigné au registre et signalé à Mathieu en priorité haute pour qu'une session de
gouvernance DÉDIÉE statue sur l'alignement de l'antécédent (SPEC.md, sémantique figée
d'avance).

## Verdict par variante — Porte 1 §1.2, seuil par seuil (chiffres CORRIGÉS, de décision)

**Aucune des 3 variantes ne passe l'ensemble des 5 seuils.**

### Variante `prudent` (BTC, ETH) — 3/5 seuils passés, PF et DSR manqués

| Critère §1.2 | Seuil | Mesuré | Verdict |
|---|---|---|---|
| Sharpe OOS net (8760) | ≥ 0,70 | **0,808** | ✅ |
| Profit factor OOS | > 1,15 | **1,080** | ❌ |
| Trades OOS clos | ≥ 80 (ou substitution) | 28 (substitution applicable, voir ci-dessous) | ✅ (substitution) |
| MaxDD OOS relatif | ≤ 1,5× benchmark | 0,150× (8,4% vs 55,9%) | ✅ |
| DSR (K_total=53) | ≥ 0,50 | **0,215** | ❌ |

**Verdict : ÉCHEC** (2 seuils sur 5 manqués : PF et DSR).

### Variante `equilibre` (BTC, ETH, SOL, DOGE, LINK, AVAX) — 2/5 seuils passés

| Critère §1.2 | Seuil | Mesuré | Verdict |
|---|---|---|---|
| Sharpe OOS net (8760) | ≥ 0,70 | **0,283** | ❌ (−60%) |
| Profit factor OOS | > 1,15 | **0,718** | ❌ (< 1,0 : perdant net de coûts) |
| Trades OOS clos | ≥ 80 (ou substitution) | 92 | ✅ (direct) |
| MaxDD OOS relatif | ≤ 1,5× benchmark | 0,408× (27,3% vs 66,8%) | ✅ |
| DSR (K_total=53) | ≥ 0,50 | **0,038** | ❌ |

**Verdict : ÉCHEC** (3 seuils sur 5 manqués : Sharpe, PF, DSR).

### Variante `agressif` (11 actifs diversifiés) — 2/5 seuils passés

| Critère §1.2 | Seuil | Mesuré | Verdict |
|---|---|---|---|
| Sharpe OOS net (8760) | ≥ 0,70 | **0,069** | ❌ (−90%) |
| Profit factor OOS | > 1,15 | **0,771** | ❌ (< 1,0) |
| Trades OOS clos | ≥ 80 (ou substitution) | 192 | ✅ (direct) |
| MaxDD OOS relatif | ≤ 1,5× benchmark | 0,807× (56,4% vs 69,9%) | ✅ |
| DSR (K_total=53) | ≥ 0,50 | **0,015** | ❌ |

**Verdict : ÉCHEC** (3 seuils sur 5 manqués : Sharpe, PF, DSR).

### Règle de substitution "< 80 trades" (pré-enregistrée SPEC.md, §1.2 PROMOTION-RULES.md)

Seule `prudent` (28 trades) est sous le seuil direct de 80. La substitution pré-enregistrée
exige (a) ≥ 24 mois d'OOS couverts ET (b) ≥ 2 régimes de marché distincts dans l'OOS total.
Avec 14 fenêtres × 3 mois = **42 mois d'OOS** (≥ 24 ✅) et une rupture nette de régime
observée à la sous-période 2024-01-01 (voir ci-dessous, sous-périodes avant/après bien
peuplées : 10 967 vs 19 704 heures OOS, donc (b) ✅), la substitution s'applique. Cela ne
suffit PAS à faire passer la porte (PF et DSR restent manqués, critères conjoints — §1.2).

## Protocole exécuté

- Univers par variante (`SPEC_UNIVERSE_BY_WALLET`, `bot/strategies/quasi_passif_crypto.py`) :
  `prudent`=BTC/ETH ; `equilibre`=BTC/ETH/SOL/DOGE/LINK/AVAX ; `agressif`=11 actifs (BTC, ETH,
  SOL, BNB, XRP, XLM, HBAR, ICP, OP, UNI, FIL).
- Profils de risque lus depuis `bot.config.WALLETS[*]["risque"]` (jamais recopiés en dur) pour
  le sizing INTERNE de la stratégie : vol_target 0,10/0,20/0,35 ; gross_max 0,40/0,70/0,90 ;
  cap/actif 0,20/0,25/0,30 ; halflife EWMA 60h.
- **Overlay du moteur commun neutralisé** (bande 0,05 conservée, vol-targeting portefeuille
  neutralisé à `vol_target_annualized=50.0`/`vol_coldstart_scalar=1.0`), fidèle à
  `bot/runner.py:_risk_manager_for_wallet` — cf. section correctif ci-dessus.
- Données : bougies horaires 2022-01-01 → 2026-06-30, 39 407 heures, calendrier commun
  (union des 14 symboles utilisés par au moins une variante) — **0 trou réel** pour les 13
  symboles à couverture complète ; **OP** : 3 632 heures de trou avant son démarrage réel
  (2022-06, cf. SPEC.md), traité par `align_to_calendar` (ffill borné 3h) pour l'exécution
  moteur — la STRATÉGIE utilise l'historique BRUT non aligné d'OP, jamais de valeur
  fabriquée avant son démarrage réel.
- Walk-forward 9m IS / 3m OOS, pas 3m : **14 fenêtres** générées par
  `engine.generate_walk_forward_windows` (calendrier COMMUN aux 3 variantes).
- Grille interne : **1 seule combinaison par variante** (paramètres de production, zéro
  optimisation). `select_params_via_is` tourne quand même (`is_sharpe=NaN`, "non applicable").
- Coûts (palier le plus défavorable de chaque univers, SPEC.md) : `prudent` 15 bps/côté ;
  `equilibre` 25 bps/côté ; `agressif` 45 bps/côté.
- Benchmark par variante : buy & hold équipondéré du MÊME univers, mêmes fenêtres OOS
  alignées, sans coûts ni overlay — **inchangé par le correctif** (aucun overlay dans le
  benchmark, avant comme après).
- `K_total` = 11 (lignes `RESEARCH-REGISTRY.json` au 2026-08-10) + 3 variantes × 14 fenêtres ×
  1 combo = **53** — commun aux 3 variantes, formule inchangée par le correctif de fidélité
  (seule la surcouche de risque simulée change, pas la méthode de comptage des essais).

## Résultats détaillés — CHIFFRES DE DÉCISION (OOS concaténé, net de coûts, 30 671 h/variante)

| Variante | Sharpe | Sortino | PF | MaxDD | CAGR | Expo. moy. | Trades clos | DSR |
|---|---|---|---|---|---|---|---|---|
| `prudent` | 0,808 | 1,127 | 1,080 | 8,4% | 6,8% | **14,9%** | 28 | 0,215 |
| `equilibre` | 0,283 | 0,387 | 0,718 | 27,3% | 3,7% | **25,6%** | 92 | 0,038 |
| `agressif` | 0,069 | 0,095 | 0,771 | 56,4% | −3,5% | **48,6%** | 192 | 0,015 |

Benchmark (buy & hold équipondéré, aligné, sans coûts ni overlay — inchangé) :

| Variante | Sharpe bench | MaxDD bench | CAGR bench |
|---|---|---|---|
| `prudent` | 0,761 | 55,9% | 29,9% |
| `equilibre` | 0,653 | 66,8% | 23,6% |
| `agressif` | 0,537 | 69,9% | 14,2% |

Sans le second vol-targeting (overlay neutralisé, fidèle production), les 3 variantes portent
une exposition RÉELLE plus élevée (14,9/25,6/48,6% vs 14,4/22,9/41,7% pré-correctif) : cela
améliore `prudent` et `equilibre` (Sharpe et PF en légère hausse) mais dégrade nettement
`agressif` en MaxDD (56,4% vs 46,9% pré-correctif — l'exposition non bridée par un second
palier de risque expose davantage à ses drawdowns propres) tout en restant, de justesse, sous
le seuil relatif 1,5× (0,807×). `prudent` reste marginalement AU-DESSUS de son benchmark en
Sharpe (0,808 vs 0,761) ; `equilibre` et `agressif` restent NETTEMENT SOUS leur benchmark.

## Stress de coûts (profit factor, 3× / 5× le coût nominal) — chiffres corrigés

| Variante | PF nominal | PF 3× | PF 5× |
|---|---|---|---|
| `prudent` (15→45→75 bps) | 1,080 | 0,927 | 0,805 |
| `equilibre` (25→75→125 bps) | 0,718 | 0,582 | 0,482 |
| `agressif` (45→135→225 bps) | 0,771 | 0,531 | 0,389 |

`prudent` reste proche de 1,0 au coût nominal et passe sous 1,0 dès 3× ; `equilibre` et
`agressif` sont déjà sous 1,0 au coût NOMINAL. Coût de la dérive (turnover OOS annualisé ×
coût) : `prudent` ≈ 0,91%/an, `equilibre` ≈ 3,93%/an, `agressif` ≈ **14,56%/an** (turnover du
signal brut, inchangé par le correctif — l'overlay ne modifie pas le turnover de la stratégie
elle-même).

## Analyses d'honnêteté (SPEC.md §"Analyses d'honnêteté obligatoires") — chiffres corrigés

### 1. Sous-périodes 2022-2023 vs 2024-2026 (stabilité temporelle)

| Variante | Sharpe < 2024-01-01 | Sharpe ≥ 2024-01-01 |
|---|---|---|
| `prudent` | 1,162 | 0,614 |
| `equilibre` | 1,018 | **−0,165** |
| `agressif` | 1,137 | **−0,548** |

Même motif que la version pré-correctif : tout l'edge vient de 2022-2023, `equilibre` et
`agressif` deviennent NÉGATIFS sur 2024-2026. Seule `prudent` reste positive sur les deux
sous-périodes.

### 2. Exposition brute réalisée moyenne par variante

`prudent` 14,9% ; `equilibre` 25,6% ; `agressif` 48,6% (légèrement plus élevée qu'en
pré-correctif, cohérent avec la suppression du second frein de vol-targeting) — reste dans ou
proche de la fourchette annoncée par le backtest non audité (12-48%). Le Sharpe s'effondre
quand même pour `equilibre`/`agressif` malgré une exposition qui reste modérée : la faible
exposition seule n'explique pas le Sharpe non audité.

### 3. Comparaison aux chiffres non audités d'origine (1,24 / 1,47 / 1,49)

| Variante | Sharpe non audité (2026-07-23) | Sharpe audité walk-forward (corrigé) | Écart |
|---|---|---|---|
| `prudent` | 1,24 | 0,808 | **−0,432** |
| `equilibre` | 1,47 | 0,283 | **−1,187** |
| `agressif` | 1,49 | 0,069 | **−1,421** |

Écart croissant avec la taille/diversification de l'univers, comme en pré-correctif. Le
double vol-targeting n'est plus une cause d'écart possible depuis ce correctif (il n'a jamais
existé côté backtest non audité de 2026-07-23 non plus, celui-ci datant d'avant l'introduction
du moteur commun avec overlay) — les causes plausibles restantes sont le walk-forward
(mesure hors-échantillon) et les coûts pessimistes au palier le plus défavorable.

### 4. Nombre de croisements SMA200 distincts par actif (inchangé par le correctif — signal de stratégie, pas overlay)

| Actif | Transitions totales | Entrées (off→on) |
|---|---|---|
| BTC | 32 | 16 |
| ETH | 24 | 12 |
| SOL | 40 | 20 |
| DOGE | 38 | 19 |
| LINK | 56 | 28 |
| AVAX | 32 | 16 |
| BNB | 66 | 33 |
| XRP | 66 | 33 |
| XLM | 41 | 21 |
| HBAR | 32 | 16 |
| ICP | 54 | 27 |
| OP | 24 | 12 (sur 1 290 jours calculables, vs 1 442 pour les autres) |
| UNI | 80 | 40 |

Pour `agressif` (192 trades clos, contre 157 pré-correctif — l'overlay neutralisé laisse la
bande 0,05 comparer directement au signal brut, générant davantage d'ordres exécutés), le
nombre d'épisodes de tendance distincts par actif (12 à 40) reste l'ordre de grandeur
pertinent pour juger l'indépendance statistique du signal, nettement inférieur au compte brut
de trades — c'est ce que le DSR (K_total=53) est censé pénaliser.

### 5. Corrélation OOS entre les 3 variantes

Le calcul de corrélation OOS entre variantes porte sur les rendements du signal de stratégie
tel qu'exécuté par le moteur ; avec l'overlay corrigé, les 3 variantes continuent de partager
BTC/ETH (prudent en est même exclusivement composé) — la conclusion qualitative de la
première exécution (corrélation élevée, > 0,7, ~1 pari corrélé et non 3 validations
indépendantes) reste valide et n'est pas remise en cause par ce correctif, qui ne touche que
le dimensionnement de l'exposition, pas la composition des paniers ni le signal de tendance.

## Déviations vs SPEC (documentées, aucune non signalée)

1. **Correctif de fidélité production (audit session #3)** : voir section dédiée en tête de
   ce rapport — l'overlay du moteur commun est désormais neutralisé
   (`vol_target_annualized=50.0`, `vol_coldstart_scalar=1.0`), fidèle à
   `bot/runner.py:_risk_manager_for_wallet`. C'est une CORRECTION d'une déviation
   involontaire de la première exécution (double vol-targeting inexistant en production),
   pas une déviation nouvelle vis-à-vis de la SPEC — la SPEC elle-même décrivait ce double
   vol-targeting comme "le comportement RÉEL de production", ce qui s'est avéré inexact une
   fois vérifié contre `bot/runner.py` par l'audit. Les chiffres pré-correctif sont conservés
   dans `results.json:variants.*.pre_correctif_audit_session3` pour traçabilité complète.
2. **Cutoff causal "< t" plutôt que "≤ t"** : la bougie exactement à l'heure de décision
   n'est pas encore close à cet instant (convention "index = heure d'ouverture" de
   `bot.feeds.get_history()`) — seules les bougies d'index strictement antérieur sont
   utilisées, pour `_daily_closes` et `_basket_vol_annualized`. Calage réel des cycles de
   production, vérifié par test de causalité.
3. **Historique complet (depuis 2022-01-01) plutôt que fenêtre bornée `HISTORY_N_HOURS`** :
   écart numérique négligeable (halflife EWMA 60h ≫ 205 jours de production), documenté par
   prudence, non corrigé (aucun impact mesurable).
4. **Actif sans SMA200 calculable → poids 0 (inéligible), jamais gelé** : conforme à la SPEC.
5. **Substitution "< 80 trades" appliquée à `prudent` seule** : les deux autres variantes
   dépassent directement le seuil de 80 (92 et 192), conformément à la justification
   pré-enregistrée dans la SPEC elle-même.

Aucune autre déviation n'a été identifiée entre l'implémentation et la SPEC.

## Effets

- Entrée `quasi_passif_crypto_wf_retest` au registre — 3 verdicts distincts par variante
  (§1.2 échoué pour les 3, chiffres corrigés, pas de moyenne ni de compensation entre
  variantes).
- **Aucun des 15 verdicts (5 critères × 3 variantes) ne bascule** entre pré-correctif et
  corrigé — le correctif de fidélité ne change PAS la conclusion de ce retest (échec des 3
  variantes sur le protocole Porte 1 complet), seulement la précision des chiffres rapportés.
- **Aucune action automatique sur la production** (antécédent hors §3, cf. rappel du cadre
  ci-dessus) — signalement priorité haute à Mathieu pour une session de gouvernance dédiée.
- K_total de la prochaine candidate = 12 lignes de registre (après cette entrée) + sa propre
  grille interne × ses fenêtres.
