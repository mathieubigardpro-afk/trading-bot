# backtest/ — moteur commun de backtest (gouvernance de la recherche)

Moteur vectorisé (pandas/numpy), imposé par `docs/PROMOTION-RULES.md` §1.1 : **toute**
candidate qui prétend passer la Porte 1 (labo) doit être backtestée avec CE moteur, jamais un
script ad hoc parallèle non versionné (c'est l'absence de ce module qui constituait le finding
critique de l'audit du 27/07 — la seule instance auparavant était un script externe hors dépôt,
non ré-exécutable ni auditable).

> **AVERTISSEMENT — toute modification de ce moteur doit être re-auditée adversarialement
> avant de juger une nouvelle candidate.** `engine.py`/`metrics.py`/`risk_overlay.py` sont la
> RÈGLE DU JEU commune à toutes les décisions de promotion (§1.1-§1.3) : un changement
> silencieux ici (formule de coût, fenêtres walk-forward, sizing, DSR...) peut faire basculer
> une décision passée OU future sans que personne ne s'en rende compte si le changement n'est
> pas explicitement revu par un audit indépendant (même principe que §1.4 pour les candidates
> elles-mêmes). Le correctif du 2026-07-27 (`risk_overlay.py`, ce commit) n'a PAS reçu cet audit
> séparé — budget de la session insuffisant, dette explicite consignée en tête de
> `docs/RESEARCH-BACKLOG.md` plutôt que passée sous silence.

## Fichiers

| Fichier | Rôle |
|---|---|
| `data.py` | Chargement CSV.gz par ticker (branche orpheline `market-data`), série brute (signal) vs alignée sur calendrier canonique SPY (simulation portefeuille), sans backfill. |
| `engine.py` | `simulate_segment()` : simulation vectorisée long-only, signal décidé à la clôture `t`, exécuté à l'**open** de `t+1`, coûts bps/côté sur le turnover dollar réel. `generate_walk_forward_windows()`, `select_params_via_is()` (sélection IS-only), `concatenate_segments()` (équity OOS unique), `summarize_segment()` (bloc de métriques standard). |
| `risk_overlay.py` | Surcouche de risque appliquée par défaut par `simulate_segment()` (correctif audit 2026-07-27) : bande de non-négociation + vol targeting, alignées `bot/risk/manager.py`. Voir §"Surcouche de risque" ci-dessous. |
| `metrics.py` | Sharpe, Sortino, profit factor, max drawdown (pic-à-creux), CAGR, ratio d'information, exposition moyenne, Deflated Sharpe Ratio / PSR (Bailey & López de Prado 2014). |
| `strategies/xsmom.py` | Version backtest vectorisée de `bot/strategies/xs_momentum_sp100.py` (constantes SPEC importées, jamais dupliquées). |
| `run_xsmom_invvol.py` | Script d'exécution de bout en bout (exemple complet, cf. `backtest/results/xs_momentum_invvol_sp100/`). |
| `perp.py` | Extension short/perpétuels + funding (`PERP-EXTENSION-SPEC.md`) : chargeurs `load_perp_klines`/`load_funding`, alignement `align_funding_to_calendar` (+ `funding_alignment_report`), et `build_aligned_perp_matrices` (opens/highs/lows/closes/funding perp alignés sur un calendrier). Voir §"Extension perp" ci-dessous. |
| `run_funding_carry.py` | Orchestration `funding_carry_6majors` + option opt-in `--carry-across-windows` (`CARRY-EXTENSION-SPEC.md`). Voir §"Portage inter-fenêtres" ci-dessous. |
| `tests/` | Preuves pytest : anti-look-ahead, coûts proportionnels au turnover, bornes walk-forward, cas limites DSR, bande de non-négociation par défaut, vol targeting, extension perp (`test_perp.py`), portage inter-fenêtres (`test_carry.py`, `test_run_funding_carry_carry_option.py`). |

## Règles non négociables implémentées

