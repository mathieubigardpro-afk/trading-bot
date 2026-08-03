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

---

## 2026-07-23 — Boucle MÉCANIQUE d'auto-amélioration : `tools/weekly_maintenance.py`

**Contexte.** Mission dédiée à l'infrastructure (pas à une candidate ni à une décision de
promotion) : construire la boucle hebdomadaire, gratuite, sur GitHub Actions, qui (a)
signale la dérive backtest-vs-vécu de chaque stratégie sans jamais décider, et (b) recalibre
de façon étroitement encadrée le seul paramètre de signal disponible pour
`quasi_passif_crypto`. Aucune session d'évaluation de candidate n'a eu lieu dans cette même
session, conformément à `docs/PROMOTION-RULES.md` §0.

**Livré** :
- `tools/weekly_maintenance.py` : moniteur de dérive (compare `bot/reporting/tracking.py`-style
  métriques vécues, reconstruites depuis `state/wallets/*/decisions.jsonl`/`equity.jsonl`, aux
  métriques OOS de `docs/RESEARCH-REGISTRY.json`) + recalibrage encadré de `REGIME_SMA_DAYS`
  (walk-forward 9m IS / 3m OOS sur données rafraîchies via `tools/fetch_data.py`). Écrit
  `docs/DRIFT-REPORT.md`. Ne prend aucune décision de promotion/rétrogradation/mort.
- `docs/RECALIBRATION-SPEC.md` : pré-enregistrement, AVANT toute exécution réelle (réseau
  bloqué en développement), de la grille `REGIME_SMA_DAYS ∈ [150, 175, 200, 225, 250]` --
  **seul** paramètre retenu pour `quasi_passif_crypto` car c'est le seul qui ne relève PAS du
  cadre de risque (`WALLETS[*]["risque"]`, hors de portée de la recherche par
  `docs/PROMOTION-RULES.md` §4.3). Document séparé de `PROMOTION-RULES.md` (jamais touché ici,
  volontairement, cf. §0 "gravé").
- `.github/workflows/weekly-maintenance.yml` : cron dimanche 22h UTC, `workflow_dispatch`,
  push de validation sur lui-même, permissions `contents: write`, timeout 60 min.
- `tools/tests/test_weekly_maintenance.py` (57 tests, fixtures synthétiques uniquement) :
  classification OK/SURVEILLER/ALERTE sur des cas construits (§2.1/§2.2/§3.1/§3.2), refus
  structurel d'une valeur hors grille, refus d'une amélioration OOS <= 10 %, sanity du
  simulateur de walk-forward, `main()` en mode `--skip-push`.

