# Analyse dédiée #19 — bande de non-négociation : écarts moteur/production (2026-10-12)

*Session hebdomadaire #10, session d'ANALYSE DÉDIÉE (aucune candidate jugée, conformément à la
priorité n°1 pré-enregistrée par la revue de la session #9). Analyse produite par un agent
d'analyse sur copie isolée du dépôt (remote neutralisé), données de marché réelles de la branche
`market-data` (OHLCV horaire 6 majors 2022-01 → 2026-09, 41 615 barres). Les affirmations
load-bearing sur le code ont été re-vérifiées par l'orchestrateur (fichier:ligne ci-dessous), et
le dossier a été soumis à un audit adversarial indépendant AVANT commit — verdict initial
`isSound: false` : les CHIFFRES ont tous été reproduits indépendamment (S1/S2/S3 à ±0,001,
dérivation T2 reconfirmée sur ~16 000 états du vrai RiskManager), mais 5 findings MAJEURS de
RÉDACTION et de garde ont été corrigés dans la présente version (le récit initial minimisait
l'écart en citant la fenêtre complète là où le protocole réel est le walk-forward — cf.
RESEARCH-LOG.md 2026-10-12). Chaque chiffre provient d'un script exécuté présent dans ce dossier, avec son JSON
de résultats. Le cache d'exécution (~38 Mo de pickles de runs moteur) n'est pas committé — les
scripts sont re-exécutables depuis la racine du dépôt avec la branche `market-data` exportée.*

**Légende** : [DÉMONTRÉ] = prouvé par le code ou un test exécuté ; [ESTIMÉ] = mesuré sur la
donnée réelle (une seule réalisation historique) ; [HYPOTHÈSE] = modèle ou supposition.

---

## 1. Verdict T1 — caractérisation exacte des deux écarts

### Écart (a) — référentiel de la bande [DÉMONTRÉ]

| Aspect | Moteur (`backtest/engine.py`) | Production (`bot/`) |
|---|---|---|
| Seuil de bande | 0,05 plat (l. ~705-721) | 0,05 × `capital_alloc_pct` par symbole (`bot/risk/manager.py` l. 317-319) |
| Poids comparé | poids de poche, base = équity de la POCHE simulée seule | poids mesuré sur l'équity TOTALE du wallet (`bot/runner.py` l. ~421-423, 806) |
| Cibles | poids de poche W | poids de poche × alloc |

Avec le reste du wallet en cash, la relation équity poche → équity wallet est AFFINE
(`E = 1 + a·(P−1)`), pas homothétique (`a·P`) — c'est le finding F2 (CRITIQUE) de l'audit du
2026-09-07, vérifié ici à erreur 0,0 dans le moteur. Les deux règles de décision hold/trade
sont équivalentes à un décalage δ = w_p·(1−a)·(P−1)/(1+a·(P−1)) de l'argument de la bande :
**δ = 0 à l'entrée depuis flat** (les deux référentiels entrent au même moment), δ borné quand
|P−1| est petit, δ non connaissable en général car il dépend du P&L du RESTE du wallet (R(t)),
invisible depuis un backtest poche-standalone. Vérification de la règle dérivée sur 4 000 états
aléatoires contre le vrai `RiskManager` : 0 désaccord (`t2_analytic.py`).

### Écart (b) — verdict : la fiche #19 était IMPRÉCISE [DÉMONTRÉ]

La fiche affirmait « la production exécute TOUJOURS un flatten (cible 0) même sous la bande ;
le moteur, lui, retient un flatten sous la bande ». Vérification littérale contre le code
(`t1_prove_code.py`, cas C1-C5b exécutés sur le vrai `RiskManager`) :

- Le SEUL contournement de la bande en production est le flag `flatten` d'un circuit breaker
  (`bot/risk/manager.py` l. 313-314, `bot/risk/circuit_breakers.py` l. 160-176). Un **zéro
  stratégique** (poids cible 0 décidé par la stratégie) est CONSERVÉ sous la bande, en
  production COMME dans le moteur (cas C1 : position 1 %, bande effective 1,5 % → conservée).
- Le moteur ne contient AUCUN « flatten » (recherche exhaustive `engine.py`/`risk_overlay.py`)
  parce qu'il ne modélise AUCUN circuit breaker.

**Le vrai écart (b′) est donc : le moteur ne modélise pas les circuit breakers du tout**
(flatten, half-size, freeze_entries) — pas une asymétrie de bande sur le flatten.

