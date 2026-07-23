# RESEARCH-LOG.md — Journal chronologique des sessions de recherche

*Journal append-only. Chaque session de recherche (backtest, audit, changement de
gouvernance, décision de promotion/rétrogradation/mort) y ajoute une entrée datée, jamais
une réécriture du passé. Complète `docs/RESEARCH-REGISTRY.json` (état structuré, une ligne
par stratégie) : ce fichier raconte le **fil narratif** des décisions, avec leur contexte
et leurs verdicts, notamment pour tout ce qui n'est pas capturable dans le format JSON
strict du registre (raisonnement d'audit, arbitrages, changements de règles).*

---

## 2026-07-23 — Vague 1 : premiers backtests audités (actions, ETF, crypto) — antérieure à la gouvernance formelle

**Contexte.** Première campagne de recherche du projet, menée intégralement **avant**
l'existence de `docs/PROMOTION-RULES.md` et du wallet labo 🧪 (créé plus tard le même jour,
commit `9ab6700`). Aucune règle de promotion chiffrée n'existait encore -- les décisions de
cette vague ont été prises directement au niveau "backtest → production", sans étape
d'incubation intermédiaire. Consignée ici a posteriori pour que l'historique reste complet
et honnête (cf. `docs/PROMOTION-RULES.md` §5, antécédent explicitement assumé comme non
conforme au protocole qui le suit).

**Moteur** : `backtest/engine.py` (moteur commun), signal à la clôture t, exécution à
l'open t+1, coûts en bps/côté systématiquement appliqués (jamais de backtest brut).

**8 stratégies auditées avec walk-forward complet** (méthode : IS/OOS glissant, grille de
paramètres sélectionnée en IS uniquement, métriques de décision = équity OOS concaténée) :

| Stratégie | Classe | Sharpe OOS | Verdict |
|---|---|---|---|
| `donchian_ensemble_6majors` | crypto | 0.31 | Écartée (sous-benchmark 0.55) |
| `ema_momentum_6majors` | crypto | 0.24 | Écartée (sous-benchmark 0.55) |
| `meanrev_rsi2_6majors` | crypto | -10.47 | Rejetée (MaxDD 94.9%) |
| `ema_momentum_30univers` (trend-30) | crypto (30 actifs) | -0.29 | Rejetée |
| `xs_momentum_30` (30 complet + 15 restreint) | crypto | -0.34 (les deux variantes) | Rejetée |
| `meanrev_30` (30 complet + 15 restreint) | crypto | -11.14 / -10.05 | Rejetée (catastrophique) |
| `xs_momentum_sp100` | actions | 0.82 | **Retenue** (seule stratégie active à edge net) |
| `dual_momentum_multiclasse_etf` | ETF multi-classes | 0.65 | Retenue avec réserve majeure (ne bat pas le 60/40) |

**Verdict global de la vague** : sur les 6 stratégies actives crypto testées, **aucune ne
survit** (0/6 déployée). Sur les 8 stratégies auditées au total, une seule
(`xs_momentum_sp100`) a un edge net, robuste et statistiquement crédible ; une seconde
(`dual_momentum_multiclasse_etf`) est retenue malgré un edge non prouvé, pour sa valeur de
diversification structurelle, avec un critère d'échec chiffré explicite posé en
contrepartie.

**Audit adversarial appliqué** : un correctif d'audit a été identifié et appliqué sur
`xs_momentum_sp100` -- biais de warm-up favorisant artificiellement le lookback 6 mois vs
12 mois. Correction appliquée : Sharpe 0.73 → 0.82 (dans le bon sens, la conclusion ne
change pas -- pas un artefact qui disparaît sous correction, plutôt un signe de robustesse).
Note pour la gouvernance future : cet audit n'a pas produit de verdict formel `isSound`
tel que `docs/PROMOTION-RULES.md` §1.4 l'exige désormais -- c'est précisément ce type
d'informalité que la Porte 1 formalisée cherche à éliminer pour toute recherche future.

