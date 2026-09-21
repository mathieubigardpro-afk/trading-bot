# RESEARCH-BACKLOG.md — Backlog d'idées de recherche, classées par priorité

> ~~ACTION REQUISE (2026-07-27, chantier 3, dette explicite) : audit adversarial de
> `backtest/risk_overlay.py` + `backtest/engine.py`~~ — **✅ SOLDÉE 2026-08-03 (session #2)** :
> audit mené sur copie isolée, verdict initial `isSound: false` (3 findings CRITIQUES : bande
> vidée de son effet par reprojection continue des shares ; défauts de vol quotidiens
> silencieusement faux sur données horaires ; poids NaN désactivant le vol targeting), tous
> corrigés + tests de non-régression (commit 2be992b, détail : `RESEARCH-LOG.md` 2026-08-03 (b)).
> Le moteur est désormais audité pour usage quotidien ET horaire.

*Alimente les sessions de recherche futures qui incuberont des candidates dans le wallet
labo 🧪. Chaque idée doit passer intégralement par `docs/PROMOTION-RULES.md` (Porte 1 puis
Porte 2) avant tout capital réel. Ce document ne préjuge d'aucun résultat -- une idée bien
classée ici peut très bien échouer au backtest, et une idée mal classée peut surprendre ;
le classement reflète la priorité d'investigation, pas une promesse de performance.
Sources : `/home/claude/trading-bot/docs/rapport-recherche.md` (recherche initiale) et
connaissance générale de la littérature quant/practitioner à la date de rédaction
(2026-07-23) -- toute idée doit être revalidée empiriquement, une référence de littérature
n'est jamais un raccourci de preuve.*

---

## Comment lire ce backlog

Pour chaque idée : **Hypothèse** (pourquoi ça marcherait, avec référence si disponible),
**Données nécessaires** (ce qu'il faut avoir/construire avant de backtester), **Complexité**
(effort d'implémentation, du simulateur au signal), **Risques de biais spécifiques** (au-delà
des pièges génériques déjà couverts par `docs/PROMOTION-RULES.md` §1.4 -- ce qui est propre
à CETTE idée). Priorité = P0 (à investiguer en premier) à P3 (spéculatif/lointain).

---

## P0 — Priorité haute

### 1. Funding carry sur perpétuels SIMULÉS (crypto) — ✅ TRAITÉE 2026-08-31 : REJETÉE (Porte 1, 4/5 seuils)

**VERDICT (session hebdomadaire #5, cf. `RESEARCH-LOG.md` 2026-08-31 (b)/(c) et
`RESEARCH-REGISTRY.json:funding_carry_6majors`)** : extension short/perp + funding du moteur
commun livrée, auditée (1 CRITIQUE + 3 MAJEURS corrigés, contre-audit `isSound: true`) puis
Porte 1 sur les 6 majors : Sharpe OOS −0,05, PF 0,96, 14 lignes perp closes, DSR 0,0065
(K_total = 68). Delta-neutralité confirmée, funding net +2,74 %/3,5 ans mais intégralement
absorbé par les coûts pessimistes (25 bps/côté × 2 jambes). Audit de la candidate
`isSound: false` pour un artefact du MOTEUR qui pénalise les candidates à faible poids (cf. #16
ci-dessous) — lecture continue de l'auditeur : Sharpe +0,84 mais PF 1,01 et 24 trades, le rejet
tient dans les deux lectures. Ne pas retester sans (a) le moteur amendé #16 ET (b) une décision
de gouvernance sur le palier de coûts perp (25 bps pessimistes vs ~5 bps taker réel) — un re-run
= nouvel id, nouvelle ligne K_total (§3.3). Incubation de toute façon impossible tant que
`bot/sim/` reste long-only (#17).

**AVANCEMENT (session #4)** : le pipeline de données est étendu (`tools/fetch_data.py`, sections
`funding` + `perp` par défaut) — funding rates 8h/4h et klines 1h des perpétuels USDT-M Binance
seront publiés sur `market-data` au prochain run du workflow fetch-data (**vérifier le premier
run réel** : formats d'archives et budget temps non validables hors Actions, cf. RESEARCH-LOG
2026-08-24 (b)). **Reste bloquant avant toute Porte 1** : l'extension short/perp du simulateur
(`bot/sim/`/moteur commun), soumise à audit adversarial préalable obligatoire.

**Hypothèse** : capter le funding rate versé/perçu périodiquement entre positions long et
short sur futures perpétuels crypto, en restant delta-neutre (long spot + short perp, ou
l'inverse selon le signe du funding). C'est la stratégie identifiée comme **la plus robuste
de la littérature crypto quant** dans la recherche initiale du projet
(`rapport-recherche.md` §2, "Stratégie écartée explicitement : carry / funding rate") :
persistance élevée (AR(1)≈0,98 rapporté), APY brut 8-20% selon les sources practitioner,
edge structurel (déséquilibre offre/demande de levier long vs short, pas un signal
technique fragile). Écartée du scope initial uniquement parce que la plateforme retenue à
l'époque (Alpaca) ne proposait ni perpetuals ni short -- **contrainte de plateforme, pas
contrainte de fond**, et ce projet n'a de toute façon plus de dépendance broker externe
(`ARCHITECTURE.md` §0 : simulateur maison alimenté par prix publics réels).

**Données nécessaires** : (a) historique de funding rate horaire/8h par paire perpétuelle
(Binance Futures API publique expose `fundingRate` historique gratuitement, pas de clé
requise pour les endpoints publics) ; (b) prix spot ET prix perp (le funding se calcule sur
l'écart, et la position réelle nécessite les deux jambes) ; (c) historique de frais
d'emprunt/marge si la jambe short est modélisée via emprunt plutôt que perp shortable
nativement.

**Complexité** : **élevée, grosse valeur si réussie**. Nécessite d'étendre
`bot/sim/exchange.py`/`bot/sim/ledger.py` pour supporter des positions **short** et un
mécanisme de règlement périodique du funding (rien de tel n'existe aujourd'hui -- le
simulateur est actuellement long-only, cf. `ARCHITECTURE.md`). C'est un changement
structurel du simulateur, pas juste une nouvelle stratégie au sens `bot/strategies/` --
implique probablement une nouvelle classe d'actif ("perp_synthetic") avec ses propres
règles de marge/liquidation simulées (pessimistes par construction, §0 principe 2), avant
même de pouvoir backtester quoi que ce soit.

**Risques de biais spécifiques** :
- Le funding rate historique lui-même peut être biaisé par des changements de mécanisme
  d'exchange dans le temps (formules de funding qui évoluent, plafonds introduits après
  des épisodes extrêmes) -- vérifier la stabilité de la définition sur toute la période
  testée.
- Risque de liquidation en position short/levier non capturé par un simulateur qui ne
  modélise pas de vraie marge -- si la jambe short est simulée de façon trop généreuse
  (pas de vrai risque de liquidation en cascade lors d'un short squeeze), le backtest
  sous-estime structurellement le risque réel. Doit être traité avec le même principe
  pessimiste que le reste du simulateur (§0.2) : toute ambiguïté tranchée en défaveur du
  bot, y compris en simulant des appels de marge/liquidations partielles réalistes.
- Coûts de financement de la jambe short (si empruntée plutôt que perp native) souvent
  sous-estimés dans les études académiques qui ignorent le funding négatif prolongé (le
  bot devrait aussi payer, pas seulement recevoir, selon le signe).

---

### 2. Breakout de volatilité crypto avec filtre de régime — ✅ TRAITÉE 2026-08-03 : ÉCARTÉE

**VERDICT (session hebdomadaire #2, cf. `RESEARCH-LOG.md` 2026-08-03 (c) et
`RESEARCH-REGISTRY.json:vol_breakout_6majors`)** : ÉCHEC Porte 1, 2/5 seuils manqués avec
marges larges (Sharpe OOS 0,434 < 0,70 ; DSR 0,058 avec K_total=66), sous le buy & hold
équipondéré (0,65), PF < 1 dès 3× les coûts, edge apparent concentré sur 2024-2026 seulement
(Sharpe −0,58 avant / +0,95 après). Contre-audit `isSound: true` (reproduit bit-à-bit).
3e signal technique actif de suite sous le B&H sur les 6 majors — ne pas retester de variante
sans raison structurellement neuve (§3.3, compterait dans K_total).

**Hypothèse (historique)** : les expansions soudaines de range (ex. cassure de bande de Bollinger/ATR
après une phase de compression -- "squeeze") précèdent statistiquement des mouvements
directionnels significatifs en crypto, marché structurellement plus sujet aux régimes de
compression/expansion que les actions (liquidité fragmentée, catalyseurs on-chain/news
discontinus). Complémentaire du trend-following déjà testé (Donchian, EMA) qui capte la
tendance **après** qu'elle est établie -- un filtre de compression en amont pourrait réduire
les faux signaux en range qui ont pénalisé `donchian_ensemble_6majors` et
`ema_momentum_6majors` (cf. `RESEARCH-REGISTRY.json`, tous deux sous-benchmark).

**Données nécessaires** : OHLC horaire déjà disponible (même pipeline que les stratégies
crypto existantes, `tools/fetch_data.py`) ; pas de nouvelle source de données requise.

**Complexité** : **modérée**. Signal calculable avec les primitives déjà présentes
(ATR, bandes de Bollinger) -- proche en structure de `mean_reversion_rsi2.py` /
`donchian.py` existants, pas d'extension du simulateur nécessaire (long-only, compatible
avec le cadre actuel).

**Risques de biais spécifiques** :
- Paramètres de "compression" (fenêtre de calcul du percentile de largeur de bande, seuil
  de percentile déclencheur) faciles à sur-optimiser avec un faible nombre de vrais
  épisodes de squeeze indépendants sur la période disponible (2022-2026, ~4 ans) --
  vérifier le nombre d'épisodes de squeeze réellement distincts avant de faire confiance
  au nombre brut de trades OOS (80 trades sur le même épisode de compression ne sont pas
  80 observations indépendantes).
- Risque de double-comptage avec le filtre SMA200 déjà en production sur la poche
  quasi-passive -- si la stratégie candidate est corrélée à plus de 0,7-0,8 avec le
  quasi-passif déjà déployé, son intérêt marginal de diversification est faible même si
  son Sharpe standalone est correct (cf. `bt-final/analyse-diversification/`, déjà motivé
  par ce type de raisonnement pour la composition du panier agressif).

---

### 3. Momentum actions ajusté par volatilité inverse (inverse-vol weighting) — ✅ TRAITÉE 2026-07-27 : ÉCARTÉE

**VERDICT (session hebdomadaire #1, cf. `RESEARCH-LOG.md` 2026-07-27 (c) et
`RESEARCH-REGISTRY.json:xs_momentum_invvol_sp100`)** : Porte 1 §1.2 passée 5/5 (Sharpe OOS
0.947, DSR 0.9998, K_total=10, audit `isSound: true`) mais dominée par le contrôle equal-weight
déjà en production sur les MÊMES 30 fenêtres (IR -0.837, ≈4.6σ) — écartée, pas d'incubation.
Le risque anticipé ci-dessous (« le gain de Sharpe pourrait ne pas venir d'un vrai edge ») s'est
matérialisé en pire : il n'y a pas de gain du tout. Ne pas retester sans raison structurellement
neuve (compterait dans K_total, cf. PROMOTION-RULES §3.3).

**Hypothèse** : `xs_momentum_sp100` (seule stratégie active retenue à ce jour, Sharpe OOS
0,82) pondère actuellement en `equal-weight` parmi le top 10 (cf.
`docs/SELECTION-FINALE.md` §1.1, paramètre retenu en production). Remplacer par une
pondération inverse-volatilité (poids ∝ 1/σ_réalisée de chaque titre du top 10) est une
extension standard documentée dans la littérature momentum (réduction du risque de
concentration sur les titres les plus volatils du panier sélectionné, sans changer le
signal de sélection lui-même) -- amélioration **incrémentale** d'une stratégie déjà
validée, pas une nouvelle famille de risque.

**Données nécessaires** : identiques à `xs_momentum_sp100` (déjà en place) + calcul de
volatilité réalisée par titre (déjà disponible via le pipeline vol-targeting générique du
bot, `bot/risk/vol_targeting.py`).

**Complexité** : **faible**. Changement du poids de rebalance, même signal de sélection,
même univers, même walk-forward déjà calibré (36m IS / 12m OOS). Bon candidat pour une
première itération complète du cycle labo (Porte 1 → Porte 2) avec un risque
d'implémentation limité.

**Risques de biais spécifiques** :
- Comparer la variante inverse-vol au **même** jeu de fenêtres walk-forward que
  `xs_momentum_sp100` (déjà dans le registre) plutôt que refaire une recherche de fenêtres
  ad hoc qui pourrait accidentellement resélectionner une découpe favorable.
- S'assurer que le gain de Sharpe (si mesuré) ne vient pas simplement d'une réduction
  mécanique de la vol du portefeuille (Sharpe peut monter sans que l'edge par titre change)
  -- comparer aussi le Sortino et le ratio d'information vs la version equal-weight, pas
  seulement le Sharpe brut.

---

## P1 — Priorité moyenne

### 4. Saisonnalité horaire BTC (21h-23h UTC) — ✅ TRAITÉE 2026-09-07 : REJETÉE (Porte 1, 4/5 seuils)

**VERDICT (session hebdomadaire #6, cf. `RESEARCH-LOG.md` 2026-09-07 (c) et
`RESEARCH-REGISTRY.json:btc_seasonality_2123utc`)** : SPEC pré-enregistrée (fenêtre 21h-23h UTC
strictement, zéro grille, zéro variante d'heure), walk-forward 15 fenêtres 9m/3m horaire,
K_total = 28. Sharpe OOS **−6,68**, PF 0,33, MaxDD relatif 1,67×, DSR ~0 — seul le seuil de
trades passe (1 369). Audit adversarial indépendant `isSound: true` (reproduction from scratch,
zéro look-ahead, aucun artefact pénalisant). Constat d'honnêteté important : **l'effet BRUT
existe et n'a pas disparu** (+6,4 bps/jour OOS ; +3,6 avant 2024, +7,8 depuis) mais reste ~4,7×
sous le hurdle de coûts du projet (30 bps/jour round-trip pour 2 h de détention). Conclusion
transférable : aucune stratégie à détention ~2 h/jour n'est viable sur ce périmètre de coûts
sans un edge brut ≥ ~5× celui-ci — ne pas retester de variante calendaire horaire sans
changement structurel du régime de coûts (§3.3, compterait dans K_total).

**Hypothèse** : la recherche initiale (`rapport-recherche.md` §7) rapporte un effet de
saisonnalité horaire BTC (rendement annualisé 40%+ dans des études Quantpedia) sur la
fenêtre 21h-23h UTC, mais explicitement **non tranché** : "études datées ≤2023, aucune
validation out-of-sample 2024-2026 trouvée ; débat académique ouvert sur la disparition de
l'effet avec l'institutionnalisation 24/7". Le rapport recommandait explicitement de ne
**pas** l'allouer sur cette seule base et de la traiter comme piste secondaire à tester
proprement -- ce backlog formalise cette piste en attente depuis le rapport initial.

**Données nécessaires** : OHLC horaire BTC déjà disponible sur 2022-2026 -- suffisant pour
un test walk-forward propre incluant la fenêtre 2024-2026 spécifiquement visée par la
réserve du rapport.

**Complexité** : **faible à modérée**. Signal purement calendaire (heure UTC de la bougie),
pas de nouvel indicateur technique. La difficulté est méthodologique, pas d'implémentation.

**Risques de biais spécifiques** :
- **C'est l'exemple canonique de sur-apprentissage temporel** : un effet horaire testé sur
  24 heures possibles, si on cherche celle qui a le mieux marché historiquement, a une
  probabilité élevée de faux positif par construction (24 "bins" testés implicitement même
  si une seule fenêtre est citée par la littérature externe) -- **le test doit porter
  spécifiquement et uniquement sur la fenêtre 21h-23h UTC déjà identifiée par la
  littérature externe, jamais une re-recherche de la "meilleure" fenêtre horaire sur les
  données du projet**, sous peine de doublement du biais de sélection (une fois dans
  l'étude source, une fois ici).
- Risque de non-stationnarité structurelle : si l'effet existait pour des raisons de
  microstructure (horaires d'ouverture des marchés asiatiques/US, comportement retail),
  l'institutionnalisation 24/7 du marché crypto (dérivés institutionnels, market makers
  actifs en continu) est une hypothèse plausible de disparition progressive -- un test
  2022-2023 vs 2024-2026 en sous-périodes séparées est nécessaire pour détecter une
  dégradation dans le temps, pas seulement un Sharpe agrégé sur toute la fenêtre.
- Coûts de transaction à haute fréquence relative (signal quotidien récurrent sur une
  fenêtre horaire précise) à comparer strictement au seuil d'edge net déjà posé par
  `rapport-recherche.md` §4 (0,7-1,1% round-trip) -- un edge annualisé "40%" ne dit rien
  sur l'edge par trade si la fréquence est quotidienne.

---

### 5. Pairs trading ETH/BTC (spread trading, marché-neutre relatif) — ✅ TRAITÉE 2026-09-21 : REJETÉE (version dégradée long-only, Porte 1 3/5 + dominée)

**VERDICT (session hebdomadaire #8, cf. `RESEARCH-LOG.md` 2026-09-21 (b) et
`RESEARCH-REGISTRY.json:pairs_ethbtc_ratio_rotation`)** : version dégradée long-only
(rotation 50/50 → 100/0 sur z-score du log-ratio, grille 4 combos, contrôle apparié 50/50,
K_total = 90). Le pré-requis de cette fiche (test de cointégration/stationnarité AVANT tout
signal) a été exécuté et est DÉFAVORABLE : ratio NON stationnaire (ADF p = 0,34 pré-OOS,
3/15 fenêtres IS stationnaires à 10 %), log-prix NON cointégrés (Engle-Granger p = 0,65) —
le risque n°1 de la fiche (« la stationnarité ne doit pas être supposée ») s'est matérialisé
tel quel. Sharpe OOS 0,331, 38 trades, DSR 0,032 — 3/5 seuils manqués ET **dominée par le
contrôle 50/50 sur Sharpe/Sortino/MaxDD** (IR −1,14) : la rotation détruit de la valeur vs ne
rien faire. Audit adversarial `isSound: false` (chiffres reproduits bit-exact 15/15 fenêtres ;
le finding CRITIQUE porte sur l'infrastructure de sizing, cf. #21 — le rejet survit dans
toutes les lectures). **La non-stationnarité condamne l'hypothèse dans ses DEUX versions : ne
pas poursuivre la version marché-neutre complète (#17) sans raison structurellement neuve
(§3.3, compterait dans K_total).**

**Hypothèse (historique)** : ETH et BTC partagent un bêta crypto commun élevé (corrélation 0,89 rapportée
par `rapport-recherche.md` §3E) mais leur ratio ETH/BTC a historiquement des phases de
rotation (BTC dominance montante/descendante) qui pourraient être exploitables par un
signal de retour à la moyenne sur le **ratio** plutôt que sur le prix absolu de l'un ou
l'autre -- structurellement différent du mean reversion RSI2 déjà rejeté (qui opérait sur
le prix absolu de chaque actif, pas sur un spread relatif).

**Données nécessaires** : OHLC horaire BTC et ETH déjà disponibles. Pas de nouvelle source.

**Complexité** : **modérée à élevée selon l'implémentation**. Si implémenté en position
longue ETH + courte BTC (ou l'inverse), nécessite la même extension short du simulateur que
l'idée #1 (funding carry) -- synergie possible si l'extension short est développée pour l'un
des deux projets, elle bénéficie à l'autre. Une version dégradée long-only (rotation
d'allocation entre BTC et ETH selon le signal de ratio, jamais short) est possible sans
extension du simulateur, mais capture moins bien l'hypothèse de fond (retour à la moyenne
pur, indépendant du bêta crypto directionnel).

**Risques de biais spécifiques** :
- Le spread ETH/BTC n'est pas stationnaire sur longue période (changements structurels :
  DeFi summer 2020, transition PoS d'Ethereum 2022, cycles de "altseason" vs "BTC
  dominance") -- un test de cointégration/stationnarité du ratio doit précéder tout signal
  de retour à la moyenne, pas être supposé implicitement. La fenêtre de données disponible
  (2022-2026) ne couvre qu'un sous-ensemble des régimes structurels connus du couple
  ETH/BTC.
- Avec seulement 2 actifs, la taille d'échantillon d'épisodes de divergence/convergence
  indépendants est structurellement faible sur 4 ans de données -- risque élevé de
  sur-ajuster les seuils d'entrée/sortie du spread à quelques épisodes historiques
  spécifiques (2022 bear, reprise 2023-2024).

---

### 6. Protective put synthétique (couverture de queue pour les wallets réels) — ✅ TRAITÉE 2026-09-14 : ÉCARTÉE (Porte 1, 3/5 seuils)

**VERDICT (session hebdomadaire #7, cf. `RESEARCH-LOG.md` 2026-09-14 (b) et
`RESEARCH-REGISTRY.json:protective_vol_gate_6majors`)** : SPEC pré-enregistrée (gate percentile
de vol du panier, grille 4 combos, contrôle apparié sans gate compté dans K_total = 89),
15 fenêtres 9m/3m horaires. Sharpe OOS **0,075**, PF 1,064, DSR 0,0095 — 3/5 seuils manqués ;
audit adversarial `isSound: true` (reproduction bit-exacte 15/15 fenêtres). Les deux risques
pré-identifiés par cette fiche se sont matérialisés tels quels : **whipsaw dominant** (35
épisodes de protection OOS : 16 pertes évitées vs 19 hausses ratées, somme nette +100 % de
rendement B&H raté) et **redondance avec le vol targeting de production** (contrôle sans gate :
MaxDD 42,4 % vs 38,0 % pour la candidate — la gate n'ajoute que ~4,5 points de MaxDD évités,
payés ~0,37 point de Sharpe, IR −0,99, corr 0,93). Conclusion transférable : une gate de vol
discrète PAR-DESSUS un vol targeting continu actif est structurellement redondante sur ce
marché — ne pas retester de variante (seuils/rampe) sans mécanisme structurellement neuf
(§3.3, compterait dans K_total).

**Hypothèse (historique)** : plutôt qu'une nouvelle source d'edge, une brique de **réduction de risque**
-- répliquer synthétiquement l'effet d'un put protecteur (limiter la queue gauche du
drawdown) via une règle mécanique de désengagement accéléré en cas de move directionnel
violent (ex. stop-loss dynamique déclenché par un franchissement rapide de percentile de
volatilité, redéploiement progressif après stabilisation), sans avoir besoin d'options
réelles (non disponibles dans le simulateur actuel et hors scope probable). Motivation :
les MaxDD OOS mesurés sur ce projet sont substantiels même pour la stratégie retenue la
plus solide (`xs_momentum_sp100`, MaxDD 50,3% sur la fenêtre dot-com) -- une brique de
protection de queue, si elle ne détruit pas trop de rendement en régime normal, pourrait
améliorer le couple rendement/risque du wallet dans son ensemble plutôt que d'une poche
isolée.

**Données nécessaires** : aucune nouvelle donnée de marché -- uniquement les prix déjà
suivis. Nécessite en revanche de définir précisément la métrique de déclenchement (vol
réalisée, vitesse de drawdown, etc.) et son horizon de mesure.

**Complexité** : **modérée**. Pas d'options réelles à modéliser (évite la complexité de
pricing/grecques d'un vrai simulateur d'options) -- une règle de désengagement mécanique
reste dans le cadre actuel du simulateur (ordres spot, pas de nouvel instrument). La
difficulté principale est la calibration walk-forward du déclencheur, pas l'infrastructure.

**Risques de biais spécifiques** :
- **Risque de confusion avec le "equity curve trading" déjà explicitement écarté** par la
  recherche initiale (`rapport-recherche.md` §7 : réduire l'exposition après une baisse de
  sa propre courbe d'equity est "presque toujours pire que le trading continu" d'après
  l'étude empirique citée, Kevin Davey). Cette idée doit être backtestée comme une règle
  **indépendante et pré-définie** sur des signaux de marché (vol réalisée, vitesse de move)
  -- jamais comme une réaction à la propre performance récente du portefeuille, qui est le
  piège déjà identifié et écarté. À formuler et tester avec cette distinction explicite dès
  la conception, pas a posteriori.
- Le principal risque de biais d'un stop de protection est le "whipsaw" (sortie sur un move
  brutal suivi d'un rebond immédiat, qui rate la reprise) -- doit être quantifié
  explicitement (coût d'opportunité des faux signaux de protection), pas seulement le
  bénéfice des vrais signaux (biais classique d'évaluation asymétrique des stops déjà
  signalé par `rapport-recherche.md` §7 pour les stops ATR en général).

---

## P2 — Priorité basse (spéculatif, à explorer si les priorités P0/P1 sont épuisées)

### 7. Extension de l'univers actions au-delà du S&P100 (mid-caps momentum)

**Hypothèse** : l'edge momentum cross-sectionnel documenté sur `xs_momentum_sp100`
(mega-caps liquides) pourrait être plus fort sur un univers moins efficient (mid-caps,
Russell 1000 ex-S&P100) -- l'edge momentum est généralement documenté comme plus fort sur
les segments moins couverts par les analystes/moins arbitragés.

**Données nécessaires** : historique OHLC quotidien d'un univers mid-cap plus large --
nécessite une extension du pipeline de données actuel (`bot/feeds/equities.py`, Yahoo
Finance gratuit) à un panel plus large, avec vérification de la disponibilité/qualité des
données pour des titres moins liquides.

**Complexité** : **modérée** (pipeline de données à étendre, logique de stratégie
réutilisable telle quelle depuis `xs_momentum_sp100`).

**Risques de biais spécifiques** :
- **Biais du survivant amplifié** : le S&P100 a déjà ce problème (constituants actuels
  utilisés sur tout l'historique, cf. `RESEARCH-REGISTRY.json`), mais un univers mid-cap
  plus large a un taux de disparition (faillite, radiation, rachat) structurellement plus
  élevé sur longue période -- le biais serait probablement plus sévère, pas moindre, sans
  base de données point-in-time (déjà signalée comme indisponible localement pour ce
  projet).
- Coûts de transaction plus élevés et moins bien documentés sur les mid-caps (spread plus
  large, profondeur de carnet moindre) -- le coût nominal de 5 bps/côté retenu pour
  `xs_momentum_sp100` (mega-caps liquides) n'est probablement pas transposable tel quel.

### 8. Stratégie de volatilité relative crypto vs actions (régime cross-asset)

**Hypothèse** : utiliser le ratio de volatilité réalisée crypto/actions (ex. BTC vs SPY)
comme signal de régime pour moduler l'allocation entre les poches crypto et actions/ETF
d'un même wallet -- au-delà du filtre SMA200 déjà en place par poche individuellement,
un signal de régime cross-asset explicite pourrait capter les phases de "risk-off"
généralisé où les deux poches chutent ensemble (déjà identifiée comme réserve qualitative
non chiffrée dans `docs/SELECTION-FINALE.md` §3, "corrélation actions/crypto élevée en
régime de vente généralisée, même si le rapport de diversification ne la chiffre pas
directement").

**Données nécessaires** : déjà disponibles (BTC + SPY, ou tout indice actions déjà suivi)
mais nécessite une nouvelle brique d'allocation **inter-poches**, distincte des stratégies
actuelles qui opèrent chacune dans leur poche isolément -- changement d'architecture
potentiel (allocation dynamique entre poches d'un wallet, pas encore un concept implémenté
aujourd'hui, où les `capital_alloc_pct` sont fixes par wallet).

**Complexité** : **élevée** (nouvelle catégorie de logique, pas une nouvelle stratégie de
poche mais un mécanisme d'allocation de niveau wallet -- nécessiterait une extension de
`bot/runner.py`/`bot/config.py:WALLETS[*]["pockets"]` pour supporter des poches à
`capital_alloc_pct` variable dans le temps selon un signal, actuellement toutes fixes).

**Risques de biais spécifiques** :
- Risque élevé de rétro-ajustement du signal de régime sur les 2-3 épisodes de crise déjà
  connus de la période disponible (2022 bear crypto+actions, éventuels épisodes plus
  récents) -- un signal de régime calibré sur 2-3 événements historiques n'est pas
  significativement testé, quel que soit le nombre de "trades" qu'il génère par ailleurs.
- Interaction complexe avec les circuit breakers déjà en place par wallet (`CB_DD_*`) --
  risque de double-réaction (le signal de régime réduit l'exposition ET le circuit breaker
  se déclenche indépendamment) qui doit être backtestée conjointement, pas isolément.

---

## P3 — Spéculatif / lointain

### 9. Sentiment/on-chain crypto (signaux non-prix)

**Hypothèse** : signaux dérivés de données on-chain (flux d'exchange, activité de réseau,
ratio MVRV, etc.) ou de sentiment (réseaux sociaux, recherche Google) comme complément aux
signaux techniques déjà testés, sur la base que ces signaux capturent une information non
reflétée immédiatement dans le prix.

**Données nécessaires** : sources on-chain/sentiment tierces, souvent payantes ou à API
peu fiable pour un usage gratuit/pérenne (contrainte forte de ce projet : uniquement des
sources publiques gratuites, sans clé, testables depuis un runner GitHub Actions --
cf. `ARCHITECTURE.md` principe des feeds actuels). C'est le facteur bloquant principal, pas
la théorie de l'idée elle-même.

**Complexité** : **élevée**, dominée par la disponibilité/fiabilité/gratuité de la donnée
plus que par la logique de signal elle-même.

**Risques de biais spécifiques** :
- Beaucoup de ces signaux ont un historique public court ou instable (changements de
  méthodologie de calcul par le fournisseur au fil du temps) -- risque de walk-forward
  biaisé si la définition du signal elle-même a changé silencieusement dans la période
  testée.
- Risque élevé de data snooping généralisé : la littérature "sentiment crypto" est vaste et
  peu répliquée indépendamment -- traiter toute référence externe à ce type de signal avec
  un scepticisme au moins égal à celui déjà appliqué à la saisonnalité horaire (idée #4).

### 10. Stratégies actions short/market-neutral (nécessite extension simulateur)

**Hypothèse** : toute stratégie actions qui nécessite une jambe short (ex. long/short
momentum, pairs trading actions) reste hors de portée tant que `bot/sim/` n'a pas été
étendu au short (même dépendance structurelle que l'idée #1 funding carry et #5 pairs
ETH/BTC) -- regroupée ici comme rappel que cette extension, si elle est faite, ouvre
plusieurs idées de backlog simultanément et devrait être évaluée comme un investissement
d'infrastructure partagé plutôt que pour une seule stratégie candidate.

**Données nécessaires** : selon la stratégie précise retenue une fois l'infrastructure
disponible -- non détaillé ici, cette entrée sert de marqueur de dépendance.

**Complexité** : **élevée** (dépend entièrement de l'extension du simulateur, cf. idée #1).

**Risques de biais spécifiques** : à évaluer par stratégie concrète le moment venu -- pas
de risque spécifique identifiable avant qu'une hypothèse précise soit formulée.

---

## Idées ajoutées par la session hebdomadaire #1 (2026-07-27)

### 11. [P0 — infrastructure] Durcissement du pipeline de données actions : détection d'anomalies de corporate actions — ✅ TRAITÉE 2026-08-24

**LIVRÉ (session hebdomadaire #4, cf. `RESEARCH-LOG.md` 2026-08-24 (b))** :
`tools/check_data_anomalies.py` + intégration dans `tools/fetch_data.py` — chaque régénération
de `market-data` publie désormais `DATA_ANOMALIES.md`/`anomalies.json` (journal de revue humaine,
jamais de correction silencieuse). Démonstration sur données réelles : 21 anomalies, dont le cas
DHR/Fortive fondateur (+61,2% 2016-07-05) et 2 incohérences OHLC récentes (ABT/MS, séance du
2026-07-24 — à vérifier après la prochaine régénération hebdo). Audit adversarial : 1 CRITIQUE +
2 MAJEURS corrigés en session, contre-vérification `isSound: true`.

**Hypothèse/motivation** : l'audit adversarial du backtest inverse-vol a identifié une anomalie
concrète dans les données `market-data` : le spin-off DHR/Fortive (juillet 2016) mal ajusté
(+62% en un jour dans la série « ajustée »), qui a fait entrer DHR artificiellement dans le
top-10 momentum pendant 6 mois. Sans impact favorable sur CE backtest (l'exclure améliore même
le résultat), mais rien ne garantit qu'une future anomalie du même type ne gonflera pas une
future candidate. **À faire avant le prochain backtest actions** : un check automatique dans
`tools/fetch_data.py` ou `backtest/data.py` (flag des rendements quotidiens > seuil ~±40% sur
titres large-cap, croisés avec un calendrier de corporate actions ou au minimum journalisés pour
revue humaine). Complexité : faible. Risque : faux positifs sur vrais krachs idiosyncratiques
(à journaliser, pas à corriger silencieusement).

### 12. [P1 — gouvernance, session DÉDIÉE obligatoirement, jamais une session de jugement (§0)] Deux amendements à proposer pour PROMOTION-RULES.md

(a) **Critère de valeur marginale vs incumbent** : la session #1 a rencontré le cas non prévu
« tous les seuils Porte 1 passés mais candidate dominée par la version en production de la même
stratégie sur les mêmes fenêtres » — tranché conservativement (écartée), à formaliser.
(b) **Discriminance du DSR sur OOS longs** : sur ~30 ans d'OOS concaténé (n≈7500), DSR≥0.50 est
quasi automatiquement satisfait quel que soit K (constat chiffré de l'audit : DSR encore 0.89 à
K=10 000). Pistes : seuil sur le DSR par fenêtre, ou PSR à SR* > 0 plus exigeant, ou seuil de
significativité de l'écart vs benchmark (Jobson-Korkie déjà utilisé dans la vague 1).

### 13. [P2 — dette de recherche] Retester `quasi_passif_crypto` avec le protocole complet — ✅ TRAITÉE 2026-08-10 : ÉCHEC 3/3

**VERDICT (session hebdomadaire #3, cf. `RESEARCH-LOG.md` 2026-08-10 (b) et
`RESEARCH-REGISTRY.json:quasi_passif_crypto_wf_retest`)** : les 3 variantes déployées échouent
la validation Porte 1 (prudent : PF 1,08 et DSR 0,215 sous seuils ; équilibré/agressif : sous
leur benchmark B&H ET perdants nets aux coûts, Sharpe négatif depuis 2024). Audit adversarial
`isSound: true`. Les Sharpe non audités d'origine (1,24/1,47/1,49) ne sont pas reproduits et ne
doivent plus servir de référence. Aucune action automatique (antécédent hors §3, sémantique
pré-enregistrée dans la SPEC) — **une session de gouvernance DÉDIÉE doit statuer sur
l'alignement de l'antécédent (cf. idée #14 ci-dessous, désormais LA priorité)**.

### 14. [P0 — gouvernance, session DÉDIÉE obligatoirement (§0), AJOUTÉE 2026-08-10] Statuer sur l'antécédent `quasi_passif_crypto` après l'échec du retest

La seule brique crypto des 3 wallets réels repose sur un backtest non confirmé par le protocole
complet (idée #13). Options à instruire hors de toute session de jugement de candidate :
(a) statu quo sous le critère d'échec vécu de `SELECTION-FINALE.md` §5 (bascule à 3 mois de
sous-performance vécue — les wallets n'ont que ~18j de vécu) ; (b) alignement formel de
l'antécédent sur les règles de mort §3 ; (c) réduction/retrait de la poche crypto (équilibré et
agressif sont les plus atteints ; la variante prudente BTC+ETH est la moins loin des seuils :
Sharpe 0,81 > benchmark 0,76, MaxDD 8,4%). Décision humaine requise — la boucle de recherche ne
touche pas à la composition des wallets réels de sa propre initiative (§4.3 et SPEC du retest).

### 15. [P2 — hygiène monitoring, AJOUTÉE 2026-08-24] Re-baser la référence « attendue » du DRIFT-REPORT pour `quasi_passif_crypto`

`tools/weekly_maintenance.py` lit encore dans le registre les Sharpe/MaxDD non audités d'origine
(1,24/1,47/1,49) comme référence « attendue » — chiffres explicitement discrédités par le retest
#3 (`quasi_passif_crypto_wf_retest`). La référence devrait devenir celle du retest audité
(0,808/0,283/0,069 ; MaxDD 8,4/27,3/56,4%). Attention : registre append-only — ne pas réécrire
l'entrée d'origine ; faire pointer le moniteur vers l'entrée de retest (ou une résolution
explicite « entrée la plus récente de la même famille »). Simple outil de monitoring (aucune
règle de PROMOTION-RULES en jeu), mais à tester avec les fixtures existantes.

### 16. [P0 — infrastructure moteur] Portage de position entre fenêtres OOS contiguës — ✅ TRAITÉE 2026-09-07

**LIVRÉ (session hebdomadaire #6, cf. `RESEARCH-LOG.md` 2026-09-07 (b))** : SPEC pré-enregistrée
`backtest/CARRY-EXTENSION-SPEC.md` committée AVANT implémentation, extension opt-in
(`carry_in`/`carry_out`, reconstruction au dernier close sans coût, anti-gaming du comptage de
trades). Audit adversarial : verdict initial `isSound: false` (F1 CRITIQUE : gonflement du
profit factor par artefact de normalisation aux frontières — corrigé, biais 15/15 seeds
favorables → répartition équilibrée ; F3 MAJEUR : NaN non gardé au packaging — corrigé ;
1 MINEUR corrigé), contre-audit **`isSound: true`**. Rétro-compat bit-à-bit prouvée (hash +
reproduction bit-exacte de `vol_breakout_6majors/results.json` sur données réelles, 46 min).
**F2 (CRITIQUE, non corrigeable ici)** : l'équivalence « bande poche = bande wallet » supposée
par la spec §5 est **RÉFUTÉE** en régime de compounding (relation affine, pas homothétique) —
clause d'arrêt respectée, comportement de bande inchangé, statut NON RÉSOLU → **#19 ci-dessous**.
Premier usage : Porte 1 de `btc_seasonality_2123utc` (portage structurellement neutre) ;
re-run informatif de `funding_carry_6majors`, cf. RESEARCH-LOG 2026-09-07 (b).

Finding F1 (CRITIQUE) de l'audit de `funding_carry_6majors` : `backtest/engine.py` remet
`shares`/`cash` à zéro à chaque fenêtre OOS (conception historique pour concaténer des fenêtres
indépendantes). Combiné à la bande de non-négociation PLATE de 5 % du moteur et au vol targeting
sur |w|, une candidate dont le poids vol-scalé reste sous 0,05 n'entre JAMAIS en position
(fenêtre entière à 0 trade malgré un signal actif 100 % du temps). En production, (a)
l'exécution est continue et (b) la bande est PAR POCHE : 5 % × `capital_alloc_pct`
(`bot/risk/manager.py` étape 6). Le moteur est donc aujourd'hui plus sévère que la production
pour toute candidate à faible poids nominal ; les 12 candidates précédentes (poids plus grands)
n'étaient vraisemblablement pas affectées, à vérifier. À faire : (1) pré-enregistrer la
sémantique (position réellement détenue en fin de fenêtre k portée en fenêtre k+1 quand les
fenêtres sont contiguës — la sélection IS reste fenêtrée ; paramètre `no_trade_band`
exprimable en fraction de poche) ; (2) implémenter en conservant la rétro-compat bit-à-bit par
défaut ; (3) audit adversarial ; (4) re-run informatif des 3 `results.json` existants pour
quantifier l'effet. Complexité : faible à modérée. Risque : introduire une fuite d'état IS→OOS
(la position portée doit venir de l'OOS précédent, jamais d'un segment IS).

### 17. [P2 — infrastructure production, AJOUTÉE 2026-08-31] Extension short/perp de `bot/sim/` (incubation d'une candidate perp)

Pré-requis pour incuber TOUTE candidate perp (statut pré-enregistré
`validee_porte1_en_attente_infra` prévu dans la SPEC du funding carry, non utilisé). Déclassée
en P2 : aucune candidate perp n'a passé la Porte 1 ; ne pas investir avant qu'une idée perp ait
une valeur démontrée sur le moteur commun amendé (#16). **Note 2026-09-21 (session #8)** : la
motivation « pairs ETH/BTC marché-neutre » est tombée — le ratio est non stationnaire et la
version long-only est rejetée dominée (cf. P1#5) ; seule la voie funding carry justifierait
encore cette extension, et elle attend elle-même une décision de gouvernance sur le palier de
coûts perp.

### 21. [P0 — infrastructure sizing, session DÉDIÉE, AJOUTÉE 2026-09-21] Fidélité backtest/production du sizing pour les candidates SANS sizing interne

Finding F1 (CRITIQUE) de l'audit de `pairs_ethbtc_ratio_rotation` (session #8), démontré par
exécution : `bot/runner.py:_risk_manager_for_wallet` construit le RiskManager portefeuille
avec `vol_target_annualized=50.0` EN DUR pour TOUS les wallets, **labo 🧪 compris** — design
documenté (correctif session #3) pour ne pas doubler le vol-targeting INTERNE de
`quasi_passif_crypto`, mais jamais réévalué pour le cas opposé. Conséquence : une candidate
labo SANS sizing interne ne recevrait en production AUCUN vol-targeting réel
(`compute_vol_scalar` = 1,0 à cible 50 pour toute vol réaliste 35-120 %), alors que le moteur
commun de backtest lui applique l'overlay production (scalar réel 0,23-0,79, exposition
moyenne réalisée 0,57 vs cible brute 1,0 sur la candidate #8). Le backtest est donc PLUS
PROTECTEUR que ce que l'incubation livrerait — le « sizing fictif plus généreux que la
production » que PROMOTION-RULES.md §1.4 interdit, dans le sens inverse du précédent
quasi-passif (session #3). Portée rétroactive documentée : la prémisse « overlay standard =
chemin correct pour une candidate sans sizing interne » utilisée par les SPEC des sessions #7
et #8 est réfutée (notes datées au registre ; aucun verdict passé ne change — les deux
candidates échouaient indépendamment, et candidate/contrôle subissaient la même distorsion).
**À trancher en session dédiée (options identifiées par l'audit)** : (a) champ explicite
« sizing_interne: oui/non » dans le schéma `INCUBATING_STRATEGIES` + RiskManager labo
conditionnel (attention : toucher `bot/runner.py` = code de production, audit adversarial
obligatoire, et le cadre de risque §4.3 reste hors de portée — il s'agit de FIDÉLITÉ, pas de
desserrer quoi que ce soit) ; (b) exiger un vol-targeting interne de toute candidate avant
incubation (zéro changement production, contrainte de conception par SPEC) ; (c) convention
backtest `apply_vol_targeting=False` pour les candidates sans sizing interne (fidèle à la
production ACTUELLE — mais expose les candidates au régime de vol brut). **BLOQUANT : aucune
future Porte 1 de candidate sans sizing interne avant que ce point soit tranché** — sinon ses
chiffres seraient produits sous une hypothèse de sizing connue pour être infidèle.

### 18. [P2 — infrastructure données, AJOUTÉE 2026-09-07] Robustesse début-de-mois du rafraîchissement crypto de la maintenance — ⚙️ ÉTAPE DIAGNOSTIC LIVRÉE 2026-09-14

**AVANCEMENT (session #7)** : (a) le recalibrage du 2026-09-13 s'est exécuté normalement (15
fenêtres, aucun changement) — l'incident ne se reproduit qu'en tout début de mois, comme
diagnostiqué ; (b) l'instrumentation demandée est livrée : `tools/weekly_maintenance.py` remonte
désormais `missing_symbols_reasons` (raison d'exclusion PAR SYMBOLE, lue du MANIFEST.json de
`fetch_data.py`) dans DRIFT-REPORT.json et une ligne compacte dans le rendu markdown quand le
recalibrage est sauté — purement additif, aucune règle de décision touchée, tests offline verts.
La prochaine occurrence début-de-mois sera diagnosticable sur run réel. **Reste à faire** (après
confirmation par une vraie occurrence) : le correctif du complément API étendu aux mois d'archive
manquants EN QUEUE d'historique (jamais aux trous anciens).

Incident du 2026-09-06 : recalibrage sauté (`DONNEES_INSUFFISANTES`, 0/6 symboles) alors que le
rafraîchissement rapportait « OK ». Cause la plus probable : maintenance exécutée un 6 du mois ⇒
le « dernier mois complet » (août) n'avait vraisemblablement pas encore son archive mensuelle
sur Binance Vision ⇒ règle de complétude ⇒ tous les symboles exclus — le complément API
(`fetch_binance_current_month_completion`) ne couvre que le mois COURANT, jamais un mois
d'archive manquant. Mode d'échec systématique de toute maintenance en tout début de mois.
Correctif candidat : étendre le complément API aux mois requis manquants EN QUEUE d'historique
(jamais aux trous anciens — la trace du biais du survivant doit rester), avec tests offline.
Diagnostic à confirmer au préalable sur un run réel (journaliser les `reason` d'exclusion par
symbole dans DRIFT-REPORT.json — actuellement seuls les `missing_symbols` remontent).

### 19. [P2 — fidélité moteur, analyse dédiée, AJOUTÉE 2026-09-07] Bande de non-négociation : deux écarts moteur/production documentés, non résolus

(a) **Équivalence poche/wallet RÉFUTÉE en compounding** (audit F2 de l'extension carry,
démonstration : `equity_wallet = 1 + alloc·(equity_pocket − 1)`, relation affine — décisions
hold/trade divergentes dès que l'équity s'écarte de 1, d'autant plus que l'alloc est petite).
Le statut de fidélité de `no_trade_band=0.05` du moteur vs `no_trade_band × capital_alloc_pct`
de la production pour `capital_alloc_pct < 1` est NON RÉSOLU (test de garde committé :
`test_no_trade_band_pocket_wallet_homothety_refuted_under_compounding`). (b) **Flatten sous la
bande** : la production exécute TOUJOURS un flatten (cible 0) même sous la bande
(`bot/risk/manager.py` étape 6) ; le moteur, lui, retient un flatten sous la bande (position qui
dérive au lieu de sortir). Les deux écarts sont à instruire ensemble (analyse quantitative de
l'impact sur les candidates passées et futures), en session dédiée, AVANT de re-revendiquer la
fidélité de la bande dans une spec.

**Pièce versée au dossier (session #7, audit de `protective_vol_gate_6majors`)** : quantification
sur données réelles d'une candidate à poids cible médian 0,064 (tout près de la bande 0,05) —
la bande avale ~99 % de l'exécution d'une rampe de redéploiement 72 h (12 ordres sur 1 630 h) et
95 % des coupures immédiates (21/22 sans ordre à l'heure du signal). Testé BIDIRECTIONNELLEMENT
par l'auditeur : forcer l'exécution immédiate DÉGRADE le Sharpe (turnover) — l'effet n'est pas
un biais directionnel simple, mais la « texture » réellement exécutée peut différer radicalement
de la sémantique écrite d'une SPEC. Toute future SPEC de candidate à faible poids nominal ou à
rampe DOIT documenter ce comportement dans ses analyses d'honnêteté.

### 20. [P2 — infrastructure données, AJOUTÉE 2026-09-14] Ticker BK : 3 échecs consécutifs de récupération (yfinance ET stooq)

Échecs aux régénérations du 2026-08-24, 2026-09-12 (et absence constatée entre les deux). Trois
échecs consécutifs sur les deux sources = vraisemblablement un problème durable de mapping/
symbole (renommage, migration d'API) plutôt qu'un rate-limiting transitoire. Sans incidence sur
les poches actives (BK absent des positions), mais BK fait partie de l'univers S&P100 de
`xs_momentum_sp100` : son absence du panel réduit silencieusement l'univers effectif de 103 à
102 titres pour tout futur backtest actions. À investiguer (mapping alternatif, source de repli
supplémentaire, ou exclusion documentée de l'univers avec note au registre).

### 22. [P3 — hygiène monitoring, AJOUTÉE 2026-09-21] Référence MaxDD manquante pour `dual_momentum_multiclasse_etf` dans le DRIFT-REPORT

Depuis que le Sharpe roulant 60 j est calculable (rapport du 2026-09-21), les 2 lignes
`dual_momentum_etf` restent « SURVEILLER » pour une raison purement mécanique : l'entrée
d'origine de la vague 1 au registre n'a pas de MaxDD OOS consigné (registre append-only —
ne pas réécrire l'entrée). Même mécanisme de solution que #15 : table de redirection/complément
explicite dans `tools/weekly_maintenance.py` (le MaxDD OOS de la vague 1 existe dans les
rapports d'origine : ~19,9 % — à re-sourcer proprement avant de l'inscrire), avec note au
rendu. Purement informatif, aucune règle de décision en jeu.

**Priorité de la prochaine session (revue 2026-09-21, session #8)** :

1. **#21 (P0 infrastructure sizing, session dédiée)** : trancher la fidélité
   backtest/production du sizing des candidates sans sizing interne (options (a)/(b)/(c) de
   la fiche) — BLOQUANT pour toute future Porte 1 de candidate sans sizing interne. Si
   l'option retenue touche `bot/runner.py` : SPEC pré-enregistrée + audit adversarial
   obligatoires, et jamais dans une session qui juge une candidate (§0).
2. **#14 (gouvernance, décision HUMAINE — Mathieu)** : en attente depuis le 2026-08-24.
   Le Sharpe roulant 60 j est désormais calculable (rapport du 2026-09-21 : tout est OK,
   vécus au-dessus des références auditées) ; le critère vécu de `SELECTION-FINALE.md` §5
   tranche de lui-même vers fin octobre. Peut absorber #12a/#12b et le palier de coûts perp.
3. **Prochaine candidate de jugement** : le backlog technique s'épuise (P0/P1 : 6 traitées,
   toutes écartées/rejetées). Restent P2#7 (mid-caps momentum — biais du survivant amplifié,
   coûts à revoir) et P2#8 (régime cross-asset — nécessite une brique d'allocation
   inter-poches, grosse infra). Aucune des deux n'est incubable sans travail préalable ;
   si #21 retient l'option (b), toute nouvelle candidate devra em porter un sizing interne.
   Alternative recommandée : consacrer la session de jugement suivante à #19 (bande de
   non-négociation, analyse dédiée) plutôt qu'à une candidate faible — le dossier est mûr
   (3 pièces) et conditionne la fidélité de toutes les futures SPEC.
4. #20 (ticker BK, 4 échecs consécutifs au 2026-09-19) : investigation mapping/source de
   repli ou exclusion documentée de l'univers.
5. Revue : DRIFT-REPORT à re-suivre (premières semaines de verdicts 60 j réels) ; #18
   (début de mois) : prochaine occurrence possible le dimanche 2026-10-04.

**Priorité de la session #8 (2026-09-14, conservée pour mémoire — libellé d'origine « prochaine session » de la revue #7)** :

1. **#14 (gouvernance, décision HUMAINE — Mathieu)** : toujours en attente depuis le
   2026-08-24. Fenêtre utile : le Sharpe roulant 60 j devient calculable ~fin septembre (les
   verdicts du DRIFT-REPORT deviendront réellement informatifs) et le critère vécu de
   `SELECTION-FINALE.md` §5 tranche de lui-même vers fin octobre. Peut absorber #12a/#12b et
   le palier de coûts perp — questions de règle, jamais dans une session de jugement.
2. **P1#5 (pairs ETH/BTC, version dégradée long-only)** : prochaine candidate de jugement
   recommandée — rotation d'allocation BTC/ETH sur signal de ratio, sans extension short
   (la version complète attend #17/P2). Pré-requis de SPEC : test de
   cointégration/stationnarité du ratio AVANT tout signal (documenté dans la fiche), grille
   minimale, attention au faible nombre d'épisodes de divergence indépendants sur 2022-2026.
3. **#19 (bande de non-négociation, session d'analyse dédiée)** : le dossier s'épaissit
   (F2 carry + flatten + quantification session #7) — instruire les deux écarts
   moteur/production ensemble, hors de toute session de jugement.
4. Revue : premier DRIFT-REPORT avec Sharpe roulant 60 j calculable attendu vers le
   2026-09-20/27 — première revue où les verdicts SURVEILLER peuvent basculer en signaux
   réels ; y consacrer du temps de lecture.
5. #20 ci-dessus (BK) et, si occurrence début-de-mois d'ici là, boucler le correctif #18.

**Priorité de la session #7 (2026-09-07, conservée pour mémoire)** :

1. **#14 (gouvernance, décision HUMAINE)** : toujours en attente depuis le 2026-08-24 — à
   défaut, le critère vécu de `SELECTION-FINALE.md` §5 tranche de lui-même vers fin octobre
   2026 (le Sharpe roulant 60j devient calculable ~fin septembre : les verdicts du
   DRIFT-REPORT deviendront réellement informatifs). Peut absorber #12a/#12b et le palier de
   coûts perp — questions de règle, jamais dans une session de jugement.
2. **P1#6 (protective put synthétique)** : prochaine candidate de jugement recommandée —
   long-only, aucune extension d'infra requise, valeur de réduction de risque pour les 3
   wallets ; attention au piège « equity curve trading » explicitement documenté dans la fiche.
3. **P1#5 (pairs ETH/BTC)** : version dégradée long-only possible sans extension short ; la
   version complète attend #17 (P2).
4. #18 ci-dessus (diagnostic + correctif début-de-mois — rapide, avec le premier point :
   vérifier que le recalibrage du 2026-09-13 s'est exécuté normalement).
5. Vérifier le premier run du cron fetch-data (samedi 2026-09-12 05h UTC) : régénération de
   `market-data`, funding/perp à jour, ticker BK, anomalies OHLC.

**Priorité de la session #6 (2026-08-31, conservée pour mémoire)** :

1. **#16 (P0 infrastructure moteur)** : session dédiée, spec pré-enregistrée + audit adversarial
   AVANT toute candidate — le moteur commun a un défaut documenté vs la production.
2. **#14 : décision humaine toujours attendue** (dossier `GOVERNANCE-DOSSIER-2026-08-24`) ; à
   défaut, le critère vécu de `SELECTION-FINALE.md` §5 tranche vers fin octobre 2026. Peut
   absorber #12a/#12b et une instruction du palier de coûts perp (25 bps pessimistes vs taker
   réel) — question de règle, jamais tranchée dans une session de jugement.
3. **P1#4 (saisonnalité horaire BTC 21h-23h UTC)** : première candidate long-only possible
   après #16 (poids BTC = 1 × scalar, non affectée par F1 mais autant juger sur le moteur amendé).
4. P2#15 (re-baser la référence « attendue » du DRIFT-REPORT — rapide, non fait en #5).
5. Surveiller le ticker **BK** (échec yfinance + stooq au run du 2026-08-24) à la prochaine
   régénération de `market-data`.

**Priorité de la session #4 (2026-08-24, conservée pour mémoire)** :

1. **#14 : le dossier d'instruction est prêt** (`docs/GOVERNANCE-DOSSIER-2026-08-24-quasi-passif.md`)
   — décision HUMAINE attendue (Mathieu). Si une option est choisie, l'appliquer en session
   dédiée (§0), avec les amendements #12a/#12b proposés dans le même dossier. À défaut de
   décision, le critère vécu de `SELECTION-FINALE.md` §5 tranche de lui-même vers fin octobre.
2. Vérifier le premier run réel du workflow fetch-data étendu (sections funding/perp +
   publication de `DATA_ANOMALIES.md`) : formats d'archives, budget temps (75 min), paires perp
   réellement disponibles ; puis vérifier si les incohérences OHLC ABT/MS (2026-07-24) ont
   disparu à la régénération.
3. P0#1 étape simulateur : conception + audit adversarial de l'extension short/perp de
   `bot/sim`/du moteur commun (pré-requis Porte 1 du funding carry). Grosse pièce — peut
   occuper une session entière.
4. Alternative si les données funding ne sont pas encore disponibles : P1#4 (saisonnalité
   horaire BTC 21h-23h UTC, test strictement confiné à la fenêtre pré-identifiée par la
   littérature) — première candidate possible d'incubation depuis l'ouverture du labo.
5. P2#15 ci-dessus (rapide, améliore la lisibilité du DRIFT-REPORT).

**Priorité de la session #3 (2026-08-10, conservée pour mémoire)** :
1. **#14 (gouvernance : statuer sur l'antécédent `quasi_passif_crypto`)** — LA priorité, en
   session DÉDIÉE (§0, jamais mêlée à un jugement de candidate). L'échec 3/3 du retest laisse
   la seule brique crypto de production sans validation protocolaire ; à défaut, le critère
   vécu de `SELECTION-FINALE.md` §5 (3 mois) tranchera de lui-même vers fin octobre 2026.
   Peut absorber aussi les amendements #12 (valeur marginale vs incumbent, discriminance DSR).
2. P0#11 (détection d'anomalies de corporate actions — rapide, débloque la confiance des
   futurs backtests actions).
3. P0#1 (funding carry) : toujours la plus grosse valeur potentielle, toujours bloquée par
   (a) l'historique de funding rates ABSENT de `market-data` (étendre `tools/fetch_data.py`
   et laisser les Actions le faire tourner AVANT toute session qui voudrait la traiter) et
   (b) l'extension short/perp du simulateur, soumise à audit adversarial préalable.
4. Note moteur (session #2) : les niveaux absolus de `xs_momentum_invvol_sp100`/contrôle
   equal-weight (session #1) ont été produits sur le moteur PRÉ-correctif F1 — tout futur
   usage de ces chiffres comme référence exige un re-run sur moteur corrigé (le verdict
   apparié 'ecartee', lui, tient — cf. note au registre).
5. Note moteur (session #3) : pour toute candidate à SIZING INTERNE (vol-targeting dans la
   stratégie), la configuration de l'overlay du moteur doit répliquer le chemin de production
   réel (`bot/runner.py:_risk_manager_for_wallet` neutralise le vol-targeting portefeuille,
   vol_target=50.0) — jamais les défauts de l'overlay. Précédent : finding CRITIQUE corrigé
   du retest #13 (`backtest/run_quasi_passif.py`).

---

## Idées explicitement écartées du backlog (pour mémoire, ne pas retester sans raison neuve)

- **Carry/funding rate sur plateforme avec vrai broker externe** (Alpaca ou équivalent) :
  hors de propos, ce projet n'utilise plus aucun broker externe (`ARCHITECTURE.md` §0) --
  seule la version "simulée maison" (idée #1) a du sens ici.
- **Kelly fractionné comme moteur de sizing principal** (`rapport-recherche.md` §3A) :
  le rapport initial le positionnait déjà comme un plafond additionnel activable seulement
  après ≥100-300 trades réels de paper trading, jamais comme moteur principal avant
  d'avoir des statistiques fiables -- reste une idée de raffinement du `RiskManager`
  générique (hors du scope "stratégie candidate" de ce backlog, et de toute façon hors de
  portée de la boucle de recherche par construction, cf. `PROMOTION-RULES.md` §4.3).