1. **Aucun look-ahead** : `weights_decided.loc[t]` doit être calculable uniquement à partir de
   données de clôture `<= t` (responsabilité de la couche stratégie) — le moteur exécute
   TOUJOURS à `opens.iloc[i+1]`, jamais au prix ayant servi à la décision. Testé explicitement
   (`test_lookahead_cheat_collapses_...`).
2. **Long-only sur le spot** : un poids négatif sur une colonne spot lève `ValueError` (cohérent
   avec les 4 stratégies de production, toutes long/flat). Le short est autorisé UNIQUEMENT sur
   les colonnes explicitement déclarées via `perp_symbols` (extension short/perpétuels, cf.
   §"Extension perp" ci-dessous) — jamais un short spot simulé par ce moteur.
3. **Coûts systématiques sur turnover dollar réel** (`cost_bps`, points de base PAR CÔTÉ) —
   jamais de backtest "sans coûts" comme chiffre de décision (`docs/PROMOTION-RULES.md` §1.1).
4. **Walk-forward IS/OOS, sélection IS-only, métriques sur l'OOS concaténé** — jamais sur une
   fenêtre isolée ni sur la période complète non découpée (§1.1/§1.4).
5. **Surcouche de risque alignée production, activée par défaut** (voir section dédiée
   ci-dessous) — un moteur qui ignore le sizing réel de `bot/risk/` est structurellement PLUS
   généreux que ce qui se passerait en production.

## Surcouche de risque (`risk_overlay.py`, correctif audit 2026-07-27)

Avant ce correctif, `simulate_segment()` exécutait les poids décidés BRUTS. L'audit a mesuré
deux conséquences concrètes :

- **Bande de non-négociation à 0** : 16,3 fills exécutés par transition de signal *réellement
  voulue* (le moteur ré-exécutait un ordre à chaque micro-variation du poids recalculé) — environ
  0,30 point de Sharpe de pénalité artificielle en coûts de transaction qui ne se produirait
  jamais en production (`bot/risk/manager.py` absorbe ce bruit, étape 6 de son pipeline).
- **Absence de vol targeting** : le backtest simule un portefeuille structurellement plus gros
  (donc plus risqué) que celui réellement dimensionné en production — MaxDD sous-estimé d'un
  facteur ~2.

`simulate_segment()` applique désormais, **par défaut**, la même logique que
`bot/risk/manager.py` (vol targeting puis bande, dans cet ordre) :

```python
no_trade_band: float = risk_overlay.DEFAULT_NO_TRADE_BAND          # 0.05  (bot.config.NO_TRADE_BAND)
apply_vol_targeting: bool = True
vol_target_annualized: float = risk_overlay.DEFAULT_VOL_TARGET_ANNUALIZED   # 0.275
vol_ewma_halflife_days: float = risk_overlay.DEFAULT_VOL_EWMA_HALFLIFE_DAYS # 2.5j (60h de prod)
vol_coldstart_min_points: int = risk_overlay.DEFAULT_VOL_COLDSTART_MIN_POINTS  # 30
vol_coldstart_scalar: float = risk_overlay.DEFAULT_VOL_COLDSTART_SCALAR        # 0.5
```

`risk_overlay.py` **réutilise** `bot.risk.compute_vol_scalar` (jamais réimplémenté) — seule la
fréquence d'annualisation change (252 jours de bourse ici, contre 8760 heures en production ;
même demi-vie physique de 60h, ré-exprimée en 2,5 jours). Pour désactiver explicitement l'une
ou l'autre correction (ex. pour isoler une autre propriété du moteur en test, cf.
`backtest/tests/test_engine.py`) : `no_trade_band=0.0`, `apply_vol_targeting=False`.

**Écart connu, assumé, documenté** : cette surcouche ne réimplémente PAS `bot/risk/manager.py`
intégralement — pas de circuit breakers, pas de caps par actif, pas de bande par poche, pas de
cap d'exposition brute totale. Un backtest qui active un breaker de drawdown sévère en
production continuerait donc de trader normalement ici. Ligne de tête de
`docs/RESEARCH-BACKLOG.md` : re-audit demandé pour la prochaine session hebdomadaire.

## Convention temporelle interne

