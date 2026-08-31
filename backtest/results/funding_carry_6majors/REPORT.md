# REPORT — `funding_carry_6majors` (backlog P0#1) — ÉCHEC PORTE 1, REJETÉE

*Session hebdomadaire #5, 2026-08-31. Backtest exécuté APRÈS le commit de `SPEC.md` (e581289),
sur le moteur commun étendu short/perp (`backtest/PERP-EXTENSION-SPEC.md`, audité
adversarialement : verdict initial `isSound: false`, 1 CRITIQUE + 3 MAJEURS corrigés, contre-audit
`isSound: true`). Chiffres complets : `results.json` ; log d'exécution : `run.log` (durée 3 590 s).*

## Verdict §1.2 (seuil par seuil, aucun compensable)

| Critère | Seuil | Mesuré | Verdict |
|---|---|---|---|
| Sharpe OOS net (√8760) | ≥ 0,70 | **−0,050** | ÉCHEC |
| Profit factor OOS | > 1,15 | **0,964** | ÉCHEC |
| Trades OOS clos (lignes PERP, option stricte de la SPEC) | ≥ 80 | **14** (14 spot + 14 perp = 28 lignes) | ÉCHEC |
| MaxDD OOS relatif | ≤ 1,5× benchmark | 0,0095× (0,68 % vs 71,98 %) | passe (trivial pour une stratégie delta-neutre) |
| DSR (K_total = 68) | ≥ 0,50 | **0,0065** | ÉCHEC |

**4 seuils sur 5 manqués, marges très larges. Sharpe ≤ 0 ⇒ statut `rejetee`** (sémantique
pré-enregistrée dans `SPEC.md`). Audit adversarial indépendant §1.4 : cf. `RESEARCH-LOG.md`
2026-08-31 (mené séparément après ce rapport).

## Protocole exécuté (conforme à la SPEC)

- Calendrier 2022-04-03 00:00 → 2026-07-31 23:00 UTC (37 943 h), 0 NaN spot/perp après alignement.
- 14 fenêtres 9m IS / 3m OOS / pas 3m (OOS 2023-01-03 → 2026-07-02, 30 647 h concaténées).
- Grille D ∈ {7, 30} × θ_in ∈ {0,05, 0,10}, sélection IS par Sharpe ; D = 30 choisi 12/14 fois.
- Coûts 25 bps/côté sur les deux jambes ; overlay production (vol targeting sur |w|, bande 0,05) ;
  marge 50 %, maintenance 2,5 %, frais de liquidation 100 bps.
- K_total = 12 lignes du registre + 14 × 4 = 68.

## Résultats OOS concaténés

| | Sharpe | Sortino | PF | MaxDD | CAGR | Lignes closes spot/perp | Expo. brute moy. |
|---|---|---|---|---|---|---|---|
| Candidate | −0,050 | −0,066 | 0,964 | 0,68 % | −0,02 %/an | 14 / 14 | 21,5 % |
| Benchmark B&H spot équipondéré | 0,703 | 0,970 | — | 71,98 % | +27,9 %/an | — | 100 % |

Sharpe OOS par fenêtre : de −2,64 à +1,94 (2 fenêtres sans aucun trade).

## Décomposition du PnL (analyse d'honnêteté centrale)

| Composante (OOS, 3,5 ans) | % du capital |
|---|---|
| Spot (prix) | −5,32 % |
| Perp (variation margin) | +5,38 % |
| **Funding net encaissé** | **+2,74 %** |
| Coûts spot | −1,44 % |
| Coûts perp | −1,44 % |
| Liquidations | 0,00 % |
| **Total** | **−0,08 %** |

Résidu de couverture spot + perp = +0,06 % : la delta-neutralité fonctionne (corrélation des
rendements OOS avec le B&H : −0,011). **Le funding collecté (+2,74 %) est intégralement absorbé
par les coûts (−2,88 %)** au dimensionnement pessimiste imposé (25 bps/côté × 2 jambes, overlay
qui ramène l'exposition brute moyenne à 21,5 %). L'hypothèse (funding positif en moyenne) est
directionnellement vraie mais sa magnitude, nette des coûts du projet, est nulle.

## Autres analyses d'honnêteté

- **Liquidations / faillites / ruine : 0** sur tout l'OOS.
- **Sous-périodes** : Sharpe +0,47 avant 2024 (≈1 an), **−0,44 depuis 2024** (≈2,5 ans) —
  instabilité temporelle, edge non persistant.
- **Stress de coûts (deux jambes)** : Sharpe −2,08 / PF 0,85 à 3× ; −2,70 / 0,75 à 5×.
- **Levier (informatif)** : marge initiale 1,0 faisable sur tout l'historique (jamais 6
  symboles actifs simultanément) ⇒ métriques identiques ; à w = 0,08 : Sharpe 0,34 (< 0,70).
- **Activation** : 48 % des heures actives en moyenne (LINK 67 %, SOL 33 %), 77 épisodes
  d'activation comptés (sur-compte aux frontières de fenêtres — documenté ; le chiffre de
  décision reste les 14 lignes perp closes du moteur).
- **Orphelins de funding** : 277/symbole, tous dus à la restriction du calendrier au 2022-04-03
  (≈ 92 jours × 3 règlements) — aucun trou réel sur la fenêtre utilisée.

## Ambiguïtés tranchées par l'implémentation (documentées, aucune retouche post-OOS)

Fenêtre glissante temporelle `rolling("<D>D", closed="right")` (robuste au trou DST du
2023-03-24) ; warm-up forcé flat tant que t < début + D jours ; « indisponible » étendu au spot
(sans effet, calendrier sans trou) ; reset de l'hystérésis sur indisponibilité (sans effet) ;
highs/lows fournis pour toutes les colonnes. Un bug de logique dans le test INFORMATIF de levier
du runner (repli inversé) a été corrigé après le run et le test relancé seul — aucun paramètre
de décision n'a été touché après avoir vu l'OOS.

## Conséquence

Statut `rejetee` dans `RESEARCH-REGISTRY.json` (entrée n°13), K_total = 68 documenté. Aucune
incubation. Ne pas retester de variante (seuil, fenêtre, univers plus large) sans raison
structurellement neuve — §3.3, compterait dans K_total. Ce qui changerait STRUCTURELLEMENT la
donne : des coûts perp réalistes (5-10 bps/côté) plutôt que 25 bps pessimistes — mais le seuil de
coûts du projet est une règle, pas un paramètre de recherche.
