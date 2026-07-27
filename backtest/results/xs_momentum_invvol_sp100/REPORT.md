# Rapport de recherche — `xs_momentum_sp100` en pondération inverse-volatilité

*Candidate testée : `docs/RESEARCH-BACKLOG.md` idée #3 (P0). Moteur utilisé :
`backtest/engine.py` (moteur commun du projet, cf. `docs/PROMOTION-RULES.md` §1.1 — ce moteur
n'existait pas dans le dépôt avant cette session, c'était le finding critique d'audit qui motive
la Partie A de cette mission). Résultats bruts : `backtest/results/xs_momentum_invvol_sp100/
results.json`. Tests unitaires du moteur : `backtest/tests/test_engine.py` (7/7 verts).*

**Ce document n'est PAS l'audit adversarial indépendant requis par `docs/PROMOTION-RULES.md`
§1.4 avant toute entrée en incubation — c'est le backtest walk-forward lui-même (Porte 1, §1.1 à
§1.3). L'audit §1.4, par construction, doit être mené par une session distincte de celle qui a
produit ce backtest.**

---

## 1. Résumé exécutif

| | Sharpe OOS | Sortino OOS | Profit factor OOS | MaxDD OOS | CAGR OOS | Trades clos OOS |
|---|---|---|---|---|---|---|
| **`equal` (contrôle, = production)** | 1,034 | 1,485 | 1,821 | 48,68 % | 20,56 % | 1970 |
| **`inv_vol` (candidate)** | 0,947 | 1,349 | 1,718 | 49,27 % | 17,07 % | 1970 |
| **Benchmark SPY buy & hold** | 0,608 | 0,863 | n/a* | 54,84 % | 10,35 % | 0 |

*mesuré sur 30 fenêtres walk-forward (36 mois IS / 12 mois OOS / pas 12 mois), 1996-01-29 →
2026-01-28, équity OOS concaténée, coûts 5 bps/côté (actions) / 3 bps/côté (SPY).*
*n/a : un buy & hold n'a, par construction, aucune position fermée — le profit factor n'est pas
une statistique pertinente pour ce benchmark (cf. §5).*

**Verdict Porte 1 (`docs/PROMOTION-RULES.md` §1.2)** : **tous les seuils chiffrés sont atteints**
pour la candidate `inv_vol` (détail §4). Mais **la candidate n'apporte AUCUN gain mesurable par
rapport au réglage de production déjà actif** (`equal`) — elle fait même très légèrement
**moins bien** sur Sharpe, Sortino et MaxDD, avec un ratio d'information négatif (-0,84) contre
`equal`. C'est exactement le risque que `docs/RESEARCH-BACKLOG.md` idée #3 identifiait par
avance ("s'assurer que le gain de Sharpe... ne vient pas simplement d'une réduction mécanique de
la vol... comparer aussi le Sortino et le ratio d'information") : le contrôle a posteriori
infirme l'hypothèse d'amélioration incrémentale.

---

## 2. Ce qui a été construit (Partie A — moteur commun)

`backtest/` n'existait pas avant cette session — absence confirmée constituant un finding
critique d'audit (`docs/PROMOTION-RULES.md` §1.1 l'exige explicitement, "jamais un script ad hoc
parallèle" ; les métriques historiques de `xs_momentum_sp100` proviennent d'un script externe
`bt-final/xs-momentum-sp100/`, absent du dépôt et donc non ré-exécutable ni auditable ici).

