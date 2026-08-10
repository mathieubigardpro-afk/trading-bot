# SPEC pré-enregistrée — `quasi_passif_crypto_wf_retest` (backlog P2#13, priorité n°1 de la session #3)

*Session hebdomadaire #3, 2026-08-10. Committée AVANT toute exécution du backtest
(pré-enregistrement au sens de `docs/PROMOTION-RULES.md` §0). Toute déviation entre cette
spec et l'implémentation est un finding d'audit, pas une liberté d'implémentation.*

## Objet — dette de recherche, PAS une nouvelle candidate

`quasi_passif_crypto` est la SEULE brique crypto en production (poche crypto des 3 wallets
réels) et a été déployée sur un backtest **explicitement non audité** (exécution unique, sans
walk-forward, sans DSR, sans audit adversarial — `docs/SELECTION-FINALE.md` §2.2,
`RESEARCH-LOG.md` 2026-07-23). Ce retest applique le protocole Porte 1 complet
(`PROMOTION-RULES.md` §1) comme **grille de validation**, pas comme porte d'incubation :
la stratégie est déjà déployée, antécédent hors cadre §3 (`PROMOTION-RULES.md` §5).

**Sémantique des issues, figée à l'avance :**
- **Tous seuils §1.2 passés + audit `isSound: true`** → dette soldée, la brique crypto en
  production est validée a posteriori au niveau de rigueur du protocole. Aucun changement de
  production (elle y est déjà).
- **Un ou plusieurs seuils manqués, ou `isSound: false`** → AUCUNE action automatique sur la
  production (l'antécédent est hors §3 tant qu'un humain ne l'aligne pas explicitement sur le
  cadre formel, §5 ; son critère d'échec vécu propre — `docs/SELECTION-FINALE.md` §5, bascule
  si sous-performance nette sur 3 mois réels — reste le seul déclencheur opérationnel).
  Le résultat est consigné au registre, signalé à Mathieu en priorité haute, et une session de
  gouvernance DÉDIÉE (jamais celle-ci, §0) devra statuer sur l'alignement de l'antécédent.

## Stratégie testée — paramètres de production GELÉS, AUCUNE grille

Réplique fidèle de `bot/strategies/quasi_passif_crypto.py` (les helpers du module de
production — `_daily_closes`, `_is_trend_on`, `_basket_vol_annualized` — sont IMPORTÉS,
jamais réimplémentés) :

