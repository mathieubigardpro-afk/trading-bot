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

---

## 2026-08-10 — Session hebdomadaire #3 (a) : REVUE des stratégies actives et candidates

- **Candidates labo** : `INCUBATING_STRATEGIES` toujours vide — aucune Porte 2 à évaluer,
  aucun kill 56j. **Zéro action requise, zéro action prise.**
- **Stratégies actives** : les 3 stratégies de production restent l'antécédent HORS cadre §3
  (`PROMOTION-RULES.md` §5). `DRIFT-REPORT.md` du 2026-08-09 : 7 lignes, toutes **SURVEILLER**
  pour la même raison mécanique (18j vécus < 28j — trop tôt pour un diagnostic fiable). Aucune
  action. Recalibrage du 2026-08-09 : exécuté normalement (le saut du 2026-08-02 ne s'est pas
  répété), `REGIME_SMA_DAYS` inchangé (175 vs 200 : +3.0% < seuil 10%) — conforme à la spec.
- Wallets au cycle 2026-08-10T06 : 🛡️ 997 € | ⚖️ 988 € | 🔥 984 € | 🧪 988 € (labo 100% cash,
  état attendu).

---

## 2026-08-10 — Session hebdomadaire #3 (b) : RETEST de `quasi_passif_crypto` (backlog P2#13, dette de recherche) — ÉCHEC 3/3, dette soldée

**Contexte.** La seule brique crypto en production (poche crypto des 3 wallets réels) avait été
déployée le 2026-07-23 sur un backtest explicitement NON audité (exécution unique, ni
walk-forward ni DSR ni audit — `SELECTION-FINALE.md` §2.2). Dette de recherche inscrite dès
l'origine, promue priorité n°1 par la revue de la session #2. SPEC intégralement PRÉ-ENREGISTRÉE
et committée AVANT toute exécution (commit fb5b8b5), y compris la sémantique des issues : un
échec ne déclenche AUCUNE action automatique sur la production (antécédent hors §3, §5).

**Protocole.** Moteur commun post-correctifs F1/F2/F3, chargeur horaire commun, 14 fenêtres
9m IS / 3m OOS (2022-10→2026-03, 30 671 h OOS/variante), AUCUNE grille (paramètres de production
gelés, helpers de production IMPORTÉS jamais réimplémentés), 3 variantes = les 3 déploiements
réels, coûts au palier le plus défavorable par univers (15/25/45 bps/côté), K_total = 11 + 3×14×1
= **53**, benchmark B&H équipondéré par univers sur fenêtres OOS alignées.

**Résultats (chiffres de décision, post-correctif de fidélité — cf. finding ci-dessous).**

| Variante | Sharpe OOS | PF | MaxDD | Trades | DSR | Bench Sharpe | Verdict §1.2 |
|---|---|---|---|---|---|---|---|
| prudent (BTC+ETH, 15 bps) | 0,808 | 1,080 | 8,4% | 28 | 0,215 | 0,761 | ÉCHEC 2/5 (PF, DSR) |
| équilibré (6 majors, 25 bps) | 0,283 | 0,718 | 27,3% | 92 | 0,038 | 0,653 | ÉCHEC 3/5 (Sharpe, PF, DSR) |
| agressif (11 divers., 45 bps) | 0,069 | 0,771 | 56,4% | 192 | 0,015 | 0,537 | ÉCHEC 3/5 (Sharpe, PF, DSR) |

Équilibré et agressif : SOUS leur benchmark ET perdants nets aux coûts nominaux. Rupture
temporelle (motif `vol_breakout_6majors`) : Sharpe avant 2024 = 1,16/1,02/1,14, depuis 2024 =
0,61/**−0,17**/**−0,55** — tout l'edge apparent vient de 2022-2023. Corrélation inter-variantes
0,75-0,83 (~1 pari corrélé). Les Sharpe non audités d'origine (1,24/1,47/1,49) ne sont PAS
reproduits — écart ×1,5 à ×21, note ajoutée au registre : ils ne doivent plus servir de référence.

**Audit adversarial indépendant (copie isolée, remote neutralisé) : `isSound: true`** —
reproduction bit-à-bit des 3 variantes depuis les données brutes, zéro look-ahead (attaque par
perturbation), DSR réimplémenté from scratch, fidélité à la production confirmée par confrontation
directe adaptateur vs `QuasiPassifCrypto.target_weights` (21 tests, 0 mismatch). **1 finding
CRITIQUE corrigé en session** : le runner de backtest appliquait à l'overlay du moteur le
vol_target du wallet (2e couche de vol-targeting) alors que la production le NEUTRALISE
(`bot/runner.py:_risk_manager_for_wallet`, vol_target=50.0 — le seul vol-targeting réel est
interne à la stratégie). Corrigé, re-exécuté (Sharpe 0,786/0,224/0,083 → 0,808/0,283/0,069),
**aucun des 15 verdicts ne bascule** ; chiffres pré-correctif conservés dans results.json.
Point d'attention pour TOUTE future candidate à sizing interne : la configuration de l'overlay
doit être explicitement décidée d'après le chemin de production réel, pas par défaut.

**Décision (conforme à la sémantique pré-enregistrée).** Aucune action automatique :
`quasi_passif_crypto` reste en production (antécédent hors §3), son critère d'échec opérationnel
reste `SELECTION-FINALE.md` §5 (bascule si sous-performance vécue 3 mois). MAIS le constat est
lourd : la seule brique crypto des 3 wallets repose sur un backtest désormais NON confirmé par le
protocole complet. **Signalement priorité haute à Mathieu + session de gouvernance DÉDIÉE requise**
(jamais celle-ci, §0) pour statuer sur l'alignement de l'antécédent — options à trancher hors
session de jugement : conserver sous critère §5 vécu, resserrer, ou retirer/réduire la poche.
Entrée n°12 au registre ; prochaine candidate → K_total = 12 lignes + sa grille. Labo toujours
vide (0/3). Suite de tests : 684 verts.

---

## 2026-08-24 — Session hebdomadaire #4 (a) : REVUE des stratégies actives et candidates

- **Candidates labo** : `INCUBATING_STRATEGIES` toujours vide — aucune Porte 2 à évaluer,
  aucun kill 56j. **Zéro action requise, zéro action prise.**
- **Stratégies actives** : les 3 stratégies de production restent l'antécédent HORS cadre §3
  (`PROMOTION-RULES.md` §5). `DRIFT-REPORT.md` du 2026-08-23 : 7 lignes, toutes **SURVEILLER**
  pour la même raison mécanique (32j vécus < 60j — Sharpe roulant 60j non calculable). Les
  Sharpe vécus extrêmes (quasi_passif équilibré +5,41, prudent −2,57) restent du bruit
  d'échantillon court — aucune action. NOTE d'hygiène : le rapport affiche encore comme
  « attendu » les Sharpe non audités discrédités par le retest #3 (1,24/1,47/1,49 lus dans le
  registre) — inscrit au backlog (#15), la référence devrait devenir celle du retest audité.
- Recalibrage du 2026-08-23 : exécuté normalement, `REGIME_SMA_DAYS` inchangé (175 vs 200 :
  +3,0% < seuil 10%) — conforme à la spec.
- Wallets au cycle 2026-08-24T07 : 🛡️ 985 € | ⚖️ 998 € | 🔥 970 € | 🧪 974 € (labo 100% cash,
  état attendu).

---

## 2026-08-24 — Session hebdomadaire #4 (b) : infrastructure P0#11 (anomalies de données) et P0#1 étape données (funding/perp) — audit adversarial, 3 findings corrigés

**Contexte.** La priorité n°1 du backlog (#14, gouvernance de l'antécédent `quasi_passif_crypto`)
exige une décision humaine hors boucle de recherche — traitée en (c) ci-dessous par un dossier
d'instruction, sans décision. Conformément à l'ordre pré-enregistré par la revue de la session #3,
cette session n'a jugé AUCUNE candidate et a livré les deux chantiers d'infrastructure suivants
(implémentation par 2 agents dédiés, audit par un agent adversarial indépendant sur copie isolée,
remote neutralisé).

**P0#11 — Détecteur d'anomalies de corporate actions (livré).**
`tools/check_data_anomalies.py` (+ 24 tests) : détection sur les CSV quotidiens actions/ETF de
(a) |rendement close-to-close| > 40%, (b) incohérences OHLC (avec tolérance de comparaison 1e-8
documentée contre le bruit de flottant des prix ajustés — jamais une correction de donnée),
(c) trous de calendrier > 10 j. Principe : JOURNALISER pour revue humaine, jamais corriger, jamais
bloquer. Intégré à `tools/fetch_data.py:main()` : chaque régénération de `market-data` publie
désormais `DATA_ANOMALIES.md` + `anomalies.json` sur la branche et résume dans `DATA_REPORT.md`.
**Démonstration sur les données réelles actuelles : 21 anomalies** — le spin-off DHR/Fortive
(+61,2% le 2016-07-05) qui motivait l'idée est bien détecté ; la plupart des autres spikes sont de
vrais mouvements de crise 2008-2009 (faux positifs assumés, à trancher par revue humaine) ; et
**2 incohérences OHLC réelles et récentes** (ABT et MS, séance du 2026-07-24 : open hors du range
high/low, volumes anormalement bas — capture de séance vraisemblablement tronquée côté
fournisseur), dignes de revue — elles devraient disparaître à la prochaine régénération hebdo,
sinon investiguer.

**P0#1 étape (a) — Pipeline de données funding rates + klines perp (livré, PREMIER RUN RÉEL À
SURVEILLER).** `tools/fetch_data.py` étendu (sections `funding` et `perp` de `--only`, actives
par défaut) : archives bulk Binance Vision USDT-M (fundingRate + klines 1h) + complément mois
courant via fapi, publication `data/funding/*.csv.gz` et `data/perp/*.csv.gz` sur `market-data`,
manifest/rapport étendus, tolérance aux perps listés après le spot (skip AVANT listing = normal
journalisé ; trou AU MILIEU = anomalie journalisée), flag |funding| > 3% sans suppression.
38 tests offline (réseau bloqué dans le conteneur de session — les formats réels d'archives et le
budget temps seront validés au premier run GitHub Actions ; timeout du workflow porté à 75 min).
Ceci débloque la moitié « données » du funding carry — reste l'extension short/perp du simulateur
(audit adversarial préalable obligatoire) avant toute Porte 1.

**Audit adversarial indépendant (copie isolée) : verdict initial `isSound: false` — corrigé puis
contre-vérifié `isSound: true`.** 3 findings, tous démontrés par exécution :
- **F1 (CRITIQUE)** : l'intégration du scan dans `main()` pointait `staging/equities` au lieu de
  `staging/data/equities` — le scan n'aurait JAMAIS rien scanné en production (« 0 anomalie sur
  0 fichier » présenté fièrement). Corrigé, re-démontré (104/104 fichiers scannés).
- **F2 (MAJEUR)** : `anomalies.json`/`DATA_ANOMALIES.md` écrits en staging mais absents de
  `_DEFAULT_PUBLISH_ENTRIES` — jamais publiés sur la branche (lien mort dans DATA_REPORT.md).
  Corrigé + test de publication réelle sur dépôt jetable.