| Fichier | Rôle |
|---|---|
| `backtest/data.py` | Chargement CSV gz, calendrier canonique (SPY), alignement (sans backfill). |
| `backtest/metrics.py` | Sharpe, Sortino, profit factor, MaxDD, CAGR, exposition, ratio d'information, DSR/PSR (Bailey & López de Prado 2014). |
| `backtest/engine.py` | Simulation de portefeuille (signal à la clôture `t`, exécution à l'ouverture `t+1`, coûts bps/côté sur turnover réel), fenêtres walk-forward IS/OOS, sélection de paramètres IS-only, concaténation OOS. |
| `backtest/strategies/xsmom.py` | Version backtest, vectorisée, de `bot/strategies/xs_momentum_sp100.py` (mêmes règles, réutilise les constantes SPEC par import — jamais dupliquées), + paramètre `weighting`. |
| `backtest/tests/test_engine.py` | 4 garanties testées : anti-look-ahead, coûts proportionnels au turnover, bornes des fenêtres walk-forward, cas limites du DSR. **7/7 tests passent** (`pytest backtest/tests/`). |
| `backtest/run_xsmom_invvol.py` | Script d'exécution de la Partie B (ce rapport). |

Principes non négociables implémentés (détail dans les docstrings des modules) :
- **Aucun look-ahead** : `weights_decided.loc[t]` n'utilise que des clôtures `<= t` ; exécution
  systématique à `open[t+1]`. Testé explicitement (`test_lookahead_cheat_collapses_...`) : un
  signal construit pour "voir" le rendement de demain produit un Sharpe artificiel > 8 sous une
  exécution biaisée (même prix pour décider et exécuter), et s'effondre à un Sharpe non
  significatif (< 1,5 en valeur absolue) sous l'exécution correctement décalée du moteur.
- **Coûts bps/côté sur turnover réel**, achats ET ventes, attribués au niveau de chaque ligne
  (prix d'achat majoré, prix de vente net du coût) pour que le profit factor par trade soit
  correctement sensible au coût (cf. stress test §4).
- **Convention "trade" vs "évènement de réalisation"** (`n_trades_closed` vs profit factor) :
  documentée en détail dans `backtest/engine.py` — une ligne compte comme "trade clos"
  uniquement à son retour à zéro ; les réductions partielles réalisent un PnL qui alimente le
  profit factor sans incrémenter `n_trades_closed` (cf. mission, "comptent proportionnellement").
- **Walk-forward 36m IS / 12m OOS**, fenêtres non chevauchantes, sélection de paramètres
  (si grille) strictement IS-only.

---

## 3. Ce qui a été testé (Partie B)

- **Univers** : les 103 tickers `UNIVERSE_SP100` (importés depuis
  `bot.strategies.xs_momentum_sp100`, jamais retranscrits) + SPY (filtre de régime et
  benchmark).
- **Période disponible** : 1993-01-29 → 2026-07-24 (8428 jours de bourse), bornée par le début de
  l'historique SPY. **30 fenêtres complètes** walk-forward générées (vs 27 dans le backtest
  historique — 3 fenêtres supplémentaires couvrant 2023-2026, données plus récentes).
- **Paramètres** : réglages de production FIGÉS (`top_k=10`, `skip=21j`, `lookback=126j`,
  régime SPY>SMA200 réévalué chaque jour, gel mensuel au dernier jour de bourse du mois) +
  `vol_lookback_days=63` (documenté par le SPEC, jamais utilisé en production). **Aucune
  grille** : l'IS de chaque fenêtre n'est donc jamais utilisé pour sélectionner un paramètre
  (`is_selection_used: false` dans `results.json`) — choix délibérément conservateur, zéro degré
  de liberté nouveau créé par ce test.
- **Coûts** : 5 bps/côté (actions, convention du projet) pour `equal`/`inv_vol` ; 3 bps/côté
  (ETF liquide) pour le benchmark SPY.
- **Deux variantes** exécutées sur les MÊMES fenêtres : `equal` (contrôle = réglage de
  production) et `inv_vol` (candidate).

---

## 4. Seuils `docs/PROMOTION-RULES.md` §1.2 — verdict candidate `inv_vol`

| Critère | Seuil | Valeur obtenue | Verdict |
|---|---|---|---|
| Sharpe OOS net de coûts | ≥ 0,70 | **0,947** | ✅ PASS |
| Profit factor OOS | > 1,15 | **1,718** | ✅ PASS |
| Trades OOS clos | ≥ 80 | **1970** | ✅ PASS |
| MaxDD OOS ≤ 1,5× MaxDD benchmark OOS aligné | ratio ≤ 1,5 | **0,898** (49,27 % / 54,84 %) | ✅ PASS |
| DSR (K_total = 39) | ≥ 0,50 | **0,9983** | ✅ PASS |

**`promotion_rules_1_2_all_pass: true`** dans `results.json`.

`K_total = 39` (CORRIGÉ 2026-07-27, chantier 2 — formule §1.3 fixée pour multiplier par le
nombre de fenêtres walk-forward, pas seulement compter les combinaisons de grille) : 9 lignes
de `docs/RESEARCH-REGISTRY.json` au moment de ce test + (30 fenêtres walk-forward × 1 seule
combinaison interne `inv_vol` — la variante `equal` est un **contrôle** de reproduction du
réglage de production déjà existant, pas une combinaison candidate additionnelle, elle n'ajoute
donc pas au compte de K_total) = 9 + 30 = 39. Ancienne valeur (`K_total = 10`, DSR = 0,9998)
calculée avec la formule pré-correctif, désormais fausse au sens de `docs/PROMOTION-RULES.md`
§1.3 — conservée uniquement dans l'historique git, ne change pas le verdict (0,9983 reste
trivialement au-dessus du seuil 0,50).