1. Filtre de tendance : éligible si dernière clôture journalière complète > SMA200 des
   clôtures journalières (agrégées depuis l'horaire, jours complets 24h uniquement).
2. Vol EWMA (halflife 60 h) des rendements horaires du panier équipondéré des actifs "on",
   annualisée √8760.
3. `poids_brut = min(gross_exposure_max, vol_target / vol_réalisée)`.
4. Répartition égale entre actifs "on", plafonnée à `cap_per_asset`.
5. Décision quotidienne (le signal ne change qu'au premier cycle après minuit UTC) ;
   poids décidé à la clôture t, exécuté à l'open t+1 (moteur commun).

**3 variantes = les 3 déploiements réels, paramètres lus de `bot/config.py:WALLETS`
(source de vérité, jamais recopiés en dur) :**

| Variante | Univers (SPEC_UNIVERSE_BY_WALLET) | vol_target | gross_max | cap/actif |
|---|---|---|---|---|
| `prudent` | BTC, ETH | 0,10 | 0,40 | 0,20 |
| `equilibre` | BTC, ETH, SOL, DOGE, LINK, AVAX | 0,20 | 0,70 | 0,25 |
| `agressif` | BTC, ETH, SOL, BNB, XRP, XLM, HBAR, ICP, OP, UNI, FIL (11) | 0,35 | 0,90 | 0,30 |

Zéro degré de liberté nouveau : aucun paramètre n'est optimisé, la grille interne est de
1 combinaison par variante. `select_params_via_is` tourne quand même (1 combo) pour rester
dans le cadre du moteur — conformément à §1.3, chaque fenêtre compte 1 essai.

**Différences assumées backtest vs production (à documenter, pas à corriger en douce) :**
- La politique "donnée manquante → gel 24 cycles puis liquidation" (correctif §12.4) est une
  robustesse d'exploitation temps réel : en backtest sur données statiques alignées, un actif
  sans historique suffisant (ex. OP avant ~2023-01, SMA200 non calculable) est simplement
  inéligible (poids 0), jamais "gelé".
- La surcouche `RiskManager` de production (vol targeting portefeuille + bande 0,05) est
  répliquée par l'overlay du moteur commun (post-correctifs F1/F2/F3) avec les constantes
  HORAIRES : `vol_ewma_halflife_days=HOURLY_VOL_EWMA_HALFLIFE_PERIODS` (60),
  `vol_periods_per_year=HOURLY_VOL_PERIODS_PER_YEAR` (8760), `vol_target_annualized` = celui
  du wallet de la variante, bande 0,05. Le double vol-targeting (stratégie PUIS overlay) est
  le comportement RÉEL de production — il est conservé (et il est pessimiste : jamais
  d'augmentation d'exposition par l'overlay).

## Données, fenêtres, coûts

- Bougies horaires branche `market-data` (2022-01-01 → 2026-06-30), chargeur commun
  `backtest/data_hourly.py`. OP démarre 2022-06 (éligibilité sans backfill).
- Walk-forward 9 m IS / 3 m OOS, pas 3 m (convention crypto horaire §1.1) — attendu ≈ 14
  fenêtres, générées par `engine.generate_walk_forward_windows` (le nombre exact produit par
  le générateur fait foi).
- Coûts : **uniformes au palier le plus défavorable de chaque univers** (précédent
  `vol_breakout_6majors`, pessimisme §0.2 ARCHITECTURE) :
  - `prudent` : 15 bps/côté (BTC/ETH majors : 10 fee + 5 slippage) ;
  - `equilibre` : 25 bps/côté (DOGE/LINK/AVAX mids : 15 + 10) ;
  - `agressif` : 45 bps/côté (XLM/HBAR/ICP/OP/FIL smalls : 25 + 20) — TRÈS pessimiste pour
    les 5 majors du panier (~moitié du poids typique) ; assumé, ambiguïté en défaveur du bot.
  - Stress §1.4 : PF à 3× et 5× le coût nominal de chaque variante + coût de la dérive :
    turnover OOS annualisé × coût, rapporté au rendement.

## Benchmark et seuils (Porte 1 §1.2, appliqués par variante)

- Benchmark par variante : buy & hold équipondéré du MÊME univers, mêmes fenêtres OOS
  alignées, sans coûts ni overlay (convention du registre).
- Sharpe OOS net ≥ 0,70 ; PF OOS > 1,15 ; MaxDD OOS ≤ 1,5× MaxDD OOS benchmark aligné ;
  DSR ≥ 0,50 ; trades OOS clos ≥ 80 **OU** justification écrite §1.2 pour stratégie
  structurellement lente — pré-enregistrée ici : le quasi-passif décide 1 fois/jour avec
  bande 0,05 (faible turnover PAR CONSTRUCTION) ; si < 80 trades clos, le critère de
  substitution est (a) ≥ 24 mois d'OOS couverts ET (b) ≥ 2 régimes de marché distincts dans
  l'OOS total (bear 2022-2023 + haussier/latéral 2024-2026 sont dans la fenêtre).
- **DSR / K_total (§1.3), figé maintenant** : registre = 11 lignes au 2026-08-10 ; essais
  internes = 3 variantes × n_fenêtres × 1 combo (attendu 3 × 14 = 42) →
  **K_total attendu = 53** (le nombre exact de fenêtres du générateur fait foi ; formule
  figée, pas le chiffre). Le DSR de CHAQUE variante est déflaté sur ce K_total commun
  (les 3 variantes sont 3 essais de la même session, jamais 3 registres séparés).
- Verdict global : la validation est prononcée PAR VARIANTE (3 verdicts distincts au
  registre sous une entrée unique `quasi_passif_crypto_wf_retest`) — pas de moyenne entre
  variantes, pas de compensation.

## Analyses d'honnêteté obligatoires dans le rapport

- Sous-périodes 2022-2023 vs 2024-2026 (stabilité temporelle — le motif d'échec de
  `vol_breakout_6majors` doit être cherché ici aussi) ;
- Exposition brute réalisée moyenne par variante (le Sharpe non audité 1,24-1,49 était
  attribué à une exposition faible 12-48% : vérifier si le mécanisme persiste sur le moteur
  commun audité) ;
- Comparaison aux chiffres non audités d'origine (1,24 / 1,47 / 1,49) : écart et causes
  plausibles — SANS jamais retoucher un paramètre pour s'en rapprocher ;
- Nombre de croisements SMA200 distincts par actif (épisodes indépendants vs nombre de
  trades) ;
- Corrélation OOS entre les 3 variantes (elles partagent BTC/ETH : ce sont ~1 pari corrélé,
  pas 3 validations indépendantes — à chiffrer).

## Audit adversarial (§1.4)

Agent indépendant de la session d'implémentation, copie isolée, remote git neutralisé.
Au minimum : anti-look-ahead (perturbation des données futures), fidélité ligne à ligne aux
helpers de production importés (aucune réimplémentation divergente), recalcul indépendant du
DSR/K_total et des seuils, reproduction des chiffres depuis les données brutes, sizing
cohérent avec le `RiskManager` réel, absence de retouche post-OOS. `isSound: false` = échec
de la validation, quel que soit §1.2.
