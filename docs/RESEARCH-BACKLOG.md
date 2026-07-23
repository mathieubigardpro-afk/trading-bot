# RESEARCH-BACKLOG.md — Backlog d'idées de recherche, classées par priorité

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

### 1. Funding carry sur perpétuels SIMULÉS (crypto)

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

### 2. Breakout de volatilité crypto avec filtre de régime

**Hypothèse** : les expansions soudaines de range (ex. cassure de bande de Bollinger/ATR
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

### 3. Momentum actions ajusté par volatilité inverse (inverse-vol weighting)

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

### 4. Saisonnalité horaire BTC (21h-23h UTC) — à revalider 2024-2026

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

### 5. Pairs trading ETH/BTC (spread trading, marché-neutre relatif)

**Hypothèse** : ETH et BTC partagent un bêta crypto commun élevé (corrélation 0,89 rapportée
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

### 6. Protective put synthétique (couverture de queue pour les wallets réels)

**Hypothèse** : plutôt qu'une nouvelle source d'edge, une brique de **réduction de risque**
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