**Justification "slow strategy" (§1.2, note bas de page)**, fournie par prudence même si le
seuil brut de 80 trades est atteint : 360 cycles de rebalance mensuel dans les fenêtres OOS
(≥ 24 requis), régimes haussier ET baissier tous deux rencontrés (1774 jours-de-bourse OOS en
régime SPY<SMA200 sur les 30 fenêtres, couvrant au moins les corrections 2000-2002, 2008,
2020, 2022).

### Sensibilité aux coûts (candidate `inv_vol`)

| Coût | Profit factor OOS |
|---|---|
| 5 bps/côté (nominal) | 1,718 |
| 15 bps/côté (3×) | 1,605 |
| 25 bps/côté (5×) | 1,500 |

Le profit factor décroît de façon monotone et significative avec le coût (comportement attendu,
vérifié techniquement — cf. §6) mais reste **au-dessus de 1,0 même à 5× le coût nominal**,
contrairement au précédent historique cité par `docs/RESEARCH-REGISTRY.json` (profit factor
tombant sous 1,0 à 25 bps/côté pour la version `equal` originale) — écart cohérent avec le
niveau globalement plus élevé du profit factor obtenu ici sur l'ensemble des variantes
(cf. réserve §5).

---

## 5. Validation du moteur — écart baseline `equal` vs backtest historique (SIGNALÉ, pas masqué)

| | `equal` (ce moteur, 30 fenêtres) | Historique (`RESEARCH-REGISTRY.json`, 27 fenêtres) | Écart relatif |
|---|---|---|---|
| Sharpe OOS | 1,034 | 0,8227 | **+25,6 %** |
| Profit factor OOS | 1,821 | 1,0938 | **+66,5 %** |
| MaxDD OOS | 48,68 % | 50,29 % | 3,2 % |
| Trades OOS clos | 1970 | 1758 | 12,1 % |

La mission fixe une tolérance anticipée (~15 % sur le Sharpe, imputable à des révisions de
données yfinance) et demande explicitement d'investiguer, pas de masquer, tout écart au-delà de
25 %. **L'écart Sharpe (+25,6 %) dépasse ce seuil, et l'écart de profit factor (+66,5 %) est
important.** Investigation menée :

1. **Effet de fenêtre (3 fenêtres supplémentaires, 2023-2026)** : EXCLU. En restreignant aux 27
   premières fenêtres (même période ~1996-2023 que la référence historique), le Sharpe reste à
   1,033 (quasi inchangé) — la période supplémentaire n'explique pas l'écart.
