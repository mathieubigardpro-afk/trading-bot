# SPEC pré-enregistrée — `pairs_ethbtc_ratio_rotation` (backlog P1#5, version dégradée long-only)

*Committée AVANT toute exécution du backtest (docs/PROMOTION-RULES.md §0 : les règles sont
figées avant de regarder les résultats qu'elles vont juger). Session hebdomadaire #8,
2026-09-21. Toute déviation de cette SPEC observée a posteriori est un finding d'audit.*

---

## 1. Hypothèse et périmètre

Backlog P1#5 : le ratio ETH/BTC connaît des phases de rotation (dominance BTC montante /
descendante) potentiellement exploitables par un signal de **retour à la moyenne sur le
RATIO** — structurellement différent du mean reversion RSI2 déjà rejeté (prix absolus).
Version **dégradée long-only** pré-identifiée par la fiche : rotation d'allocation entre BTC
et ETH selon le signal de ratio, jamais de short — la version complète (long ETH / short BTC,
marché-neutre) attend l'extension `bot/sim/` (#17, P2). Cette version capture moins bien
l'hypothèse de fond (le bêta crypto directionnel reste porté en permanence) — assumé et
documenté d'avance, pas découvert après coup.

**Pré-requis de la fiche backlog (obligatoire, AVANT tout signal)** : test de
cointégration/stationnarité du ratio — jamais supposée implicitement. Cf. §5 : les tests
sont exécutés et consignés AVANT le walk-forward, leur rôle décisionnel est fixé au §5
(diagnostic d'honnêteté pré-enregistré, les seuils Porte 1 §1.2 restent seuls juges du
verdict — aucun critère ne s'ajoute ni ne se retire après coup).

## 2. Univers, données, coûts, moteur

- Univers : **BTC, ETH** (2 actifs, tous deux `COST_TIER_MAJORS`).
- Données : `_data/crypto/{BTC,ETH}.csv.gz` (branche `market-data` régénérée le
  **2026-09-19**, bougies horaires 2022-01-01 → 2026-08-31 + complément mois courant),
  chargées via `backtest/data_hourly.py` (calendrier = union, ffill borné 3 h).
- Coûts : **15 bps/côté** (palier majors : 10 taker + 5 slippage, importé de
  `bot/config.py` — jamais recopié en dur). Stress : 45 bps (3×) et 75 bps (5×), profit
  factor rapporté aux trois niveaux.
- Moteur : `backtest/engine.py` commun EXCLUSIVEMENT, overlay standard horaire
  (`no_trade_band=0.05`, vol targeting production, constantes `HOURLY_*` de
  `risk_overlay.py`), **portage inter-fenêtres ACTIVÉ** (`carry_across_windows=True`,
  standard depuis la session #6), `periods_per_year=8760`. Signal à la clôture t,
  exécution à l'open t+1. La candidate n'a AUCUN sizing interne (précédent quasi-passif :
  le chemin overlay standard est le bon pour une candidate sans vol-targeting propre).
- AUCUNE modification du moteur commun, de `bot/risk/`, des breakers ou de la config des
  wallets réels dans cette session de jugement (§0, §4.3).

## 3. Signal (candidate), entièrement causal

À chaque heure t (calculé une seule fois sur le calendrier complet, indexé par position) :

1. `x_t = ln(close_ETH,t / close_BTC,t)` (log-ratio).
2. `z_t = (x_t − SMA_L(x)_t) / STD_L(x)_t`, fenêtre glissante de **L heures** (L ∈ grille),
   fenêtre pleine requise (`min_periods = L`) ; `STD` échantillon (ddof=1) ; si
   `STD_L = 0` ⇒ z traité comme 0 (aucune position tiltée sur une vol nulle).
3. Machine à états (état initial et warm-up : **NEUTRE = 50/50**) :
   - NEUTRE, `z_t ≥ +θ` (ratio ETH/BTC anormalement HAUT) ⇒ état **TILT_BTC** :
     `w = {BTC: 1, ETH: 0}` dès t+1 ;
   - NEUTRE, `z_t ≤ −θ` (ratio anormalement BAS) ⇒ état **TILT_ETH** :
     `w = {BTC: 0, ETH: 1}` dès t+1 ;
   - TILT_BTC ou TILT_ETH, `|z_t| ≤ θ/2` (hystérésis FIXE, convention
     `funding_carry_6majors`) ⇒ retour NEUTRE 50/50 ;
   - TILT_BTC, `z_t ≤ −θ` ⇒ bascule directe TILT_ETH (et symétriquement) — cas rare,
     défini d'avance pour éviter toute ambiguïté.
4. Poids décidés : NEUTRE = `{BTC: 0.5, ETH: 0.5}` ; tilt = rotation TOTALE vers l'actif
   « bon marché » relatif (zéro paramètre d'intensité de tilt — degré de liberté
   volontairement non ouvert). Long-only, toujours investi à 100 % avant overlay.

Le signal ne lit QUE les closes des 2 symboles — jamais l'équity, le PnL ou les positions
de la stratégie (garde structurelle habituelle contre l'« equity curve trading »).

## 4. Grille pré-enregistrée (4 combinaisons, RIEN d'autre)

| Paramètre | Valeurs |
|---|---|
| L (fenêtre du z-score, heures) | **{720, 2160}** (30 j, 90 j) |
| θ (seuil d'entrée en tilt, z-score) | **{1,5, 2,0}** |

Fixés (zéro degré de liberté supplémentaire) : hystérésis de sortie θ/2, tilt 100/0,
base 50/50, `min_periods = L`. Sélection IS : critère par défaut du moteur
(`select_params_via_is`), jamais en regardant l'OOS.

## 5. Tests de stationnarité/cointégration (pré-requis fiche, rôle FIGÉ ici)

Exécutés et consignés dans `results.json` AVANT le walk-forward :
1. **ADF** (statsmodels, `maxlag` auto AIC, régression `c`) sur le log-ratio `x` :
   (a) sur la période PRÉ-OOS complète (tout ce qui précède le début de la première
   fenêtre OOS) ; (b) sur chaque fenêtre IS (diagnostic par fenêtre).
2. **Engle-Granger** (`statsmodels.coint`, log-prix ETH vs BTC) sur la période pré-OOS.
3. **Demi-vie de retour à la moyenne** (AR(1) OLS sur Δx vs x retardé, période pré-OOS) —
   à comparer à L : une demi-vie >> L rend le z-score structurellement mal calibré.

**Rôle décisionnel (figé)** : ces tests sont un DIAGNOSTIC d'honnêteté — ils ne
remplacent ni n'assouplissent aucun seuil §1.2. S'ils concluent à la non-stationnarité
(ADF p > 0,10 pré-OOS), l'attendu honnête §9 (échec probable) est renforcé et le résultat
est interprété comme « hypothèse de fond non soutenue par les données » dans le registre —
mais le verdict chiffré reste intégralement celui de la Porte 1. Aucun re-choix de fenêtre,
d'univers ou de grille n'est autorisé après lecture de ces tests (ils précèdent le
walk-forward mais SUIVENT le gel de cette SPEC).

## 6. Contrôle apparié (obligatoire, pré-enregistré)

Run de contrôle : poids CONSTANTS `{BTC: 0.5, ETH: 0.5}` (aucun signal), même moteur, même
overlay, mêmes coûts, mêmes fenêtres, 1 seule combinaison. Il isole l'effet marginal de la
rotation de tout le reste du pipeline. Précédents : sessions #1 et #7 — une candidate
DOMINÉE par son contrôle trivial sur les mêmes fenêtres n'est pas incubée, quels que
soient ses seuils.

## 7. Walk-forward, benchmark, seuils

- Fenêtres : **9 mois IS / 3 mois OOS, pas 3 mois**, horaire, sur tout le calendrier
  disponible (2022-01 → 2026-08). Nombre de fenêtres déterminé par le calendrier (attendu
  ~15), jamais choisi en regardant les résultats.
- Benchmark : **buy & hold équipondéré BTC+ETH** (sans coûts ni overlay — convention du
  registre pour la classe crypto), aligné sur l'OOS concaténé (`*_OOS_ALIGNED`).
- Verdict Porte 1 §1.2 sur l'OOS concaténé net de coûts, 5 seuils habituels :
  Sharpe ≥ 0,70 ; PF > 1,15 ; trades clos ≥ 80 ; MaxDD ≤ 1,5× benchmark aligné ;
  DSR ≥ 0,50.
- **K_total (§1.3), formule figée** : `K_total = 15 (lignes de RESEARCH-REGISTRY.json au
  2026-09-21) + n_fenêtres × 4 (candidate) + n_fenêtres × 1 (contrôle)`. Avec 15
  fenêtres : K_total = 90. DSR : Bailey & López de Prado 2014 via `backtest/metrics.py`,
  sur les rendements horaires OOS concaténés de la candidate.

## 8. Analyses d'honnêteté (pré-enregistrées ; seule la domination §6 est un critère)

1. **Épisodes de divergence indépendants** (risque n°1 de la fiche) : nombre d'épisodes de
   tilt OOS distincts (entrée → retour neutre), durée médiane, PnL relatif par épisode
   (rendement candidate − rendement contrôle sur l'épisode). Beaucoup de trades issus de
   peu d'épisodes ≠ observations indépendantes.
2. Sous-périodes : Sharpe OOS avant / depuis 2024-01-01 (motif de rupture récurrent des
   candidates crypto précédentes).
3. Corrélation des rendements horaires OOS : candidate vs contrôle, et candidate vs proxy
   quasi-passif SMA200 sur le même panier (redondance avec la brique déjà en production).
4. Comparaison appariée candidate vs contrôle sur les MÊMES fenêtres : Sharpe, Sortino,
   MaxDD, Information Ratio. **Critère de décision** (§9) : domination.
5. Stationnarité §5 : synthèse pré-OOS + par fenêtre IS.

## 9. Sémantique des issues (FIGÉE avant exécution)

- **Tous les seuils §1.2 passent + audit adversarial indépendant `isSound: true` + la
  candidate n'est PAS dominée par le contrôle** (dominée = Sharpe ET Sortino ET MaxDD tous
  moins bons sur l'OOS apparié) ⇒ proposition d'incubation : entrée
  `INCUBATING_STRATEGIES` (labo 🧪), `capital_alloc_pct = 0.30`, params gelés = la
  combinaison la plus souvent sélectionnée en IS (égalité tranchée par θ le plus HAUT puis
  L le plus LONG — le plus conservateur : moins de trades, signal plus lent), statut
  `en_incubation` au registre.
- **Un seuil §1.2 manqué, ou domination par le contrôle** ⇒ pas d'incubation : statut
  `ecartee` (Sharpe positif) ou `rejetee` (Sharpe négatif, PF < 1, ou `isSound: false`),
  conventions du registre ; entrée registre + log, cimetière sans état d'âme.
- **`isSound: false` portant sur le code PROPRE à la candidate** (runner/stratégie) ⇒ un
  (1) correctif + re-run autorisé dans la session, documenté (précédent : session #3).
  **`isSound: false` portant sur le moteur COMMUN** ⇒ aucun re-run, verdict conservateur,
  backlog (précédent : session #5). Jamais d'amendement du moteur après avoir vu un
  résultat (§0).
- Un futur re-test de l'idée (y compris la version marché-neutre complète après #17) =
  nouvel id, nouvelle ligne K_total (§3.3).

## 10. Attendu honnête, écrit AVANT le run

Échec probable. (a) Le ratio ETH/BTC a fortement TENDU à la baisse sur 2022-2026
(sous-performance structurelle d'ETH) — un signal de retour à la moyenne sur un ratio
tendanciel achète mécaniquement le perdant relatif ; les tests §5 devraient le montrer
(non-stationnarité probable sur la période pré-OOS). (b) Avec 2 actifs et L de 30-90 j, le
nombre d'épisodes de divergence indépendants sur ~4 ans est structurellement faible
(risque n°2 de la fiche). (c) Toutes les surcouches de timing crypto testées ont fini sous
leur B&H (donchian 0,31, ema 0,24, vol_breakout 0,43, gate 0,08). La valeur attendue de la
session est la CLÔTURE PROPRE de la famille « pairs/ratio long-only » au cimetière (K_total
honnête pour les suivantes) + les chiffres de stationnarité comme intrant pour décider si
la version marché-neutre complète (#17) mérite un jour l'investissement d'infrastructure.
