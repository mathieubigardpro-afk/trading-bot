# SIZING-FIDELITY-SPEC.md — Fidélité backtest/production du sizing des candidates labo

*Document de convention PRÉ-ENREGISTRÉE, adopté en session hebdomadaire #9 (2026-09-28),
session DÉDIÉE d'infrastructure ne jugeant AUCUNE candidate (conforme à
`docs/PROMOTION-RULES.md` §0 : gouvernance et jugement toujours séparés). Il solde le
backlog #21 (P0, ajouté 2026-09-21) né du finding F1 (CRITIQUE) de l'audit adversarial de
`pairs_ethbtc_ratio_rotation` (session #8). Ce document opérationnalise l'exigence DÉJÀ
GRAVÉE de `PROMOTION-RULES.md` §1.4 (« cohérence de la logique de sizing avec le
`RiskManager` réel du bot — pas un sizing fictif plus généreux que ce que `bot/risk/`
appliquerait en production ») — il n'ajoute, ne retire ni ne modifie AUCUN seuil chiffré de
`PROMOTION-RULES.md`, qui reste intouché (§0 « gravé » ; même pattern que
`docs/RECALIBRATION-SPEC.md`, document séparé volontairement).*

---

## 1. Le problème (constat d'audit, démontré par exécution — session #8)

`bot/runner.py:_risk_manager_for_wallet` construit le RiskManager PORTEFEUILLE avec
`vol_target_annualized=50.0` EN DUR pour TOUS les wallets, **labo 🧪 compris** (design
documenté depuis la session #3 pour ne pas doubler le vol-targeting INTERNE de
`quasi_passif_crypto` — jamais réévalué pour le cas opposé). Conséquences vérifiées dans le
code :

- Une candidate labo **sans sizing interne** ne recevrait en incubation AUCUN vol-targeting
  réel (`compute_vol_scalar` ≈ 1,0 à cible 50 pour toute vol réaliste) ni cap d'exposition
  brute réel (`gross_exposure_max=1.0` trivial dans le constructeur du runner), alors que le
  moteur commun de backtest lui applique par défaut l'overlay « production »
  (`apply_vol_targeting=True`, scalar réel mesuré 0,23–0,79 sur la candidate #8). Le
  backtest est alors PLUS PROTECTEUR que ce que l'incubation livrerait — précisément le
  « sizing fictif plus généreux » que §1.4 interdit.
- Ce qui RESTE réellement appliqué au labo par le runner (vérifié ligne à ligne,
  `bot/runner.py:_risk_manager_for_wallet`) : cap par actif crypto (0,20), bande de
  non-négociation par poche, et les 4 circuit breakers du profil. Ces protections sont
  réactives ou par-actif — elles ne remplacent pas la fonction préventive du vol-targeting.
- Le même 50.0 en dur s'applique aux 3 wallets réels : le déficit ne s'arrête pas à la
  promotion — il suit la candidate sans sizing interne sur TOUTE sa vie.

## 2. Décision (options (a)/(b)/(c) de la fiche #21 instruites)

**Adoptée : (c) comme convention de backtest PAR DÉFAUT + (b) comme exigence de conception
pour toute candidate revendiquant un vol-targeting réel. Option (a) ÉCARTÉE.**

Justification (résumé de l'instruction, détail en `RESEARCH-LOG.md` 2026-09-28) :

- **(a) écartée** (champ `sizing_interne` + RiskManager labo conditionnel dans
  `bot/runner.py`) : touche du code de production partagé par les 4 wallets (audit
  adversarial obligatoire), crée un NOUVEAU défaut de fidélité au moment de la promotion
  (les 3 wallets réels gardent 50.0 en dur : la candidate promue perdrait son vol-targeting
  du jour au lendemain), et est ambiguë si le labo héberge simultanément une candidate à
  sizing interne et une sans (RiskManager unique par wallet). Surtout, elle est INUTILE :
  le mécanisme visé existe déjà côté stratégie (voir (b)).
- **(b) retenue comme exigence de conception** : chaque stratégie reçoit déjà
  `profile=wallet_cfg` dans `target_weights()` (`bot/runner.py:_combine_pockets`) et peut
  lire `profile["risque"]` — les VRAIES valeurs configurées, non neutralisées (labo : 0,20 ;
  wallets réels : leurs profils respectifs). C'est exactement le pattern de
  `bot/strategies/quasi_passif_crypto.py` (vol-targeting interne, borné par
  `gross_exposure_max`). Une candidate qui implémente son sizing ainsi est fidèle sur TOUT
  son cycle de vie (backtest → labo → wallet réel) sans AUCUN changement de production.
- **(c) retenue comme convention par défaut** : une candidate SANS sizing interne est
  backtestée avec `apply_vol_targeting=False` (le paramètre existe déjà,
  `backtest/engine.py:simulate_segment`) — en vol brute, comme elle vivrait réellement en
  incubation. C'est l'hypothèse la plus défavorable ET la plus fidèle à la production
  actuelle (principe pessimiste, ARCHITECTURE.md §0.2). Ses métriques Porte 1 (MaxDD en
  tête) reflètent alors ce que le labo verrait vraiment.

## 3. Règles opérationnelles (applicables à toute candidate à partir du 2026-09-28)

1. **Toute nouvelle entrée de `bot/config.py:INCUBATING_STRATEGIES` DOIT déclarer
   `sizing_interne: bool`** (champ ajouté au schéma documenté, validé mécaniquement par
   `bot/tests/test_governance_limits.py`). Ce champ est déclaratif ET vérifié : l'audit
   adversarial de Porte 1 (§1.4) doit confronter la déclaration au code de la candidate.
2. **`sizing_interne: false`** ⇒ la SPEC de backtest de la candidate DOIT fixer
   `apply_vol_targeting=False` (vol brute). Toute SPEC qui voudrait l'overlay actif pour une
   candidate sans sizing interne doit démontrer, code de production à l'appui, que ce chemin
   existe réellement en production — à ce jour il n'existe pas.
3. **`sizing_interne: true`** ⇒ la candidate lit `profile["risque"]` et applique une formule
   fonctionnellement équivalente à `bot/risk/vol_targeting.compute_vol_scalar` (bornée par
   `gross_exposure_max` et compatible `cap_per_asset`), sur le modèle de
   `quasi_passif_crypto`. Son backtest désactive l'overlay portefeuille
   (`apply_vol_targeting=False`) et modélise le sizing DANS le module candidat — précédent :
   retest `quasi_passif_crypto`, session #3 (correctif CRITIQUE du chemin d'overlay). L'audit
   §1.4 vérifie l'équivalence fonctionnelle (confrontation directe adaptateur backtest vs
   `target_weights`, comme les 21 tests du retest #3).
4. **Point d'audit obligatoire (les deux cas)** : la section « cohérence sizing » du verdict
   `isSound` cite ce document et vérifie la cohérence SPEC ↔ code candidate ↔ chemin de
   production réel. La prémisse implicite « overlay standard = chemin correct » (sessions #7
   et #8) est RÉFUTÉE et ne doit plus jamais être utilisée.
5. **Recommandation de conception (non bloquante)** : toute candidate crypto devrait porter
   un sizing interne (`sizing_interne: true`). Une candidate crypto en vol brute reste
   recevable en Porte 1, mais ses MaxDD backtest/vécus seront jugés tels quels aux seuils
   §1.2/§2.2/§2.3 — sans « rabais » : c'est l'effet voulu, pas un durcissement de seuil.
6. **Portée toutes classes d'actifs** : la convention (c) se généralise telle quelle aux
   candidates equities/ETF — le runner ne leur applique pas non plus de vol-targeting
   portefeuille réel (`_risk_manager_for_wallet` : `cap_per_asset_equity=1.0`,
   `gross_exposure_max=1.0`, design documenté — le sizing des poches actions/ETF de
   production est borné par construction, top-k équipondéré). Précision d'honnêteté : le
   seul précédent de sizing interne (voie (b)) est crypto (`quasi_passif_crypto`) — ni
   `xs_momentum_sp100` ni `dual_momentum_etf` ne lisent `profile["risque"]` ; une première
   candidate equities/ETF `sizing_interne: true` ne bénéficierait d'aucun précédent audité
   et devra en faire la démonstration complète dans sa propre SPEC.

## 4. Ce que ce document NE fait PAS

- Il ne modifie AUCUNE ligne de `bot/runner.py`, `bot/risk/*`, ni les constantes
  `WALLETS[*]["risque"]`/`CB_*` (`PROMOTION-RULES.md` §4.3 intégralement respecté — le
  50.0 en dur du runner, design documenté, reste tel quel).
- Il ne modifie pas `docs/PROMOTION-RULES.md` (gravé). Une future session de gouvernance
  peut décider d'inscrire la clause du §3 ci-dessus dans §1.4 — proposition versée au
  dossier des amendements en attente (#12, avec #12a/#12b), décision humaine.
- Il ne rejuge aucune candidate passée : les notes rétroactives datées des sessions #7/#8
  au registre suffisent (aucun verdict ne changeait — candidate et contrôle subissaient la
  même distorsion, et les échecs étaient indépendants de l'overlay).

## 5. Traçabilité

- Fiche d'origine : `docs/RESEARCH-BACKLOG.md` #21 (P0, 2026-09-21, close par ce document).
- Finding fondateur : `backtest/results/pairs_ethbtc_ratio_rotation/audit/VERDICT.md` (F1).
- Analyse d'instruction et revue adversariale du présent document : `RESEARCH-LOG.md`
  2026-09-28 (session #9).
- Gardes mécaniques : `bot/tests/test_governance_limits.py` (champ `sizing_interne`
  obligatoire, booléen strict, avec test rouge/vert non tautologique) et
  `backtest/tests/test_sizing_fidelity_lint.py` (lint statique : tout appel à
  `simulate_segment` dans un script `backtest/run_*.py` doit fixer `apply_vol_targeting=`
  explicitement — exemption fermée limitée aux scripts pré-convention archivés).