2. **Sensibilité aux coûts mal câblée** : bug réel trouvé et corrigé en cours de session (le PnL
   par trade n'incorporait pas le coût, rendant le profit factor quasi insensible au stress de
   coûts) — corrigé (cf. §6), mais son effet sur l'écart historique est mineur (PF `equal` passe
   de 1,878 à 1,821 après correction, l'essentiel de l'écart persiste).
3. **Mécanique de la stratégie (régime, sélection, tenue des positions)** : MaxDD (48,7 % vs
   50,3 %, écart 3,2 %) et exposition moyenne quasi identiques entre les deux — signe que le
   **calendrier des décisions et le comportement en drawdown sont correctement reproduits** (une
   erreur d'exécution/de look-ahead se serait très probablement aussi vue sur le MaxDD, pas
   seulement sur le rendement moyen).
4. **Cause la plus probable, non tranchée ici** : différence de SOURCE DE DONNÉES (ajustements
   dividendes/splits, révisions de prix — cette réserve est explicitement anticipée par la
   mission) et/ou une convention de calcul du profit factor différente dans le script historique
   disparu (`bt-final/xs-momentum-sp100/`, absent de ce dépôt — **impossible à comparer
   directement au code**, ce qui est précisément le problème que la création de `backtest/`
   résout pour l'avenir).

**Conclusion honnête** : le moteur est interne cohérent (4 garanties dédiées testées et vertes,
cf. `backtest/tests/test_engine.py`), et sa reproduction du réglage de production suit la bonne
DIRECTION et la bonne MÉCANIQUE (régime, MaxDD, exposition), mais sa fidélité **au chiffre absolu
historique** n'est **pas démontrée bit-à-bit** ici. C'est une réserve ouverte à traiter dans un
audit ultérieur (accès aux données/scripts originaux si retrouvés), pas une invalidation du
moteur ni une raison de retoucher les paramètres de la candidate a posteriori (ce qui serait
exactement le sur-apprentissage que la gouvernance interdit).

**Note technique (benchmark)** : le profit factor et `n_trades_closed` du benchmark SPY buy &
hold ne sont **pas des statistiques significatives** — un buy & hold n'a par construction aucune
vente réelle ; les quelques micro-évènements résiduels observés (< 1e-6 en valeur relative,
issus du réamortissement du coût d'entrée sur 1-2 jours dans la comptabilité de caisse) ont été
identifiés, tracés et confirmés sans impact sur l'équity/Sharpe/MaxDD (qui restent calculés
directement sur la courbe d'équity, correcte) — reportés ici par souci de transparence plutôt que
supprimés silencieusement du calcul.

---

## 6. Correction appliquée en cours de session

Le calcul initial du PnL par trade n'imputait pas le coût de transaction au niveau de la ligne
(seule la comptabilité de caisse agrégée le faisait), rendant le profit factor quasiment
insensible au stress de coûts (variation de la 4ᵉ décimale entre 5 et 25 bps). Corrigé dans
`backtest/engine.py::simulate_segment` : prix d'achat majoré de `cost_rate`, prix de vente net
de `cost_rate`, pour un PnL par trade réellement NET de coûts — sans changer la courbe d'équity
globale (déjà correcte, coût déduit en agrégat). Les 7 tests de `backtest/tests/test_engine.py`
restent tous verts après cette correction ; le script a été ré-exécuté intégralement.

---

## 7. Comparaison candidate vs contrôle — le cœur de la question de recherche

| | `inv_vol` − `equal` |
|---|---|
| Sharpe | **-0,087** |
| Sortino | **-0,136** |
| MaxDD (points de %) | **+0,59 pt** (légèrement pire) |
| Ratio d'information (`inv_vol` vs `equal`) | **-0,837** |

La pondération inverse-volatilité **ne réduit PAS le MaxDD** (hypothèse du backlog : "réduction
du risque de concentration sur les titres les plus volatils") et **dégrade légèrement** le
Sharpe, le Sortino, et affiche un ratio d'information négatif marqué — c'est-à-dire qu'elle
introduit un tracking error contre `equal` sans compensation de rendement. Sur ce jeu de données
et cette période, **l'hypothèse de la candidate n'est pas confirmée** : le signal de sélection
(momentum 6-1, top 10, filtre positif, régime SPY) porte l'intégralité de l'edge documenté ;
repondérer par l'inverse de la volatilité réalisée à 63 jours ne change quasiment rien à
l'exposition moyenne (76,50 % dans les deux cas) et légèrement moins bien au global.

**Conséquence pour la gouvernance** : la candidate franchit formellement tous les seuils
chiffrés de la Porte 1 (§4) — mais cela tient presque entièrement au fait que le réglage de
production `equal` est déjà solide sur cette période, pas à un edge propre à `inv_vol`. Le
backlog anticipait explicitement ce risque ("s'assurer que le gain de Sharpe... ne vient pas
d'une réduction mécanique de la vol") ; le contrôle le confirme dans le sens négatif : il n'y a
même pas de gain à expliquer. Cette réserve doit être portée à la connaissance de l'audit
adversarial (§1.4) et de `RESEARCH-LOG.md` si cette candidate est proposée pour incubation :
passer la Porte 1 sur le papier ne constitue pas, ici, une amélioration démontrée par rapport à
la stratégie déjà active.

---

## 8. Réserves méthodologiques (biais du survivant, données, sizing)

- **Biais du survivant** : univers = 103 constituants ACTUELS du S&P100, pas une composition
  point-in-time historique. Un titre qui serait sorti de l'indice (faillite, rachat à bas prix,
  retrait) n'apparaît pas dans cet historique — biais optimiste structurel, non corrigé ici (le
  même biais affecte déjà `xs_momentum_sp100` en production, documenté de longue date dans
  `docs/RESEARCH-REGISTRY.json`).
- **Source de données** : CSV ajustés fournis pour cet exercice (`/home/claude/mdata/data/`),
  jamais copiés dans ce dépôt. Écart significatif constaté vs le chiffre historique (§5), cause
  précise non tranchée — traiter la magnitude absolue des métriques ci-dessus avec prudence tant
  que cet écart n'est pas expliqué.
- **Sizing** : ce backtest simule la poche "actions" en isolation (poids relatifs au sein du
  top 10 sélectionné, comme `bot/strategies/xs_momentum_sp100.target_weights()`), sans reproduire
  le `RiskManager` réel du bot (`bot/risk/`) ni le scaling par `capital_alloc_pct` fait par
  `bot/runner.py` — cohérence à revérifier explicitement lors de l'audit §1.4 (`docs/PROMOTION-
  RULES.md` §1.4, point "cohérence de la logique de sizing avec le RiskManager réel").
- **`bt-final/xs-momentum-sp100/` est absent de ce dépôt** : impossible de comparer ce moteur
  ligne à ligne avec le script ayant produit les chiffres historiques de référence — exactement
  le problème que la création de `backtest/` (Partie A) est censée résoudre pour toute
  candidate FUTURE, mais qui limite la profondeur de l'investigation §5 pour CETTE candidate.

---

## 9. Fichiers produits

- `backtest/engine.py`, `backtest/data.py`, `backtest/metrics.py` — moteur commun.
- `backtest/strategies/xsmom.py` — logique momentum cross-sectionnel (version backtest).
- `backtest/tests/test_engine.py` — 7 tests, tous verts (`python3 -m pytest backtest/tests/ -q`).
- `backtest/run_xsmom_invvol.py` — script d'exécution de cette candidate.
- `backtest/results/xs_momentum_invvol_sp100/results.json` — toutes les métriques (par fenêtre et
  concaténées), verdicts §1.2 un par un.
- `backtest/results/xs_momentum_invvol_sp100/REPORT.md` — ce document.

Aucune donnée de marché n'a été copiée dans le dépôt. Aucun fichier hors de `backtest/` n'a été
modifié.
