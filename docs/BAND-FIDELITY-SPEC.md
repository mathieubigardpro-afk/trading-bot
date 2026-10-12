# BAND-FIDELITY-SPEC.md — Fidélité backtest/production de la bande de non-négociation

*Document de convention PRÉ-ENREGISTRÉE, adopté en session hebdomadaire #10 (2026-10-12),
session DÉDIÉE d'analyse ne jugeant AUCUNE candidate (conforme à `docs/PROMOTION-RULES.md`
§0 : gouvernance et jugement toujours séparés). Il solde le backlog #19 (P2, ajouté
2026-09-07), dont le dossier comptait 3 pièces : finding F2 (CRITIQUE) de l'audit de
l'extension carry (session #6), écart « flatten sous la bande » (même session), et la
quantification sur candidate réelle de la session #7. L'instruction quantitative complète est
dans `backtest/results/band_fidelity_analysis_2026-10-12/REPORT.md` (scripts + JSON
re-exécutables) ; le présent document et cette analyse ont été soumis à un audit adversarial
indépendant avant commit (verdict initial `isSound: false`, 5 findings MAJEURS corrigés —
détail en `RESEARCH-LOG.md` 2026-10-12). Ce document définit une OBLIGATION DE MESURE ET DE
TRANSPARENCE (quelle annexe produire, comment) — il n'ajoute, ne retire ni ne modifie AUCUN
seuil chiffré de `PROMOTION-RULES.md` §1.2, qui reste intouché, et il ne crée AUCUNE règle de
rejet automatique nouvelle (voir §3.3 — la seule proposition de règle nouvelle est versée aux
amendements #12, décision humaine ; même pattern que `docs/SIZING-FIDELITY-SPEC.md` et
`docs/RECALIBRATION-SPEC.md`).*

---

## 1. Le problème (deux écarts documentés, instruits par l'analyse du 2026-10-12)

### 1.1 Écart (a) — référentiel de la bande