**DSR calculé, mais sans K_total inter-stratégies** : `xs_momentum_sp100` a un DSR de 0.92
déflaté sur 216 combinaisons (`dsr_K216_conservative_all_windows_all_combos`) -- mais ce
`K=216` ne compte que la grille interne à cette stratégie, pas les 7 autres stratégies déjà
testées avant elle dans la même vague (le concept de `K_total` inter-stratégies,
`docs/PROMOTION-RULES.md` §1.3, n'existait pas encore). Un futur audit rétrospectif
pourrait recalculer un DSR plus conservateur avec `K_total ≈ 8` (les 8 stratégies
auditées de cette vague) + 216 -- non fait à ce stade, signalé pour transparence.

**Décision de portefeuille** : cf. `docs/SELECTION-FINALE.md` (document de décision séparé,
rédigé à partir de ces résultats + de l'analyse de diversification ci-dessous). Composition
retenue par wallet : poche ETF = dual-momentum (prudent/équilibré), poche actions =
xs_momentum_sp100 (équilibré/agressif), poche crypto = quasi-passif vol-targeté (les 3
wallets, cf. entrée suivante).

**Entrées `RESEARCH-REGISTRY.json`** : les 9 entrées initiales du registre (8 stratégies
auditées ci-dessus + `quasi_passif_crypto`, cf. entrée suivante) correspondent
intégralement à cette vague.

---

## 2026-07-23 — Analyse de diversification crypto (complément vague 1)

**Contexte.** Étude complémentaire, non un backtest de stratégie de trading à proprement
parler, mais une analyse de composition de portefeuille (nombre effectif de paris/ENB,
corrélations de crise) sur l'univers crypto, produite pour trancher la composition du
panier "agressif" (12 actifs diversifiés vs 30 cryptos complet vs 6 majors V1).

**Résultat clé** : la composition du panier crypto compte plus que son nombre nominal
d'actifs -- un panier de 6 noms bien choisis (BTC + les 5 moins corrélés à BTC) obtient un
score de diversification (PR/ENB) supérieur aux 30 cryptos complets. Les 6 majors V1
originels sont, à l'inverse, le panier de 6 le plus corrélé disponible dans l'univers.
Conséquence directe : le book crypto doit être traité comme ~1 pari corrélé, pas comme N
paris indépendants, quel que soit le nombre nominal d'actifs -- confirmation empirique
chiffrée du principe déjà posé par `rapport-recherche.md` §3E.

**Décision** : recommandation de resserrer l'univers crypto du wallet agressif à un panier
de 12 actifs diversifiés (`CRYPTO_SYMBOLS_AGRESSIF_12` dans `bot/config.py`), adoptée et
implémentée (remplace l'ancien univers 30 cryptos complet).

**Fichiers sources** : `bt-final/analyse-diversification/diversification-univers-crypto.md`,
`key_numbers.json`.

---

## 2026-07-23 — Backtest quasi-passif crypto (non audité, complément vague 1)

**Contexte.** Suite au verdict "0/6 stratégies actives crypto retenues", un backtest de
l'alternative "quasi-passive" (détention long/flat vol-targetée, filtre SMA200, paramètres
non optimisés sur ces données -- repris tels quels de la configuration déjà en production +
composition de panier motivée par l'analyse de diversification ci-dessus) a été produit
pour vérifier que cette alternative domine bien le buy & hold brut et les stratégies
actives rejetées.

**Résultat** : Sharpe 1.24 (prudent, BTC+ETH), 1.47 (équilibré, 6 majors), 1.49 (agressif,
panier 12 diversifié) -- tous largement supérieurs au buy & hold équipondéré du même panier
(0.18-0.44) et aux stratégies actives crypto de la vague 1 (0.24 à -11.14). MaxDD contenu
(8-33% contre 69-80% pour le buy & hold brut).

**Niveau de rigueur explicitement inférieur** : ce backtest est une **exécution unique du
moteur commun, sans walk-forward, sans audit adversarial, sans test de significativité**
-- documenté comme tel dans `docs/SELECTION-FINALE.md` §2.2 dès l'origine, pas découvert
après coup. Les Sharpe élevés sont expliqués par une exposition brute réalisée faible
(12-48%, le mécanisme de vol-targeting laisse le book majoritairement en cash) plutôt que
par un signal exceptionnel -- lecture qualitative jugée non suspecte par le rapport de
décision, mais explicitement signalée comme "à retester avec le protocole complet avant de
considérer ces chiffres comme définitifs".

**Décision** : adoptée comme base de la poche crypto des 3 wallets réels malgré ce niveau
de rigueur inférieur -- seule alternative disponible face à l'échec net des 6 stratégies
actives testées, avec un critère d'échec chiffré explicite posé en contrepartie
(`docs/SELECTION-FINALE.md` §5, "point de vigilance le plus important du document").

**Action de suivi non close à ce jour** : ce backtest reste à retester avec le protocole
walk-forward + DSR + audit adversarial complet tel que défini par `docs/PROMOTION-RULES.md`
§1 avant d'envisager toute augmentation de capital sur cette brique -- inscrit ici comme
dette de recherche explicite, pas oublié.

---

## 2026-07-23 — Création du wallet labo 🧪 (infrastructure d'incubation)

**Contexte.** Décision de construire une capacité d'auto-amélioration continue du bot :
incuber de futures stratégies candidates dans un 4e wallet isolé, à capital strictement
séparé des 3 wallets réels, avant toute promotion. Commit `9ab6700`.

**Livré** : `bot/config.py:INCUBATING_STRATEGIES` (vide, schéma documenté), helpers
`labo_pockets()`/`labo_crypto_universe()`/`incubating_strategy()`, `LABO_WALLET_ID`,
`PRODUCTION_WALLET_IDS`, 4e wallet dans `WALLETS` (profil "équilibré-strict" : vol_target
0.20, gross_exposure_max 0.70, cap_per_asset 0.20 -- volontairement plus resserré que le
profil équilibré standard, 0.25, pour qu'aucune candidate seule ne concentre une part
disproportionnée du capital labo pendant sa période de jugement). Poches/univers vides et
dynamiques (dérivées de `INCUBATING_STRATEGIES`, vide à ce stade → labo intégralement en
cash, état attendu).

**Ce qui n'a PAS encore été livré à ce stade** : les règles de promotion chiffrées
elles-mêmes -- explicitement hors périmètre de ce commit (cf. bandeau
`INCUBATING_STRATEGIES` dans `bot/config.py`). Objet de l'entrée suivante.

---

## 2026-07-23 — Gouvernance de la recherche : PROMOTION-RULES, RESEARCH-REGISTRY, RESEARCH-BACKLOG, RESEARCH-LOG

**Contexte.** Mission dédiée : écrire les règles pré-enregistrées qui empêchent le wallet
labo 🧪 de devenir une usine à sur-apprentissage. Rédigées et committées **avant** toute
recherche menée dans le cadre de ce processus (aucune candidate n'a encore été proposée à
la date de cette entrée) -- condition nécessaire à leur validité en tant que
pré-enregistrement (`docs/PROMOTION-RULES.md` §0).

**Livré** :
- `docs/PROMOTION-RULES.md` : Porte 1 (backtest → labo, seuils walk-forward/DSR/audit
  adversarial), Porte 2 (labo → wallets réels, seuils d'incubation vécue), règles de mort
  (stratégie active dégradée/tuée, candidate labo tuée après 56 jours), limites
  structurelles (max 3 candidates labo, max 5 stratégies actives/wallet, framework de
  risque hors de portée de la recherche, critère chiffré de création d'un 4e wallet
  permanent).
- `docs/RESEARCH-REGISTRY.json` : initialisé avec les 9 stratégies de la vague 1
  (2026-07-23, entrée précédente), condition nécessaire pour que le calcul du DSR
  (`K_total`) de toute future candidate soit honnête dès son premier test.
- `docs/RESEARCH-BACKLOG.md` : 10 idées classées P0 à P3, semées depuis
  `rapport-recherche.md` §7 (saisonnalité horaire BTC, carry/funding déjà identifié comme
  le plus robuste de la littérature mais incompatible avec la contrainte de plateforme
  d'origine -- contrainte levée dans ce projet) et connaissance générale
  (breakout volatilité, momentum inverse-vol, pairs ETH/BTC, protective put synthétique,
  extensions actions mid-cap, régime cross-asset, sentiment/on-chain, infrastructure
  short/perp partagée).
- `docs/RESEARCH-LOG.md` : ce document.

**Point d'attention explicite pour toute session future** : aucune modification de
`docs/PROMOTION-RULES.md` ne doit être committée dans le même commit qu'une décision de
promotion/rétrogradation/mort d'une stratégie précise (règle que ce document s'impose à
lui-même, §0). Toute future entrée de ce journal qui documente un changement de règle doit
donc être une session dédiée, distincte de toute évaluation de candidate.

**Prochaine étape attendue (non réalisée à cette date)** : première proposition de
candidate suivant intégralement le protocole Porte 1 (cf. `docs/RESEARCH-BACKLOG.md` pour
les idées les mieux priorisées -- momentum inverse-vol P0#3 est le candidat le plus simple
à instrumenter en premier, changement incrémental d'une stratégie déjà validée).
