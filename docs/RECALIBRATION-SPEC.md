# RECALIBRATION-SPEC.md — Grille pré-enregistrée pour le recalibrage encadré

*Document de gouvernance complémentaire à `docs/PROMOTION-RULES.md`. Pré-enregistre, AVANT toute
exécution réelle contre des données rafraîchies, la (les) grille(s) de paramètres que
`tools/weekly_maintenance.py` a le droit de re-walk-forward. Écrit dans la MÊME session que
l'infrastructure mécanique elle-même (`tools/weekly_maintenance.py`,
`.github/workflows/weekly-maintenance.yml`) — aucune exécution réelle contre des données
rafraîchies n'a eu lieu avant l'écriture de ce document (réseau bloqué dans l'environnement de
développement, cf. `docs/ARCHITECTURE.md` §0.2/§0.3) : la pré-inscription précède
structurellement tout résultat, dans l'esprit de `docs/PROMOTION-RULES.md` §0, même si ce
document N'EST PAS lui-même une modification de `PROMOTION-RULES.md` (fichier "gravé", jamais
touché ici — c'est une raison délibérée de créer un document séparé plutôt que d'y ajouter une
section).*

---

## 0. Pourquoi une grille séparée, étroite, et un seul paramètre

`docs/config-strategies.json` (hors dépôt, `/home/claude/trading-bot/docs/` — poste de travail de
recherche, jamais poussé sur `origin`) documente explicitement, pour
`crypto_quasi_passif_vol_targete.signal_rules.no_optimization` : **aucun paramètre de cette
stratégie n'a jamais été optimisé sur des données** — `vol_target_annualized`,
`gross_exposure_max`, `cap_per_asset`, `vol_ewma_halflife_hours` sont repris tels quels de
`bot/config.py:WALLETS[*]["risque"]`.

`docs/PROMOTION-RULES.md` §4.3 et §0 interdisent explicitement à toute session de
recherche/promotion (donc à ce recalibrage automatique aussi) de toucher
`CB_*`/`vol_target_annualized`/`gross_exposure_max`/`cap_per_asset`, et plus généralement tout
`bot/config.py:WALLETS[*]["risque"]` — cadre de risque hors de portée de la boucle de recherche,
réservé à un humain hors du cadre recherche.

**Conséquence directe : ce document ne pré-enregistre AUCUNE grille sur `vol_target_annualized`,
`gross_exposure_max`, `cap_per_asset` ni `vol_ewma_halflife_hours`** (tous les quatre vivent dans
`WALLETS[*]["risque"]`, donc explicitement hors de portée). Le seul paramètre éligible retenu ici
est **`REGIME_SMA_DAYS`** — la fenêtre du filtre de tendance SMA
(`bot/config.py:REGIME_SMA_DAYS` **et** `bot/strategies/quasi_passif_crypto.py:REGIME_SMA_DAYS` :
les deux constantes doivent être changées ENSEMBLE, cf. `tools/weekly_maintenance.py:
apply_recalibration_to_files()` — la stratégie lit sa propre copie locale du paramètre, jamais
celle de `bot/config.py`). Ce n'est pas un paramètre du cadre de risque : c'est un paramètre de
SIGNAL (la fenêtre du filtre de tendance), analogue à `lookback_months` pour `xs_momentum_sp100`
ou `dual_momentum_multiclasse_etf` (tous deux déjà traités comme des paramètres de stratégie
walk-forwardables dans leurs backtests respectifs, `bt-final/xs-momentum-sp100/`,
`bt-final/dual-momentum-etf/`).

---

## 1. Grille pré-enregistrée

| Stratégie | Paramètre | Grille (jours) | Valeur actuelle en production |
|---|---|---|---|
| `quasi_passif_crypto` | `regime_sma_days` | `[150, 175, 200, 225, 250]` | 200 (`bot/config.py:REGIME_SMA_DAYS`) |

