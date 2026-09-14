# SPEC pré-enregistrée — `protective_vol_gate_6majors` (backlog P1#6, « protective put synthétique »)

*Committée AVANT toute exécution du backtest (docs/PROMOTION-RULES.md §0 : les règles sont
figées avant de regarder les résultats qu'elles vont juger). Session hebdomadaire #7,
2026-09-14. Toute déviation de cette SPEC observée a posteriori est un finding d'audit.*

---

## 1. Hypothèse et périmètre

Brique de **réduction de risque de queue** (backlog P1#6) : détenir le panier équipondéré
des 6 majors V1 et se désengager mécaniquement (cible 0) quand la volatilité réalisée du
panier franchit un percentile extrême de sa propre histoire, puis se redéployer
**progressivement** après stabilisation. L'hypothèse n'est PAS une source d'edge
directionnel : c'est que couper la queue gauche (crashs prolongés) coûte moins en régime
normal (whipsaw) qu'elle ne rapporte en crise.

**Distinction obligatoire vs « equity curve trading »** (déjà écarté,
`rapport-recherche.md` §7, repris par la fiche backlog) : le signal est calculé
EXCLUSIVEMENT sur des données de MARCHÉ (rendements de clôture du panier), JAMAIS sur la
courbe d'équity, le PnL ou les positions de la stratégie elle-même. La fonction de signal
ne reçoit que `closes` — vérifiable structurellement.

**Redondance assumée à mesurer, pas à cacher** : la production applique déjà un vol
targeting continu (overlay, cible 27,5 % annualisée, EWMA demi-vie 60 h). La candidate
teste la valeur MARGINALE d'une coupure discrète percentile + redéploiement différé
AU-DELÀ de ce vol targeting (l'overlay reste actif dans le backtest, fidélité production).
D'où le **contrôle apparié** (§5) : panier constant SANS gate, même moteur/overlay/coûts —
la candidate doit battre ce contrôle, pas seulement le B&H brut.

## 2. Univers, données, coûts, moteur

- Univers : `bot.config.SYMBOLS_CRYPTO` = BTC, ETH, SOL, DOGE, LINK, AVAX (6 majors V1),
  vérification défensive à l'import comme `run_vol_breakout.py`.
- Données : `_data/crypto/<SYM>.csv.gz` (branche `market-data` régénérée le 2026-09-12,
  bougies horaires 2022-01-01 → 2026-08-31 + mois courant), chargées via
  `backtest/data_hourly.py` (calendrier = union, ffill borné 3 h).
- Coûts : **25 bps/côté uniforme** (palier le plus défavorable de l'univers — même
  convention pessimiste que `vol_breakout_6majors` et `funding_carry_6majors`).
  Stress : 75 bps (3×) et 125 bps (5×) — profit factor rapporté aux trois niveaux.
- Moteur : `backtest/engine.py` commun EXCLUSIVEMENT, overlay standard horaire
  (`no_trade_band=0.05`, vol targeting production, constantes `HOURLY_*` de
  `risk_overlay.py`), **portage inter-fenêtres ACTIVÉ** (`carry_across_windows=True`,
  CARRY-EXTENSION-SPEC.md, audité isSound=true — standard depuis la session #6).
  `periods_per_year=8760` partout. Signal à la clôture t, exécution à l'open t+1.
- AUCUNE modification du moteur commun, de `bot/risk/`, des breakers ou de la config des
  wallets réels dans cette session de jugement (§0, §4.3).

## 3. Signal (candidate), entièrement causal

À chaque heure t (calculé une seule fois sur le calendrier complet, indexé par position —
même approche que `run_vol_breakout.py`) :

1. Rendement du panier : `r_t` = moyenne équipondérée des `pct_change` des closes des 6
   symboles à t.
2. Vol réalisée : écart-type glissant de `r` sur **V heures** (V ∈ grille), fenêtre pleine
   requise (`min_periods=V`).
3. Rang percentile : fraction des valeurs de vol des **4 320 dernières heures (180 j)**
   strictement inférieures ou égales à la vol courante. Historique minimal requis :
   **2 160 observations de vol valides (90 j)** ; en deçà (warm-up), état = investi
   (protection inactive — on ne prétend pas protéger sans historique ; le warm-up est
   entièrement consommé avant la première fenêtre OOS, 9 mois d'IS).
4. Machine à états (état initial : investi, facteur de déploiement `g=1`) :
   - investi, percentile_t ≥ **p_in** (grille) ⇒ état protégé, `g` cible 0 dès t+1 (pas de
     rampe à la coupure : la protection est immédiate par construction) ;
   - protégé, percentile_t ≤ **p_out = 0,80** (fixé) ⇒ état redéploiement : `g` remonte
     LINÉAIREMENT de 0 à 1 en **R = 72 h** (fixé), incrément 1/72 par heure ;
   - en redéploiement, percentile_t ≥ p_in ⇒ retour immédiat à protégé (`g=0`).
5. Poids décidés : `w_t(sym) = g_t / 6` pour chacun des 6 symboles (long-only, jamais de
   sizing interne — l'overlay production fait le vol targeting, cf. §1 redondance).

## 4. Grille pré-enregistrée (4 combinaisons, RIEN d'autre)

| Paramètre | Valeurs |
|---|---|
| V (fenêtre de vol, heures) | **{24, 72}** |
| p_in (percentile de coupure) | **{0,95, 0,98}** |

Fixés (zéro degré de liberté supplémentaire) : `W_pct = 4320 h`, `p_out = 0,80`,
`R = 72 h`, historique minimal 2 160 h. Sélection IS : critère par défaut du moteur
(`select_params_via_is`), jamais en regardant l'OOS.

## 5. Contrôle apparié (obligatoire, pré-enregistré)

Run de contrôle : poids CONSTANTS `1/6` par symbole (g≡1, aucune gate), même moteur, même
overlay, mêmes coûts, mêmes fenêtres, 1 seule combinaison. Il isole l'effet marginal de la
gate de tout le reste du pipeline (overlay + coûts + bande). Précédent : contrôle
equal-weight de `xs_momentum_invvol_sp100` (session #1) — une candidate DOMINÉE par son
contrôle trivial sur les mêmes fenêtres n'est pas incubée, quels que soient ses seuils.

## 6. Walk-forward, benchmark, seuils

- Fenêtres : **9 mois IS / 3 mois OOS, pas 3 mois**, horaire, sur tout le calendrier
  disponible (2022-01 → 2026-08). Nombre de fenêtres déterminé par le calendrier (attendu
  ~15-16), jamais choisi en regardant les résultats.
- Benchmark : **buy & hold équipondéré des 6 majors** (sans coûts ni overlay — convention
  du registre pour cette classe), aligné sur l'OOS concaténé (`*_OOS_ALIGNED`).
- Verdict Porte 1 §1.2 sur l'OOS concaténé net de coûts, 5 seuils habituels :
  Sharpe ≥ 0,70 ; PF > 1,15 ; trades clos ≥ 80 ; MaxDD ≤ 1,5× benchmark aligné ;
  DSR ≥ 0,50.
- **K_total (§1.3), formule figée** : `K_total = 14 (lignes de RESEARCH-REGISTRY.json au
  2026-09-14) + n_fenêtres × 4 (candidate) + n_fenêtres × 1 (contrôle)`. Le contrôle est
  compté comme essais à part entière (conservateur : c'est une variante regardée). Avec 15
  fenêtres : K_total = 89. DSR : Bailey & López de Prado 2014 via `backtest/metrics.py`,
  sur les rendements horaires OOS concaténés de la candidate.

## 7. Analyses d'honnêteté (toutes pré-enregistrées, aucune n'est un critère de décision sauf mention)

1. **Whipsaw explicite** (exigence de la fiche backlog) : liste des épisodes de protection
   OOS distincts (de la coupure exécutée au redéploiement complet) ; pour chacun, rendement
   du panier B&H sur l'épisode = ce que la gate a évité (négatif = vraie protection,
   positif = faux signal / coût d'opportunité) ; tableau évités-vs-ratés + somme nette +
   nombre d'épisodes (des trades OOS nombreux issus de peu d'épisodes ne sont pas des
   observations indépendantes).
2. Sous-périodes : Sharpe OOS avant / depuis 2024-01-01 (précédent : tout l'edge de
   vol_breakout et du quasi-passif venait d'une seule sous-période).
3. Corrélation des rendements horaires OOS : candidate vs contrôle, et candidate vs proxy
   quasi-passif (panier long/flat SMA200 — redondance avec la brique déjà en production ;
   corr > 0,7-0,8 = intérêt marginal faible, cf. fiche backlog #2).
4. Comparaison appariée candidate vs contrôle sur les MÊMES fenêtres : Sharpe, Sortino,
   MaxDD, Information Ratio. **Critère de décision** (§8) : domination.
5. MaxDD : candidate vs contrôle vs benchmark (la promesse de la brique est LÀ — à
   documenter même si la Porte 1 échoue).

## 8. Sémantique des issues (FIGÉE avant exécution)

- **Tous les seuils §1.2 passent + audit adversarial indépendant `isSound: true` + la
  candidate n'est PAS dominée par le contrôle** (dominée = Sharpe ET Sortino ET MaxDD tous
  moins bons sur l'OOS apparié) ⇒ proposition d'incubation : entrée
  `INCUBATING_STRATEGIES` (labo 🧪), `capital_alloc_pct = 0.30`, params gelés = la
  combinaison la plus souvent sélectionnée en IS (égalité tranchée par p_in le plus bas
  puis V le plus court — règle déterministe fixée ici), statut `en_incubation` au registre.
- **Un seuil §1.2 manqué, ou domination par le contrôle** ⇒ pas d'incubation : statut
  `ecartee` (Sharpe positif) ou `rejetee` (Sharpe négatif, PF < 1, ou `isSound: false`),
  conventions du registre ; entrée registre + log, cimetière sans état d'âme.
- **`isSound: false` portant sur le code PROPRE à la candidate** (runner/stratégie) ⇒ un
  (1) correctif + re-run autorisé dans la session, documenté (précédent : session #3).
  **`isSound: false` portant sur le moteur COMMUN** ⇒ aucun re-run, verdict conservateur,
  backlog (précédent : session #5). Jamais d'amendement du moteur après avoir vu un
  résultat (§0).
- **Observation « MaxDD fortement réduit mais Porte 1 échouée »** ⇒ note de gouvernance
  pour l'amendement #12 (PROMOTION-RULES n'a pas de voie pour une brique de réduction de
  risque) — JAMAIS une incubation de contournement dans cette session.
- Un futur re-test de l'idée = nouvel id, nouvelle ligne K_total (§3.3).

## 9. Attendu honnête, écrit AVANT le run

Échec probable sur Sharpe ≥ 0,70 : (a) toutes les surcouches de timing testées sur ce
panier ont fini sous le B&H (donchian 0,31, ema 0,24, vol_breakout 0,43, quasi-passif
équilibré 0,28 au retest) ; (b) un percentile de vol se déclenche APRÈS le début du crash
et le redéploiement à 72 h rate les rebonds en V fréquents en crypto ; (c) le vol
targeting continu de l'overlay capte déjà une partie de l'effet — l'effet marginal
mesurable de la gate en est réduit d'autant. La valeur attendue de la session est surtout
l'analyse §7.5 (réduction de MaxDD) comme intrant de gouvernance #12, et l'ajout d'une
famille « tail protection » au cimetière si elle échoue — pour que le K_total des
suivantes reste honnête.