**Constat corollaire (nuance la fiche #16)** : le piège « un poids vol-scalé < 0,05 n'entre
jamais en position » existe AUSSI en production (δ = 0 à l'entrée depuis flat : poids de poche
0,04 avec a = 0,25 → pas d'entrée non plus côté production, cas C5/C5b) — ce n'est pas un
artefact propre au moteur, contrairement à ce que la lecture de #16 pouvait laisser croire.
L'artefact réel de #16 (remise à zéro entre fenêtres) reste, lui, propre au moteur.

## 2. Chiffres clés T3 — expérience appariée sur données réelles [ESTIMÉ]

Sémantiques comparées sur la même série de poids décidés : **S1** = moteur actuel (bande plate
0,05, frame poche) ; **S2** = production modélisée (frame wallet affine reste-en-cash, bande
0,05·a, métriques reconverties en unités de poche) ; **S2b** = a·W sans bande ; **S3** = W sans
bande. Coûts 25 bps/côté. Profils : A = quasi_passif équilibré 6 majors (a = 0,30, poids non
nul médian 0,061) ; B = candidate synthétique faible poids type vol-gate (a = 0,25, médiane
0,066) ; C = équilibré brut 1,0 ; sensibilité a ∈ {0,10, 0,25, 0,60}.

- **Structure walk-forward officielle 9m/3m (le PROTOCOLE réel de la Porte 1 ; 21 configs,
  capital remis à 1 par fenêtre)** : ΔSharpe = S2−S1 ∈ [−0,220 ; +0,007], médiane −0,052,
  négatif 18/21, IC bootstrap excluant zéro pour 3 configs (A a=0,25, A a=0,30, C a=0,25).
  Sur les 12 configs de signaux réels de CANDIDATES passées : ΔSharpe ∈ [−0,066 ; +0,007],
  |ΔMaxDD| ≤ 1,9 pts. Le profil A (signal de PRODUCTION quasi_passif, hors de ce
  sous-ensemble) est le plus exposé : ΔSharpe −0,103/−0,117/−0,111 (a=0,10/0,25/0,30),
  S1 0,273 → S2 0,162 à a=0,30, pour une valeur de la bande S1−S3 de 0,122 en walk-forward —
  **sur ce profil, l'écart de sémantique est du même ordre que la valeur de la bande**
  (correction d'audit : une version antérieure de ce rapport affirmait « un ordre de
  grandeur en dessous » en citant la fenêtre complète ; c'était trompeur sur le protocole).
- **Fenêtre complète 4,75 ans (contextuel, HORS protocole)** : profil A Sharpe S1 0,699 / S2
  0,738 / S2b 0,410 / S3 0,377 — la bande y vaut ≈ +0,32 de Sharpe et Δ ∈ [+0,013 ; +0,118],
  tous positifs. **Le signe de l'écart dépend donc de l'horizon** (effet de référentiel, pas
  de bande) — aucune des deux sémantiques n'est uniformément pessimiste : en walk-forward S2
  est presque toujours PLUS sévère, sur fenêtre complète toujours PLUS favorable.
- **Reste du wallet (profil A, a = 0,30)** : ΔSharpe +0,039 (reste en cash), +0,038 (SPY/IEF),
  **−0,347 (reste crypto B&H)** — le facteur dominant est R(t), inconnaissable depuis un
  backtest poche-standalone. S2 est donc un MODÈLE, pas « la production ».
- **Divergences d'ordres S1 vs S2** : profil A 91 barres XOR (non appariés à ±24 h : 5,4 % des
  ordres S1, 8,4 % des S2) ; profil B 205 barres XOR (13,6 % / 11,0 %).
- **Exposition aux circuit breakers (écart b′, mesure S2)** : MaxDD wallet simulé — A : 2,1 %
  (a=0,10) → 13,0 % (a=0,60) ; B : 11,9 % (a=0,25) → **26,3 % (a=0,60)** ; C (brut 1,0) :
  21,4 % → 45,3 %, flatten déclenché + 92 épisodes de freeze (seuils appliqués par l'analyse : labo pour a < 0,5, « agressif » au-delà — cf. erratum de tables_generated.md).
  **Correction d'audit (M5)** : rapportés aux seuils du wallet de DESTINATION de toute
  Porte 1 — le LABO (half-size 0,15 / flatten 0,25) — le profil B à a=0,60 DÉPASSE le seuil
  de flatten (26,3 % > 25 %) avec des milliers d'heures au-dessus du seuil half-size ;
  l'analyse initiale appliquait les seuils « agressif » dès a ≥ 0,5 et concluait à tort
  « aucun flatten » pour B. Une candidate à poids significatif ou à brut élevé sans sizing
  interne peut donc vivre en incubation des interventions CB que le moteur ne simule pas du
  tout — d'où le bloc `cb` obligatoire de la convention, aux seuils du labo.

## 3. Impact rétrospectif T4 [ESTIMÉ]

Rejeu de la structure walk-forward sur les signaux reconstruits des candidates du registre :
vol_breakout ΔSharpe ∈ [−0,037 ; +0,007] ; retest quasi-passif −0,111 ; protective_vol_gate
∈ [−0,066 ; +0,004] ; pairs ∈ [−0,056 ; +0,002] ; seasonality dominée par les coûts (bande sans
effet sur le verdict) ; funding_carry non rejouable ici, mais l'entrée depuis flat étant
identique dans les deux référentiels, le blocage d'entrée documenté par son audit n'est pas
aggravé par l'écart (a). Les verdicts de DOMINATION par contrôle apparié (pairs : dominée ;
PVG : non dominée) sont identiques sous S1 et S2. **Aucun verdict du registre ne bascule.**
Les franchissements de seuils Porte 1 observés entre S1 et S2 sur fenêtre complète (A : Sharpe
0,699→0,738 autour du seuil 0,70 ; C : PF ; B a=0,10 : n_trades 85→79) montrent en revanche
qu'une FUTURE candidate marginale peut voir un seuil basculer selon la sémantique — c'est
l'objet de la convention (`docs/BAND-FIDELITY-SPEC.md`).

## 4. Recommandation T5 (adoptée après correction d'audit — cf. docs/BAND-FIDELITY-SPEC.md)

O2 renforcée + O4, SANS amendement du moteur : Porte 1 jugée sur S1 ; la SPEC de toute
candidate déclare son `capital_alloc_pct` prévu ; l'analyse d'honnêteté rapporte S2
(reproductible avec le moteur existant : poids a·W, bande 0,05·a, sélection IS non rejouée,
métriques reconverties en unités de poche P = 1 + (E−1)/a) et l'exposition aux CB mesurée aux
seuils du LABO. `band_fragile` est un FLAG (un seuil §1.2 bascule entre S1 et S2 — définition
sans nouveau seuil chiffré, correction d'audit M3 : la version initiale en faisait une règle
de rejet automatique avec un seuil |ΔSharpe| > 0,10, ce qui aurait constitué un nouveau
critère de Porte 1 déguisé ; le cas band-fragile est tranché en session selon la pratique
d'ambiguïté conservatrice existante et versé au dossier #12, où est aussi versée la
proposition formelle d'un critère « S1 ET S2 »). O1 (aligner le moteur) est écartée pour
COMPARABILITÉ (16 entrées du registre) et INCONNAISSABILITÉ de R(t) — pas au nom du
pessimisme : correction d'audit M2, sur le protocole walk-forward réel S2 est PLUS sévère que
S1 dans 18/21 configs, l'argument initial « S2 pourrait rehausser les chiffres » ne valait
que sur fenêtre complète, hors protocole.

## 5. Limites

Pas de ré-exécution walk-forward bit-à-bit du moteur complet (structure 9m/3m reproduite sur
21 configs, runs moteur réels en validation d'équité à 0,0 d'écart) ; S2 est un modèle
(reste-en-cash) et les CB n'y sont PAS simulés (seule leur exposition est mesurée) ; une seule
réalisation historique, IC bootstrap (blocs 168 h) larges ; les cellules factorielles mixtes du
JSON sont des diagnostics, pas des sémantiques de production.

## 6. Fichiers et reproduction

Scripts `t1_*.py` → `t7_*.py` + `common.py`, résultats `results_*.json`, tableaux
`tables_generated.md`. Reproduction : exécuter depuis la racine du dépôt, dans l'ordre
`t3a_build_weights.py` → `t3b_run_engine.py` (construit le cache local de runs moteur,
~45 min) → `t3c_analyze.py`/`t3d_*`/`t3e_*`/`t3f_*`/`t3g_*` → `t4_*` → `t6_tables.py` →
`t7_summary.py` ; `t1_prove_code.py` et `t2_analytic.py` sont autonomes. Chemins paramétrés
par les variables d'environnement `BAND19_REPO` (racine du dépôt, défaut : déduite de
l'emplacement du script) et `BAND19_DATA_DIR` (export de la branche `market-data`,
sous-dossier `data/crypto` ; `t3d_rest_of_wallet.py` utilise aussi `data/etf`). Le cache
d'exécution n'est pas committé (reconstruit par `t3b`).