**Point d'attention pour toute session future** : le simulateur de recalibrage (`simulate_
daily_returns`) SIMPLIFIE le calcul de vol réalisée par rapport à la formule exacte de
production (`_basket_vol_annualized`) pour rester rapide sur plusieurs années de données --
documenté explicitement dans `docs/RECALIBRATION-SPEC.md` §2, n'invalide que la valeur
absolue de ses chiffres, pas la comparaison relative entre valeurs de la grille (seul usage
qui en est fait). `xs_momentum_sp100`/`dual_momentum_multiclasse_etf`/`quasi_passif_crypto`
restent un antécédent hors du cadre formel §3 (cf. entrée du 2026-07-23 "Gouvernance") : le
moniteur les évalue quand même à titre informatif, sans déclencher d'action automatique.

---

## 2026-07-27 — Session hebdomadaire #1 (a) : AUDIT ADVERSARIAL du système d'auto-amélioration lui-même

**Contexte.** Première session hebdomadaire de recherche exécutée. Conformément au mandat de
première session, un audit adversarial du système d'auto-amélioration (jamais réalisé — le
workflow qui devait le porter n'a jamais tourné) a été mené AVANT toute autre activité, par un
agent dédié sur copie isolée (remote git neutralisé), sur 3 axes : isolation du labo,
contournabilité de `PROMOTION-RULES.md`, bornes du recalibrage `weekly_maintenance.py`.

**12 findings (3 CRITIQUES, 5 MAJEURS, 2 MINEURS, 2 INFO). Les plus graves :**

- **F1 (CRITIQUE, démontré par exécution)** : `load_strategies()` indexait les stratégies par
  `name` sans contrôle d'unicité — un module candidate labo définissant `name="quasi_passif_crypto"`
  remplaçait silencieusement la vraie stratégie de production POUR LES 3 WALLETS RÉELS
  (démonstration : poids `{'BTC': 0.99}` servi au wallet prudent). **Corrigé** : collision de
  `name` → `ValueError` + tests.
- **F2 (MAJEUR, démontré)** : une candidate labo déclarant `asset_class="crypto"` avec un
  symbole action réel (ex. AAPL) contournait le gate horaires NYSE (ordre AAPL exécuté un
  dimanche, démontré). **Corrigé** : la classe d'actif réelle d'un symbole est désormais dérivée
  des univers actions/ETF connus de `bot/config.py`, jamais de la déclaration de la poche + tests
  reproduisant l'attaque.
- **F5 (CRITIQUE)** : `backtest/engine.py`, le « moteur commun » exigé par la Porte 1 §1.1,
  **n'existait nulle part dans le dépôt** (références `bt-final/*` = poste de travail d'origine,
  jamais committé). La porte la plus fondamentale du protocole reposait à 100% sur la bonne foi
  déclarative. **Corrigé** : moteur commun créé dans `backtest/` (cf. entrée (c) ci-dessous),
  avec ses propres tests anti-look-ahead.
- **F6 (CRITIQUE)** : aucun code ne calculait ni ne vérifiait le DSR/K_total — tout reposait sur
  la discipline documentaire. **Corrigé** : `tools/verify_research.py` (`--check` valide le
  registre, `--compute --grid-size N` donne le K_total du jour) + tests.
- **F7 (MAJEUR)** : aucune vérification mécanique de la règle §0 (gouvernance et jugement jamais
  dans le même commit). **Corrigé** : `tools/check_governance_commit.py` (échoue si un commit
  touche à la fois `PROMOTION-RULES.md` et `bot/config.py`/`RESEARCH-REGISTRY.json`), branché en CI.
- **F8 (MAJEUR)** : `weekly_maintenance.py` appliquait 56 jours à toute candidate, sans porter la
  distinction §3.1 (28 jours pour une stratégie rétrogradée). **Corrigé** : champ optionnel
  `max_incubation_days` (défaut 56 ; 28 à poser explicitement pour une rétrogradée), documenté
  dans le schéma `INCUBATING_STRATEGIES`.
- **F9 (MAJEUR)** : aucun workflow CI ne lançait la suite de tests (595 tests jamais vérifiés
  avant production). **Corrigé** : `.github/workflows/ci.yml` (permissions `contents: read`,
  pytest complet + check de gouvernance).
- **F12 (MAJEUR, partiellement traité)** : les 4 workflows réseau ont `contents: write` et
  installent pandas/numpy/yfinance NON épinglés (risque supply-chain avec accès en écriture au
  dépôt entier). Traitement partiel : `requirements.txt` épinglé créé, utilisé par la CI.
  **DÉCISION HUMAINE REQUISE** (volontairement non prise par cette session, prudence sur la
  continuité de production) : épingler aussi les 5 workflows existants, et activer la protection
  de branche côté GitHub.
- Findings mineurs corrigés en passant : F3/F4 (validations structurelles max 3 candidates /
  max 5 poches / bornes `capital_alloc_pct` — désormais testées), F10 (test de synchronisation
  grille code ↔ `RECALIBRATION-SPEC.md`), F11 (historique 0-ligne traité comme absent).

**Verdicts par axe** : isolation labo = exploitable (corrigé) ; contournabilité des règles =
les seuils chiffrés eux-mêmes sont sans ambiguïté, mais TOUT reposait sur la confiance
documentaire (mécanisation ajoutée) ; recalibrage = sain dans son cœur (grille infranchissable,
seuil 10% strict, scope git étroit — vérifié par lecture ET tests), findings d'hygiène corrigés.

Suite de tests après correctifs : **637 tests verts** (595 existants + 42 nouveaux).

---

## 2026-07-27 — Session hebdomadaire #1 (b) : REVUE des stratégies actives et candidates

- **Candidates labo** : `INCUBATING_STRATEGIES` vide — aucune Porte 2 à évaluer, aucun kill 56j.
- **Stratégies actives** : `xs_momentum_sp100`, `dual_momentum_multiclasse_etf`,
  `quasi_passif_crypto` sont l'antécédent explicitement HORS du cadre §3 (`PROMOTION-RULES.md`
  §5) — pas de règle de mort applicable. À titre informatif, `DRIFT-REPORT.md` du 2026-07-26 :
  7 lignes, toutes **SURVEILLER** pour la même raison mécanique « historique vécu 4j < 28j —
  trop tôt pour un diagnostic fiable ». Le Sharpe vécu -6.68 affiché pour `quasi_passif_crypto`
  (agressif) sur 4 jours est du bruit pur (n≈4 points quotidiens) — aucune action, aucune
  décision. **Décisions de revue : zéro action requise, zéro action prise.**
- Recalibrage automatique du 2026-07-26 : `REGIME_SMA_DAYS` inchangé (175 vs 200 : +3.0% < seuil
  10%) — conforme à la spec, rien à redire.

---

## 2026-07-27 — Session hebdomadaire #1 (c) : Porte 1 de `xs_momentum_invvol_sp100` (backlog P0#3) — ÉCARTÉE

**Moteur commun d'abord (correctif F5).** `backtest/` créé : `engine.py` (signal clôture t →
exécution open t+1, coûts bps/côté sur turnover, walk-forward IS/OOS, métriques sur équity OOS
concaténée, DSR Bailey & López de Prado), `data.py` (CSV branche `market-data`, éligibilité sans
backfill), `strategies/xsmom.py` (fidèle à `bot/strategies/xs_momentum_sp100.py`, constantes
importées jamais dupliquées), `tests/` (7 tests dont anti-look-ahead démonstratif : un moteur
volontairement biaisé « en avance » donne Sharpe 1.097 vs 1.034 pour le moteur correct).

**Protocole.** Walk-forward 36m IS / 12m OOS, pas 12m, 30 fenêtres (1996-2026), coûts 5 bps/côté,
**aucune grille** : tous les paramètres pré-fixés (réglages de production + `vol_lookback_days=63`
du SPEC d'origine, présent dans le code AVANT cette session — vérifié par l'audit dans
l'historique git). K interne = 1, K_total = 9 + 1 = **10** (§1.3). Deux variantes sur les MÊMES
fenêtres : contrôle equal-weight (reproduction de la version en production) et candidate inv_vol.

**Résultats (OOS concaténé, net de coûts).**

| | Candidate inv_vol | Contrôle equal | Benchmark SPY |
|---|---|---|---|
| Sharpe | 0.947 | 1.034 | 0.608 |
| Sortino | 1.349 | 1.485 | 0.863 |
| Profit factor | 1.718 | 1.821 | — |
| MaxDD | 49.3% | 48.7% | 54.8% |
| Trades clos | 1970 | 1970 | — |

Porte 1 §1.2 : **5/5 seuils passés** (Sharpe 0.947≥0.70 ; PF 1.718>1.15 ; 1970≥80 trades ;
MaxDD relatif 0.90≤1.5 ; DSR 0.9998≥0.50 avec K_total=10). Stress de coûts : PF 1.61 à 3×,
1.50 à 5× (robuste). Audit adversarial indépendant (§1.4) : **`isSound: true`** — anti-look-ahead
vérifié par expérience contradictoire, coûts recalculés indépendamment (identiques à 3 décimales),
sizing compatible avec le `RiskManager` réel (poids inv_vol max 24.2%, jamais clippé par les
wallets réels), aucune retouche post-OOS (la seule correction de bug en session a DÉGRADÉ le PF —
sens opposé à la complaisance).

**Verdict : ÉCARTÉE, pas d'incubation.** Sur les mêmes 30 fenêtres, la candidate est dominée par
le contrôle equal-weight (= la version DÉJÀ en production) sur tous les axes ; Information Ratio
inv_vol vs equal = **-0.837** (≈4.6σ sur 7549 obs. — pas du bruit). L'audit confirme qu'aucun des
biais identifiés (survivant, anomalie spin-off DHR, source de données) ne peut inverser ce
classement relatif apparié (mêmes titres, mêmes fenêtres, même moteur). Incuber une variante
strictement dominée ne créerait aucune valeur et consommerait une place de labo + du K_total.
**Ambiguïté de règle notée** (option conservatrice retenue) : `PROMOTION-RULES.md` §1 ne prévoit
pas explicitement le cas « tous les seuils passés mais dominée par la version incumbent de la même
stratégie ». Proposition pour une future session de gouvernance DÉDIÉE (jamais dans une session
de jugement, §0) : ajouter à la Porte 1 un critère explicite de valeur marginale vs incumbent.

**Réserves documentées (honnêteté).** (a) Baseline equal reproduite à Sharpe 1.034 vs 0.823
historique (+25.6%) — données yfinance révisées/ajustées différemment du poste d'origine
(`bt-final/` absent du dépôt, comparaison directe impossible), biais du survivant quantifié par
l'audit (~-6% de Sharpe en excluant les IPO≥2005), anomalie de spin-off DHR identifiée (juillet
2016, sans impact favorable — l'exclure AMÉLIORE légèrement le résultat). Le classement relatif,
seul fondement de la décision, y est insensible. (b) Note méthodologique de l'audit : sur un
historique OOS de ~30 ans (n=7549), le seuil DSR≥0.50 est peu discriminant quel que soit K —
à discuter dans une future session de gouvernance.

**Effets** : entrée n°10 dans `RESEARCH-REGISTRY.json` (statut `ecartee`, K_total documenté) ;
prochaine candidate → K_total = 11. Labo toujours vide (0/3 places), état attendu.

---

## 2026-08-03 — Session hebdomadaire #2 (a) : REVUE des stratégies actives et candidates

- **Candidates labo** : `INCUBATING_STRATEGIES` toujours vide — aucune Porte 2 à évaluer,
  aucun kill 56j. **Zéro action requise, zéro action prise.**
- **Stratégies actives** : les 3 stratégies de production restent l'antécédent HORS cadre §3
  (`PROMOTION-RULES.md` §5). `DRIFT-REPORT.md` du 2026-08-02 : 7 lignes, toutes **SURVEILLER**
  pour la même raison mécanique (11j vécus < 28j — trop tôt). Les Sharpe vécus extrêmes
  (dual_momentum 10.11, quasi_passif agressif -2.28) sont du bruit d'échantillon court —
  aucune action. Recalibrage du 2026-08-02 : SAUTÉ par le workflow (données crypto
  indisponibles ce cycle-là) — non bloquant, le moniteur retentera dimanche prochain ; à
  surveiller si le saut se répète 2 semaines de suite.
- Hygiène : correctif d'un test qui pourrissait avec le temps (`test_crypto.py`, `_now_utc`
  non figé — vert à l'écriture le 27/07, rouge mécaniquement dès J+5). Suite : 670 verts.

---

## 2026-08-03 — Session hebdomadaire #2 (b) : AUDIT ADVERSARIAL de `backtest/risk_overlay.py` (dette du 27/07) — 3 CRITIQUES corrigés

**Contexte.** Dette explicite en tête de `RESEARCH-BACKLOG.md` : la surcouche de risque du
moteur commun (ajoutée le 27/07) n'avait jamais reçu d'audit adversarial indépendant. Audit
mené par un agent dédié sur copie isolée (remote neutralisé), scripts d'attaque exécutés sur
données réelles, AVANT toute décision fondée sur ce moteur. Verdict initial : **isSound: false**,
3 findings CRITIQUES — tous corrigés dans la même session (commit 2be992b) :

- **F1 (CRITIQUE, démontré)** : la no-trade band figeait le POIDS cible puis reprojetait les
  shares dessus À CHAQUE barre (equity/prix mouvants) → un ordre sur **96,9% des bougies BTC
  réelles malgré un signal constant** — bande vidée de son effet, coûts et « rebalancing
  premium » fantômes, sémantique opposée à `bot/risk/manager.py` étape 6. Corrigé : la bande
  compare désormais le poids cible au poids réellement PORTÉ (position dérivée marquée à
  l'open) ; symbole dans la bande = shares strictement conservées. Le masquage venait des
  fixtures de tests à prix CONSTANTS (reprojection invisible) — 2 tests à prix mouvants ajoutés.
- **F2 (CRITIQUE, démontré)** : les défauts de vol targeting « quotidiens » (halflife 2.5
  LIGNES, √252) appliqués tels quels à des bougies horaires sous-estimaient la vol ~12× (BTC
  réel : 0.04 au lieu de 0.48) → scalar cloué à 1.0, aucun dérisking. Corrigé : garde
  explicite (ValueError sur calendrier intra-journalier + défauts quotidiens), constantes
  `HOURLY_*` alignées production (60 lignes, √8760), `sim_kwargs` propagés à
  `select_params_via_is` (la sélection IS doit simuler comme l'OOS).
- **F3 (CRITIQUE, démontré)** : un poids NaN n'était ni filtré (`NaN < eps` = False) ni
  propagé (`min(1.0, nan)` = 1.0 en Python) → UN SEUL poids NaN désactivait silencieusement
  le vol targeting de TOUT le portefeuille. Corrigé : refus bruyant (engine + overlay).
- Axes vérifiés SANS finding : causalité de `precompute_vol_stats` (attaque par perturbation
  des données futures : propre), coûts sur turnover post-bande (arithmétique correcte),
  réinitialisation d'état entre segments walk-forward (correcte et voulue).

**Portée sur l'antécédent** : les chiffres de `xs_momentum_invvol_sp100` (session #1) ont été
produits sur le moteur pré-correctif. Le biais F1 s'appliquait identiquement aux deux variantes
comparées (même moteur, mêmes fenêtres) — le verdict apparié « ecartee » tient ; les NIVEAUX
absolus (Sharpe 1.034/0.947) ne doivent plus servir de référence sans re-run (note ajoutée au
registre). Suite complète après correctifs : **674 tests verts** (+4).

---

## 2026-08-03 — Session hebdomadaire #2 (c) : Porte 1 de `vol_breakout_6majors` (backlog P0#2) — ÉCHEC, ÉCARTÉE

**Protocole.** SPEC intégralement PRÉ-ENREGISTRÉE et committée AVANT toute exécution (commit
59647d3) : grille figée 4 combos (W∈{55,110} × P∈{0.20,0.35}, valeurs reprises des lookbacks
Donchian/EMA du projet, pas optimisées pour l'idée), coûts 25 bps/côté (palier « mids »
uniforme, pessimiste pour BTC/ETH/SOL), walk-forward 9m IS / 3m OOS / pas 3m sur bougies
horaires 2022-01→2026-06 (14 fenêtres, 30 671 h OOS), moteur commun post-correctifs F1/F2/F3
avec paramètres horaires explicites. Signal : squeeze Bollinger (percentile de bandwidth sur
90j ≤ P dans les 24 dernières heures) + cassure de la bande haute + filtre de régime SMA 200j,
sortie sous la bande médiane, long-only 1/6 par actif. Implémentation par agent dédié,
chargeur horaire commun livré au passage (`backtest/data_hourly.py`, réutilisable).

**Résultats (OOS concaténé, net de coûts, √8760).**

| | Candidate | Benchmark B&H équipondéré |
|---|---|---|
| Sharpe | **0,434** | 0,653 |
| Profit factor | 1,209 | — |
| MaxDD | 23,3% | 66,8% |
| Trades clos | 302 | — |

Porte 1 §1.2 : **2/5 seuils manqués, marges larges** — Sharpe 0,434 < 0,70 (−38%) et DSR
0,058 < 0,50 (−88%, K_total = 10 + 14×4 = 66). PF sous 1,0 dès 3× les coûts (0,77). Sharpe
par sous-période : **−0,58 en 2022-2023 vs +0,95 en 2024-2026** — tout l'edge apparent vient
de la moitié récente, motif classique à ne PAS retester en variante (§3.3). Analyses
d'honnêteté saines : 299 épisodes de squeeze distincts pour 302 trades (le faible nombre
d'épisodes indépendants redouté par le backlog ne s'est pas matérialisé), corrélation 0,45 au
proxy quasi-passif.

**Contre-audit adversarial indépendant : `isSound: true`** — results.json REPRODUIT BIT-À-BIT
depuis les données brutes par re-exécution indépendante, causalité attaquée sur données
réelles (16 perturbations, zéro fuite), DSR/K_total/seuils recalculés indépendamment,
aucun signe de retouche post-OOS. Un finding cosmétique : `select_params_via_is` annualise le
Sharpe IS d'affichage à √252 au lieu de √8760 (facteur constant, argmax inchangé, aucun
chiffre de décision affecté — à corriger à l'occasion).

**Verdict : ÉCARTÉE, pas d'incubation.** 3e stratégie technique active de suite sous le simple
buy & hold sur l'univers 6 majors (après Donchian 0,31 et EMA 0,24) — le constat de la vague 1
se confirme sur une famille de signal pourtant structurellement différente. Entrée n°11 au
registre ; prochaine candidate → K_total = 11 lignes + sa grille. Labo toujours vide (0/3).
