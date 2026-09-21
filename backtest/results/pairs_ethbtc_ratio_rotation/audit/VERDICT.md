# Audit adversarial indépendant — `pairs_ethbtc_ratio_rotation` (2026-09-21, session hebdo #8)

**Verdict : `isSound: false`** — 1 finding CRITIQUE (méthode/infrastructure, PROMOTION-RULES.md
§1.4 « sizing »), 1 MINEUR, 1 INFO. Aucune divergence de reproduction sur aucun chiffre de
décision. Mené par un agent adversarial indépendant sur copie isolée (remote git neutralisé),
scripts d'attaque archivés dans ce dossier.

## Reproduction (axe 1) — PASS

- Signal réimplémenté from scratch (sans importer `backtest/strategies/pairs_ratio.py`) :
  bit-identique sur les 4 combinaisons de la grille.
- Walk-forward : **les 15 fenêtres** reproduites indépendamment (sélection IS + OOS) —
  `chosen_params`, Sharpe, PF, MaxDD, trades bit-exacts ; somme des trades = 38 = officiel.
- DSR réimplémenté from scratch : 0.03196944 = officiel ; K_total = 90 recompté depuis le
  registre (15 lignes) et la formule §1.3.
- Stationnarité : ADF p=0.343721, Engle-Granger p=0.650395, demi-vie 714.36 h — match exact.
- 43 épisodes de tilt recomptés indépendamment (médiane 229 h) — exact.
- Chronologie git : SPEC committée (4c4182e) AVANT le run (e88352a), diff SPEC entre les deux
  commits vide — aucune retouche post-résultats.

## Look-ahead (axe 2) — PASS

Perturbation des closes strictement après t : poids/état/z bit-identiques avant et à t (les 4
combos) ; effet réel après t (test non trivial). Warm-up neutre 50/50 sur `min_periods=L`
vérifié. Alignement signal clôture t → exécution open t+1 confirmé (`engine.py`).

## Sélection (axe 3) — PASS (+1 INFO)

Grille code == SPEC == results.json ; sélection IS standard du moteur, aucune fuite OOS.
**[INFO]** `select_params_via_is` annualise le Sharpe IS d'affichage à √252 au lieu de √8760
(déjà relevé par l'audit de la session #2 — facteur constant, argmax invariant, sélection
vérifiée bit-exacte, aucun chiffre de décision affecté).

## Coûts (axe 4) — PASS

15 bps/côté importés de `bot/config.py` (assert dans le runner), coûts sur turnover réel,
stress 3×/5× avec params figés (pas de re-sélection) : PF 1.261 → 1.144 → 0.981, monotone.

## Sizing (axe 5) — **CRITIQUE (infrastructure production, hors code candidate)**

`bot/runner.py:_risk_manager_for_wallet` construit le RiskManager portefeuille avec
`vol_target_annualized=50.0` codé en dur pour TOUS les wallets, labo 🧪 compris — design
documenté pour ne pas doubler le vol-targeting INTERNE de `quasi_passif_crypto` (correctif
session #3). Conséquence démontrée par exécution (`compute_vol_scalar` : scalar=1.0 à cible
50.0 pour toute vol réaliste 35-120 %, vs 0.23-0.79 à cible 0.275) : une candidate **sans
sizing interne** incubée telle quelle ne recevrait AUCUN vol-targeting réel en production,
alors que le backtest lui applique l'overlay production (exposition moyenne réalisée 0.57 vs
cible brute 1.0). Le backtest est donc PLUS protecteur que ce que l'incubation livrerait —
exactement le mode de défaillance visé par §1.4 (« sizing fictif plus généreux que ce que
`bot/risk/` appliquerait en production »). La prémisse de la SPEC §2 (« chemin overlay
standard correct pour une candidate sans vol-targeting propre », héritée du précédent de la
session #7) n'est pas étayée par le code. Portée rétroactive : la même prémisse sous-tend le
`isSound: true` de `protective_vol_gate_6majors` (session #7) — sans effet sur son verdict
(écartée de toute façon). **Impact sur CETTE candidate : aucun sur le verdict** (candidate et
contrôle subissent la même distorsion, comparaison appariée intacte ; échec 3/5 à larges
marges). → backlog #21, session d'infrastructure dédiée AVANT toute future candidate sans
sizing interne.

Sous-vérification PASS : l'artefact « faible poids vs bande 0.05 » du funding carry ne se
reproduit pas ici (scalar de vol médian 0.61, transitions ≥ 14.5 pts — jamais absorbées).

## Contrôle apparié (axe 6) — PASS

Même `run_walkforward`, mêmes `sim_kwargs`/fenêtres/coûts/carry — seul le `weights_provider`
diffère ; fenêtre 0 reproduite (candidate = contrôle quand aucun tilt, cohérence croisée).

## Honnêteté (axe 7) — PASS (+1 MINEUR)

Épisodes, sous-périodes (1.44 avant 2024 / −0.22 depuis), corrélations (0.976 vs contrôle,
0.721 vs proxy SMA200), stationnarité : tous recalculés, exacts. **[MINEUR]** La réserve
« biais du survivant » exigée par §1.4 n'est pas écrite explicitement dans SPEC/results
(portée pratique quasi nulle sur BTC/ETH 2022-2026 — omission de forme, consignée ici).

## Conséquence (sémantique pré-enregistrée SPEC §9)

`isSound: false` portant sur l'infrastructure commune ⇒ AUCUN re-run dans cette session,
verdict conservateur : **statut `rejetee`** (§1.4 : rejet automatique). Le constat « dominée
par le contrôle 50/50, 3/5 seuils manqués » est par ailleurs reproduit et robuste — le rejet
tiendrait dans toutes les lectures.
