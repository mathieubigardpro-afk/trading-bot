# LOWVOL-SP100-SPEC.md — Porte 1 de `lowvol_sp100` (nouvelle idée sourcée en session #11)

*SPEC PRÉ-ENREGISTRÉE (session hebdomadaire #11, 2026-10-19), committée AVANT toute exécution
du backtest, conformément à `docs/PROMOTION-RULES.md` §0/§1, à `docs/SIZING-FIDELITY-SPEC.md`
et à `docs/BAND-FIDELITY-SPEC.md`. Le backlog technique P0/P1 étant épuisé (revue session #10),
cette candidate provient du sourcing de nouvelles idées de la session #11 (littérature
factorielle classique — anomalie « low volatility », Haugen & Baker 1991, Baker, Bradley &
Wurgler 2011 ; fiche ajoutée au backlog dans la même session). Cette spec fige TOUT degré de
liberté avant de regarder le moindre résultat.*

## 1. Candidate

- `id` : `lowvol_sp100` — famille `equities_defensive_low_volatility` (famille structurellement
  NEUVE pour ce projet : aucune entrée du registre n'est une stratégie défensive actions ;
  signal de RISQUE réalisé, pas de momentum, pas de mean-reversion de prix, pas d'overlay
  par-dessus une poche existante).
- Hypothèse : les actions à faible volatilité réalisée ont historiquement un rendement ajusté
  du risque supérieur à celui du marché (anomalie documentée depuis ~35 ans, antérieure et
  postérieure à sa popularisation ~2010). Version testable ici : long-only, bottom-N du S&P100
  par volatilité réalisée, équipondéré, rebalance mensuel.
- Univers : `UNIVERSE_SP100` de `bot/strategies/xs_momentum_sp100.py` (103 titres, constituants
  ACTUELS — biais du survivant documenté comme réserve, jamais caché, même convention que
  `xs_momentum_sp100`). **BK est indisponible dans `market-data`** (7 échecs consécutifs de
  récupération, backlog #20) : univers effectif 102 titres, documenté dans les résultats.
- Signal (à chaque date de décision mensuelle = dernier jour de bourse du mois, poids décidés
  à la clôture t, exécutés à l'open t+1, convention du moteur) :
  1. Éligibilité : ≥ 252 jours de bourse d'historique RÉEL à la date de décision (uniforme
     pour toutes les combinaisons de la grille — l'univers effectif ne dépend jamais du
     paramètre L). Un titre entre dans l'univers quand sa donnée existe (pas de point-in-time
     des constituants — c'est le biais du survivant documenté ci-dessus).
  2. Classement : volatilité réalisée = écart-type des rendements quotidiens (close ajusté)
     sur les L derniers jours de bourse, annualisé (×√252).
  3. Portefeuille : les N titres de PLUS FAIBLE volatilité, poids égaux 1/N. Si moins de N
     titres éligibles (dégénéré, non attendu après 1996) : tous les éligibles équipondérés.
  4. AUCUN filtre de régime (pas de SMA200 SPY, pas de vol gate) : la candidate est toujours
     investie. Zéro filtre additionnel — tout ajout a posteriori serait une retouche interdite.
- Grille PRÉ-ENREGISTRÉE (4 combinaisons, sélection IS par Sharpe via
  `select_params_via_is`, jamais retouchée après coup) :
  `L ∈ {63, 252}` jours × `N ∈ {20, 30}`. Aucune autre valeur ne sera testée.
- `sizing_interne: false` (déclaré ici, SIZING-FIDELITY-SPEC §3.1) ⇒ backtest en
  **`apply_vol_targeting=False`** (vol brute — le chemin de production réel pour une poche
  actions, cf. SIZING-FIDELITY-SPEC §3.2/§3.6).
- `capital_alloc_pct` prévu pour l'incubation (BAND-FIDELITY-SPEC §3.1) : **a = 0,30** du
  wallet labo. Motivation chiffrée a priori : MaxDD de poche attendu de l'ordre de 35-45 %
  (bear markets 2000-02/2008 sans filtre de régime) ⇒ MaxDD wallet ≈ a × MaxDD poche ≈ 11-14 %,
  sous le seuil half-size du labo (15 %) — à VÉRIFIER par le bloc `cb` de l'annexe, jamais
  supposé acquis.

## 2. Protocole

- Moteur : `backtest/engine.py` (moteur commun audité), chargeur quotidien `backtest/data.py`,
  **portage inter-fenêtres ACTIVÉ** (candidate toujours investie — la remise à zéro aux
  frontières de fenêtres serait l'artefact #16 ; fenêtres contiguës, pas 12 mois).
- Données : `market-data` régénérée 2026-10-17 (equities/*.csv.gz + etf/SPY.csv.gz), chemin
  externe `--data-dir`, AUCUNE donnée copiée dans le dépôt, AUCUN appel réseau.
- Walk-forward : 36 mois IS / 12 mois OOS, pas 12 mois (convention actions du projet,
  PROMOTION-RULES §1.1), via `generate_walk_forward_windows`, calendrier borné par la
  disponibilité des données (~30 fenêtres attendues, 1996 → 2026 ; le nombre exact constaté
  est documenté, jamais ajusté).
- Overlay : mêmes réglages que le précédent actions (`run_xsmom_invvol.py`) SAUF
  `apply_vol_targeting=False` (convention sizing ci-dessus) ; `no_trade_band=0.05` (sémantique
  S1 du moteur, BAND-FIDELITY-SPEC §2) ; `periods_per_year=252`.
- Coûts : **5 bps/côté** actions (convention projet, PROMOTION-RULES §1.1) ; benchmark SPY à
  3 bps/côté. Stress de coûts : PF et Sharpe à 3× (15 bps) et 5× (25 bps).
- Benchmark de classe : **SPY buy & hold**, fenêtres OOS alignées (convention du registre pour
  les actions individuelles).
- **Contrôle apparié OBLIGATOIRE** : univers éligible ENTIER équipondéré (aucune sélection),
  mêmes dates de rebalance mensuel, même moteur/overlay/coûts/fenêtres/portage. Il isole
  l'effet de la SÉLECTION low-vol de l'effet « équipondération du S&P100 » (leçon de
  `xs_momentum_invvol_sp100` : l'équipondéré large est un adversaire redoutable). Le contrôle
  compte dans K_total (option conservatrice, précédent sessions #7/#8).
- Métriques : sharpe/sortino/PF/MaxDD/CAGR sur l'OOS concaténé, trades = lignes closes.
- Analyses d'honnêteté OBLIGATOIRES (consignées, jamais critères de sauvetage) :
  - sous-périodes : Sharpe OOS avant 2010-01-01 / depuis 2010 / depuis 2020 (la littérature
    débat d'une érosion de l'anomalie après sa popularisation — la question doit être posée) ;
  - corrélation des rendements OOS candidate vs contrôle et vs SPY ;
  - turnover mensuel moyen (fraction du portefeuille remplacée) — le signal est censé être
    lent, un turnover élevé serait un signal d'alarme sur les coûts ;
  - comparaison candidate vs contrôle sur Sharpe/Sortino/MaxDD + Information Ratio.
- Annexe `band_fidelity` OBLIGATOIRE (BAND-FIDELITY-SPEC §3.2) : run S2 apparié (poids a·W,
  bande 0,05·a, a=0,30, sélection IS NON rejouée, métriques reconverties en unités de poche
  via P = 1 + (E−1)/a), les 5 seuils §1.2 sous S1 ET S2, barres divergentes, flag
  `band_fragile`, bloc `cb` aux seuils du wallet LABO (half-size 15 % / flatten 25 %) avec
  nombre d'épisodes de déclenchement simulés.

## 3. DSR (§1.3)

`K_total = 16 (lignes de RESEARCH-REGISTRY.json à ce jour) + n_fenêtres × 4 (grille candidate)
+ n_fenêtres × 1 (contrôle apparié, compté — option conservatrice pré-enregistrée)`.
Avec 30 fenêtres attendues : K_total = 166 (le chiffre exact suit le nombre de fenêtres
réellement générées). Bailey & López de Prado 2014 sur les rendements quotidiens OOS concaténés
(`backtest/metrics.py:expected_max_sharpe`/`probabilistic_sharpe_ratio`), skew/kurtosis de la
série OOS. Rappel d'honnêteté (#12b) : à n ≈ 7 500 obs. quotidiennes, le seuil DSR ≥ 0,50 est
peu discriminant — le calcul est documenté intégralement, mais aucun argument de « DSR élevé »
ne compensera un autre seuil manqué.

## 4. Seuils de décision (PROMOTION-RULES §1.2 — tous conjoints, AUCUNE exception)

Sharpe OOS net ≥ 0,70 ; PF OOS > 1,15 ; trades OOS clos ≥ 80 ; MaxDD OOS ≤ 1,5× MaxDD OOS
du benchmark SPY aligné ; DSR ≥ 0,50 (K_total §3). Audit adversarial indépendant §1.4
obligatoire (verdict `isSound`), incluant les points d'audit SIZING-FIDELITY §3.4 et
BAND-FIDELITY §3.7.

## 5. Sémantique des issues (figée AVANT exécution)

- **5/5 seuils passés + `isSound: true` + candidate NON dominée par le contrôle** →
  incubation (§1.5) : entrée au registre `en_incubation`, module de production
  `bot/strategies/` écrit avec tests d'équivalence backtest↔production (pattern xsmom),
  ajout à `INCUBATING_STRATEGIES` (a = 0,30, `sizing_interne: false`), limite §4.1 respectée
  (labo vide : 0/3 → OK).
- **5/5 seuils passés MAIS dominée par le contrôle équipondéré sur Sharpe ET Sortino ET
  MaxDD** → `ecartee` (précédent pré-enregistré : `xs_momentum_invvol_sp100` — incuber une
  sélection dominée par « ne rien sélectionner » n'a aucune valeur).
- **Seuils manqués avec Sharpe > 0 et PF > 1** → `ecartee` ; **Sharpe ≤ 0 ou PF ≤ 1** →
  `rejetee` (sémantique des statuts du registre).
- **`isSound: false`** → rejet automatique (§1.4), quel que soit le reste.
- **`band_fragile: true`** → consigné au registre + versé au dossier #12, tranché en session
  selon la pratique d'ambiguïté conservatrice (BAND-FIDELITY-SPEC §3.3) — jamais ignoré.
- Dans TOUS les cas : entrée au registre (append-only), entrée au RESEARCH-LOG, mise à jour
  du backlog. Aucune retouche de paramètres après avoir vu l'OOS ; aucun re-run « pour voir »
  avec d'autres valeurs de grille (§3.3 : toute variante future = nouvel id, nouveau K_total).
