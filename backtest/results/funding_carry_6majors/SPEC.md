# SPEC pré-enregistrée — `funding_carry_6majors` (backlog P0#1)

*Session hebdomadaire #5, 2026-08-31. Committée AVANT toute exécution du backtest
(pré-enregistrement, `docs/PROMOTION-RULES.md` §0) : univers, signal, grille, coûts, fenêtres,
seuils, sémantique des issues — tout est figé ici avant d'avoir vu le moindre résultat OOS. Le
moteur utilisé est le moteur commun étendu (`backtest/engine.py` + `backtest/perp.py`,
`backtest/PERP-EXTENSION-SPEC.md` §0-§6), audité adversarialement AVANT cette spec (verdict
initial `isSound: false`, 1 CRITIQUE + 3 MAJEURS corrigés, contre-audit — cf. RESEARCH-LOG
2026-08-31). Toute déviation constatée entre cette spec et l'implémentation est un finding
d'audit, pas une liberté d'implémentation.*

## Hypothèse (backlog P0#1)

Le funding rate des perpétuels crypto est structurellement positif en moyenne (déséquilibre de
demande de levier long) et persistant. Une position **delta-neutre** long spot / short perp de
même notionnel encaisse le funding versé par les longs quand il est positif, sans exposition
directionnelle (hors basis spot/perp et coûts). Edge « structurel », pas un signal technique —
identifié comme la stratégie la plus robuste de la littérature crypto quant par la recherche
initiale du projet, écartée alors pour une contrainte de plateforme aujourd'hui levée.

## Univers, données, calendrier, coûts

- Univers : `bot.config.SYMBOLS_CRYPTO` = BTC, ETH, SOL, DOGE, LINK, AVAX (6 majors V1 — même
  univers et même benchmark que `vol_breakout_6majors`/`donchian`/`ema`, convention du
  registre). Chaque symbole a deux colonnes : spot `<SYM>` (`_data/crypto`) et perp
  `<SYM>-PERP` (`_data/perp`), funding `_data/funding` (branche `market-data` du 2026-08-24).
- Calendrier : union des timestamps horaires spot des 6 symboles, **restreint à
  2022-04-03 00:00 UTC → 2026-07-31 23:00 UTC**. Raison (fixée AVANT exécution, indépendante de
  tout résultat) : SOL-PERP a deux trous de données bruts (72 h dès 2022-02-26, 48 h dès
  2022-04-01) et le moteur audité refuse tout prix perp manquant sur une position engagée
  (PERP-EXTENSION-SPEC §6, correctif CRITIQUE). Aucun autre trou sur les 6 perps 2022-04→2026-07.
  Alignement perp/funding : `backtest/perp.py:build_aligned_perp_matrices` (ffill borné 3 h,
  jamais de backfill ; orphelins de funding journalisés).
- Coûts : **25 bps/côté sur la jambe spot ET 25 bps/côté sur la jambe perp** (palier « mids »
  le plus défavorable de l'univers pour le spot, précédent `vol_breakout_6majors` ; sur le
  perp, le taker Binance USDT-M réel est ~5 bps + slippage — 25 bps est volontairement
  PESSIMISTE, ambiguïté tranchée en défaveur du bot). Stress §1.4 : 3× et 5× sur les deux jambes.
- Marge/liquidation : défauts de l'extension (marge initiale 50 % = levier 2 max, maintenance
  2,5 %, frais de liquidation 100 bps), jamais assouplis.

## Signal (par symbole, à la clôture horaire t — uniquement des règlements ≤ t)

- `carry_ann(t)` = Σ des funding rates réglés dans la fenêtre `(t − D jours, t]` × `365 / D`
  (annualisation par la durée réelle de la fenêtre — robuste aux épisodes d'intervalle 4h/2h,
  aucun ré-échantillonnage). Fenêtre `D` = paramètre de grille.
- Entrée (état « actif ») si `carry_ann(t) > θ_in` ; sortie (état « flat ») si
  `carry_ann(t) < θ_out = θ_in / 2` (hystérésis fixée à la moitié, non optimisée) ; entre les
  deux, l'état précédent est conservé. Jamais de NaN : flat tant que la fenêtre `D` n'est pas
  entièrement disponible (warm-up) ou que le funding/perp du symbole est indisponible.
- Poids décidés quand actif : spot `+w`, perp `−w` avec **`w = 0.10`** par symbole (fixé,
  non optimisé). Justification chiffrée : contrainte de faisabilité du moteur
  `Σ spot + 0,5 × Σ|perp| ≤ équity` ⇒ 6 × 0,10 × 1,5 = 0,90 ≤ 1 (marge de 10 % pour les
  dérives intra-rebalance). Poids 0 sur les deux jambes quand flat.
