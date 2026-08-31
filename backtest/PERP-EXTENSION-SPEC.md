# PERP-EXTENSION-SPEC.md — Extension short/perpétuels du moteur commun (pré-enregistrée)

*Session hebdomadaire #5, 2026-08-31. Ce document fixe la SÉMANTIQUE de l'extension du moteur
commun (`backtest/engine.py`) aux positions short sur perpétuels + règlement périodique du
funding, AVANT son implémentation et AVANT tout backtest qui l'utiliserait (backlog P0#1,
funding carry — `docs/PROMOTION-RULES.md` §1.1 exige le moteur commun, jamais un script
parallèle ; le mandat de session exige un audit adversarial de l'extension AVANT toute
utilisation). Toute déviation entre cette spec et l'implémentation est un finding d'audit.*

## 0. Principes (hérités de `docs/ARCHITECTURE.md` §0.2 — pessimisme systématique)

- **Rétro-compatibilité bit-à-bit** : sans perp (`perp_symbols=None`), `simulate_segment()` doit
  produire EXACTEMENT les mêmes résultats qu'avant (les 3 `results.json` existants et les tests
  actuels ne bougent pas d'un bit). Les tests existants restent verts sans modification.
- **Toute ambiguïté de modélisation est tranchée en défaveur du bot** : liquidation évaluée au
  pire prix intra-bougie, coûts par côté sur les DEUX jambes, aucune compensation entre jambe
  spot et marge perp (le spot ne sert JAMAIS de collatéral), frais de liquidation punitifs.
- **Aucun look-ahead** : le funding réglé à l'heure H n'est connu et encaissé qu'à la clôture de
  la bougie qui se termine à H ; la décision de poids reste à la clôture t, exécution à l'open
  t+1 (inchangé).

## 1. Données (`backtest/perp.py`, nouveau)