Le moteur commun backteste une POCHE seule : bande plate `no_trade_band=0.05` comparée sur les
poids de la poche, base d'équity = équity de la poche (`backtest/engine.py` ~l. 705-721). La
production applique `0,05 × capital_alloc_pct` par symbole, sur des poids mesurés sur l'équity
TOTALE du wallet (`bot/risk/manager.py` étape 6, `bot/runner.py`). L'équivalence des deux
sémantiques est RÉFUTÉE dès qu'il y a compounding (relation affine `E = 1 + a·(P−1)`, finding
F2 session #6, garde : `backtest/tests/test_carry.py::test_no_trade_band_pocket_wallet_
homothety_refuted_under_compounding`). Points établis par l'analyse, en citant d'abord les
chiffres du PROTOCOLE RÉEL de la Porte 1 (walk-forward 9m/3m, capital remis à 1 par fenêtre —
les chiffres « fenêtre complète » sont contextuels, jamais décisionnels) :

- le décalage entre les deux règles de décision est NUL à l'entrée depuis flat et borné quand
  l'équity de poche reste proche de 1 — c'est pourquoi l'effet est contenu en walk-forward
  (fenêtres OOS de 3 mois, équity proche de 1) et grossit avec l'horizon ;
- **en walk-forward (21 configs)** : ΔSharpe = S2−S1 ∈ [−0,220 ; +0,007], médiane −0,052,
  négatif 18/21, IC bootstrap excluant zéro pour 3 configs. Sur les 12 configs de signaux
  réels de CANDIDATES passées : ΔSharpe ∈ [−0,066 ; +0,007], |ΔMaxDD| ≤ 1,9 pts. Le signal de
  production `quasi_passif_crypto` (profil A, hors de ce sous-ensemble) est le plus exposé :
  ΔSharpe −0,103/−0,117/−0,111 selon a ∈ {0,10, 0,25, 0,30} (S1 0,273 → S2 0,162 à a=0,30),
  pour une valeur de la bande elle-même (S1−S3) de 0,122 en walk-forward — sur ce profil,
  l'écart de sémantique est du MÊME ordre que la valeur de la bande, pas négligeable ;
- **sur fenêtre complète 4,75 ans** (hors protocole) : Δ ∈ [+0,013 ; +0,118], tous positifs,
  et la bande vaut ≈ +0,32 de Sharpe sur le profil A. **Le signe de l'écart dépend donc de
  l'horizon** et, mesuré par l'analyse « reste du wallet » (results_t3_rest_of_wallet.json), du P&L du RESTE du wallet (ΔSharpe +0,04 reste-en-cash vs
  −0,35 reste-crypto-B&H sur le même profil) : R(t) est inconnaissable depuis un backtest
  poche-standalone, AUCUNE des deux sémantiques n'est uniformément pessimiste ;
- aucun verdict passé du registre ne bascule entre les deux sémantiques (rejeu T4, y compris
  les verdicts de domination par contrôle apparié) ; une candidate MARGINALE future peut en
  revanche voir un seuil Porte 1 basculer (cas observés autour des seuils Sharpe 0,70, PF et
  n_trades).

### 1.2 Écart (b′) — circuit breakers non modélisés (reformulation de l'écart (b))

La formulation de la fiche #19 (« la production exécute toujours un flatten même sous la
bande ; le moteur retient un flatten sous la bande ») était IMPRÉCISE. Vérifié par exécution
sur le vrai `RiskManager` (REPORT.md §1, cas C1-C5b, reconfirmé indépendamment par l'audit) :
le seul contournement de bande en production est le flag `flatten` d'un CIRCUIT BREAKER ; un
zéro stratégique sous la bande est conservé en production comme dans le moteur — aucune
asymétrie de bande là-dessus. Le vrai écart est que **le moteur ne modélise aucun circuit
breaker** (flatten, half-size, freeze_entries). Mesuré sur données réelles (MaxDD du wallet
simulé S2, à comparer aux seuils du wallet de DESTINATION — pour la Porte 1 c'est le labo :
half-size 0,15 / flatten 0,25) : profil A ≤ 13,0 % même à a = 0,60 (sous les seuils labo — le plus proche, half-size, est à 15 %) ;
profil B (faible poids) 11,9 % à a = 0,25 mais **26,3 % à a = 0,60 — au-delà du seuil de
flatten du labo (25 %)**, avec des milliers d'heures au-dessus du seuil half-size ; profil C
(brut 1,0) 21,4-45,3 %, flatten déclenché et 92 épisodes de freeze même sous seuils
« agressif ». Une candidate à poids significatif ou à brut élevé sans sizing interne peut donc
vivre en incubation des interventions CB que le moteur ne simule pas du tout — d'où
l'obligation de mesure du §3.2.

### 1.3 Constat corollaire (nuance la fiche #16)

Le piège « un poids vol-scalé sous 0,05 n'entre jamais en position » n'est PAS propre au
moteur : à l'entrée depuis flat, le décalage entre référentiels est nul et la production
bloque ces entrées à l'identique (bande par poche). L'artefact réel traité par #16 (remise à
zéro de la position entre fenêtres OOS) reste, lui, propre au moteur.

## 2. Décision (options O1-O4 de l'instruction)

**Adoptée : O2 renforcée (statu quo moteur + annexe d'honnêteté S1/S2 obligatoire) + O4
(exposition aux circuit breakers obligatoire dans l'analyse d'honnêteté). Option O1
(amendement du moteur vers un « frame wallet ») ÉCARTÉE.**

- **O1 écartée — motifs : comparabilité et inconnaissabilité, PAS pessimisme.** (i) La
  fidélité qu'un frame wallet promet exige R(t) (P&L du reste du wallet), que le backtest
  d'une poche ne peut pas connaître — on remplacerait une approximation documentée par une
  autre approximation présentée à tort comme « la production » (analyse « reste du wallet » : le signe de l'écart
  change avec la composition du reste du wallet). (ii) Modifier la sémantique de jugement
  invaliderait la comparabilité avec les 16 entrées existantes du registre sans gain de
  fidélité démontré. Honnêteté due au lecteur : sur le protocole walk-forward réel, S2 est
  PLUS SÉVÈRE que S1 dans 18 configs sur 21 (jusqu'à −0,22 de Sharpe) — l'argument du
  pessimisme ne départage donc PAS en faveur de S1 (sur fenêtre complète le signe s'inverse,
  mais la fenêtre complète n'est pas le protocole). C'est précisément pourquoi la question
  « faut-il exiger S1 ET S2 au-dessus des seuils » est une vraie question de RÈGLE, versée
  aux amendements #12 (décision humaine) plutôt que tranchée ici par une convention.
- **O2 renforcée** : la Porte 1 continue de se juger sur la sémantique S1 (moteur inchangé,
  bande plate 0,05, frame poche). En contrepartie, l'analyse d'honnêteté de toute SPEC de
  candidate doit contenir le run apparié S2 (§3.2) et le comparatif des seuils §1.2 sous S1
  et S2 — toute divergence matérielle devient VISIBLE et consignée, au lieu d'être découverte
  en incubation.
- **O4** : la même analyse d'honnêteté doit rapporter l'exposition aux circuit breakers du
  wallet de destination (§3.2, bloc `cb`). Le moteur ne les simule pas (périmètre inchangé) —
  mais une candidate dont le backtest traverse des zones où la production l'aurait coupée
  doit le DIRE, chiffres à l'appui.

## 3. Règles opérationnelles (applicables à toute candidate à partir du 2026-10-12)

1. **Déclaration d'alloc** : toute SPEC de candidate pré-enregistre le `capital_alloc_pct`
   prévu pour l'incubation (la part du capital labo demandée). C'est l'`a` des runs S2. À
   l'entrée effective en incubation, l'`a` de la fiche `INCUBATING_STRATEGIES` doit être
   celui de la SPEC (tout écart se justifie par écrit dans le log).
2. **Annexe bande obligatoire — définition PRÉCISE de S2** : S2 = re-run du MÊME moteur
   (`simulate_segment`, paramètres existants — aucun changement de moteur), sur les MÊMES
   fenêtres walk-forward que S1, avec poids décidés `a·W` et `no_trade_band = 0,05·a` ; la
   sélection IS n'est PAS rejouée (la combinaison retenue par le run S1 est réutilisée telle
   quelle — S2 est un appariement de sémantique d'exécution, jamais une ré-optimisation) ;
   toutes les métriques S2 sont reconverties en unités de poche via `P = 1 + (E−1)/a` avant
   comparaison (sans cette reconversion, MaxDD/PF/CAGR seraient mécaniquement écrasés par a
   et la comparaison serait un non-sens). La fiche de résultats (results.json + fiche de
   recherche) contient un bloc `band_fidelity` avec, a minima : `a`, `sharpe_s1`,
   `sharpe_s2`, les 5 métriques de seuil §1.2 sous S1 ET S2, le nombre de barres de décisions
   divergentes, `band_fragile` (cf. §3.3) et le bloc `cb` : MaxDD du wallet simulé S2 comparé
   aux seuils `cb_dd_half_size_pct`/`cb_dd_flatten_pct` du wallet LABO (destination de toute
   incubation : 0,15/0,25) — et du wallet réel visé si la SPEC en nomme un — plus le nombre
   d'épisodes où half-size/flatten/freeze se serait déclenché.
3. **Band-fragile — un FLAG, pas une règle de rejet** : `band_fragile: true` si AU MOINS un
   seuil de Porte 1 §1.2 bascule (passe ↔ échoue) entre S1 et S2. Définition volontairement
   SANS nouveau seuil chiffré (un critère « |ΔSharpe| > x » serait un seuil neuf, hors de
   portée d'une convention — le ΔSharpe brut est rapporté à titre informatif dans l'annexe).
   Conséquence pré-enregistrée : le flag et son détail sont consignés au registre et dans la
   fiche ; le cas est alors tranché EN SESSION, au cas par cas, selon la pratique d'ambiguïté
   conservatrice déjà établie (précédent `xs_momentum_invvol_sp100` : seuils passés mais
   signal d'alerte structurel → option la plus conservatrice retenue ET renvoi du critère
   général à la gouvernance), et le cas est versé au dossier #12. Ce document ne crée PAS de
   règle automatique « band-fragile ⇒ rejet » : une telle règle serait un nouveau critère de
   Porte 1, qui n'appartient qu'à un amendement de `PROMOTION-RULES.md` (proposition formelle
   d'un critère « S1 ET S2 au-dessus des seuils » versée à #12, décision humaine).
4. **Interdiction du « bande 0 » comme argument de fidélité** : un run sans bande mélange
   effet de bande et effet de référentiel (démontré, REPORT.md §2) — il reste permis comme
   analyse de texture/sensibilité, jamais comme preuve d'alignement production.
5. **Candidates à rampe ou à faible poids nominal** : l'exigence de la session #7 (documenter
   la texture d'exécution sous la bande) reste en vigueur et s'intègre au bloc
   `band_fidelity` (part des ordres de la rampe réellement exécutés, délais effectifs).
6. **Garde mécanique** : `tools/verify_research.py --check` refuse toute NOUVELLE entrée du
   registre (`date_test` ≥ 2026-10-12) sans bloc `band_fidelity` valide : soit un dict
   contenant au minimum les clés non nulles `a`, `sharpe_s1`, `sharpe_s2`, `band_fragile`,
   `cb`, soit une exemption explicite d'au moins 30 caractères (chaîne documentant pourquoi
   la bande est sans objet — ex. retest de validation sans incubation visée) dont la
   justification de fond est vérifiée par l'audit (§3.7). Le même check impose désormais un
   `date_test` au format ISO strict pour TOUTES les entrées (les comparaisons de dates sont
   lexicographiques — une date non-ISO les contournerait). Les 16 entrées antérieures sont
   exemptées du bloc (antériorité, registre append-only). Tests :
   `tools/tests/test_verify_research.py`.
7. **Point d'audit obligatoire** (même mécanisme que `SIZING-FIDELITY-SPEC.md` §3.4) :
   l'audit adversarial §1.4 de toute candidate vérifie désormais explicitement : l'annexe
   `band_fidelity` est présente et exacte (run S2 reproduit ou recalculé, reconversion en
   unités de poche correcte, bloc `cb` aux seuils du LABO), OU l'exemption est justifiée sur
   le fond. Une annexe manquante, fausse ou de pure forme = finding, au même titre qu'un DSR
   mal calculé.

## 4. Ce que ce document NE fait PAS

- Il ne modifie PAS `backtest/engine.py`/`risk_overlay.py` (aucun changement de moteur, les
  archives et tests restent bit-à-bit identiques), ni `bot/risk/*`, `bot/runner.py` ou toute
  constante de `WALLETS` (§4.3 : hors de portée de la recherche).
- Il ne modifie PAS `PROMOTION-RULES.md` (gravé) et ne crée AUCUNE règle de rejet nouvelle.
  Deux propositions sont VERSÉES aux amendements en attente (#12, décision humaine, session
  de gouvernance dédiée) : (i) inscrire le bloc `band_fidelity` et le point d'audit §3.7
  dans §1.4 ; (ii) statuer sur un critère formel « seuils §1.2 exigés sous S1 ET S2 »
  (motivé par le fait que S2 est plus sévère que S1 sur 18/21 configs walk-forward — cf. §2).
- Il ne rouvre PAS les verdicts passés (aucun ne bascule, T4) et n'exige aucun re-run
  d'archive.
- Il ne prétend PAS que S2 « est » la production : S2 est un modèle borne (reste-en-cash),
  documenté comme tel ; l'écart résiduel dû au P&L des autres poches du wallet (et à la base
  d'équity du dimensionnement lui-même) reste inconnaissable en backtest poche-standalone et
  est documenté comme limite STRUCTURELLE (fiche #23 du backlog, P3).

## 5. Traçabilité

- Analyse source : `backtest/results/band_fidelity_analysis_2026-10-12/` (REPORT.md, scripts
  t1-t7, results_*.json) — session #10, `RESEARCH-LOG.md` 2026-10-12.
- Audit adversarial indépendant du livrable (analyse + ce document + garde mécanique) :
  verdict initial `isSound: false` (5 MAJEURS : récit walk-forward/fenêtre-complète inversé,
  argument de pessimisme non tenable, règle band-fragile initialement rédigée comme un
  rejet automatique, garde mécanique trop faible, S2 sous-spécifié + seuils CB du mauvais
  wallet) — tous corrigés dans la présente version ; chiffres de l'analyse reproduits
  indépendamment par l'auditeur (S1/S2/S3 à ±0,001, 0 désaccord sur ~16 000 états
  RiskManager). Détail : `RESEARCH-LOG.md` 2026-10-12.
- Pièces d'origine : CARRY-EXTENSION-SPEC.md §5/§7 (F2), RESEARCH-LOG.md 2026-09-07 (b) et
  2026-09-14 (b) (quantification session #7), fiche #19 du backlog.
- Corrections de formulation actées : fiche #19 (écart (b) → (b′)), nuance fiche #16 (piège
  d'entrée sous la bande commun moteur/production), notes datées au backlog — les documents
  pré-enregistrés antérieurs (CARRY-EXTENSION-SPEC) ne sont pas réécrits, une note datée y
  renvoie ici.
- Garde mécanique : `tools/verify_research.py` (+ tests `tools/tests/test_verify_research.py`).