- Surcouche de risque du moteur : défauts de production (vol targeting ON sur `|w|` — même
  formule que `bot/risk/vol_targeting.py`, borne de corrélation 1, qui traite la paire couverte
  comme deux paris — bande de non-négociation 0,05), constantes HORAIRES
  (`HOURLY_VOL_EWMA_HALFLIFE_PERIODS`, `HOURLY_VOL_PERIODS_PER_YEAR`). Choix pessimiste et
  production-fidèle (une future poche perp serait dimensionnée par le même `RiskManager`).
  Sharpe et profit factor sont insensibles à ce scalaire (invariance d'échelle) ; CAGR ne l'est
  pas — documenter l'exposition brute moyenne réalisée.

## Grille pré-enregistrée (4 combinaisons — AUCUNE autre valeur ne sera testée)

- `D ∈ {7, 30}` jours (fenêtres de moyenne courtes usuelles : une semaine, un mois).
- `θ_in ∈ {0.05, 0.10}` (5 %/an et 10 %/an — seuils « le carry doit couvrir plusieurs fois les
  coûts d'un aller-retour à 100 bps » ; 8 %/an ≈ funding « neutre » Binance 0,01 %/8h × 3 × 365 =
  10,95 %/an : θ = 0,10 ≈ exiger un funding au-dessus du taux neutre).
- Sélection IS par Sharpe via `engine.select_params_via_is` (jamais l'OOS), `sim_kwargs`
  identiques à l'OOS (perp inclus).

## Walk-forward et moteur

- 9 mois IS / 3 mois OOS, pas 3 mois (§1.1, convention crypto horaire). Sur le calendrier
  ci-dessus, le générateur produit **14 fenêtres** (vérifié avant exécution : OOS de 2023-01-03 à
  2026-07-02), soit 14 × 4 = 56 essais internes.
- `K_total = 12 (lignes du registre au 2026-08-31, `tools/verify_research.py --compute`) + 56 =
  68` (§1.3).
- Métriques : équity OOS concaténée, `periods_per_year = 8760`.

## Benchmark et seuils (Porte 1 §1.2 — tous obligatoires, aucun compensable)

- Benchmark : buy & hold équipondéré spot des 6 majors, mêmes fenêtres OOS alignées, sans coûts
  ni overlay (convention du registre). Note pré-enregistrée : pour une stratégie delta-neutre,
  le benchmark ne sert qu'au critère de MaxDD relatif et à l'information ; le Sharpe est jugé sur
  son seuil absolu.
- Sharpe OOS net ≥ 0,70 ; PF OOS > 1,15 ; **trades OOS clos ≥ 80 comptés sur les seules lignes
  PERP closes** (chaque position de carry = 1 ligne perp ; compter spot + perp doublerait
  artificiellement le nombre — option la plus stricte retenue) ; MaxDD OOS ≤ 1,5× MaxDD OOS
  benchmark aligné ; DSR ≥ 0,50 avec K_total = 68.
- Audit adversarial indépendant obligatoire (§1.4) : `isSound: false` = rejet quel que soit le
  chiffre.

## Analyses d'honnêteté obligatoires dans le rapport

- PnL par jambe (spot / variation perp / funding / coûts / liquidations) — le funding net doit
  expliquer l'essentiel du PnL, sinon l'hypothèse n'est pas celle qui est testée ;
- Nombre de liquidations et de faillites (toute liquidation est un signal d'alarme sur le
  sizing, même si le résultat agrégé passe) ;
- Sous-périodes 2022-2023 (bear, funding souvent négatif) vs 2024-2026 (dégradation ou
  concentration temporelle de l'edge) ;
- Part des heures « actives » et nombre d'épisodes distincts d'activation par symbole ;
- Stress de coûts 3×/5× sur les deux jambes ; sensibilité au levier (informatif : marge
  initiale 1,0 = levier 1) ;
- Corrélation des rendements OOS avec le buy & hold équipondéré (attendue ≈ 0 pour une
  stratégie delta-neutre — une corrélation élevée signalerait une jambe non couverte).

## Sémantique des issues (fixée AVANT exécution)

- **Échec d'un seul seuil §1.2 ou `isSound: false`** ⇒ statut `ecartee` (Sharpe positif mais
  seuils manqués) ou `rejetee` (perdante nette / Sharpe ≤ 0) dans `RESEARCH-REGISTRY.json`,
  K_total documenté, aucune incubation, pas de variante retestée sans raison structurelle neuve
  (§3.3).
- **Porte 1 intégralement passée** ⇒ la candidate NE PEUT PAS être incubée dans cette session :
  `bot/sim/` (simulateur de production) est long-only, sans short ni funding, et son extension
  exige son propre audit adversarial (PERP-EXTENSION-SPEC §5). Statut pré-enregistré :
  `validee_porte1_en_attente_infra` (nouveau statut ajouté à `_meta.statuts_possibles`,
  sémantique : Porte 1 passée, entrée en labo bloquée par l'infrastructure de production ; le
  compteur des 56 jours ne démarre qu'à l'entrée effective dans `INCUBATING_STRATEGIES`). Tout
  re-run ultérieur (ex. après extension de `bot/sim/`) compte comme une nouvelle ligne dans
  K_total. Aucune modification des wallets réels, du framework de risque ni de
  `PROMOTION-RULES.md` (§4.3, §0).