```
target_w[t]      = poids DÉCIDÉ à la clôture de calendar[t] (fourni par la stratégie)
poids_exécuté[t] = surcouche de risque(target_w[t-1], historique <= t-1)   (risk_overlay.py)
position[t]      = poids_exécuté[t] détenu pendant open[t] -> open[t+1]
ret_période[t]   = close[t] / open[t] - 1 (côté détention) ; exécution au prix open[t]
équity[t]        = cash[t] + shares[t] * close[t]
coût[t]          = |Δshares[t]| * open[t] * cost_bps / 10000
```

La toute dernière ligne du segment ne génère pas de rendement réalisé supplémentaire (pas de
`open[n+1]` disponible) — documenté, pas un bug.

## Exemple d'usage minimal

```python
import pandas as pd
from backtest import engine, metrics

calendar = pd.bdate_range("2015-01-05", periods=500)
opens = pd.DataFrame({"X": ...}, index=calendar)   # prix d'ouverture
closes = pd.DataFrame({"X": ...}, index=calendar)  # prix de clôture

# Poids DÉCIDÉS par la stratégie à la clôture de chaque jour (sans look-ahead : ne doit
# utiliser que closes.loc[:t]).
weights_decided = pd.DataFrame({"X": ...}, index=calendar)

seg = engine.simulate_segment(
    calendar, weights_decided, opens, closes,
    start_idx=1, end_idx=len(calendar) - 1,
    cost_bps=5.0,  # actions/ETF : 5 bps/côté ; crypto : cf. bot/config.py COST_TIER_*
    # no_trade_band / apply_vol_targeting / vol_target_annualized : défauts alignés production,
    # inutile de les préciser sauf pour désactiver explicitement une correction en test.
)

print(metrics.sharpe_ratio(seg.returns), metrics.max_drawdown(seg.equity))

# Walk-forward multi-fenêtres + DSR : cf. backtest/run_xsmom_invvol.py pour l'exemple complet
# (chargement data.py, generate_walk_forward_windows, concatenate_segments, deflated_sharpe_ratio
# avec K_total = lignes du registre + fenêtres × combinaisons de grille, docs/PROMOTION-RULES.md
# §1.3).
```

## Lancer les tests

```bash
cd trading-bot   # racine du dépôt
python3 -m pytest backtest/tests/ -v
```

Preuves couvertes : anti-look-ahead, coûts proportionnels au turnover réel, bornes des fenêtres
walk-forward (IS/OOS jamais chevauchantes), cas limites du DSR (K=1 ≈ PSR(0), K croissant ->
DSR décroissant), bande de non-négociation par défaut = 0,05 (micro-rebalance filtré,
rebalance réel exécuté), vol targeting qui réduit bien l'exposition moyenne et le MaxDD par
rapport à une exécution sans surcouche.

## Extension perp (short/perpétuels + funding)

Sémantique complète, pré-enregistrée AVANT implémentation : `backtest/PERP-EXTENSION-SPEC.md`
(cette spec fait foi ; toute divergence avec l'implémentation est un finding d'audit). Résumé :

- `backtest/perp.py` charge les klines perpétuelles (`_data/perp/<SYM>.csv.gz`) et le funding
  (`_data/funding/<SYM>.csv.gz`, timestamps ISO8601 mixtes + jitter — parsing documenté dans le
  module) et les aligne sur un calendrier commun (colonnes `<SYM>-PERP`, jamais confondues avec
  le spot `<SYM>`) via `build_aligned_perp_matrices`.
- `engine.simulate_segment()` accepte des paramètres optionnels (`perp_symbols`, `funding`,
  `highs`, `lows`, `perp_cost_bps`, `perp_initial_margin_frac`, `perp_maintenance_margin_frac`,
  `perp_liquidation_fee_bps`) : poids SIGNÉS sur les colonnes `perp_symbols` (short autorisé),
  marge jamais collatéralisée par le spot, liquidation intra-bougie au pire prix, funding réglé
  à la clôture de la bonne bougie. **Rétro-compatibilité bit-à-bit garantie** quand
  `perp_symbols` est `None` (ou vide) — testé explicitement (`np.array_equal` sur `equity`,
  `backtest/tests/test_perp.py`).
