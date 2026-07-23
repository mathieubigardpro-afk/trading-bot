# DRIFT-REPORT.md — Moniteur de dérive (backtest vs vécu)

*Généré automatiquement par `tools/weekly_maintenance.py` le 2026-07-23T21:35:25.442149+00:00. Ce document NE PREND AUCUNE DÉCISION — il signale. Les décisions de promotion, rétrogradation ou mort appartiennent exclusivement à une session de recherche hebdomadaire humaine, suivant `docs/PROMOTION-RULES.md`.*

## 1. Moniteur de dérive par stratégie

Compare les métriques VÉCUES (journaux `state/wallets/*/`) aux métriques OOS de référence (`docs/RESEARCH-REGISTRY.json`, elles-mêmes issues des `results.json` des backtests audités). Verdict classé selon les seuils chiffrés des RÈGLES DE MORT de `docs/PROMOTION-RULES.md` (§2.1/§2.2 pour les candidates en incubation, §3.1/§3.2 pour les stratégies actives).

| Stratégie | Wallet | Statut | Jours observés | Sharpe vécu | Sharpe attendu | DD vécu | DD attendu | Verdict |
|---|---|---|---|---|---|---|---|---|
| dual_momentum_etf | prudent | active | 1 | n/d | n/d | 0.0% | n/d | **SURVEILLER** |
| quasi_passif_crypto | prudent | active | 1 | n/d | 1.24 | 0.0% | 8.0% | **SURVEILLER** |
| xs_momentum_sp100 | equilibre | active | 1 | n/d | 0.82 | 0.0% | 50.3% | **SURVEILLER** |
| dual_momentum_etf | equilibre | active | 1 | n/d | n/d | 0.0% | n/d | **SURVEILLER** |
| quasi_passif_crypto | equilibre | active | 1 | n/d | 1.47 | 0.0% | 16.4% | **SURVEILLER** |
| xs_momentum_sp100 | agressif | active | 1 | n/d | 0.82 | 0.0% | 50.3% | **SURVEILLER** |
| quasi_passif_crypto | agressif | active | 1 | n/d | 1.49 | 0.0% | 33.4% | **SURVEILLER** |

### Détail des raisons

- **dual_momentum_etf** (prudent, active) — **SURVEILLER**
  - historique vécu 1j < 28j (§2.1) — trop tôt pour un diagnostic fiable
- **quasi_passif_crypto** (prudent, active) — **SURVEILLER** _(antécédent hors §3, cf. PROMOTION-RULES.md §5 — informatif)_
  - historique vécu 1j < 28j (§2.1) — trop tôt pour un diagnostic fiable
- **xs_momentum_sp100** (equilibre, active) — **SURVEILLER** _(antécédent hors §3, cf. PROMOTION-RULES.md §5 — informatif)_
  - historique vécu 1j < 28j (§2.1) — trop tôt pour un diagnostic fiable
- **dual_momentum_etf** (equilibre, active) — **SURVEILLER**
  - historique vécu 1j < 28j (§2.1) — trop tôt pour un diagnostic fiable
- **quasi_passif_crypto** (equilibre, active) — **SURVEILLER** _(antécédent hors §3, cf. PROMOTION-RULES.md §5 — informatif)_
  - historique vécu 1j < 28j (§2.1) — trop tôt pour un diagnostic fiable
- **xs_momentum_sp100** (agressif, active) — **SURVEILLER** _(antécédent hors §3, cf. PROMOTION-RULES.md §5 — informatif)_
  - historique vécu 1j < 28j (§2.1) — trop tôt pour un diagnostic fiable
- **quasi_passif_crypto** (agressif, active) — **SURVEILLER** _(antécédent hors §3, cf. PROMOTION-RULES.md §5 — informatif)_
  - historique vécu 1j < 28j (§2.1) — trop tôt pour un diagnostic fiable

*Note : `xs_momentum_sp100`, `dual_momentum_multiclasse_etf` et `quasi_passif_crypto` sont un antécédent explicitement HORS du cadre formel §3 de `PROMOTION-RULES.md` (cf. §5) — leur verdict ci-dessus reste informatif (« si cette règle s'appliquait ») et ne déclenche aucune rétrogradation automatique.*

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
