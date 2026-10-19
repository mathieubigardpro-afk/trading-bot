# DRIFT-REPORT.md — Moniteur de dérive (backtest vs vécu)

*Généré automatiquement par `tools/weekly_maintenance.py` le 2026-10-19T08:31:10.691372+00:00. Ce document NE PREND AUCUNE DÉCISION — il signale. Les décisions de promotion, rétrogradation ou mort appartiennent exclusivement à une session de recherche hebdomadaire humaine, suivant `docs/PROMOTION-RULES.md`.*

## 1. Moniteur de dérive par stratégie

Compare les métriques VÉCUES (journaux `state/wallets/*/`) aux métriques OOS de référence (`docs/RESEARCH-REGISTRY.json`, elles-mêmes issues des `results.json` des backtests audités). Verdict classé selon les seuils chiffrés des RÈGLES DE MORT de `docs/PROMOTION-RULES.md` (§2.1/§2.2 pour les candidates en incubation, §3.1/§3.2 pour les stratégies actives).

| Stratégie | Wallet | Statut | Jours observés | Sharpe vécu | Sharpe attendu | DD vécu | DD attendu | Verdict |
|---|---|---|---|---|---|---|---|---|
| dual_momentum_etf | prudent | active | 89 | -0.18 | 0.65 | 2.5% | 27.6% | **SURVEILLER** |
| quasi_passif_crypto | prudent | active | 89 | 0.84 | 0.81 | 0.3% | 8.4% | **OK** |
| xs_momentum_sp100 | equilibre | active | 89 | 0.90 | 0.82 | 1.6% | 50.3% | **OK** |
| dual_momentum_etf | equilibre | active | 89 | 0.41 | 0.65 | 1.2% | 27.6% | **SURVEILLER** |
| quasi_passif_crypto | equilibre | active | 89 | 1.36 | 0.28 | 0.4% | 27.3% | **OK** |
| xs_momentum_sp100 | agressif | active | 89 | 0.56 | 0.82 | 1.9% | 50.3% | **OK** |
| quasi_passif_crypto | agressif | active | 89 | 0.87 | 0.07 | 2.2% | 56.4% | **OK** |

### Détail des raisons

- **dual_momentum_etf** (prudent, active) — **SURVEILLER**
  - Sharpe roulant 60j négatif depuis 7j (sous le seuil d'alerte 30j)
- **quasi_passif_crypto** (prudent, active) — **OK** _(antécédent hors §3, cf. PROMOTION-RULES.md §5 — informatif)_
  - aucun signal de dérive détecté
- **xs_momentum_sp100** (equilibre, active) — **OK** _(antécédent hors §3, cf. PROMOTION-RULES.md §5 — informatif)_
  - aucun signal de dérive détecté
- **dual_momentum_etf** (equilibre, active) — **SURVEILLER**
  - Sharpe roulant 60j négatif depuis 1j (sous le seuil d'alerte 30j)
- **quasi_passif_crypto** (equilibre, active) — **OK** _(antécédent hors §3, cf. PROMOTION-RULES.md §5 — informatif)_
  - aucun signal de dérive détecté
- **xs_momentum_sp100** (agressif, active) — **OK** _(antécédent hors §3, cf. PROMOTION-RULES.md §5 — informatif)_
  - aucun signal de dérive détecté
- **quasi_passif_crypto** (agressif, active) — **OK** _(antécédent hors §3, cf. PROMOTION-RULES.md §5 — informatif)_
  - aucun signal de dérive détecté

*Note : `xs_momentum_sp100`, `dual_momentum_multiclasse_etf` et `quasi_passif_crypto` sont un antécédent explicitement HORS du cadre formel §3 de `PROMOTION-RULES.md` (cf. §5) — leur verdict ci-dessus reste informatif (« si cette règle s'appliquait ») et ne déclenche aucune rétrogradation automatique.*

*Référence `quasi_passif_crypto` = retest walk-forward AUDITÉ du 2026-08-10 (`quasi_passif_crypto_wf_retest`, audit adversarial indépendant `isSound=true`) : Sharpe attendu 0.808 (prudent BTC+ETH) / 0.283 (équilibré 6 majors) / 0.069 (agressif 11 diversifié) ; DD attendu 8.4% / 27.3% / 56.4%. Les chiffres d'origine NON AUDITÉS du registre (Sharpe 1.24/1.47/1.49, MaxDD 8.0/16.4/33.4) sont explicitement discrédités par ce retest et ne servent PLUS de référence ci-dessus (backlog #15, session #6 — cf. `docs/RESEARCH-LOG.md` et `DRIFT_REFERENCE_OVERRIDES` dans `tools/weekly_maintenance.py`).*

## 2. Recalibrage encadré — quasi-passif crypto

Rafraîchissement des données de marché (`tools/fetch_data.py --only crypto`) : **OK**.

Grille pré-enregistrée (`docs/RECALIBRATION-SPEC.md`) : `REGIME_SMA_DAYS ∈ [150, 175, 200, 225, 250]` (seuil de changement : amélioration OOS relative > 10%).

- Fenêtres walk-forward (9m IS / 3m OOS) : **16**
- Valeur en production : `REGIME_SMA_DAYS = 200` (Sharpe OOS concaténé : 0.599)
- Meilleure valeur de la grille : `REGIME_SMA_DAYS = 200` (Sharpe OOS concaténé : 0.599)
- Valeur la plus souvent sélectionnée en IS (informatif) : `175`
- Amélioration relative : n/d
- **Décision : aucun changement**
  - la valeur actuellement en production reste optimale sur la grille — aucun changement
