# SEASONALITY-BTC-SPEC.md — Porte 1 de `btc_seasonality_2123utc` (backlog P1#4)

*SPEC PRÉ-ENREGISTRÉE (session hebdomadaire #6, 2026-09-07), committée AVANT toute exécution
du backtest, conformément à `docs/PROMOTION-RULES.md` §0/§1. Idée : backlog P1#4 — effet de
saisonnalité horaire BTC sur la fenêtre 21h-23h UTC rapporté par la littérature externe
(Quantpedia, études ≤ 2023, cf. `rapport-recherche.md` §7), jamais validé 2024-2026. Cette spec
fige TOUT degré de liberté avant de regarder le moindre résultat.*

## 1. Candidate

- `id` : `btc_seasonality_2123utc` — famille `crypto_calendar_seasonality`.
- Univers : **BTC seul** (la littérature source porte sur BTC ; aucun autre symbole testé).
- Signal (zéro paramètre libre) : être LONG BTC pendant les bougies horaires ouvrant à
  21h et 22h UTC, flat tout le reste du temps. Concrètement, sur la convention du moteur
  (poids décidés à la clôture de la bougie i, exécutés à l'open de la bougie i+1) :
  `poids_brut(bougie i) = 1.0` si `heure_UTC(open(i+1)) ∈ {21, 22}`, sinon `0.0` —
  soit décidé aux clôtures des bougies 20h et 21h, sorti à l'open de la bougie 23h.
- **INTERDIT ABSOLU (backlog #4)** : aucune re-recherche de la « meilleure » fenêtre horaire,
  aucune variante d'heure, aucun filtre additionnel (pas de SMA, pas de vol filter) — la
  fenêtre 21h-23h UTC vient de la littérature EXTERNE, c'est le seul test autorisé, sous
  peine de doubler le biais de sélection (une fois dans l'étude source, une fois ici).
- Grille de paramètres : **AUCUNE** (1 seule combinaison, zéro degré de liberté). La sélection
  IS `select_params_via_is` reçoit la combinaison unique (is_sharpe=NaN documenté).

## 2. Protocole

- Moteur : `backtest/engine.py` (moteur commun, incluant l'extension carry auditée
  `isSound: true` le 2026-09-07). **Mode de décision pré-enregistré : portage inter-fenêtres
  ACTIVÉ** (fidélité production maximale). Attendu structurel : aucune position ne traverse
  jamais une frontière de fenêtre (positions tenues 2 h, frontières en début de mois civil) —
  le runner VÉRIFIE et journalise que `carry_out` est bien vide/plat à chaque frontière ; un
  portage non vide serait une anomalie à investiguer, pas un choix.
- Données : `_data/crypto/BTC.csv.gz` (branche `market-data` régénérée 2026-08-24), chargeur
  horaire commun `backtest/data_hourly.py`. Calendrier : du début des données 2022 au dernier
  timestamp disponible (~2026-08-24), fixé AVANT exécution, aucune exclusion.
- Walk-forward : 9 mois IS / 3 mois OOS, pas 3 mois (convention crypto horaire du projet,
  PROMOTION-RULES §1.1) via `generate_walk_forward_windows`. Fenêtres attendues : ~15 (OOS
  2022-10-01 → 2026-06-30) — le nombre exact constaté est documenté, jamais ajusté.
- Surcouche de risque : overlay standard du moteur, calibrage horaire identique au précédent
  `vol_breakout_6majors` (`periods_per_year=8760`, mêmes paramètres de vol EWMA horaires,
  `no_trade_band=0.05`, vol targeting `VOL_TARGET_ANNUALIZED=0.275`) — la candidate n'a AUCUN
  sizing interne, le chemin de production applicable est l'overlay générique du wallet
  (note session #3 : la neutralisation de l'overlay ne concerne que les candidates à
  vol-targeting interne, ce qui n'est pas le cas ici).
- Coûts : palier « majors » de `bot/config.py` = 10 + 5 = **15 bps/côté**, appliqués
  intégralement (§1.1). Stress de coûts : PF à 3× (45 bps) et 5× (75 bps).
- Benchmark : buy & hold BTC (équipondéré d'un univers de 1), fenêtres OOS alignées
  (convention du registre).
- Métriques : `sharpe/sortino/PF/MaxDD/CAGR` sur l'OOS concaténé, `periods_per_year=8760`
  partout ; trades = lignes closes (`n_trades_closed`).
- Sous-périodes OBLIGATOIRES (backlog #4) : Sharpe OOS avant 2024-01-01 vs depuis — l'étude
  source est ≤ 2023, la question posée est précisément la survie de l'effet 2024-2026.
  La décision Porte 1 reste sur l'OOS TOTAL (§1.2) ; la sous-période est une analyse
  d'honnêteté consignée, pas un critère de sauvetage ni d'élimination supplémentaire.

## 3. DSR (§1.3)

`K_total = 13 (lignes de RESEARCH-REGISTRY.json à ce jour) + n_fenêtres × 1 combinaison`.
Avec 15 fenêtres attendues : K_total = 28 (le chiffre exact suit le nombre de fenêtres
réellement générées). Bailey & López de Prado 2014 sur les rendements horaires OOS concaténés
(`backtest/metrics.py:expected_max_sharpe/probabilistic_sharpe_ratio`), skew/kurtosis mesurés.

## 4. Seuils de décision (PROMOTION-RULES §1.2 — tous conjoints, AUCUNE exception)

Sharpe OOS net ≥ 0,70 ET PF OOS > 1,15 ET trades OOS clos ≥ 80 ET MaxDD OOS ≤ 1,5× MaxDD
benchmark OOS aligné ET DSR ≥ 0,50. Audit adversarial indépendant obligatoire (§1.4),
`isSound: false` ⇒ rejet automatique quels que soient les chiffres.

## 5. Sémantique des issues (figée AVANT exécution)

- ÉCHEC d'un seuil quelconque ou `isSound: false` ⇒ statut `rejetee` (ou `ecartee` si tous
  les seuils passent mais sous le benchmark B&H aligné — précédent `vol_breakout`), entrée
  au registre, log, backlog mis à jour. AUCUN re-run, AUCUNE variante dans cette session
  (§0 : pas de retouche après avoir vu le résultat). L'attendu honnête est un échec sur les
  coûts (2 round-trips/jour × 15 bps/côté ≈ hurdle annualisé de plusieurs dizaines de %) —
  l'écrire ici AVANT le run engage la session à ne pas « sauver » la candidate en bidouillant.
- SUCCÈS de tous les seuils ET `isSound: true` ET Sharpe/MaxDD non dominés par le B&H BTC
  aligné ⇒ Porte 1 passée : entrée en incubation (§1.5) — statut `en_incubation`, ajout à
  `INCUBATING_STRATEGIES` (labo vide : 0/3, place disponible), params gelés.
  ATTENTION : une stratégie long-BTC-2h/jour est corrélée au B&H BTC et couverte par la poche
  quasi-passive — si elle passe les seuils SANS battre le B&H BTC OOS aligné (Sharpe), le
  précédent `ecartee` (valeur marginale nulle vs incumbent, session #1) s'applique.
- Dans TOUS les cas : le nombre de fenêtres, K_total, le DSR et le verdict par seuil sont
  consignés au registre (append-only) et dans RESEARCH-LOG.md.