- Sources (branche `market-data`, régénérée 2026-08-24) : `data/perp/<SYM>.csv.gz` (klines 1h
  USDT-M, colonnes `timestamp,open,high,low,close,volume`, timestamp = OUVERTURE de la bougie)
  et `data/funding/<SYM>.csv.gz` (colonnes `timestamp,funding_rate[,funding_interval_hours]`,
  timestamp = instant de règlement, **formats ISO8601 mixtes** — parfois avec millisecondes,
  parfois avec un jitter de ±2 ms autour de l'heure ronde : parser avec `format="ISO8601"` et
  ARRONDIR à l'heure la plus proche (`round("h")`), jamais `floor`).
- Convention Binance : `funding_rate > 0` ⇒ les longs PAIENT les shorts ; le montant réglé à H
  est `position_notional_at_H × rate`. Les règlements suivent l'intervalle réel présent dans les
  données (8h en général, 4h/2h sur certains épisodes — chaque ligne = un règlement, on ne
  ré-échantillonne jamais).
- Alignement sur le calendrier horaire (index = ouverture de la bougie) : un règlement à
  l'heure H est affecté à la bougie d'index `H − 1h` (celle qui SE CLÔT à H) — il est encaissé
  à la clôture de cette bougie, sur la position détenue pendant cette bougie. Une bougie sans
  règlement porte 0.0 (jamais NaN dans la matrice alignée). Un règlement dont l'heure `H − 1h`
  est absente du calendrier est **perdu s'il est favorable et compté s'il est défavorable**
  (affecté à la bougie précédente disponible uniquement si le montant est négatif pour le bot)
  — pessimisme. Le loader documente le nombre de règlements ainsi traités.
- Les colonnes perp sont nommées `<SYM>-PERP` dans toutes les matrices (`opens`, `closes`,
  `highs`, `lows`, `weights_decided`, `funding`) pour ne jamais être confondues avec le spot
  `<SYM>`.

## 2. Nouveaux paramètres de `simulate_segment()` (tous optionnels)

| Paramètre | Défaut | Rôle |
|---|---|---|
| `perp_symbols` | `None` | Ensemble des colonnes de `weights_decided` traitées comme perpétuels (poids signés autorisés). `None` ⇒ comportement historique strict. |
| `funding` | `None` | DataFrame aligné sur `calendar` (mêmes index/colonnes perp), taux réglé à la clôture de la bougie. Obligatoire si `perp_symbols` non vide. |
| `highs`, `lows` | `None` | Matrices high/low alignées (obligatoires si perp : test de liquidation intra-bougie). |
| `perp_cost_bps` | `= cost_bps` | Coût par côté sur le turnover dollar de la jambe perp. |
| `perp_initial_margin_frac` | `0.50` | Marge initiale exigée = fraction du notionnel perp (0.50 = levier 2 max). |
| `perp_maintenance_margin_frac` | `0.025` | Marge de maintenance (2,5 % du notionnel — palier pessimiste, au-dessus des paliers réels majors de Binance). |
| `perp_liquidation_fee_bps` | `100` | Frais de liquidation en plus de `perp_cost_bps` (1 % du notionnel — pessimiste). |

Les poids d'un symbole NON perp restent contraints ≥ 0 (long-only) : un poids négatif sur une
colonne spot lève `ValueError` (pas de short spot simulé).

## 3. Comptabilité (par bougie `i`, dans cet ordre)

1. **Surcouche de risque** (inchangée) : vol targeting puis bande de non-négociation, sur le
   vecteur de poids. Pour le vol targeting, la vol portefeuille est estimée sur les poids en
   **valeur absolue** (`|w|`) — surestime la vol d'une paire couverte (spot long / perp short),
   donc DÉ-RISQUE davantage : choix pessimiste explicite, documenté.
2. **Exécution à l'open** (inchangée pour le spot). Perp : `target_shares = w × equity / open`
   (signé). Avant exécution, contrainte de faisabilité : `spot_dollars_cible +
   perp_initial_margin_frac × |notionnel_perp_cible| ≤ equity_before_trade × (1 + 1e-9)`.
   Violation ⇒ `ValueError` (la stratégie DOIT dimensionner correctement ; jamais de clipping
   silencieux qui masquerait un sizing infaisable).
3. **Coûts** : `cost_bps` sur le turnover dollar spot, `perp_cost_bps` sur le turnover dollar
   perp, déduits du cash.
4. **Liquidation intra-bougie (perp)** : prix adverse `worst = high` pour un short, `low` pour un
   long. Perte latente au pire : `L = perp_shares × (worst − ref)` (`ref` = prix de la dernière
   mise au marché : open d'exécution ou close précédent). Si
   `cash + L < perp_maintenance_margin_frac × |perp_shares| × worst` ⇒ la jambe perp est
   **liquidée à `worst`** : cash += `L − |perp_shares × worst| × (perp_cost_bps +
   perp_liquidation_fee_bps)/1e4`, `perp_shares = 0`, évènement journalisé
   (`liquidations` dans le résultat), ligne comptée close. Le spot n'est PAS touché (la
   stratégie re-décidera à la bougie suivante). Le cash de marge est le SEUL collatéral : la
   valeur du spot n'entre jamais dans ce test.
5. **Mise au marché à la clôture** : `cash += perp_shares × (close − ref)` ; `ref = close`
   (variation margin réglée chaque bougie, comme un future).
6. **Funding à la clôture** : `cash += − perp_shares × close × funding[i]` (short reçoit si
   rate > 0, paie si rate < 0 ; long l'inverse).
7. **Équity** = `cash + Σ shares_spot × close` (le perp est intégralement réglé en cash à
   chaque bougie). **Exposition brute** = `(Σ spot_dollars + Σ |notionnel perp|) / equity`.

## 4. Trades et PnL réalisé (profit factor, `n_trades_closed`)

- Une **ligne perp** s'ouvre quand `perp_shares` passe de 0 à ≠ 0 et se ferme quand il revient
  à 0 (un changement de signe = fermeture + ouverture). Son PnL réalisé = Σ variation margin
  + Σ funding − coûts (− frais de liquidation le cas échéant), accumulé pendant la vie de la
  ligne ; réduction partielle ⇒ réalisation proportionnelle (même convention que le spot).
- Une **ligne spot** garde exactement la convention existante du moteur.
- Le profit factor et `n_trades_closed` agrègent lignes spot ET perp — pas de « trade
  synthétique » spot+perp qui masquerait la contribution de chaque jambe. Le rapport de
  backtest DOIT en plus publier le PnL par jambe (spot / perp / funding / coûts).

## 5. Ce que l'extension ne fait PAS (hors périmètre, documenté)

- Pas de short spot, pas d'emprunt, pas de levier sur le spot.
- Pas de modélisation du carnet ni d'ADL (auto-deleveraging) ; la liquidation est instantanée
  au pire prix de la bougie — approximation pessimiste assumée.
- Pas de production : `bot/sim/` reste long-only. Une candidate perp qui passerait la Porte 1
  ne peut PAS être incubée tant que `bot/sim/` n'a pas été étendu et audité à son tour
  (cf. SPEC du backtest funding carry, sémantique des issues).

## 6. Amendements post-audit adversarial (2026-08-31, AVANT toute utilisation par une candidate)

L'audit adversarial indépendant de l'implémentation (copie isolée, verdict initial
`isSound: false`, 1 CRITIQUE + 3 MAJEURS démontrés par exécution) a conduit aux amendements
suivants, tous adoptés AVANT qu'un seul backtest de candidate n'utilise l'extension — ils ne
sont donc pas des retouches post-résultat :

- **§3 (nouveau point 0, CRITIQUE)** : un prix perp (open/close/high/low) ou un funding **NaN**
  à une bougie où le symbole est EN POSITION ou a un POIDS CIBLE non nul ⇒ `ValueError`
  (jamais de `fillna(0.0)` sur une colonne perp engagée : démontré sur le trou réel de 69 h de
  SOL-PERP en février 2022 — un short y encaissait +28,9 % de gain fictif à prix 0, et la
  liquidation devenait impossible car `NaN < x` est faux). Un symbole flat sans poids cible
  traverse un trou sans effet. Conséquence pour les candidates : le calendrier/l'univers doit
  éviter les trous connus (SOL-PERP : 72 h dès 2022-02-26 et 48 h dès 2022-04-01 ; aucun trou
  sur BTC/ETH/DOGE/LINK/AVAX-PERP sur 2022-01→2026-07).
- **§2** : `opens`/`closes` doivent contenir les colonnes perp (vérification bruyante, même
  niveau que `funding`/`highs`/`lows`).
- **§3.4 (MAJEUR)** : le funding réglé à la clôture de la bougie entre dans le test de marge
  s'il est **payable** par le bot (`min(0, −shares × worst × rate)`) — jamais s'il est
  favorable (un crédit à venir n'est pas un collatéral).
- **§3.4 (MAJEUR)** : la perte de liquidation est **plafonnée au cash de marge disponible**
  (prix de faillite — au-delà, le bot a perdu tout son cash, jamais plus ; `bankrupt=True`
  journalisé). Une équity ≤ 0 (**ruine**) fige le compte à 0 pour le reste du segment
  (rendements suivants nuls, ruine journalisée dans `liquidations` avec `side="ruin"`) — une
  équity négative inversait le signe économique des rendements dans `summarize_segment`.
- Orphelins de funding (§1) : l'implémentation exclut tout règlement sans bougie, quel que
  soit son signe (le signe de la position est inconnu au chargement) ; mesuré sur les 6
  majors : exactement 1 orphelin par symbole (bord de série, `rate > 0`, donc légèrement
  DÉFAVORABLE à un short) — écart à la lettre de la spec, impact nul, documenté.
- Performance mesurée par l'audit : ~66 s pour 10 000 bougies × 12 colonnes (6 spot + 6
  perp) ; un walk-forward 14 fenêtres × grille 4 sur 2022→2026 reste exécutable (~45 min).