- **F3 (MAJEUR, tests)** : le test d'intégration mockait le même chemin erroné que le code testé
  (raison structurelle pour laquelle 748 tests verts n'avaient pas attrapé F1). Corrigé ;
  l'auditeur a vérifié par expérience rouge/vert que le test corrigé attrape la réintroduction
  du bug.
Axes vérifiés sains par l'audit : zéro régression des sections crypto/equities/etf existantes
(diff 100% additif, fonction par fonction), parseurs funding/perp corrects sur variantes de
schéma, aucun fichier interdit touché (bot/risk/, config WALLETS, PROMOTION-RULES, state/ —
§4.3), workflow modifié a minima (timeout uniquement). Suite finale : **749 tests verts + 1 skip**
(683 baseline + 66 nouveaux).

---

## 2026-08-24 — Session hebdomadaire #4 (c) : dossier d'instruction gouvernance #14/#12 — AUCUNE DÉCISION PRISE

`docs/GOVERNANCE-DOSSIER-2026-08-24-quasi-passif.md` : instruction complète de la décision
requise sur l'antécédent `quasi_passif_crypto` (options (a) statu quo sous critère vécu
`SELECTION-FINALE.md` §5 — échéance naturelle fin octobre 2026 ; (b) alignement formel sur les
règles de mort §3 avec référence attendue re-basée sur le retest audité ; (c) réduction/retrait
ciblé des variantes équilibré/agressif, conservation de la variante prudente), plus les textes
proposés pour les amendements #12a (valeur marginale vs incumbent) et #12b (discriminance du DSR
sur OOS longs — recommandation : test Jobson-Korkie vs benchmark en priorité). **Décision humaine
requise (Mathieu), adoption en session dédiée uniquement (§0), rien n'est appliqué par cette
session** — la composition des wallets réels et PROMOTION-RULES.md sont inchangés.

---

## 2026-08-31 — Session hebdomadaire #5 (a) : REVUE des stratégies actives et candidates

- **Candidates labo** : `INCUBATING_STRATEGIES` toujours vide — aucune Porte 2 à évaluer,
  aucun kill 56j. **Zéro action requise, zéro action prise.**
- **Stratégies actives** : les 3 stratégies de production restent l'antécédent HORS cadre §3
  (`PROMOTION-RULES.md` §5). `DRIFT-REPORT.md` du 2026-08-31 : 7 lignes, toutes **SURVEILLER**
  pour la même raison mécanique (39j vécus < 60j — Sharpe roulant 60j non calculable). Sharpe
  vécus toujours du bruit d'échantillon court (quasi_passif équilibré +3,89, prudent −0,95 ;
  xs_momentum équilibré +3,64, agressif −0,33) ; DD vécus ≤ 1,7 % partout. Aucune action.
  Le rapport affiche toujours comme « attendu » les Sharpe non audités (1,24/1,47/1,49) —
  backlog #15 non traité cette session (priorité donnée à P0#1).
- **Gouvernance #14** : aucune décision humaine enregistrée depuis le dossier du 2026-08-24 —
  le critère vécu de `SELECTION-FINALE.md` §5 tranchera de lui-même vers fin octobre 2026 si
  rien n'est décidé d'ici là. Rappel priorité haute.
- Recalibrage du 2026-08-31 : exécuté normalement (15 fenêtres), `REGIME_SMA_DAYS` inchangé
  (175 vs 200 : +3,0 % < seuil 10 %) — conforme à la spec.
- **Vérification du premier run réel de `fetch-data` étendu (priorité #2 de la session #4)** :
  branche `market-data` régénérée le 2026-08-24 en 436 s (loin du timeout de 75 min) — 30
  paires funding (5 019-5 094 règlements, 2022-01→2026-07, intervalles 8h avec épisodes 4h/2h
  sur SOL) et 30 paires perp 1h publiées ; `DATA_ANOMALIES.md` publié (19 sauts de rendement,
  tous des mouvements de crise connus + DHR/Fortive) ; **les 2 incohérences OHLC ABT/MS du
  2026-07-24 ont disparu** à la régénération (capture tronquée côté fournisseur, comme supposé).
  Un ticker action en échec : **BK** (yfinance ET stooq) — à surveiller, sans incidence sur
  les stratégies actives (BK absent des poches).
- Wallets au cycle 2026-08-31T04 : 🛡️ 989 € | ⚖️ 1 002 € | 🔥 969 € | 🧪 978 € (labo 100 % cash,
  état attendu).

---

## 2026-08-31 — Session hebdomadaire #5 (b) : extension short/perp + funding du moteur commun (P0#1, étape simulateur) — SPEC pré-enregistrée, audit adversarial, 1 CRITIQUE + 3 MAJEURS corrigés, contre-audit `isSound: true`

**Contexte.** Les données funding/perp étant disponibles (cf. (a)), l'ordre pré-enregistré par la
session #4 imposait l'étape simulateur du funding carry : extension du moteur commun de backtest
aux positions short sur perpétuels + règlement du funding, soumise à audit adversarial AVANT
toute utilisation. Sémantique fixée AVANT implémentation dans `backtest/PERP-EXTENSION-SPEC.md`
(commit 5fd2ec7) : rétro-compatibilité bit-à-bit sans perp, pessimisme systématique (liquidation
au pire prix intra-bougie, frais de liquidation 100 bps, marge cash seule — le spot n'est jamais
collatéral, coûts sur les deux jambes), alignement du funding réglé à H sur la bougie qui se
clôt à H (aucun look-ahead), vol targeting sur |w| (même formule que
`bot/risk/vol_targeting.py`, borne de corrélation 1).

**Livré** (`backtest/perp.py`, `backtest/engine.py` étendu, `backtest/tests/test_perp.py`,
`backtest/README.md`) par un agent dédié : chargeurs perp/funding (timestamps ISO8601 mixtes
avec jitter ±2 ms → `round("h")`), `build_aligned_perp_matrices`, comptabilité par ligne perp
(variation margin réglée chaque bougie, funding, coûts, liquidation), `pnl_breakdown` par jambe
(identité `Δéquity = Σ breakdown` exacte), 274 règlements orphelins/146 568 sur 30 symboles
(0,19 %, bords de série) journalisés. Deux bugs trouvés par l'implémenteur lui-même en écrivant
ses tests (notionnel perp compté comme valeur détenue ; gap clôture→open perdu lors d'un trade
perp) — corrigés avant livraison.

**Audit adversarial indépendant (copie isolée, remote neutralisé) : verdict initial
`isSound: false`** — 9 scripts d'attaque sur données réelles et synthétiques :
- **F1 (CRITIQUE, démontré sur données réelles)** : le `fillna(0.0)` historique des prix
  (inoffensif en long-only) devenait un GAIN FICTIF pour un short sur un trou de données — sur
  le trou réel de 69 h de SOL-PERP (février 2022), une paire couverte encaissait +28,9 % de
  capital en une bougie (ligne « close » à prix 0) et la liquidation devenait impossible
  (`NaN < x` est faux). **Corrigé** : prix/funding perp NaN sur un symbole engagé ⇒ `ValueError`
  (flat sans poids cible ⇒ traversée sans effet).
- **F2 (MAJEUR)** : le funding réglé à la même bougie n'entrait pas dans le test de marge (un
  funding extrême laissait le cash devenir négatif sans liquidation — défaut de la spec elle-même).
  **Corrigé** : funding payable inclus dans le test ; contre-audit → le funding déclencheur
  n'était pas débité (MAJEUR résiduel) → **corrigé** (débit total perte + funding + frais borné au
  cash, ventilé frais → funding → prix).
- **F3 (MAJEUR)** : perte de liquidation non plafonnée → équity négative qui inversait le signe
  économique des rendements dans `summarize_segment`. **Corrigé** : perte plafonnée au cash (prix
  de faillite, `bankrupt=True`), ruine (équity ≤ 0) figée à 0 avec rendements suivants nuls.
- **F4 (MAJEUR)** : `opens`/`closes` non vérifiés pour les colonnes perp. **Corrigé.**
- Axes sains prouvés : rétro-compat bit-à-bit (HEAD~1 vs HEAD, synthétique + réel, ET
  reproduction bit-exacte de `vol_breakout_6majors/results.json` sur 7 fenêtres), zéro
  look-ahead (perturbation des données futures), signe/montant du funding exacts sur BTC réel,
  vol targeting non neutralisé, bande correctement signée, identité comptable exacte.
- Performance : ~66 s / 10 000 bougies × 12 colonnes (walk-forward complet ≈ 45-60 min).

**Contre-audit final : `isSound: true`** (commit fa8c725). Spec §6 amendée avec chaque correctif
AVANT tout backtest de candidate. Suite : **768 tests verts + 1 skip** (+19).

---

## 2026-08-31 — Session hebdomadaire #5 (c) : Porte 1 de `funding_carry_6majors` (backlog P0#1) — ÉCHEC 4/5, REJETÉE ; audit `isSound: false` (artefact du moteur qui PÉNALISE la candidate)

**Protocole.** SPEC intégralement PRÉ-ENREGISTRÉE et committée AVANT exécution (commit e581289) :
long spot / short perp delta-neutre sur les 6 majors V1, signal = funding annualisé glissant sur
`(t − D jours, t]` (D ∈ {7, 30}) avec entrée > θ_in ∈ {0,05, 0,10} et sortie < θ_in/2
(hystérésis fixée), poids ±0,10 fixé (faisabilité de marge 6 × 0,10 × 1,5 = 0,90), coûts
**25 bps/côté sur les DEUX jambes** (pessimiste : taker perp réel ~5 bps), overlay production,
marge/liquidation par défaut, calendrier restreint à 2022-04-03 → 2026-07-31 (2 trous SOL-PERP
connus, fixé avant exécution), 14 fenêtres 9m/3m, **K_total = 12 + 14 × 4 = 68**, trades comptés
sur les seules lignes perp closes (option stricte). Sémantique des issues pré-enregistrée, y
compris le cas « Porte 1 passée » (impossible à incuber : `bot/sim/` long-only).

**Résultats (OOS concaténé, 30 647 h, net de coûts).**

| | Candidate | Benchmark B&H spot équipondéré |
|---|---|---|
| Sharpe | **−0,050** | 0,703 |
| Profit factor | 0,964 | — |
| MaxDD | 0,68 % | 71,98 % |
| Lignes perp closes | 14 | — |
| DSR (K=68) | 0,0065 | — |

Porte 1 §1.2 : **4/5 seuils manqués** (seul le MaxDD relatif passe, trivial). PnL par jambe :
delta-neutralité confirmée (résidu spot + perp +0,06 %, corrélation au B&H −0,01), **funding net
+2,74 % du capital en 3,5 ans, intégralement absorbé par les coûts (−2,88 %)**. 0 liquidation.
Sharpe +0,47 avant 2024, −0,44 depuis. Stress de coûts : PF 0,85 à 3×, 0,75 à 5×.

**Audit adversarial indépendant (copie isolée) : `isSound: false`.** Reproduction bit-exacte (3
fenêtres OOS à 1e-13 + 1 sélection IS complète), causalité, funding, DSR, K_total, benchmark,
absence de retouche post-OOS : tous sains. **Mais F1 (CRITIQUE) — un artefact du MOTEUR qui
PÉNALISE la candidate** : la remise à zéro de la position à chaque fenêtre OOS (conception
historique du moteur pour concaténer des fenêtres indépendantes) × bande de non-négociation
plate de 5 % × vol targeting sur |w| (poids scalé 0,015-0,046 < 0,05) ⇒ **aucun ordre n'est
jamais émis dans certaines fenêtres** malgré un signal actif 100 % du temps (fenêtre 4 : 0
trade en 3 mois). Re-simulation continue de l'auditeur (position portée entre fenêtres OOS
contiguës, mêmes params/coûts/overlay) : **Sharpe +0,836, PF 1,014, 24 lignes perp** — le rejet
reste robuste (PF < 1,15 et 24 < 80 dans les deux lectures) mais les marges affichées ne le
sont pas. En production, la bande est PAR POCHE (5 % × `capital_alloc_pct`,
`bot/risk/manager.py` étape 6) et l'exécution est continue : le moteur est aujourd'hui PLUS
sévère que la production pour toute candidate à faible poids nominal — un défaut jamais
détecté sur les 12 candidates précédentes (poids plus grands). F2 (MAJEUR) : l'exception
« stratégie structurellement lente » de §1.2 n'avait pas été évaluée — non pré-enregistrée
donc non invoquée (option conservatrice), sans effet.

