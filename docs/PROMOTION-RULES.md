# PROMOTION-RULES.md — Gouvernance de la recherche (règles pré-enregistrées)

*Document de gouvernance. Écrit AVANT toute recherche menée dans le cadre de la boucle
d'auto-amélioration continue (wallet labo 🧪, cf. `bot/config.py:INCUBATING_STRATEGIES`,
commit 9ab6700). Complète `docs/ARCHITECTURE.md` (§0 principes non négociables) et
`docs/SELECTION-FINALE.md` (portefeuille de production actuel, issu de la 1ère vague de
recherche, AVANT l'existence de ce document — cf. `RESEARCH-LOG.md` pour la reconstitution
honnête de cet antécédent).*

---

## 0. Pourquoi ce document existe, et pourquoi il ne doit JAMAIS être modifié à la légère

**L'ennemi mortel de ce système est le sur-apprentissage.** Tester suffisamment de
combinaisons de stratégies/paramètres/univers fait *toujours* émerger de faux gagnants par
pur hasard — c'est une propriété mathématique de la recherche multiple (Bailey & López de
Prado, *The Deflated Sharpe Ratio*, 2014 ; `bt-final/xs-momentum-sp100/results.json` en
donne une illustration chiffrée directe sur ce projet, cf. §1.1).

La seule défense connue contre ce phénomène est le **pré-enregistrement** : figer les règles
de décision **avant** de regarder les résultats qu'elles vont juger. Une règle modifiable
après coup, en fonction du résultat qu'on regarde, n'est plus une règle — c'est une
rationalisation. En conséquence :