Grille symétrique autour de la valeur actuelle (±25 %, pas de 25 jours), choisie AVANT tout
résultat par un raisonnement de fond (fenêtres de moyenne mobile usuelles pour un filtre de
tendance lent, cohérent avec `docs/ARCHITECTURE.md` §2 `REGIME_SMA_DAYS`/
`REGIME_ATR_PERCENTILE_WINDOW_DAYS`) — jamais élargie ou resserrée après coup pour "capturer" un
résultat. Toute extension future de cette grille exige une nouvelle session dédiée, documentée
dans `docs/RESEARCH-LOG.md`, distincte de toute évaluation de résultat (même principe que
`docs/PROMOTION-RULES.md` §0).

`tools/weekly_maintenance.py:validate_param_in_grid()` refuse structurellement (lève
`ValueError`) toute valeur hors de cette liste, à chaque appel — jamais une valeur "presque dans
la grille" tolérée par approximation.

---

## 2. Méthode de walk-forward

- Moteur : `tools/weekly_maintenance.py:simulate_daily_returns()`, qui réutilise directement
  `bot.strategies.quasi_passif_crypto._daily_closes()` (même définition exacte de "jour
  calendaire complet" que la production) pour le filtre de tendance.
- **Simplification documentée et assumée** par rapport à la formule exacte de production
  (`_basket_vol_annualized`) : la vol réalisée utilisée pour le sizing est une EWM sur le
  rendement quotidien MOYEN de TOUT l'univers (pas seulement les actifs "on" ce jour-là), pour
  rester vectorisable/rapide sur plusieurs années de données horaires × 5 valeurs de grille × N
  fenêtres walk-forward, dans le budget de 60 minutes du workflow GitHub Actions. Cette
  simplification est appliquée IDENTIQUEMENT à chaque valeur de la grille testée : elle
  n'invalide donc pas la comparaison RELATIVE entre elles, seul usage fait de ce simulateur
  (jamais un chiffre de performance de ce simulateur affiché comme un résultat de backtest
  définitif — cf. `bt-final/quasi-passif-crypto/` pour le backtest de référence réel, non
  audité par ailleurs).
- Walk-forward 9 mois IS / 3 mois OOS (glissant, non chevauchant), même convention que les 8
  backtests audités de la vague 1 (`docs/RESEARCH-REGISTRY.json`).
- Univers/coûts/profil de risque : wallet `equilibre` (6 majors, `bot/config.py:WALLETS`) sert de
  référence unique pour le recalibrage — le SPEC ne fait varier `REGIME_SMA_DAYS` ni par wallet ni
  par univers (la stratégie n'expose qu'un seul `REGIME_SMA_DAYS` module-level, partagé par les 3
  wallets).
- Décision : `tools/weekly_maintenance.py:decide_recalibration()` — change si et seulement si :
  1. la meilleure valeur de la grille (Sharpe OOS concaténé sur toutes les fenêtres) diffère de
     la valeur en production ;
  2. le Sharpe OOS concaténé actuel de la valeur en production est strictement positif (sinon
     refus automatique — un changement de signe n'est pas mesurable en relatif, revue humaine
     requise plutôt qu'une décision automatique) ;
  3. l'amélioration relative de Sharpe OOS concaténé dépasse 10 %.
- Toute valeur hors grille est refusée structurellement (jamais générée par le code, et vérifiée
  explicitement par `validate_param_in_grid()` à chaque appel de `decide_recalibration()`).

---

## 3. Ce que ce document NE fait PAS

- Ne modifie pas `docs/PROMOTION-RULES.md`.
- Ne touche à aucun paramètre de `bot/config.py:WALLETS[*]["risque"]`.
- Ne s'applique à aucune autre stratégie (`xs_momentum_sp100`, `dual_momentum_multiclasse_etf`
  restent mensuelles, hors scope du recalibrage hebdomadaire par construction, cf. mission —
  "les mensuelles n'en ont pas besoin à ce rythme").
- N'élargit jamais automatiquement sa propre grille.
- Ne prend aucune décision de promotion/rétrogradation/mort — seulement un ajustement borné d'un
  paramètre de signal déjà en production, jamais l'entrée ou la sortie d'une stratégie d'un
  wallet.