**Décision (conforme à la sémantique pré-enregistrée et à §1.4).** `isSound: false` ⇒ rejet
automatique ; statut **`rejetee`** (entrée n°13 du registre, K_total = 68). **Aucun re-run dans
cette session** — amender le moteur après avoir vu le résultat d'une candidate est exactement
ce que §0 interdit. L'amendement du moteur (portage de la position entre fenêtres OOS contiguës
+ bande par poche alignée production) est inscrit au backlog comme **P0 infrastructure (#16)**,
à pré-enregistrer et auditer dans une session dédiée AVANT toute future candidate. Un re-run du
funding carry sur moteur amendé serait un nouvel id (§3.3) — valeur attendue faible tant que
le palier de coûts perp reste à 25 bps (la vraie question est une question de règle de coûts,
pas de recherche : à instruire en gouvernance, jamais dans une session de jugement).
Prochaine candidate → K_total = 13 lignes + sa grille. Labo toujours vide (0/3). Suite de
tests : 779 verts + 1 skip.

---

## 2026-09-07 — Session hebdomadaire #6 (a) : REVUE des stratégies actives et candidates + incidents données

- **Candidates labo** : `INCUBATING_STRATEGIES` toujours vide — aucune Porte 2 à évaluer,
  aucun kill 56j. **Zéro action requise, zéro action prise.**
- **Stratégies actives** : les 3 stratégies de production restent l'antécédent HORS cadre §3
  (`PROMOTION-RULES.md` §5). `DRIFT-REPORT.md` du 2026-09-06 : 7 lignes, toutes **SURVEILLER**
  (46j vécus < 60j — Sharpe roulant 60j non calculable). Sharpe vécus toujours du bruit
  d'échantillon court ; DD vécus ≤ 1,7 % partout. Aucune action. À partir de ~fin septembre le
  Sharpe roulant 60j deviendra calculable et le critère vécu de `SELECTION-FINALE.md` §5
  (3 mois) arrivera à maturité vers fin octobre.
- **Gouvernance #14** : toujours aucune décision humaine enregistrée depuis le dossier du
  2026-08-24 (`GOVERNANCE-DOSSIER-2026-08-24-quasi-passif.md`). Rappel priorité haute.
- **INCIDENT 1 — recalibrage du 2026-09-06 SAUTÉ** : le rafraîchissement crypto de la
  maintenance a rapporté « OK » mais 0/6 symboles en staging (`DONNEES_INSUFFISANTES`).
  Cause la plus probable (déduite du code, logs de maintenance non conservés) : maintenance
  exécutée le 6 septembre ⇒ « dernier mois complet » = août 2026, dont l'archive mensuelle
  Binance Vision n'était vraisemblablement pas encore publiée ⇒ règle de complétude ⇒ TOUS
  les symboles exclus (le complément API ne couvre que le mois COURANT, pas un mois d'archive
  manquant). Mode d'échec systématique de tout début de mois — inscrit au backlog (#18),
  aucune correction improvisée cette session. Sans impact : `REGIME_SMA_DAYS` inchangé depuis
  6 recalibrages, le prochain (2026-09-13) rattrapera.
- **INCIDENT 2 — branche `market-data` stale depuis le 2026-08-24** : `fetch-data.yml` n'avait
  AUCUN schedule (dispatch/push uniquement — le run du 2026-08-24 venait du push de la session
  #4). Corrigé cette session : cron hebdomadaire samedi 05h UTC (avant la maintenance du
  dimanche), le push de la session déclenche une régénération immédiate — qui permettra aussi
  de re-vérifier le ticker **BK** (échec yfinance+stooq du 2026-08-24, toujours à surveiller).
- **Note cadence** : les cycles du bot tournent à ~10-12/jour (trous horaires journalisés par
  le bot lui-même) — famine connue du scheduler GitHub Actions, déjà mitigée en session #1
  (6 tentatives/heure), dédup par run_id saine. Aucune action.
- Wallets au cycle 2026-09-07T06 : 🛡️ 992 € | ⚖️ 1 014 € | 🔥 1 005 € | 🧪 980 € (labo 100 %
  cash, état attendu).

---

## 2026-09-07 — Session hebdomadaire #6 (b) : backlog #16 — portage de la position entre fenêtres OOS contiguës (SPEC pré-enregistrée, audit adversarial, 2 CRITIQUES + 1 MAJEUR + 1 MINEUR traités, contre-audit `isSound: true`)

**Contexte.** Priorité n°1 pré-enregistrée par la revue de la session #5 : le finding F1
(CRITIQUE) de l'audit du 2026-08-31 avait démontré que la remise à zéro de la position à chaque
fenêtre OOS × bande de non-négociation × vol targeting rendait le moteur PLUS SÉVÈRE que la
production pour toute candidate à faible poids nominal (funding carry : fenêtres entières sans
un seul ordre malgré un signal actif 100 % du temps).

**Protocole.** SPEC pré-enregistrée `backtest/CARRY-EXTENSION-SPEC.md` committée AVANT
implémentation (commit 060a643) : extension 100 % opt-in (rétro-compat bit-à-bit par défaut),
portage exclusivement d'OOS k vers OOS k+1 contiguë (jamais depuis un segment IS),
reconstruction au dernier close SANS coût (c'est la même position, pas un ordre), anti-gaming
du comptage (une ligne traversant N fenêtres = 1 seul trade clos, à sa vraie sortie).
Implémentation par un agent dédié, audit par un agent adversarial indépendant sur copie isolée
(remote neutralisé).

**Audit adversarial : verdict initial `isSound: false`** — 3 findings démontrés par exécution :
- **F1 (CRITIQUE)** : le portage GONFLAIT le profit factor (chiffre de seuil §1.2 !) — l'équity
  restait exacte à 1e-16 mais la classification réalisé/latent aux frontières était
  systématiquement favorable (15/15 seeds, jusqu'à +54 % de PF relatif). Cause racine : PnL
  accumulé normalisé par le capital de DÉBUT de fenêtre alors que les poids portés sont
  normalisés par l'équity de FIN. **Corrigé** (normalisation sur l'équity finale, télescopage
  exact) — après correctif : biais disparu (5 favorables / 8 défavorables / 2 nuls, résidu
  équilibré, tests multi-seeds committés).
- **F2 (CRITIQUE, spec elle-même)** : l'équivalence « bande poche = bande wallet » AFFIRMÉE par
  la spec §5 est **RÉFUTÉE** en régime de compounding (relation affine
  `E_wallet = 1 + alloc·(E_pocket − 1)`, jamais homothétique — décisions hold/trade totalement
  divergentes dès que l'équity s'écarte de 1). Clause d'arrêt de la spec respectée : AUCUN
  changement de bande, statut documenté NON RÉSOLU (amendement §7 + README + test de garde
  committé), instruction renvoyée au backlog (#19) avec le second écart observé au passage
  (la production exécute toujours un flatten même sous la bande, le moteur non).
- **F3 (MAJEUR)** : `carry_out` empoisonnable par un close final NaN (spot mark-to-zero
  historique) détecté une fenêtre trop tard. **Corrigé** : `ValueError` immédiate au packaging.
- 1 MINEUR (ligne à poids ~0 dont le PnL accumulé disparaissait) corrigé.

**Contre-audit : `isSound: true`** (rejeu des scripts d'attaque de l'auditeur sur le code
corrigé). Preuves finales : rétro-compat bit-à-bit (hash HEAD vs modifié, synthétique + réel
spot/perp, ET reproduction bit-exacte de `vol_breakout_6majors/results.json` sur données
réelles, 46 min de calcul) ; conservation économique vs simulation continue unique à 1e-16
(bande active) ; zéro fuite IS→OOS (perturbations) ; liquidation/funding perp dès la première
bougie post-frontière (aucune immunité). Commit 0fcd699.

**Re-run informatif de `funding_carry_6majors`** (spec §6.6, quantification de l'artefact F1 du
2026-08-31 — INFORMATIF uniquement, aucun verdict passé ne change, §3.3) : chiffres consignés
dans l'entrée (e) ci-dessous (calcul long, terminé en fin de session).

---

## 2026-09-07 — Session hebdomadaire #6 (c) : Porte 1 de `btc_seasonality_2123utc` (backlog P1#4) — ÉCHEC 4/5, REJETÉE ; audit `isSound: true`

**Protocole.** SPEC intégralement PRÉ-ENREGISTRÉE et committée AVANT exécution (commit 70b75b5) :
long BTC pendant les bougies ouvrant à 21h et 22h UTC, flat sinon — fenêtre STRICTEMENT issue de
la littérature externe (Quantpedia ≤ 2023), zéro grille, zéro variante d'heure, zéro filtre
(interdit absolu du backlog #4 : pas de re-recherche de la « meilleure » heure). 15 fenêtres
9m IS / 3m OOS horaires (OOS 2022-10-01 → 2026-06-30, 32 855 h), coûts majors 15 bps/côté
(importés de `bot/config.py`), overlay standard horaire, **K_total = 13 + 15 × 1 = 28**.
Premier backtest de candidate exécuté avec le portage inter-fenêtres ACTIVÉ (vérifié
structurellement neutre : positions tenues 2 h, 0 frontière traversée). Sémantique des issues
figée d'avance, y compris l'attendu honnête écrit AVANT le run : échec probable sur les coûts.

**Résultats (OOS concaténé, net de coûts).**

| | Candidate | Benchmark B&H BTC aligné |
|---|---|---|
| Sharpe | **−6,68** | 0,864 |
| Profit factor | 0,328 | — |
| MaxDD | 89,7 % | 53,7 % (relatif : 1,67× > 1,5×) |
| Trades clos | 1 369 | — |
| DSR (K=28) | ~0 (3e-38) | — |

Porte 1 §1.2 : **4/5 seuils manqués** (seul le seuil de trades passe). Stress de coûts : PF 0,05
à 3×, 0,01 à 5×. **Constat d'honnêteté central** : l'effet BRUT existe et n'a PAS disparu avec
l'institutionnalisation (+6,4 bps/jour OOS ; +3,6 avant 2024, **+7,8 depuis 2024**) — mais il
reste ~4,7× SOUS le hurdle de coûts du projet (2 côtés × 15 bps = 30 bps/jour pour 2 h de
détention). C'est un échec de STRUCTURE DE COÛTS, pas une absence d'effet ni un artefact.

**Audit adversarial indépendant (copie isolée) : `isSound: true`.** Reproduction from scratch
(hors pipeline) convergente à 0,0005 de Sharpe, 1 369 trades identiques ; zéro look-ahead
(perturbation des données futures : diff bit-exacte 0,0) ; DSR/K_total recalculés identiques au
dernier chiffre ; benchmark aligné reproduit à 5 décimales ; hypothèse « annualisation 8760 sur
série à 94 % de zéros = artefact » réfutée mathématiquement ; vérification explicite de
l'artefact du précédent funding_carry : vol scalar min 0,178 >> bande 0,05, 0 entrée bloquée.
1 MINEUR : la citation `rapport-recherche.md` (source de la fenêtre) est intraçable dans le
dépôt — la fenêtre était pré-identifiée par le backlog dès l'origine (aucun choix a posteriori),
mais la traçabilité de la référence de littérature est à améliorer.

**Décision (conforme à la sémantique pré-enregistrée).** Statut **`rejetee`**, entrée n°14 du
registre (commit 639c5c9). Conclusion transférable consignée au registre : aucune stratégie à
détention ~2 h/jour n'est viable sur ce périmètre de coûts sans un edge brut ≥ ~5× celui
mesuré — ne pas retester de variante calendaire horaire sans changement structurel du régime de
coûts. Backlog P1#4 soldé. Prochaine candidate → K_total = 14 lignes + sa grille. Labo toujours
vide (0/3).

---

## 2026-09-07 — Session hebdomadaire #6 (d) : hygiène — backlog #15 (référence du DRIFT-REPORT) et cron fetch-data

- **#15 soldé** (commit 2068097) : `tools/weekly_maintenance.py` référence désormais le retest
  AUDITÉ de `quasi_passif_crypto` (Sharpe attendu 0,808/0,283/0,069 ; MaxDD 8,4/27,3/56,4 % par
  wallet) via une table de redirection explicite — le registre append-only reste intouché,
  fallback silencieux si l'entrée de retest venait à manquer, note explicite dans le rendu.
  Dès le prochain rapport (2026-09-13), les verdicts de dérive seront calculés contre des
  chiffres crédibles.
- **Cron fetch-data** (commit précédent, cf. (a) incident 2) : samedi 05h UTC.
- Suite de tests complète au moment du push : verte (cf. commit final).

---

## 2026-09-07 — Session hebdomadaire #6 (e) : re-run informatif de `funding_carry_6majors` sur moteur amendé + correctif d'un test tautologique

**Re-run informatif (spec carry §6.6, AUCUN verdict ne change — §3.3).** `run_funding_carry
--carry-across-windows` (2,5 h de calcul) : le run NOMINAL (à plat entre fenêtres) reproduit
exactement l'archive (Sharpe −0,0503) ; le run avec PORTAGE donne **Sharpe OOS +0,836,
PF 1,014, MaxDD 0,79 %, 24 lignes perp closes** — identique à la re-simulation continue de
l'auditeur du 2026-08-31, qui quantifiait l'artefact F1. Confirmation : (a) l'artefact était
réel et matériel (−0,89 point de Sharpe pour cette candidate) ; (b) **le rejet reste inchangé
et robuste dans les deux lectures** (PF 1,014 ≤ 1,15 ; 24 < 80 trades). Note datée ajoutée à
l'entrée du registre (append-only respecté), résultats archivés dans
`backtest/results/funding_carry_6majors/results_carry_informative_2026-09-07.json`.
`vol_breakout_6majors` et `quasi_passif_crypto_wf_retest` n'ont PAS été re-runnés (budget de
calcul ; poids nominaux 0,2-1,0 largement au-dessus de la bande 0,05, artefact F1 non
plausible pour eux — la reproduction bit-exacte des 14 fenêtres de vol_breakout sur le moteur
amendé, extension désactivée, en tient lieu de non-régression).

**Correctif de session (honnêteté des preuves).** Le test de reproduction §1.1a livré par
l'implémentation était TAUTOLOGIQUE (il comparait le results.json que `main()` venait
d'écraser… à lui-même) et DESTRUCTEUR (il écrasait l'archive `vol_breakout_6majors/results.json`
avec un run sur données plus récentes). Détecté par l'orchestrateur en revoyant le diff de
working tree, archive restaurée (sha256 vérifié), test réécrit (archive chargée AVANT, sortie
en tmp_path, garde anti-écriture) et RE-EXÉCUTÉ pour de vrai : **14/14 fenêtres communes
bit-identiques** (sharpe, PF, MaxDD, trades, bornes IS/OOS) — la preuve de rétro-compatibilité
est désormais réelle. Leçon consignée : un test de reproduction doit toujours charger sa
référence avant d'exécuter quoi que ce soit qui puisse l'écrire.

**Bilan de session** : registre à 14 entrées (K_total prochaine candidate = 14 + grille),
labo toujours vide (0/3), aucune stratégie active modifiée, framework de risque et circuit
breakers intouchés. Suite de tests complète verte au push final.

---

## 2026-09-14 — Session hebdomadaire #7 (a) : REVUE des stratégies actives et candidates + vérifications pré-enregistrées

- **Candidates labo** : `INCUBATING_STRATEGIES` toujours vide — aucune Porte 2 à évaluer,
  aucun kill 56j. **Zéro action requise, zéro action prise.**
- **Stratégies actives** : les 3 stratégies de production restent l'antécédent HORS cadre §3
  (`PROMOTION-RULES.md` §5). `DRIFT-REPORT.md` du 2026-09-13 : 7 lignes, toutes **SURVEILLER**
  pour la même raison mécanique (53j vécus < 60j — Sharpe roulant 60j non calculable ; il
  deviendra calculable dans ~1 semaine, et le critère vécu de `SELECTION-FINALE.md` §5
  arrivera à maturité vers fin octobre). Le rapport calcule désormais bien la dérive contre
  les références AUDITÉES du retest (#15 soldé en #6 — vérifié effectif sur le rapport réel :
  Sharpe attendu 0,81/0,28/0,07, note explicite au pied du tableau). Sharpe vécus toujours du
  bruit d'échantillon court (xs_momentum équilibré +3,14 vs agressif +0,36 — mêmes signaux,
  divergence d'échantillon) ; DD vécus ≤ 1,7 % partout. **Aucune règle de mort déclenchée,
  aucune action.**
- **Gouvernance #14** : toujours aucune décision humaine enregistrée
  (`GOVERNANCE-DOSSIER-2026-08-24-quasi-passif.md`). Rappel : à défaut, le critère vécu de
  `SELECTION-FINALE.md` §5 tranche de lui-même vers fin octobre 2026. Priorité haute maintenue.
- **Recalibrage du 2026-09-13 : exécuté normalement** (rafraîchissement OK, 15 fenêtres,
  175 vs 200 : +3,0 % < seuil 10 % ⇒ aucun changement — 7e recalibrage consécutif sans
  changement). L'incident « début de mois » du 2026-09-06 (#18) ne s'est pas reproduit,
  conformément au diagnostic (le 13, l'archive mensuelle d'août était disponible). Le
  diagnostic précis reste NON CONFIRMÉ sur run réel (les `reason` d'exclusion par symbole ne
  sont toujours pas journalisés) — instrumentation prévue cette session si le budget le permet.
- **Premier run PLANIFIÉ du cron `fetch-data` (samedi 2026-09-12 05h UTC + dispatch) : OK.**
  Branche `market-data` régénérée le 2026-09-12 en 512 s : 30 paires crypto (2022-01 →
  2026-08 + mois courant), 30 funding, 30 perp, 103 actions, 18 ETF. `DATA_ANOMALIES.md` :
  19 sauts de rendement, tous des crises connues + DHR/Fortive (fondateur) — **les 2
  incohérences OHLC ABT/MS du 2026-07-24 ont bien disparu** (confirmé une 2e fois). Ticker
  **BK** : 3e échec consécutif (yfinance ET stooq) — toujours sans incidence sur les poches
  actives (BK absent), mais 3 échecs = plus vraisemblablement un problème de mapping/source
  durable qu'un incident transitoire ; ajouté au backlog (#20) pour investigation dédiée.
- Wallets au cycle 2026-09-14T05 : 🛡️ 998 € | ⚖️ 1 007 € | 🔥 995 € | 🧪 983 € (labo 100 %
  cash, état attendu).

---

## 2026-09-14 — Session hebdomadaire #7 (b) : Porte 1 de `protective_vol_gate_6majors` (backlog P1#6, « protective put synthétique ») — ÉCHEC 3/5, ÉCARTÉE ; audit `isSound: true`

**Protocole.** SPEC intégralement PRÉ-ENREGISTRÉE et committée AVANT exécution (commit c0b4177) :
panier équipondéré des 6 majors V1, coupure à 0 quand le rang percentile (fenêtre 4 320 h, min
2 160 obs) de la vol réalisée du panier (V h glissantes) franchit p_in, redéploiement linéaire en
72 h une fois revenu sous p_out = 0,80, re-coupure immédiate possible. Grille pré-enregistrée
4 combos (V ∈ {24, 72} × p_in ∈ {0,95, 0,98}), signal calculé sur les SEULS prix (garde
structurelle anti « equity curve trading », vérifiée par test d'introspection). 15 fenêtres
9m IS / 3m OOS horaires (OOS 2022-10 → 2026-08, 32 855 h), coûts 25 bps/côté pessimistes,
overlay production standard (la candidate n'a AUCUN sizing interne — chemin overlay vérifié
correct par l'audit, à l'inverse du précédent quasi-passif), portage inter-fenêtres actif,
**contrôle apparié pré-enregistré** (poids constants 1/6 sans gate, même pipeline) compté dans
K_total : **K_total = 14 + 15×4 + 15×1 = 89**. Attendu honnête écrit AVANT le run : échec
probable sur le Sharpe, valeur attendue = l'analyse whipsaw/MaxDD comme intrant de gouvernance.

**Résultats (OOS concaténé, net de coûts).**

| | Candidate | Contrôle sans gate | Benchmark B&H 6 majors |
|---|---|---|---|
| Sharpe | **0,075** | 0,442 | 0,548 |
| Sortino | 0,103 | 0,608 | 0,757 |
| Profit factor | 1,064 | 3,61 | — |
| MaxDD | **38,0 %** | 42,4 % | 72,0 % |
| Trades clos | 139 | 0 | — |
| DSR (K=89) | 0,0095 | — | — |

Porte 1 §1.2 : **3/5 seuils manqués** (Sharpe, PF, DSR ; trades et MaxDD relatif 0,53× passent).
**Le verdict de fond tient en deux chiffres** : (a) whipsaw — sur 35 épisodes de protection OOS
distincts, 16 pertes évitées contre 19 hausses ratées, somme nette **+100 % de rendement B&H
raté** (le risque n°1 pré-identifié par la fiche backlog s'est matérialisé tel quel) ; (b)
redondance — la réduction de MaxDD vs B&H brut (38 % vs 72 %) vient pour l'essentiel de
l'overlay de vol targeting DÉJÀ en production (contrôle sans gate : 42,4 %) ; la valeur
marginale de la gate est ~4,5 points de MaxDD payés ~0,37 point de Sharpe (IR −0,99, corr 0,93
au contrôle). Rupture temporelle habituelle : Sharpe 0,96 avant 2024, −0,36 depuis.

**Audit adversarial indépendant (copie isolée, remote neutralisé) : `isSound: true`.**
Reproduction from scratch (sans importer le module candidat) **bit-exacte 15/15 fenêtres**
(Sharpe à 1e-14, choix IS et portage identiques) ; zéro look-ahead (perturbation des données
futures à 4 dates : signal/états/poids bit-identiques avant t) ; DSR réimplémenté from scratch
identique au 10e chiffre, K_total=89 revérifié ligne à ligne, échec robuste aux conventions
alternatives de K ; 35 épisodes recomptés indépendamment, classification vérifiée épisode par
épisode ; grille code == SPEC == results ; chronologie git propre (aucune retouche post-OOS).
**1 finding MAJEUR (méthode, moteur commun)** : la bande de non-négociation plate 0,05 avale
~99 % de l'exécution de la rampe 72 h et 95 % des coupures immédiates (poids cible médian
post-overlay 0,064, tout près de la bande) — la « rampe linéaire » de la SPEC ne s'exécute
quasiment jamais comme un lissage réel. Testé BIDIRECTIONNELLEMENT par l'auditeur : forcer
l'exécution immédiate des coupures DÉGRADE encore le Sharpe (−0,067 → −0,138, turnover) — pas
un biais pénalisant, et le comportement serait le même en production (le flatten forcé de
`bot/risk/manager.py` est spécifique aux circuit breakers, pas aux signaux ordinaires). Versé
au dossier backlog #19 (bande). 1 MINEUR : PF de stress de coûts non monotone (le re-run de
stress ne reproduit pas le portage — limitation auto-documentée, sans impact sur le seuil).

**Décision (conforme à la sémantique pré-enregistrée SPEC §8).** Sharpe positif, PF > 1,
`isSound: true`, mais 3/5 seuils manqués ⇒ statut **`ecartee`** (entrée n°15 du registre),
pas d'incubation. Conclusion transférable consignée : une gate de vol percentile PAR-DESSUS un
vol targeting continu déjà actif est structurellement redondante et son coût de whipsaw domine
sur ce marché — ne pas retester de variante (autres seuils/rampe) sans mécanisme
structurellement neuf (§3.3). Scripts d'attaque de l'audit archivés dans
`backtest/results/protective_vol_gate_6majors/audit/`. Labo toujours vide (0/3). Prochaine
candidate → K_total = 15 lignes + sa grille.

---

## 2026-09-21 — Session hebdomadaire #8 (a) : REVUE des stratégies actives et candidates

- **Candidates labo** : `INCUBATING_STRATEGIES` toujours vide — aucune Porte 2 à évaluer,
  aucun kill 56j. **Zéro action requise, zéro action prise.**
- **Stratégies actives** : les 3 stratégies de production restent l'antécédent HORS cadre §3
  (`PROMOTION-RULES.md` §5). `DRIFT-REPORT.md` du 2026-09-21 : **premier rapport avec Sharpe
  roulant 60 j réellement calculable** (60 j vécus). 5 lignes **OK** (aucun signal de dérive :
  quasi_passif 1,72/4,92/1,61 vécus vs 0,81/0,28/0,07 attendus — au-dessus des attentes ;
  xs_momentum 4,70/1,37 vs 0,82 ; DD vécus ≤ 1,7 % partout, très loin des 2× attendus) ;
  2 lignes **SURVEILLER** pour `dual_momentum_etf` : raison purement mécanique « DD de
  référence indisponible dans le registre » (l'entrée d'origine de la vague 1 n'a pas de
  MaxDD OOS consigné — hygiène de monitoring inscrite au backlog, pas un signal de marché).
  Aucune règle de mort ne serait déclenchée même si §3 s'appliquait. **Aucune action.**
- **Gouvernance #14** : toujours aucune décision humaine enregistrée
  (`GOVERNANCE-DOSSIER-2026-08-24-quasi-passif.md`, en attente depuis 4 semaines). Le critère
  vécu de `SELECTION-FINALE.md` §5 tranche de lui-même vers fin octobre 2026. Les vécus 60 j
  actuels du quasi-passif sont au-dessus des références auditées — aucune pression à la
  bascule à ce jour, mais la décision reste ouverte. Rappel priorité haute maintenu.
- Recalibrage du 2026-09-21 : exécuté normalement (15 fenêtres), `REGIME_SMA_DAYS` inchangé
  (175 vs 200 : +3,0 % < seuil 10 %) — 8e recalibrage consécutif sans changement, conforme.
- Cron `fetch-data` du samedi 2026-09-19 : OK (468 s), 30 crypto + 30 funding + 30 perp +
  103 actions + 18 ETF régénérés. Ticker **BK** : **4e échec consécutif** (yfinance ET stooq)
  — confirme le diagnostic « problème durable de mapping/source » (#20, toujours sans
  incidence sur les poches actives).
- Wallets au cycle 2026-09-21T04 : 🛡️ 1 014 € | ⚖️ 1 040 € | 🔥 1 041 € | 🧪 994 € (labo
  100 % cash, état attendu).

---

## 2026-09-21 — Session hebdomadaire #8 (b) : Porte 1 de `pairs_ethbtc_ratio_rotation` (backlog P1#5, version dégradée long-only) — ÉCHEC 3/5 + dominée par le contrôle, REJETÉE ; audit `isSound: false` (finding CRITIQUE d'INFRASTRUCTURE, sans effet sur ce verdict)

**Protocole.** SPEC intégralement PRÉ-ENREGISTRÉE et committée AVANT exécution (commit
4c4182e) : rotation long-only BTC/ETH sur z-score du log-ratio ETH/BTC (base 50/50, tilt
100/0, hystérésis θ/2 fixée), grille figée 4 combos (L ∈ {720, 2160} h × θ ∈ {1,5, 2,0}),
coûts majors 15 bps/côté importés de `bot/config.py`, overlay production standard, portage
inter-fenêtres actif, contrôle apparié 50/50 constant compté dans K_total. 15 fenêtres
9m IS / 3m OOS (OOS 32 855 h, 2022-10 → 2026-08), **K_total = 15 + 15×4 + 15×1 = 90**.
Pré-requis de la fiche backlog exécuté AVANT le walk-forward, rôle diagnostic pré-enregistré
(SPEC §5) : **ratio NON stationnaire** (ADF p = 0,344 pré-OOS ; 3/15 fenêtres IS
stationnaires à 10 %), **log-prix NON cointégrés** (Engle-Granger p = 0,650), demi-vie
714 h. Attendu honnête écrit AVANT le run : échec probable, ratio tendanciel baissier.

**Résultats (OOS concaténé, net de coûts).**

| | Candidate | Contrôle 50/50 | Benchmark B&H |
|---|---|---|---|
| Sharpe | **0,331** | 0,590 | 0,616 |
| Sortino | 0,464 | 0,825 | 0,861 |
| Profit factor | 1,261 | 1,795 | — |
| MaxDD | 48,7 % | 39,7 % | 60,9 % |
| Trades clos | 38 | 0 | — |
| DSR (K=90) | 0,032 | — | — |

Porte 1 §1.2 : **3/5 seuils manqués** (Sharpe, trades, DSR ; PF et MaxDD relatif 0,80×
passent). **Dominée par le contrôle 50/50 sur les 3 axes** (Sharpe ET Sortino ET MaxDD,
IR −1,14, corr 0,976) — la rotation détruit de la valeur par rapport à ne rien faire.
43 épisodes de tilt distincts (médiane 229 h), PnL relatif net **−28,8 %** vs contrôle.
Rupture temporelle habituelle, 5e candidate crypto de suite : Sharpe 1,44 avant 2024,
**−0,22 depuis**. Stress de coûts : PF 1,14 à 3×, 0,98 à 5×. Corrélation 0,72 au proxy
quasi-passif (redondance en prime).

**Audit adversarial indépendant (copie isolée, remote neutralisé) : `isSound: false`.**
Reproduction PARFAITE d'abord : signal from scratch bit-identique (4 combos), **15/15
fenêtres reproduites bit-exact** (sélection IS comprise), DSR from scratch identique
(0,03196944), K_total recompté, stationnarité/épisodes/corrélations exacts, zéro
look-ahead (perturbation), chronologie git propre. **MAIS finding F1 (CRITIQUE,
INFRASTRUCTURE production, démontré par exécution)** : `bot/runner.py:_risk_manager_for_wallet`
construit le RiskManager portefeuille avec `vol_target_annualized=50.0` EN DUR pour TOUS les
wallets, labo compris (design documenté pour ne pas doubler le vol-targeting INTERNE de
`quasi_passif_crypto`, correctif session #3). Conséquence : une candidate **sans sizing
interne** incubée ne recevrait AUCUN vol-targeting réel en production (scalar 1,0 à cible
50), alors que le moteur de backtest lui applique l'overlay production (scalar réel
0,23-0,79, exposition moyenne 0,57 vs cible brute 1,0) — le backtest est PLUS protecteur que
la production, exactement le « sizing fictif plus généreux » que §1.4 interdit. La prémisse
« chemin overlay standard correct pour une candidate sans sizing interne » (SPEC §2, héritée
de la session #7) n'était pas étayée par le code. Portée rétroactive : même prémisse sous le
`isSound: true` de `protective_vol_gate_6majors` — sans effet sur son verdict (écartée de
toute façon) ; note datée ajoutée au registre. 1 MINEUR (réserve biais du survivant non
écrite explicitement — portée quasi nulle sur BTC/ETH), 1 INFO (Sharpe IS d'affichage à √252,
déjà relevé en session #2, argmax invariant).

**Décision (conforme à la sémantique pré-enregistrée SPEC §9 et §1.4).** `isSound: false` ⇒
rejet automatique : statut **`rejetee`** (entrée n°16 du registre). Finding sur
l'INFRASTRUCTURE commune ⇒ AUCUN re-run dans cette session (amender l'infra après avoir vu un
résultat = interdit §0) — l'audit établit par ailleurs que le rejet (« dominée par le
contrôle, 3/5 seuils à larges marges ») survivrait dans toutes les lectures. Le correctif
d'infrastructure est inscrit au backlog comme **P0 (#21)**, à traiter en session dédiée AVANT
toute future candidate sans sizing interne. Conclusion transférable consignée : la
non-stationnarité du ratio ETH/BTC (ADF p=0,34, coint p=0,65 ; 3/15 fenêtres IS
stationnaires) condamne l'hypothèse « retour à la moyenne du ratio » dans ses DEUX versions —
la version marché-neutre complète (#17) ne doit pas être poursuivie sans raison
structurellement neuve (§3.3). Backlog P1#5 soldé. Prochaine candidate → K_total = 16 lignes
+ sa grille. Labo toujours vide (0/3).

---

## 2026-09-21 — Session hebdomadaire #8 (c) : hygiène

- Scripts d'attaque de l'audit archivés dans
  `backtest/results/pairs_ethbtc_ratio_rotation/audit/` (8 scripts + VERDICT.md).
- Registre : entrée n°16 ajoutée (`pairs_ethbtc_ratio_rotation`, rejetee) + note datée sur
  `protective_vol_gate_6majors` (portée rétroactive du finding sizing — append-only
  respecté, aucune ligne réécrite). `tools/verify_research.py --check` : OK, 16 entrées.
- Backlog : P1#5 soldé (verdict consigné), **#21 ajouté (P0 infrastructure : fidélité du
  sizing backtest/production pour les candidates sans sizing interne)**, #17 re-noté
  (version marché-neutre déconseillée par la non-stationnarité), priorités de la prochaine
  session mises à jour.
- Suite de tests complète verte au push final (les 2 échecs rencontrés en cours de session
  étaient dus aux données absentes de l'environnement — résolus en stageant les fichiers
  `market-data` requis, aucun code modifié).

---

## 2026-09-28 — Session hebdomadaire #9 (a) : REVUE des stratégies actives et candidates

- **Candidates labo** : `INCUBATING_STRATEGIES` toujours vide — aucune Porte 2 à évaluer,
  aucun kill 56j. **Zéro action requise, zéro action prise.**
- **Stratégies actives** : les 3 stratégies de production restent l'antécédent HORS cadre §3
  (`PROMOTION-RULES.md` §5). `DRIFT-REPORT.md` du 2026-09-28 (67 j vécus, Sharpe roulant 60 j
  calculable) : **5 lignes OK** (quasi_passif 2,39/4,66/2,26 vécus vs 0,81/0,28/0,07 attendus ;
  xs_momentum 4,53/2,05 vs 0,82 — tous au-dessus des attentes ; DD vécus ≤ 1,7 % partout, très
  loin des seuils 2×) ; 2 lignes **SURVEILLER** pour `dual_momentum_etf`, raison purement
  mécanique inchangée (« DD de référence indisponible dans le registre », backlog #22 — pas un
  signal de marché ; son Sharpe vécu 0,76/3,10 est positif). Aucune règle de mort ne serait
  déclenchée même si §3 s'appliquait. **Aucune action.**
- **Gouvernance #14** : toujours aucune décision humaine enregistrée
  (`GOVERNANCE-DOSSIER-2026-08-24-quasi-passif.md`, en attente depuis 5 semaines). Le critère
  vécu de `SELECTION-FINALE.md` §5 tranche de lui-même vers FIN OCTOBRE — dernière ou
  avant-dernière session avant l'échéance. Vécus au-dessus des références auditées : aucune
  pression à la bascule à ce jour. Rappel priorité haute maintenu.
- Recalibrage du 2026-09-28 : exécuté normalement (rafraîchissement OK, 15 fenêtres,
  175 vs 200 : +3,0 % < seuil 10 %) — 9e recalibrage consécutif sans changement, conforme.
- Cron `fetch-data` du samedi 2026-09-26 : OK (397 s), 30 crypto (archives → 2026-08 + mois
  courant) + 30 funding + 30 perp + 103 actions + 18 ETF régénérés ; 19 anomalies (crises
  connues + DHR/Fortive), aucune incohérence OHLC nouvelle. Ticker **BK** : **5e échec
  consécutif** (yfinance ET stooq) — #20 confirmé durable, à traiter.
- **Attention pré-enregistrée pour la semaine** : la maintenance du dimanche 2026-10-04 tombe
  un 4 du mois — occurrence-test probable du mode d'échec « début de mois » (#18) ;
  l'instrumentation `missing_symbols_reasons` (session #7) donnera cette fois le diagnostic
  par symbole sur run réel.
- Wallets au cycle 2026-09-28T06 : 🛡️ 1 020 € | ⚖️ 1 046 € | 🔥 1 071 € | 🧪 999 € (labo
  100 % cash, état attendu).

---

## 2026-09-28 — Session hebdomadaire #9 (b) : backlog #21 TRANCHÉ — fidélité backtest/production du sizing (session DÉDIÉE infrastructure, aucune candidate jugée)

**Contexte.** Priorité n°1 pré-enregistrée par la revue de la session #8, BLOQUANTE pour toute
future Porte 1 de candidate sans sizing interne : l'audit adversarial de `pairs_ethbtc_ratio_
rotation` avait démontré par exécution que `bot/runner.py:_risk_manager_for_wallet` neutralise
le vol-targeting portefeuille (`vol_target_annualized=50.0` en dur, `gross_exposure_max=1.0`)
pour TOUS les wallets, labo compris, alors que le moteur commun applique par défaut un overlay
« production » (`apply_vol_targeting=True`, scalar réel 0,23-0,79 mesuré) — backtest PLUS
protecteur que le vécu d'incubation, le « sizing fictif plus généreux » que §1.4 interdit.

**Instruction.** Analyse par agent dédié (lecture seule) des 3 options identifiées par l'audit,
chaque affirmation load-bearing REVÉRIFIÉE dans le code par l'orchestrateur (runner l.606 :
50.0 en dur pour les 4 wallets ; labo configuré `vol_target_annualized=0.20` jamais appliqué ;
`backtest/engine.py:simulate_segment` expose déjà `apply_vol_targeting` ; chaque stratégie
reçoit déjà `profile=wallet_cfg` donc `profile["risque"]` NON neutralisé — le pattern
`quasi_passif_crypto`). Constats structurants : (i) ce qui reste réellement appliqué au labo
sans vol-targeting = cap par actif 0,20, bande par poche, 4 circuit breakers — protections
réactives/par-actif, pas préventives ; (ii) le 50.0 en dur s'applique AUSSI aux 3 wallets
réels : l'option (a) (RiskManager labo conditionnel) créerait un CLIFF de fidélité à la
promotion et resterait ambiguë avec 2 candidates de régimes différents dans le labo (un seul
RiskManager par wallet) ; (iii) l'intention documentée du profil labo « équilibré-strict »
(création 2026-07-23) est de fait vidée pour toute candidate sans sizing interne — constat
d'écart, PAS corrigé ici (§4.3 : le runner et le cadre de risque restent hors de portée).

**DÉCISION (pré-enregistrée dans `docs/SIZING-FIDELITY-SPEC.md`, nouveau document de
convention — même pattern que `RECALIBRATION-SPEC.md`, `PROMOTION-RULES.md` reste GRAVÉ et
intouché)** : **(c) + (b) combinées, (a) écartée.**
- **(c) convention par défaut** : candidate sans sizing interne ⇒ backtest
  `apply_vol_targeting=False` (vol brute), l'hypothèse la plus défavorable ET la plus fidèle
  au chemin de production réel (principe pessimiste §0.2). Ses MaxDD Porte 1/Porte 2 sont
  jugés tels quels — effet voulu, aucun seuil modifié.
- **(b) exigence de conception** : candidate revendiquant un vol-targeting réel ⇒ sizing DANS
  la stratégie via `profile["risque"]` (formule fonctionnellement équivalente à
  `bot/risk/vol_targeting.compute_vol_scalar`, bornée `gross_exposure_max`), backtest overlay
  désactivé + sizing modélisé dans le module candidat (précédent : retest quasi-passif,
  session #3). Seule voie fidèle sur TOUT le cycle backtest → labo (0,20) → wallet réel
  (profil de destination), sans toucher une ligne de production.
- **Mécanisation** : champ `sizing_interne: bool` OBLIGATOIRE dans le schéma
  `INCUBATING_STRATEGIES` (bandeau `bot/config.py`, commentaire uniquement) + 2 validations
  et un test négatif de garde dans `bot/tests/test_governance_limits.py` (booléen STRICT — une
  chaîne « false » truthy inverserait silencieusement la convention) ; avertissement bloquant
  dans `backtest/README.md` (le défaut `apply_vol_targeting=True` du moteur ne sert plus
  JAMAIS à un chiffre de décision — conservé pour la rétro-compat des archives et tests).
- **Ce qui n'est PAS fait, volontairement** : aucun changement de `bot/runner.py`/`bot/risk/*`/
  constantes `WALLETS` (§4.3) ; aucun changement de `PROMOTION-RULES.md` (la proposition
  d'inscrire la convention dans §1.4 est versée aux amendements en attente #12, décision
  humaine) ; aucun re-run des candidates passées (notes rétroactives datées des sessions #7/#8
  déjà au registre ; aucun verdict ne dépendait de l'overlay — candidate et contrôle
  subissaient la même distorsion).

**Le blocage est levé : les futures Porte 1 sont de nouveau possibles, sous la convention
pré-enregistrée.** Registre INCHANGÉ (aucune stratégie testée cette session — prochaine
candidate : K_total = 16 lignes + sa grille). Labo toujours vide (0/3). Le livrable a été
soumis à une revue adversariale indépendante AVANT commit — résultat en entrée (c).

---

## 2026-09-28 — Session hebdomadaire #9 (c) : revue adversariale du livrable #21 — verdict initial `isSound: false`, 3 MAJEURS corrigés, garde mécanique ajoutée

**Protocole.** Agent adversarial indépendant sur copie isolée (remote git neutralisé),
mandaté pour démolir le livrable (b) AVANT tout commit : exactitude factuelle de chaque
affirmation technique contre le code réel (fichier:ligne), périmètre du diff (aucun fichier
interdit), cohérence de fond de la convention, gouvernance §0, exécution des tests.

**Vérifications qui tiennent (à charge de preuve exécutée)** : toutes les affirmations
load-bearing de `SIZING-FIDELITY-SPEC.md` confirmées dans le code (runner 50.0/1.0 en dur
pour les 4 wallets ; cap 0,20/bande/4 breakers = ce qui reste réel au labo ;
`profile["risque"]` transmis non neutralisé ; `quasi_passif_crypto` borné par
`gross_exposure_max` ; citations §1.4/§4.3 exactes ; scalar 0,23-0,79 cité à l'identique) ;
aucun fichier interdit touché, diff `bot/config.py` intégralement en commentaire, registre
inchangé, `check_governance_commit.py` trivialement OK (PROMOTION-RULES intouché) ; suite
`bot/tests/` 561 verts sur la copie d'audit.

**Verdict initial `isSound: false` — 3 findings MAJEURS + 1 MINEUR + 1 INFO, tous traités :**
- **M1 (intégrité du journal)** : la première rédaction de l'entrée (b) affirmait AU PASSÉ une
  revue adversariale et des tests « verts » qui n'avaient pas encore eu lieu, avec renvoi vers
  une entrée (c) inexistante — l'anti-pattern exact que le pré-enregistrement combat, appliqué
  au méta-processus. **Corrigé avant commit** (le journal n'était pas encore committé — aucune
  réécriture d'historique publié) : (b) n'affirme plus que ce qui est advenu, la présente
  entrée (c) documente la revue réellement menée.
- **M2 (contradictions résiduelles)** : `backtest/README.md` gardait 3 passages non touchés
  affirmant l'overlay « aligné production » (table des fichiers, règle n°5, §Surcouche) —
  contredits 30 lignes plus bas par le nouveau bandeau. **Corrigés** : les 3 passages
  renvoient désormais explicitement à la convention et ne revendiquent plus l'alignement.
- **M3 (garde tautologique)** : l'extension du test négatif de schéma ré-affirmait des vérités
  Python génériques sans jamais exécuter les vraies fonctions de validation. **Corrigée** :
  nouveau test rouge/vert `test_sizing_interne_guards_actually_fire_on_malformed_entries`
  (monkeypatch de `config.INCUBATING_STRATEGIES`, `pytest.raises(AssertionError)` sur les
  validations RÉELLES — champ absent ET booléen non strict).
- **Mineur (portée)** : la SPEC ne disait pas si (c) se généralise aux candidates
  equities/ETF. L'auditeur a vérifié que OUI (le runner ne leur applique pas non plus de
  vol-targeting portefeuille réel, `cap_per_asset_equity=1.0`) et qu'AUCUN précédent (b)
  n'existe hors crypto. **§3.6 ajouté à la SPEC** (généralisation + réserve d'honnêteté).
- **INFO (garde manquante, suggestion de l'auditeur adoptée)** : l'avertissement README était
  documentaire, pas mécanique — un futur script `run_*.py` omettant le paramètre hériterait
  silencieusement de l'overlay protecteur. **Livré** :
  `backtest/tests/test_sizing_fidelity_lint.py` (lint statique AST : tout appel à
  `simulate_segment` dans `backtest/run_*.py` doit passer `apply_vol_targeting=` explicitement ;
  exemption FERMÉE limitée à `run_xsmom_invvol.py`, script pré-convention archivé ; test
  rouge/vert de la fonction de lint elle-même — zéro changement de comportement runtime,
  aucune archive re-exécutée).

Suite de tests complète exécutée sur le dépôt corrigé au moment du push final (résultat dans
le commit). Leçon consignée (M1, récidive du motif « test tautologique » de la session #6e) :
un journal append-only n'écrit JAMAIS un résultat avant qu'il n'existe, et une garde de
non-régression doit exécuter le code qu'elle garde.

---

## 2026-10-12 — Session hebdomadaire #10 (a) : REVUE des stratégies actives et candidates + incidents

- **Note de calendrier (honnêteté)** : aucune session hebdomadaire ne s'est tenue le
  2026-10-05 — le déclencheur planifié n'a pas tourné cette semaine-là ; la présente session
  (#10) couvre deux semaines de revue. Aucune décision n'était en attente (labo vide), aucun
  effet au-delà du retard des chantiers #18/#19.
- **Candidates labo** : `INCUBATING_STRATEGIES` toujours vide — aucune Porte 2 à évaluer,
  aucun kill 56 j. **Zéro action requise, zéro action prise.** Labo 100 % cash (1 139,20 USD
  constants ; l'équité en € ne bouge qu'au gré du change).
- **Stratégies actives** : les 3 stratégies de production restent l'antécédent HORS cadre §3
  (`PROMOTION-RULES.md` §5). `DRIFT-REPORT.md` du 2026-10-12 (81 j vécus) : **5 lignes OK**
  (quasi_passif 0,91/1,64/1,55 vécus vs 0,81/0,28/0,07 attendus ; xs_momentum 1,27/1,29 vs
  0,82 ; DD vécus ≤ 1,7 % partout, très loin des seuils 2×) ; 2 lignes **SURVEILLER** pour
  `dual_momentum_etf`, raisons mécaniques connues (#22 : DD de référence absent du registre) +
  Sharpe roulant 60 j négatif depuis 3 j sur la ligne prudente (sous le seuil d'alerte de
  30 j consécutifs — à suivre, pas un signal §3.1 même si §3 s'appliquait). **Aucune règle de
  mort ne serait déclenchée même si §3 s'appliquait. Aucune action.**
- **Incident #18 CONFIRMÉ (occurrence-test prédite par la revue #9)** : maintenance du
  dimanche 2026-10-05 → recalibrage SAUTÉ, 0/6 symboles, `missing_symbols_reasons` donnant
  exactement le diagnostic attendu (« 1 mois manquant(s) (2026-09) » ×6 — archive mensuelle
  de septembre pas encore publiée). L'instrumentation de la session #7 a fait son travail ;
  le correctif pré-enregistré est livré cette session, cf. entrée (d).
- Recalibrage du 2026-10-12 : exécuté normalement (rafraîchissement OK, 16 fenêtres,
  200 optimal sur la grille) — 10e verdict consécutif « aucun changement », conforme.
- Cron fetch-data du samedi 2026-10-10 : OK (491 s), 30 crypto (archives → 2026-09) + 30
  funding + 30 perp + actions/ETF ; 19 anomalies connues, rien de neuf. Ticker **BK** :
  **6e échec consécutif** (yfinance ET stooq) — #20 toujours ouvert, non traité cette
  session (chantiers #19/#18 prioritaires ; l'investigation exige un run réel côté Actions,
  le réseau du conteneur de session étant bloqué vers les sources marché).
- **Gouvernance #14** : toujours aucune décision humaine enregistrée
  (`GOVERNANCE-DOSSIER-2026-08-24-quasi-passif.md`, en attente depuis 7 semaines). Le
  critère vécu de `SELECTION-FINALE.md` §5 arrive à échéance FIN OCTOBRE — c'est
  vraisemblablement la DERNIÈRE session avant qu'il ne tranche de lui-même. Vécus toujours
  au-dessus des références auditées : aucune pression à la bascule à ce jour. **Rappel
  priorité haute maintenu à Mathieu.**
- Wallets au cycle 2026-10-12T01 : 🛡️ 1 021 € | ⚖️ 1 047 € | 🔥 1 076 € | 🧪 1 017 € (labo
  100 % cash, état attendu).

---

## 2026-10-12 — Session hebdomadaire #10 (b) : backlog #19 INSTRUIT — analyse dédiée de la bande de non-négociation (session DÉDIÉE, aucune candidate jugée)

**Contexte.** Priorité n°1 pré-enregistrée par la revue de la session #9 : le dossier #19
(3 pièces : F2 CRITIQUE de l'audit carry #6, écart « flatten sous la bande » #6, quantification
sur candidate réelle #7) conditionnait la fidélité de toutes les futures SPEC.

**Instruction.** Analyse quantitative par agent dédié sur copie isolée (remote neutralisé),
données réelles `market-data` (6 majors horaires 2022-01→2026-09), chaque affirmation
load-bearing revérifiée par l'orchestrateur puis re-démontrée par l'audit (entrée (c)).
Livrable committé : `backtest/results/band_fidelity_analysis_2026-10-12/` (REPORT.md +
14 scripts + 11 JSON de résultats, re-exécutables ; cache de runs moteur non committé).

**Constats structurants** (détail chiffré au REPORT) : (i) écart (a) RÉEL — en walk-forward
(le protocole), ΔSharpe S2−S1 ∈ [−0,220 ; +0,007] sur 21 configs, négatif 18/21 ; contenu sur
les candidates passées ([−0,066 ; +0,007]) mais du même ordre que la valeur de la bande sur le
signal quasi-passif (−0,11 vs 0,12) ; signe dépendant de l'horizon ET du P&L du reste du
wallet (inconnaissable en poche-standalone) ; AUCUN verdict passé du registre ne bascule ;
(ii) la formulation de l'écart (b) de la fiche était IMPRÉCISE — seul le flatten des CIRCUIT
BREAKERS contourne la bande en production, un zéro stratégique sous la bande est conservé des
deux côtés ; le vrai écart (b′) : le moteur ne modélise aucun CB — matériel aux seuils du LABO
dès un profil à faible poids porté à a=0,60 (MaxDD wallet simulé 26,3 % > flatten 25 %) ;
(iii) le piège « poids < bande ⇒ jamais d'entrée » existe AUSSI en production (δ nul à
l'entrée depuis flat) — nuance datée versée à la fiche #16.

**DÉCISION (pré-enregistrée dans `docs/BAND-FIDELITY-SPEC.md`, nouveau document de convention
— même pattern que SIZING-FIDELITY ; `PROMOTION-RULES.md` reste GRAVÉ et intouché, le moteur
reste bit-à-bit inchangé)** : **O2 renforcée + O4, option O1 (amendement du moteur) écartée.**
- Porte 1 toujours jugée sur la sémantique moteur S1 (comparabilité avec les 16 entrées du
  registre ; O1 écartée pour inconnaissabilité de R(t) — PAS au nom du pessimisme : en
  walk-forward S2 est plus sévère 18/21, constat consigné honnêtement et versé à #12).
- Toute SPEC de candidate déclare son `capital_alloc_pct` et fournit l'annexe `band_fidelity` :
  run S2 apparié précisément défini (poids a·W, bande 0,05·a, mêmes fenêtres, sélection IS NON
  rejouée, métriques reconverties en unités de poche P = 1 + (E−1)/a) + bloc CB aux seuils du
  wallet LABO (0,15/0,25) + texture d'exécution pour les candidates à rampe/faible poids.
- `band_fragile` = FLAG consigné (un seuil §1.2 bascule S1↔S2 — définition SANS nouveau seuil
  chiffré), tranché en session selon la pratique d'ambiguïté conservatrice existante — AUCUNE
  règle de rejet automatique nouvelle (ce serait un critère de Porte 1, domaine d'amendement
  humain). Deux propositions versées à #12 : inscription de l'annexe dans §1.4, et critère
  formel « S1 ET S2 ».
- Mécanisation : `tools/verify_research.py --check` impose le bloc `band_fidelity` (clés
  minimales typées) ou une exemption ≥ 30 caractères à toute entrée datée ≥ 2026-10-12, et un
  `date_test` ISO à TOUTES les entrées (comparaisons lexicographiques sinon contournables) ;
  limite documentée : l'antidatage n'est détectable que par l'audit, pas par la garde.
  Tests : `tools/tests/test_verify_research.py` (33 tests).
- Nouvel écart résiduel (c) — base d'équity du dimensionnement / P&L du reste du wallet —
  ouvert en P3 (#23), limite STRUCTURELLE documentée, pas un correctif à faire.

**Registre INCHANGÉ** (aucune stratégie testée cette session — prochaine candidate :
K_total = 16 lignes + sa grille). Labo toujours vide (0/3).

---

## 2026-10-12 — Session hebdomadaire #10 (c) : audit adversarial du livrable #19 — verdict initial `isSound: false`, 5 MAJEURS corrigés, contre-audit `isSound: true`

**Protocole.** Auditeur indépendant sur copie isolée (remote neutralisé), mandaté pour démolir
l'analyse, la convention et la garde AVANT tout commit ; reproductions from scratch (sans les
scripts de l'analyse) ; mutations de la garde ; vérification du périmètre du diff.

**Ce qui tient (à charge de preuve exécutée par l'auditeur)** : tous les chiffres de l'analyse
reproduits indépendamment (S1 0,6994 / S2 0,7382 / S3 0,3770 à ±0,001 ; walk-forward profil A
ΔSharpe −0,103/−0,117/−0,111 identiques ; dérivation δ reconfirmée : 0 désaccord sur ~16 000
états du vrai RiskManager) ; preuve par exécution que seul le flatten CB contourne la bande en
production ; aucun fichier interdit touché, moteur bit-à-bit inchangé ; suite complète 891
tests verts sur la copie d'audit.

**Verdict initial `isSound: false` — 5 MAJEURS + 5 mineurs, tous corrigés avant commit :**
- **M1 (récit inversé)** : la première rédaction minimisait l'écart (a) en citant la fenêtre
  complète (« un ordre de grandeur sous la valeur de la bande ») là où le protocole réel est
  le walk-forward — où l'écart est du MÊME ordre que la valeur de la bande sur le profil
  quasi-passif (−0,11 vs 0,12) et où 3 configs ont un IC excluant zéro. **Corrigé** : SPEC,
  REPORT et backlog citent le walk-forward d'abord, chiffres complets.
- **M2 (argument de pessimisme intenable)** : O1 était écartée au motif que S2 « pourrait
  rehausser les chiffres » — vrai seulement hors protocole ; en walk-forward S2 est PLUS
  sévère 18/21. **Corrigé** : motifs réels (comparabilité + R(t) inconnaissable), constat
  18/21 consigné et versé à l'amendement #12d.
- **M3 (règle de rejet déguisée)** : « band-fragile » était initialement rédigé en règle de
  rejet automatique avec un seuil neuf (|ΔSharpe| > 0,10) — un critère de Porte 1 déguisé,
  contradictoire avec l'en-tête « aucun seuil ajouté », et le précédent invoqué
  (xs_momentum_invvol) tranchait au cas par cas, sans pré-enregistrer de règle générale.
  **Corrigé** : band_fragile est un FLAG sans seuil chiffré, tranché en session, proposition
  de règle formelle versée à #12 (décision humaine).
- **M4 (garde trop faible)** : la garde acceptait tout dict non vide ({"foo": null}), toute
  chaîne (« n/a »), et les comparaisons lexicographiques étaient contournables par une date
  non-ISO. **Corrigé** : clés minimales typées (a/sharpe numériques, band_fragile booléen,
  cb dict non vide), exemption ≥ 30 caractères, regex ISO sur TOUTES les entrées ; limite
  d'antidatage documentée. Mutations de la garde toutes tuées par les tests (vérifié par
  l'auditeur sur 6 mutations, puis re-vérifié au contre-audit sur 5).
- **M5 (S2 sous-spécifié, seuils CB du mauvais wallet)** : la convention ne disait ni la
  reconversion en unités de poche ni le sort de la sélection IS ; l'analyse appliquait les
  seuils « agressif » dès a ≥ 0,5 alors que la destination d'une Porte 1 est le LABO
  (0,15/0,25) — sous lesquels le profil B à a=0,60 DÉPASSE le seuil de flatten (26,3 % >
  25 %), contrairement au « aucun flatten » initial. **Corrigé** : SPEC §3.2 précise tout,
  REPORT rectifié, erratum daté ajouté à `tables_generated.md` (artefact généré non
  régénéré — les MaxDD sont exacts, seule la colonne de déclenchement était à relire).
- Mineurs corrigés : affirmation prématurée de revue adversariale au passé (récidive M1 #9,
  reformulée pour ne dire que l'advenu) ; chemins en dur des scripts t1/t2/t3d (paramétrés
  `BAND19_REPO`/`BAND19_DATA_DIR`, ordre de reproduction documenté) ; message de succès et
  docstrings de verify_research ; amendements #12c/#12d explicitement versés à la fiche #12
  (le #21 de la session #9 ne l'avait jamais été — régularisé) ; en-têtes README nuancés.

**Contre-audit (même auditeur, après corrections) : `isSound: true`** — les 5 points
re-vérifiés par exécution (grep des formulations, registres malformés ×15 sur la garde via le
point d'entrée réel, cohérence croisée des chiffres, 230 tests tools verts, aucun fichier
interdit). Résidus mineurs du contre-audit soldés dans la foulée : validation de TYPE des
clés du bloc (+2 tests), docstring de la garde, 4 formulations, erratum tables. La leçon M1
de la session #9 a tenu : aucune entrée de ce journal n'a été écrite avant les faits qu'elle
rapporte.

---

## 2026-10-12 — Session hebdomadaire #10 (d) : backlog #18 SOLDÉ — correctif début-de-mois du rafraîchissement crypto, audit `isSound: true`

**Occurrence-test confirmée** (cf. entrée (a)) : le correctif pré-enregistré par la fiche #18
est livré. `tools/fetch_data.py` : le complément API klines est étendu aux mois requis
manquants EN QUEUE d'historique — suffixe contigu se terminant au dernier mois requis,
plafonné à `CRYPTO_TAIL_COMPLETION_MAX_MONTHS = 3` (au-delà, ce n'est plus un retard de
publication : exclusion comme avant, sans appel API) — JAMAIS aux trous anciens/du milieu
(trace du biais du survivant, non-régression prouvée par test différentiel exhaustif
ancien/nouveau : 0 divergence sur 201 cas hors queue). Critère « mois couvert » pessimiste :
première bougie ≤ début+1h, dernière ≥ fin−1h, ET densité horaire complète (heures distinctes
≥ heures du mois − 1). Traçabilité : champ `tail_months_completed_via_api` au MANIFEST
(additif, seul lecteur `weekly_maintenance.load_crypto_exclusion_reasons` vérifié intact) +
section dédiée du DATA_REPORT + raison d'inclusion explicite.

**Audit adversarial indépendant : `isSound: true`** (aucun CRITIQUE/MAJEUR). 3 findings
MINEURS, tous corrigés : (1) trou INTÉRIEUR d'un mois de queue non détecté (le scan
d'anomalies ne couvre pas la crypto, contrairement à ce que disait le commentaire) → critère
de densité horaire ajouté ; (2) KeyError possible sur colonne absente → garde pessimiste ;
(3) une API qui renverrait des bougies antérieures à `startTime` aurait écrasé l'archive via
`drop_duplicates(keep="last")` → complément borné strictement à [début de queue, +∞). INFO
corrigées : libellé « non encore publiée » étendu au cas archive en erreur ; en-tête crypto
du DATA_REPORT. INFO acceptée telle quelle (pré-existante) : mois courant tronqué toléré si
la pagination s'interrompt après la queue. Les 13 tests d'attaque de l'auditeur sont intégrés
au dépôt (`tools/tests/test_fetch_data_tail_completion_audit.py` — les 3 « rouges » qui
documentaient les findings sont verts sur le code corrigé ; le test différentiel se skippe
proprement hors contexte d'audit, son résultat fait foi ici). Suite tools : 232 verts.
**Validation sur run réel attendue** à la prochaine maintenance en tout début de mois
(occurrence candidate : dimanche 2026-11-01).

---

## 2026-10-12 — Session hebdomadaire #10 (e) : hygiène et passation

- `RESEARCH-REGISTRY.json` : INCHANGÉ (aucune stratégie testée). `verify_research --check` :
  16 entrées, OK (nouvelles vérifications band_fidelity + dates ISO actives).
- `RESEARCH-BACKLOG.md` : #19 ✅ traitée (convention), #18 ✅ soldée (correctif), #16 nuancée
  (note datée), #12 complétée ((c) convention #21, (d) annexe band_fidelity + critère
  « S1 ET S2 »), #20 mis à jour (6e échec BK), #23 créée (écart (c), P3, fiche de mémoire),
  priorités session #11 posées : 1) prochaine candidate OU sourcing de nouvelles idées (le
  backlog technique P0/P1 est épuisé — 8 jugées, 0 incubée), 2) #14 (échéance fin octobre !),
  3) vérification run réel #18 (2026-11-01), 4) #20, 5) #22/#15.
- Suite de tests complète exécutée avant push (résultat dans le commit final).

---

## 2026-10-19 — Session hebdomadaire #11 (a) : REVUE des stratégies actives et candidates

- **Candidates labo** : `INCUBATING_STRATEGIES` toujours vide — aucune Porte 2 à évaluer,
  aucun kill 56 j. **Zéro action requise, zéro action prise.** Labo 100 % cash (1 139,20 USD
  constants).
- **Stratégies actives** : les 3 stratégies de production restent l'antécédent HORS cadre §3
  (`PROMOTION-RULES.md` §5). `DRIFT-REPORT.md` du 2026-10-19 (88 j vécus) : **5 lignes OK**
  (quasi_passif 0,83/1,34/0,82 vécus vs 0,81/0,28/0,07 attendus ; xs_momentum 0,88/0,50 vs
  0,82 ; DD vécus ≤ 2,5 % partout, très loin des seuils 2×) ; 2 lignes **SURVEILLER** pour
  `dual_momentum_etf` : (i) référence de dérive indisponible — cause réelle identifiée et
  corrigée cette session (décalage d'identifiant, backlog #22, cf. entrée (c)) ; (ii) Sharpe
  roulant 60 j négatif depuis 6 j sur la ligne prudente — sous le seuil d'alerte §3.1 de 30 j
  consécutifs, à suivre. **Aucune règle de mort ne serait déclenchée même si §3 s'appliquait.
  Aucune action.**
- Recalibrage du 2026-10-19 : exécuté normalement (rafraîchissement OK, 16 fenêtres,
  `REGIME_SMA_DAYS=200` optimal sur la grille, IS le plus souvent 175 — informatif) — 11e
  verdict consécutif « aucun changement », conforme.
- Cron fetch-data du samedi 2026-10-17 : OK (394 s), 30 crypto (archives → 2026-09 +
  complément API) + 30 funding + 30 perp + actions/ETF → 2026-10-16 ; 19 anomalies connues,
  rien de neuf. Ticker **BK** : 7e échec consécutif — mais la fiche #20 est CORRIGÉE cette
  session : BK ne fait PAS partie de `UNIVERSE_SP100` (vérifié par exécution — seule la liste
  de `tools/fetch_data.py` le contient), aucun impact backtest/stratégie, priorité déclassée
  P3.
- **Gouvernance #14** : toujours aucune décision humaine enregistrée (8 semaines). Le critère
  vécu de `SELECTION-FINALE.md` §5 arrive à échéance FIN OCTOBRE (~1-2 semaines) — vécus
  toujours au-dessus des références auditées, aucune pression à la bascule, mais sans décision
  le statu quo tranchera de facto. **Rappel priorité haute maintenu à Mathieu.**
- Wallets au cycle 2026-10-19T01 : 🛡️ 1 014 € | ⚖️ 1 037 € | 🔥 1 038 € | 🧪 1 012 € (labo
  100 % cash, état attendu ; repli ~-25 à -38 € sur la semaine, pullback crypto/actions
  mi-octobre, aucun breaker actif).

---

## 2026-10-19 — Session hebdomadaire #11 (b) : sourcing de nouvelles idées + Porte 1 de `lowvol_sp100` — REJETÉE (rejet AUTOMATIQUE §1.4, audit `isSound: false`)

**Contexte.** Priorité n°1 de la revue #10 : le backlog technique P0/P1 est épuisé (8
candidates jugées, 0 incubée) — « prochaine candidate OU sourcing de nouvelles idées ».
Sourcing mené en début de session (revue de littérature practitioner) : 2 pistes retenues et
documentées AVANT tout test — (i) low-volatility défensif S&P100 (anomalie documentée depuis
~35 ans, données en place, coûts 5 bps, famille structurellement neuve pour le projet) ;
(ii) tilt cross-sectionnel par funding rate (nouvelle fiche #27, prior honnêtement FAIBLE :
l'étude practitioner la plus directe — Presto Research 2024 — trouve un pouvoir prédictif T+1
NUL en série temporelle et un alpha xs à turnover extrême sans coûts). La piste (i), au prior
le plus fort et au régime de coûts le plus favorable, est retenue pour la Porte 1.

**Protocole.** SPEC pré-enregistrée `backtest/LOWVOL-SP100-SPEC.md` committée AVANT exécution
(commit 35df9b5) : grille figée L∈{63,252}×N∈{20,30} (bottom-N par vol réalisée, équipondéré,
rebalance mensuel, AUCUN filtre de régime), walk-forward 36m/12m (30 fenêtres, OOS 1996→2026,
7 549 j), coûts 5 bps/côté, benchmark SPY aligné, contrôle apparié équipondéré compté dans
K_total = 16 + 30×4 + 30×1 = 166, `sizing_interne: false` ⇒ `apply_vol_targeting=False`
(SIZING-FIDELITY), annexe `band_fidelity` complète (a=0,30, S2, bloc CB labo), portage
inter-fenêtres actif. Implémentation et run par agent délégué ; moteur, risque, config :
INTOUCHÉS.

**Résultat brut.** Les 5 seuils §1.2 passent nominalement : Sharpe OOS 0,813 ≥ 0,70 ;
PF 11,12 ; 371 trades ; MaxDD relatif 0,95× ; DSR 0,957 (K=166). L'implémenteur signale
LUI-MÊME l'anomalie A3 avant tout verdict — honnêteté conforme au protocole.

**Audit adversarial indépendant (copie isolée, remote neutralisé) : `isSound: FALSE`.**
Ce qui tient : zéro look-ahead (perturbation bit-exacte, 378/378), reproduction indépendante
sans le code de la candidate à 1e-15, DSR recalculé from scratch identique, S2 reproduit,
grille conforme à la SPEC. Ce qui tombe :
- **C1 (CRITIQUE)** : le moteur n'a pas exécuté la stratégie déclarée. Bande plate 0,05 vs
  poids 1/N : entrées à 0,05 pile (égalité flottante 1/20 == 0.05), sorties d'un titre sorti
  du bottom-20 JAMAIS exécutées (poids dérivé < bande) → 44 titres détenus en moyenne (max
  62), brut moyen 1,25 (max 1,80), **cash négatif 98 % des jours (min −0,80 de l'équity)** —
  un levier implicite non financé que la production interdit (`bot/sim` `Ledger` :
  `ValueError` sur achat sans cash). Le moteur a donc appliqué un sizing PLUS GÉNÉREUX que la
  production — l'interdit exact de §1.4. PF 11 = artefact (307 gains réalisés / 64 pertes —
  les pertes sous la bande ne sont jamais vendues ; à rebalance réellement exécuté, PF ~2,7).
- **C2 (CRITIQUE)** : contrôle apparié INERTE (poids ~1/100 sous la bande, 0 ordre sur 30
  fenêtres, 100 % cash) — la branche « domination » de la SPEC §5 est inévaluable. Défaut de
  la SPEC pré-enregistrée elle-même (bande non anticipée), pas de l'implémentation.
- **M1-M4** : N=30 inentrable (grille de facto 2 combos) ; fidélité sizing (le vrai
  `RiskManager` reproduit la bande à l'identique mais pas le cash négatif) ; biais du
  survivant quantifié (contrôle équipondéré CAGR 17,1 % vs SPY 10,4 %) ; lecture pertinente
  du bloc CB = équity wallet S2 chaînée (MaxDD ~20,7 %, 4 épisodes half-size labo, 0 flatten
  — la prédiction a priori de la SPEC, 11-14 %, est RÉFUTÉE).

**DÉCISION (sémantique SPEC §5, figée avant exécution)** : `isSound: false` ⇒ **REJET
AUTOMATIQUE** (§1.4), statut `rejetee` au registre (17e entrée), **rejet de PROCÉDURE — pas un
verdict sur l'anomalie low-vol** (contrefactuel indicatif de l'auditeur, hors protocole :
candidate ~0,83 vs contrôle équipondéré ~0,92, IR −0,58, candidate meilleure uniquement sur le
MaxDD). Aucun re-run, aucune variante dans cette session (§3.3). Conditions de tout retest
consignées dans la fiche #24. `band_fragile: false` (aucun seuil ne bascule S1↔S2).

**Retombées structurelles (nouvelles fiches)** : #25 (P0 — gardes de financement du moteur :
`cash >= 0`/brut ≤ 1 + métriques `min_cash`/`max_gross`, BLOQUANT de fait pour toute future
Porte 1) ; #26 (P2 — incompatibilité structurelle bande × équipondéré N ≥ 20, prouvée en
production aussi, versée à #12e — question d'architecture humaine §4.3) ; #27 (P2 — funding
tilt, prior faible documenté). Checklist immédiate pour toute future SPEC : poids cibles
strictement > bande ET contrôle qui émet ≥ 1 ordre (smoke run) AVANT commit de la SPEC.

---

## 2026-10-19 — Session hebdomadaire #11 (c) : hygiène

- **Backlog #22 SOLDÉ** : la cause réelle du « DD de référence indisponible » des 2 lignes
  `dual_momentum_etf` du DRIFT-REPORT n'était pas un MaxDD absent du registre (l'entrée
  `dual_momentum_multiclasse_etf` porte 0,6538/27,57 %) mais un décalage d'identifiant
  (`strategy_ref` court vs id long du registre). Correctif : table d'alias explicite
  `STRATEGY_REGISTRY_ALIASES` dans `tools/weekly_maintenance.py` (un id direct prime toujours),
  +2 tests non tautologiques. Purement informatif, aucune règle de décision touchée.
- **Backlog #20 CORRIGÉ et déclassé P3** : BK n'est pas dans `UNIVERSE_SP100` (vérifié par
  exécution) — les 7 échecs de fetch n'affectent que la liste de téléchargement.
- `RESEARCH-REGISTRY.json` : +1 entrée (`lowvol_sp100`, `rejetee`, bloc `band_fidelity`
  complet) — 17 entrées, `verify_research --check` OK. Toute prochaine candidate : K_total =
  17 lignes + sa grille.
- `RESEARCH-BACKLOG.md` : #24-#27 créées, #12e versée, priorités session #12 posées
  (1. #25 gardes moteur ; 2. #14 — ÉCHÉANCE fin octobre ; 3. vérif run réel #18 le
  2026-11-01 ; 4. prochaine candidate après #25 ; 5. #20 P3).
- Suite de tests complète exécutée avant push (résultat dans le commit final).