- Ce document est **gravé**. Toute modification d'un seuil chiffré ci-dessous doit :
  1. être proposée dans une session de recherche **distincte** de toute session qui teste
     une stratégie candidate (jamais "je change le seuil parce que ma stratégie du jour
     est juste en dessous") ;
  2. être justifiée par écrit dans `RESEARCH-LOG.md`, avec la raison de fond (pas de
     performance d'une candidate précise) ;
  3. être committée séparément, avec un message de commit qui référence explicitement ce
     document et la raison.
- **Aucun agent de recherche, aucune session incubant/évaluant une candidate ne peut
  modifier ce fichier dans le même commit qu'une action de promotion/rétrogradation/mort.**
  Gouvernance et jugement sont deux actes séparés, toujours.
- Le framework de risque et les circuit breakers du bot (`bot/risk/`, `bot/config.py`
  `CB_*`/`WALLETS[*]["risque"]`) sont **hors de portée de la boucle de recherche** — cf. §4.
  Aucune stratégie, aucune promotion, aucun "les backtests montrent qu'on pourrait
  desserrer le breaker" ne peut les modifier depuis ce processus. Seul un humain, hors du
  cadre recherche, peut les changer (et ce serait alors un changement d'ARCHITECTURE.md, pas
  de ce document).

---

## 1. PORTE 1 — Backtest → Labo

Une stratégie candidate ne peut entrer en incubation dans le wallet labo 🧪 que si **toutes**
les conditions suivantes sont vraies simultanément. Aucune exception, aucune "quasi-réussite
compensée par un autre critère" — un seul critère manqué bloque l'entrée.

### 1.1 Walk-forward obligatoire sur le moteur commun

- Backtest exécuté avec `backtest/engine.py` (le moteur commun du projet — jamais un
  script ad hoc parallèle), signal à la clôture t, exécution à l'open t+1 (ou logique
  équivalente déjà validée pour la classe d'actif concernée).
- Coûts du projet appliqués intégralement : `bot/config.py:COST_TIER_FEE_TAKER_BPS` /
  `COST_TIER_SLIPPAGE_PENALTY_BPS` pour la crypto (paliers majors/mids/smalls selon
  l'univers réel de la candidate), ou les coûts nominaux déjà retenus pour actions/ETF
  (5 bps/côté actions, 3 bps/côté ETF liquides — cf. `bt-final/xs-momentum-sp100/`,
  `bt-final/dual-momentum-etf/`). **Jamais de backtest "brut sans coûts"** comme chiffre
  de décision (rapport-recherche.md §4, ARCHITECTURE.md §0.2).
- Split walk-forward IS/OOS (jamais un split train/test unique) : fenêtres calibrées à la
  classe d'actif (référence : 9 mois IS / 3 mois OOS pour la crypto horaire, 36 mois IS /
  12 mois OOS pour actions/ETF — cohérent avec les 9 backtests déjà menés, cf.
  `RESEARCH-REGISTRY.json`). Toute fenêtre différente doit être justifiée par écrit dans
  la fiche de recherche (`RESEARCH-LOG.md`).

### 1.2 Seuils de performance OOS (chiffrés, tous obligatoires)

| Critère | Seuil | Mesuré sur |
|---|---|---|
| Sharpe OOS net de coûts | **≥ 0,70** | Équity OOS concaténée sur toutes les fenêtres walk-forward |
| Profit factor OOS | **> 1,15** | Trades OOS clos |
| Nombre de trades OOS | **≥ 80** (ou justification écrite explicite pour les stratégies structurellement lentes — ex. rebalance mensuel, cf. §1.3) | Trades OOS clos (`n_trades_closed`, pas `n_trades_total` qui inclut les positions encore ouvertes en fin de fenêtre) |
| MaxDD OOS | **≤ 1,5× le MaxDD OOS du benchmark de sa classe d'actif**, sur la même fenêtre alignée | `benchmark_*_OOS_ALIGNED` du backtest — jamais le benchmark full-period (biaisé, cf. `bt-final/*/results.json:*_FULL_PERIOD_context_only`) |
| Sharpe Déflaté (DSR) | **≥ 0,50** | Calculé sur le nombre TOTAL de combinaisons/stratégies **jamais testées dans `RESEARCH-REGISTRY.json`** à la date du test (voir §1.4) — pas seulement la grille interne de la candidate |

Précisions :
- "Benchmark de sa classe" = buy & hold équipondéré du même univers pour la crypto, SPY
  pour les actions individuelles/momentum actions, 60/40 SPY/IEF pour les stratégies
  multi-classes d'actifs — la convention déjà utilisée par les 9 backtests existants
  (`RESEARCH-REGISTRY.json`), pas une convention à réinventer par candidate.
- Le Sharpe OOS et le profit factor sont des critères **conjoints** avec le MaxDD relatif :
  une stratégie peut avoir un Sharpe correct et un MaxDD pourtant disqualifiant (cf.
  `donchian_ensemble_6majors` : Sharpe positif 0,31 mais sous-benchmark, MaxDD 58% —
  aurait aussi buté sur le critère MaxDD relatif ici si on l'appliquait strictement : 58,1%
  vs benchmark 72,1% OOS aligné → en fait CONFORME sur ce seul critère, ce qui illustre
  bien qu'aucun seuil unique ne suffit, d'où le ET logique entre tous les critères).
- "Slow strategies" (rebalance mensuel ou plus lent — actions momentum, dual-momentum ETF)
  : le seuil de 80 trades OOS peut ne pas être atteignable sur une fenêtre raisonnable
  (`dual_momentum_multiclasse_etf` n'atteint que 130 trades OOS sur **18 fenêtres de 12
  mois**, soit ~15 ans de données). Dans ce cas, la justification écrite doit a minima
  couvrir : (a) le nombre de cycles de rebalance OOS complets observés (≥ 24, soit 2 ans à
  cadence mensuelle, à défaut de compter des trades individuels), et (b) au moins 2 régimes
  de marché distincts (haussier + baissier/latéral) dans la fenêtre OOS totale.

### 1.3 Deflated Sharpe Ratio (DSR) — méthode de calcul imposée

Le DSR **doit** être corrigé du nombre total de combinaisons/stratégies jamais essayées
dans l'histoire du projet, pas seulement de la grille de paramètres interne à la candidate
du jour. Concrètement :

```
K_total = (nombre de lignes dans RESEARCH-REGISTRY.json à la date du test)
        + (nombre de combinaisons de la grille walk-forward interne à CETTE candidate)
```

C'est plus conservateur que la méthode `dsr_K216_conservative_all_windows_all_combos`
déjà utilisée pour `xs_momentum_sp100` (qui ne comptait que les 216 combinaisons
internes à cette stratégie, pas les 8 autres stratégies déjà testées avant elle) — écart
assumé et volontaire : au moment de ce test, il n'y avait pas encore de registre commun
inter-stratégies. **Toute nouvelle candidate, à partir de ce document, doit utiliser
`K_total` tel que défini ci-dessus.** Documenter `K_total` et le DSR obtenu dans la fiche
de recherche et dans `RESEARCH-REGISTRY.json`.

### 1.4 Audit adversarial obligatoire

Avant toute entrée en incubation, une revue adversariale **indépendante de la session qui a
produit le backtest** doit rendre un verdict explicite `isSound: true|false` portant au
minimum sur :
- absence de look-ahead bias (signal à H utilise uniquement des données clôturées ≤ H-1) ;
- absence de biais de sélection de paramètres non déflaté (le DSR de §1.3 couvre le
  chiffre, l'audit vérifie que la méthode de calcul est correctement appliquée) ;
- sensibilité aux coûts (test de stress : que devient le profit factor à 3-5× le coût
  nominal retenu ? cf. `bt-final/xs-momentum-sp100/results.json:cost_sensitivity_stress_test`
  pour le précédent déjà appliqué — profit factor tombant sous 1,0 à 25 bps/côté) ;
- biais du survivant (univers testé = constituants **actuels**, jamais point-in-time,
  documenté explicitement comme réserve, pas caché) ;
- cohérence de la logique de sizing avec le `RiskManager` réel du bot (pas un sizing
  fictif plus généreux que ce que `bot/risk/` appliquerait en production) ;
- absence de retouche des paramètres après avoir vu les résultats OOS (paramètres
  sélectionnés uniquement par la grille walk-forward IS, jamais ajustés a posteriori en
  observant l'OOS — sous peine de transformer l'OOS en IS déguisé).

`isSound: false` = rejet automatique, indépendamment des seuils chiffrés du §1.2, même
s'ils sont tous atteints. Le verdict et sa justification sont consignés dans
`RESEARCH-LOG.md` et dans le champ `raison` de `RESEARCH-REGISTRY.json`.

### 1.5 Passage effectif en incubation

Si et seulement si §1.1 à §1.4 sont tous satisfaits :
1. Ajouter une entrée à `RESEARCH-REGISTRY.json` (statut `"en_incubation"`).
2. Ajouter une entrée à `bot/config.py:INCUBATING_STRATEGIES` avec params **gelés**
   (jamais réoptimisés en cours d'incubation, cf. bandeau existant du fichier),
   `entered_at`/`entry_run_id` renseignés au premier cycle réel où la candidate trade.
3. Respecter la limite structurelle §4.1 (max 3 candidates simultanées).
4. Journaliser l'entrée dans `RESEARCH-LOG.md`.

---

## 2. PORTE 2 — Labo → Wallets réels

Une candidate en incubation ne peut être promue vers un ou plusieurs des 3 wallets réels
(🛡️ prudent / ⚖️ équilibré / 🔥 agressif) que si **toutes** les conditions suivantes sont
vraies simultanément, mesurées sur sa performance **vécue** (pas son backtest).

### 2.1 Durée et volume d'observation minimum

- **≥ 28 jours civils** d'incubation continue depuis `entered_at` (pas de fenêtre
  discontinue, pas de redémarrage du compteur pour "repartir sur une meilleure série" —
  toute interruption liée à un incident technique du bot lui-même, pas à la candidate,
  peut être documentée et compensée, mais jamais silencieusement).
- **ET ≥ 20 trades vécus** (crypto/stratégies à cadence horaire ou quotidienne) **OU ≥ 2
  cycles de rebalance complets** pour les stratégies mensuelles (actions momentum,
  dual-momentum ETF) — le critère le plus adapté à la fréquence de décision de la
  candidate s'applique, jamais les deux cumulés.

### 2.2 Cohérence performance vécue vs backtest

- Sharpe vécu (fenêtre d'incubation complète) **≥ 50% du Sharpe OOS annoncé** au moment de
  l'entrée en incubation (le Sharpe qui a servi à passer la Porte 1, cf. §1.2 et
  `RESEARCH-REGISTRY.json`).
- Aucun drawdown vécu **> 1,5× le drawdown OOS attendu** (même référence que ci-dessus).
- Ces deux seuils sont volontairement **moins stricts** que les seuils d'entrée (Porte 1)
  — la période d'incubation est courte (28 jours minimum) et un Sharpe/DD vécu sur si peu
  de données a une variance élevée ; l'objectif de la Porte 2 n'est pas de reproduire
  exactement le backtest, mais d'écarter un écart *massif et disqualifiant* (candidate qui
  se comporte de façon qualitativement différente de ce qui a été annoncé).

### 2.3 Destination de la promotion (obligatoire, chiffrée par profil de risque)

La promotion **doit préciser explicitement** dans quel(s) wallet(s) réels la candidate
entre, selon la correspondance suivante entre le profil de risque mesuré de la candidate
(MaxDD vécu en incubation, classe d'actif, corrélation aux poches déjà présentes du
wallet cible) et les 3 profils existants :

| Wallet | Compatible si (tous vrais) |
|---|---|
| 🛡️ Prudent | MaxDD vécu en incubation ≤ 20% ET classe d'actif déjà présente dans les poches prudentes actuelles (ETF, crypto quasi-passif — cf. `docs/SELECTION-FINALE.md` §3) ET la candidate n'augmente pas l'exposition brute du wallet au-delà de `gross_exposure_max` (0,40) une fois intégrée à son allocation cible |
| ⚖️ Équilibré | MaxDD vécu en incubation ≤ 35% ET compatible avec `gross_exposure_max` (0,70) |
| 🔥 Agressif | MaxDD vécu en incubation ≤ 55% ET compatible avec `gross_exposure_max` (0,90) |

Une candidate peut être promue dans **plusieurs** wallets simultanément si elle satisfait
les critères de chacun (avec une `capital_alloc_pct` propre à chaque wallet, jamais
recopiée telle quelle sans revérifier la somme des poches du wallet cible, cf.
`bot/tests/test_config_strategies_sync.py`). Une candidate qui ne satisfait aucun profil
n'est pas promue — elle continue son incubation (si < 56 jours, cf. §3.2) ou est tuée.

### 2.4 Limite structurelle : max 5 stratégies actives par wallet

Aucune promotion ne peut faire dépasser **5 stratégies actives simultanées** dans un
wallet réel (poches "cash" non comptées). Si le wallet cible en a déjà 5, la promotion est
**bloquée** jusqu'à ce qu'une place se libère (mort/retrait d'une stratégie existante,
cf. §3) — jamais de dépassement temporaire "en attendant de faire le ménage".

---

## 3. RÈGLES DE MORT

### 3.1 Stratégie active (dans un wallet réel)

Rétrogradation immédiate au labo (capital réduit à **moitié de sa taille cible actuelle**,
`capital_alloc_pct` divisé par 2 dans la poche du wallet d'origine — le reliquat retombe
en cash de ce wallet) si **l'une ou l'autre** condition se déclenche :
- Drawdown vécu de la poche portée par la stratégie **> 2× le drawdown attendu** (référence
  : le MaxDD backtest OOS qui a justifié sa promotion, §1.2/§2.2) ; **OU**
- Sharpe roulant 60 jours **< 0** pendant **30 jours consécutifs**.

Une fois rétrogradée au labo à taille réduite :
- Elle reprend un cycle d'observation Porte 2 complet (§2.1/§2.2) avant toute
  re-promotion — jamais de retour automatique.
- Si elle ne satisfait pas de nouveau les critères de rétablissement (Sharpe roulant 60j
  redevenu ≥ 0, drawdown vécu revenu sous 1,5× l'attendu) dans les **28 jours** suivant la
  rétrogradation, elle est **tuée** (retirée de `INCUBATING_STRATEGIES` et de toute poche
  de wallet réel, statut `"tuee"` dans `RESEARCH-REGISTRY.json` avec la raison précise).

### 3.2 Candidate en incubation (labo, jamais encore promue)

Tuée automatiquement après **56 jours** consécutifs en incubation sans avoir franchi la
Porte 2 (§2). Pas d'extension, pas de "encore un peu de temps" — 56 jours = 2× la durée
minimale d'observation (§2.1), volontairement généreux pour absorber la variance d'un
échantillon court, mais borné pour ne jamais laisser une candidate traîner indéfiniment
au nom de l'espoir qu'elle "finisse par confirmer" (biais classique de sur-apprentissage
différé — chercher jusqu'à ce que ça marche).

### 3.3 Effet d'une mort

Statut `"tuee"` dans `RESEARCH-REGISTRY.json`, retrait de `INCUBATING_STRATEGIES`
(candidate labo) ou de la poche du wallet réel concerné (stratégie active), entrée dans
`RESEARCH-LOG.md`. **Une stratégie tuée n'est jamais automatiquement réincubée** — une
nouvelle proposition pour la même idée doit repasser intégralement par la Porte 1, avec un
nouvel `id`, et compte comme une entrée supplémentaire dans le calcul de `K_total` (§1.3) —
ceci décourage explicitement le "retesting" répété d'une même idée en variant légèrement
les paramètres jusqu'à ce qu'elle passe, qui est une forme déguisée de sur-apprentissage.

---

## 4. LIMITES STRUCTURELLES

### 4.1 Capacité du labo

**Max 3 candidates simultanées** en incubation dans `INCUBATING_STRATEGIES`. Une 4e
proposition ne peut entrer qu'après qu'une place se libère (promotion ou mort d'une
candidate existante). Objectif : limiter le nombre de "paris" ouverts en parallèle,
cohérent avec la logique de `K_total` (§1.3) — moins de candidates simultanées = registre
qui grandit plus lentement = DSR moins pénalisé pour les prochaines, sans jamais réduire
artificiellement `K_total` a posteriori (le registre, lui, ne rétrécit jamais).

### 4.2 Capacité de chaque wallet réel

**Max 5 stratégies actives** par wallet réel (cf. §2.4).

### 4.3 Framework de risque et circuit breakers : jamais modifiables par la recherche

`bot/risk/manager.py`, `bot/risk/vol_targeting.py`, `bot/risk/circuit_breakers.py`, et les
constantes `CB_*`/`vol_target_annualized`/`gross_exposure_max`/`cap_per_asset` de
`bot/config.py:WALLETS[*]["risque"]` sont **hors de portée de toute session de
recherche/promotion**. Aucune fiche de recherche, aucun résultat de backtest, aucune
promotion ne peut proposer de les modifier. Un changement de ces paramètres est un
changement d'architecture (`docs/ARCHITECTURE.md`), décidé hors du cadre recherche, par un
humain, avec sa propre justification et son propre commit — jamais mêlé à un cycle de
promotion/rétrogradation.

### 4.4 Création d'un nouveau wallet permanent

La création d'un **4e wallet réel permanent** (au-delà de 🛡️/⚖️/🔥, le labo 🧪 n'étant pas
un wallet réel) n'est autorisée que si **toutes** les conditions suivantes sont vraies pour
une stratégie qui a par ailleurs franchi la Porte 2 (§2) :

1. La stratégie ne satisfait le critère de compatibilité (§2.3) d'**aucun** des 3 profils
   existants — c'est-à-dire que son MaxDD vécu dépasse 55% (au-delà du profil agressif) OU
   que sa classe d'actif/structure de corrélation est fondamentalement incompatible avec
   les 3 gross_exposure_max existants (0,40/0,70/0,90) sans dénaturer le profil d'un wallet
   en place (ex. : une stratégie qui nécessiterait un gross_exposure_max > 0,90 pour
   exprimer son edge, ou qui nécessiterait structurellement du levier/short — hors du
   périmètre actuel du simulateur, cf. §5 RESEARCH-BACKLOG.md sur les perpétuels simulés) ;
2. **ET** son Sharpe vécu en incubation est ≥ 1,0 (barre délibérément plus haute que le
   seuil générique de promotion §2.2, car créer un nouveau véhicule permanent est un
   engagement structurel plus lourd qu'ajouter une poche à un wallet existant) ;
3. **ET** la proposition de création est documentée par écrit (nouveau profil de risque
   chiffré complet — `vol_target_annualized`, `gross_exposure_max`, `cap_per_asset`, les 6
   paramètres de circuit breakers — proposé avec la même rigueur que les 3 profils
   existants de `bot/config.py:WALLETS`) et validée par un humain hors du cadre recherche
   avant toute implémentation (cohérent avec §4.3 : même une proposition de *nouveau*
   profil de risque n'est jamais auto-appliquée par la boucle de recherche).

À défaut de ces 3 conditions réunies, la stratégie reste au labo (si encore dans sa
fenêtre de 56 jours, §3.2) ou est tuée — **elle n'est jamais forcée dans un wallet
existant "au mieux"** juste pour éviter de la perdre : un profil de risque mal assorti est
pire qu'une stratégie non déployée.

---

## 5. Antécédent — le portefeuille de production actuel n'a PAS suivi ce protocole

Honnêteté requise : `docs/SELECTION-FINALE.md` (portefeuille actif au moment de la
création de ce document) a été décidé **avant** l'existence de ce processus de
gouvernance et du wallet labo. `xs_momentum_sp100` et `dual_momentum_multiclasse_etf`
ont été déployés directement en production sans passer par une incubation labo avec
performance vécue (Porte 2) — seule la Porte 1 (walk-forward + DSR + audit informel) a
été appliquée, et encore, avec un DSR calculé sur `K` interne à la stratégie, pas sur un
`K_total` de registre (cf. §1.3). `quasi_passif_crypto` a été déployé sur un backtest
**explicitement non audité** (`bt-final/quasi-passif-crypto/results.json`, absence de
walk-forward/DSR/audit adversarial — documenté comme tel dans
`docs/SELECTION-FINALE.md` §2.2).

**Ce n'est pas rétroactivement corrigé** — ces 3 stratégies restent en production sous
leur propre critère d'échec déjà défini (`docs/SELECTION-FINALE.md` §5), et ne sont pas
soumises aux règles de mort du §3 de ce document tant qu'un audit spécifique ne les
aligne pas explicitement sur ce cadre (action future, pas couverte par ce document).
**Toute stratégie nouvelle, à partir de la date de ce document, suit intégralement le
protocole ci-dessus, sans exception de confort.**
