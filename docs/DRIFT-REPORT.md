# DRIFT-REPORT.md — Moniteur de dérive (backtest vs vécu)

*Généré automatiquement par `tools/weekly_maintenance.py` le 2026-09-07T12:04:17.582311+00:00. Ce document NE PREND AUCUNE DÉCISION — il signale. Les décisions de promotion, rétrogradation ou mort appartiennent exclusivement à une session de recherche hebdomadaire humaine, suivant `docs/PROMOTION-RULES.md`.*

## 1. Moniteur de dérive par stratégie

Compare les métriques VÉCUES (journaux `state/wallets/*/`) aux métriques OOS de référence (`docs/RESEARCH-REGISTRY.json`, elles-mêmes issues des `results.json` des backtests audités). Verdict classé selon les seuils chiffrés des RÈGLES DE MORT de `docs/PROMOTION-RULES.md` (§2.1/§2.2 pour les candidates en incubation, §3.1/§3.2 pour les stratégies actives).

| Stratégie | Wallet | Statut | Jours observés | Sharpe vécu | Sharpe attendu | DD vécu | DD attendu | Verdict |
|---|---|---|---|---|---|---|---|---|
| dual_momentum_etf | prudent | active | 47 | 0.54 | n/d | 1.5% | n/d | **SURVEILLER** |
| quasi_passif_crypto | prudent | active | 47 | -0.30 | 0.81 | 0.1% | 8.4% | **SURVEILLER** |
| xs_momentum_sp100 | equilibre | active | 47 | 4.45 | 0.82 | 0.3% | 50.3% | **SURVEILLER** |
| dual_momentum_etf | equilibre | active | 47 | 3.51 | n/d | 0.3% | n/d | **SURVEILLER** |
| quasi_passif_crypto | equilibre | active | 47 | 4.99 | 0.28 | 0.1% | 27.3% | **SURVEILLER** |
| xs_momentum_sp100 | agressif | active | 47 | 1.18 | 0.82 | 1.7% | 50.3% | **SURVEILLER** |
| quasi_passif_crypto | agressif | active | 47 | 1.53 | 0.07 | 1.2% | 56.4% | **SURVEILLER** |

### Détail des raisons

- **dual_momentum_etf** (prudent, active) — **SURVEILLER**
  - DD de référence indisponible dans le registre — comparaison impossible
  - Sharpe roulant 60j non calculable (historique < 60j)
- **quasi_passif_crypto** (prudent, active) — **SURVEILLER** _(antécédent hors §3, cf. PROMOTION-RULES.md §5 — informatif)_
  - Sharpe roulant 60j non calculable (historique < 60j)
- **xs_momentum_sp100** (equilibre, active) — **SURVEILLER** _(antécédent hors §3, cf. PROMOTION-RULES.md §5 — informatif)_
  - Sharpe roulant 60j non calculable (historique < 60j)
- **dual_momentum_etf** (equilibre, active) — **SURVEILLER**
  - DD de référence indisponible dans le registre — comparaison impossible
  - Sharpe roulant 60j non calculable (historique < 60j)
- **quasi_passif_crypto** (equilibre, active) — **SURVEILLER** _(antécédent hors §3, cf. PROMOTION-RULES.md §5 — informatif)_
  - Sharpe roulant 60j non calculable (historique < 60j)
- **xs_momentum_sp100** (agressif, active) — **SURVEILLER** _(antécédent hors §3, cf. PROMOTION-RULES.md §5 — informatif)_
  - Sharpe roulant 60j non calculable (historique < 60j)
- **quasi_passif_crypto** (agressif, active) — **SURVEILLER** _(antécédent hors §3, cf. PROMOTION-RULES.md §5 — informatif)_
  - Sharpe roulant 60j non calculable (historique < 60j)

*Note : `xs_momentum_sp100`, `dual_momentum_multiclasse_etf` et `quasi_passif_crypto` sont un antécédent explicitement HORS du cadre formel §3 de `PROMOTION-RULES.md` (cf. §5) — leur verdict ci-dessus reste informatif (« si cette règle s'appliquait ») et ne déclenche aucune rétrogradation automatique.*

*Référence `quasi_passif_crypto` = retest walk-forward AUDITÉ du 2026-08-10 (`quasi_passif_crypto_wf_retest`, audit adversarial indépendant `isSound=true`) : Sharpe attendu 0.808 (prudent BTC+ETH) / 0.283 (équilibré 6 majors) / 0.069 (agressif 11 diversifié) ; DD attendu 8.4% / 27.3% / 56.4%. Les chiffres d'origine NON AUDITÉS du registre (Sharpe 1.24/1.47/1.49, MaxDD 8.0/16.4/33.4) sont explicitement discrédités par ce retest et ne servent PLUS de référence ci-dessus (backlog #15, session #6 — cf. `docs/RESEARCH-LOG.md` et `DRIFT_REFERENCE_OVERRIDES` dans `tools/weekly_maintenance.py`).*

## 2. Recalibrage encadré — quasi-passif crypto

Rafraîchissement des données de marché (`tools/fetch_data.py --only crypto`) : **OK**.

Grille pré-enregistrée (`docs/RECALIBRATION-SPEC.md`) : `REGIME_SMA_DAYS ∈ [150, 175, 200, 225, 250]` (seuil de changement : amélioration OOS relative > 10%).

- Fenêtres walk-forward (9m IS / 3m OOS) : **15**
- Valeur en production : `REGIME_SMA_DAYS = 200` (Sharpe OOS concaténé : 0.508)
- Meilleure valeur de la grille : `REGIME_SMA_DAYS = 175` (Sharpe OOS concaténé : 0.523)
- Valeur la plus souvent sélectionnée en IS (informatif) : `175`
- Amélioration relative : 3.0%
- **Décision : aucun changement**
  - amélioration OOS relative 3.0% <= seuil 10% — pas assez significatif, aucun changement