- `SegmentResult`/`ConcatenatedOosResult` gagnent `liquidations` (évènements journalisés) et
  `pnl_breakdown` (décomposition Spot/Perp/Funding/Coûts/Liquidation du PnL, spec §4) —
  `summarize_segment()` publie `n_liquidations` et `pnl_breakdown` en plus des métriques
  existantes.
- Cette extension ne change RIEN à `bot/sim/` (toujours long-only) ni à la production — voir
  spec §5 pour le périmètre exact et ce qui reste hors scope (pas de carnet d'ordres, pas d'ADL,
  pas de levier spot).

## Portage inter-fenêtres (position portée entre fenêtres OOS contiguës)

Sémantique complète, pré-enregistrée AVANT implémentation : `backtest/CARRY-EXTENSION-SPEC.md`
(cette spec fait foi ; toute divergence avec l'implémentation est un finding d'audit). Motivée
par le finding F1 (CRITIQUE, audit adversarial 2026-08-31) : la remise à plat de la position à
chaque fenêtre OOS, combinée à la bande de non-négociation et au vol targeting, empêche toute
entrée d'une candidate dont le poids scalé reste sous la bande — le moteur était PLUS sévère que
la production (où l'exécution est continue) pour les candidates à faible poids nominal. Résumé :

- `engine.simulate_segment(..., carry_in: Optional[CarryState] = None)` — `None` (défaut) :
  comportement STRICTEMENT identique (bit-à-bit) à avant cette extension, testé explicitement
  (`backtest/tests/test_carry.py`, hash capturés AVANT toute modification du moteur).
- `SegmentResult.carry_out: Optional[CarryState]` — TOUJOURS calculé par `simulate_segment`
  (même sans `carry_in`), `None` uniquement si aucune position n'est ouverte en fin de segment.
  `CarryState` porte `last_idx` (position calendaire de la dernière bougie, pour la vérification
  de contiguïté), `weights_at_last_close`/`last_close` (nécessaires à la reconstruction du
  segment suivant), et `open_lines` (métadonnées `CarryLine` par ligne encore ouverte : symbole,
  jambe spot/perp, PnL/coûts accumulés NORMALISÉS en fraction du capital de base de chaque
  fenêtre traversée, timestamp d'entrée d'origine, nombre de fenêtres déjà traversées).
- Reconstruction (début de segment, spec §3) : `shares_0 = w_last * initial_capital /
  last_close` (marquée à la CLÔTURE portée, pas à l'ouverture — la dérive clôture→ouverture de
  la première bougie est vécue par la position, comme en continu), `cash_0 = initial_capital -
  Σ_spot shares_0*last_close` (le perp ne déduit jamais son notionnel du cash). AUCUN coût sur
  cette reconstruction (ce n'est pas un ordre) — obtenu gratuitement en pré-amorçant `avg_cost`/
  `perp_ref`/`perp_pnl_accum` à l'état porté, la mécanique par bougie existante s'appliquant
  ensuite SANS AUCUNE modification. Contiguïté VÉRIFIÉE (jamais supposée) : `carry_in.last_idx +
  1 != start_idx` lève `ValueError` immédiatement. Prix `NaN` sur un symbole porté à poids non
  nul → `ValueError` (poids exactement `0.0` → simplement omis, sauf PnL accumulé substantiel
  associé à cette ligne — cf. correctif MINEUR ci-dessous). Symétriquement, le PACKAGING d'un
  segment (à la FIN, pas au début) lève aussi `ValueError` immédiatement si le `close` final d'un
  symbole ENCORE en position est `NaN` (correctif audit MAJEUR — avant ce correctif, un tel `NaN`
  empoisonnait silencieusement `CarryState`, l'erreur n'étant détectée qu'une fenêtre plus tard,
  au moment de la reconstruction SUIVANTE, sans pointer la bonne fenêtre).
- Correctif MINEUR (audit) : un poids porté retombé sous `1e-12` (`shares*last_close/equity`,
  alors que `shares` lui-même reste `> 1e-9` — cas limite atteignable avec un prix très faible ou
  une équity élevée) n'est plus simplement OMIS lors de la reconstruction : si la ligne
  correspondante portait un PnL/coût accumulé non nul, elle est CLOSE explicitement à la
  frontière (`trades_closed`/`realized_events`, avec ses champs additifs `carried_windows`/
  `entry_ts` habituels) plutôt que de laisser ce PnL disparaître sans jamais être comptabilisé.
- Anti-gaming (spec §4) : une ligne qui traverse une frontière de fenêtre reste UNE SEULE ligne
  économique — comptée dans `n_trades_closed()` qu'à sa VRAIE sortie, jamais gonflée par le
  découpage en fenêtres (démontré : une position traversant 3 fenêtres ⇒ exactement 1 trade
  clos, `backtest/tests/test_carry.py`). Son `pnl` = somme normalisée de ses contributions par
  fenêtre ; `carried_windows`/`entry_ts` sont des champs ADDITIFS sur l'entrée `trades_closed`
  correspondante, présents UNIQUEMENT si la ligne a réellement été portée au moins une fois.
- Conservation économique : sur un scénario synthétique contigu, la concaténation AVEC portage
  reproduit l'équity d'une simulation continue unique à ~1e-9 relatif près (aux renormalisations
  de capital par fenêtre près, spec §3 préambule) — écart documenté et testé.
- **Correctif audit F1 (CRITIQUE)** : la base de normalisation de `CarryLine.pnl_accum`/
  `cost_accum` (spec §2/§4.2) est désormais **l'équity de CLÔTURE du segment qui packages**
  (`equity.iloc[-1]`), et non plus son `initial_capital` (l'ancienne convention). Ces deux bases
  ne coïncident QUE si le segment rend exactement 0 % — presque jamais en pratique. Preuve : les
  `shares` reconstruites au segment suivant sont DÉJÀ implicitement mises à l'échelle par
  `initial_capital_suivant / equity_final_précédente` (via `weights_at_last_close`, lui-même une
  fraction de `equity_final`) ; normaliser le PnL porté par `initial_capital` appliquait un
  facteur d'échelle DIFFÉRENT à `shares` et au PnL porté au moment de la reconstruction, ce qui
  cassait l'invariant de coût de revient moyen implicite (`avg_cost_vrai = avg_cost_moteur -
  PnL_restant/actions`) DÈS qu'une ligne portée était renforcée PUIS réduite après
  reconstruction — le "télescopage" (`_frac = sold/old_shares` sur une réduction partielle) ne
  redevient exact que si les DEUX facteurs d'échelle coïncident. Démontré empiriquement (audit
  adversarial) : sur des scénarios de marche aléatoire de poids avec renforcement + réduction
  à cheval sur des frontières, l'ancienne convention rendait `profit_factor` SYSTÉMATIQUEMENT
  plus favorable en mode portage (≈2/3 des seeds testés, jamais l'inverse) ; avec la nouvelle
  base de normalisation, le signe de l'écart de `profit_factor` devient ÉQUILIBRÉ (ni
  systématiquement favorable ni défavorable) sur les mêmes scénarios — quantifié par
  `backtest/tests/test_carry.py::test_anti_gaming_profit_factor_matches_continuous_simulation_
  across_seeds` (spot) et son pendant perp. Un résidu NON NUL subsiste (borné, documenté,
  quantifié dans ce test) : la comptabilité "carnet de trades" par ligne PORTÉE additionne des
  contributions exprimées CHACUNE en fraction du capital de base DE SA PROPRE fenêtre (spec
  §4.2, approximation assumée dès la pré-inscription de la spec, cohérente avec la
  concaténation de RENDEMENTS plutôt que de dollars absolus) — la courbe d'**équity** (seule
  autorité économique de décision) n'est, elle, affectée d'aucune perte ni gain caché par le
  découpage en fenêtres, à ~1e-9 relatif près (point précédent).
- Zéro fuite IS→OOS : `carry_out` d'un segment ne dépend JAMAIS de données postérieures à sa
  borne, ni des lignes de `weights_decided` en dehors du segment simulé (testé par attaque de
  perturbation, `backtest/tests/test_carry.py`). La sélection IS de la fenêtre suivante continue
  de simuler depuis une position à PLAT (divergence assumée avec la production, où les positions
  persistent pendant un recalibrage — documentée spec §1.2 : porter l'état dans l'IS
  introduirait une dépendance de la SÉLECTION au chemin OOS, jugée plus dangereuse que l'écart).
- Analyse "bande par poche" (spec §5, imposée par la spec, pas un nouveau réglage) — **STATUT :
  NON RÉSOLU (audit F2, CRITIQUE), renvoyé au backlog pour analyse dédiée. Clause d'arrêt de la
  spec §5 appliquée : AUCUN changement de comportement de la bande n'a été fait.** L'équivalence
  poids wallet = `capital_alloc_pct` × poids poche / bande wallet = `0.05 * capital_alloc_pct`
  n'est prouvée QUE pour une poche à rendement ~NUL (court terme, prix constants + coût nul, seule
  condition sous laquelle l'équity de référence des DEUX runs reste exactement `initial_capital`
  tout du long — cf. `backtest/tests/test_carry.py::test_no_trade_band_pocket_wallet_homothety`,
  dont le docstring précise maintenant explicitement cette portée limitée). Dès que le
  COMPOUNDING entre en jeu (équity poche ≠ `initial_capital`, cas normal sur un horizon réel), la
  relation poche→wallet devient **AFFINE**, pas homothétique : `equity_wallet = 1 +
  capital_alloc_pct × (equity_poche − 1)` — PAS `equity_wallet = capital_alloc_pct ×
  equity_poche`. Les décisions hold/trade (bande comparée au poids COURANT, lui-même dérivé de
  l'équity) divergent alors dès que l'équity de poche s'écarte de 1, ce qui RÉFUTE l'équivalence
  générale revendiquée par une version antérieure de cette section. Réfutation démontrée par
  `backtest/tests/test_carry.py::test_no_trade_band_pocket_wallet_homothety_refuted_under_
  compounding` (garde contre une re-revendication future). Aucun correctif de la bande n'est
  fait ici — la spec §5 exige explicitement d'arrêter et de documenter le désaccord plutôt que
  d'improviser un correctif hors périmètre pré-enregistré.
- Orchestration (runners) : `backtest/run_funding_carry.py` expose `--carry-across-windows`
  (défaut **désactivé**) — enchaîne `carry_in(fenêtre k+1) = carry_out(fenêtre OOS k)` si la
  frontière est calendaire-adjacente, sinon démarre à plat + journalise l'évènement
  (`carry_boundary_events`). Le run NOMINAL (celui qui alimente
  `promotion_rules_1_2_thresholds_verdict`) reste TOUJOURS calculé à plat entre fenêtres, que
  l'option soit passée ou non ; quand elle est active, un bloc `carry_across_windows_informative`
  séparé et clairement étiqueté est ajouté à `results.json` (absent par défaut — format existant
  intouché). Les autres runners (`run_vol_breakout.py`, `run_xsmom_invvol.py`,
  `run_quasi_passif.py`) n'ont reçu AUCUNE modification par cette extension.
- **Avant tout usage par une candidate** : audit adversarial indépendant obligatoire
  (CARRY-EXTENSION-SPEC.md §6.5, même précédent que l'extension perp) — cette extension n'a,
  à ce stade, PAS encore reçu cet audit.

## Limites connues (assumées, pas des bugs)

- Long-only strict, pas de levier ni de vente à découvert.
- Pas de taux sans risque soustrait dans les ratios (argent fictif, aucun proxy documenté pour
  la poche actions de ce projet).
- La surcouche de risque (`risk_overlay.py`) ne couvre QUE bande + vol targeting (cf. section
  dédiée ci-dessus) — circuit breakers/caps/cap d'exposition brute totale non simulés.
- `data.py` ne connaît que des barres QUOTIDIENNES (actions/ETF) — pas encore d'équivalent
  horaire pour les stratégies crypto (`quasi_passif_crypto`), hors périmètre de ce correctif.
