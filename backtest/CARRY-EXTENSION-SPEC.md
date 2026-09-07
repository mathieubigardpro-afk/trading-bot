# CARRY-EXTENSION-SPEC.md — Portage de la position entre fenêtres OOS contiguës + bande de non-négociation « par poche »

*SPEC PRÉ-ENREGISTRÉE (session hebdomadaire #6, 2026-09-07), committée AVANT toute
implémentation, conformément à `docs/PROMOTION-RULES.md` §0 et au backlog #16 (inscrit par la
session #5 APRÈS le verdict de `funding_carry_6majors`, jamais pendant — l'amendement n'est
appliqué à aucun verdict passé). Motivation : finding F1 (CRITIQUE) de l'audit adversarial du
2026-08-31 — la remise à zéro de la position à chaque fenêtre OOS, combinée à la bande de
non-négociation et au vol targeting, empêche toute entrée en position d'une candidate dont le
poids scalé reste sous la bande, alors qu'en production l'exécution est CONTINUE. Le moteur est
aujourd'hui PLUS SÉVÈRE que la production pour les candidates à faible poids nominal.*

*Cette spec prime sur toute docstring en cas de divergence. Tout correctif issu de l'audit
adversarial sera consigné en §7 AVANT tout backtest de candidate utilisant l'extension.*

---

## 1. Principes non négociables

1. **Rétro-compatibilité bit-à-bit par défaut.** L'extension est intégralement optionnelle :
   sans opt-in explicite de l'appelant, le moteur produit des résultats STRICTEMENT identiques
   (bit-à-bit) à l'actuel. Preuves exigées : (a) reproduction bit-exacte d'au moins un
   `results.json` existant (`vol_breakout_6majors`, précédent déjà utilisé par la spec perp) ;
   (b) test HEAD~1 vs HEAD sur données synthétiques ET réelles, extension désactivée.
2. **Jamais de fuite IS→OOS.** La position portée dans la fenêtre OOS k+1 provient EXCLUSIVEMENT
   de l'état de fin de fenêtre OOS k (position réellement détenue à la dernière bougie OOS de k).
   Aucun état issu d'un segment IS (sélection de grille comprise) n'est jamais porté. La
   sélection IS de la fenêtre k+1 continue de simuler depuis une position à plat (les paramètres
   sont jugés sur l'IS fenêtré, comme aujourd'hui) — divergence assumée et documentée : en
   production les positions persistent pendant un recalibrage, mais porter l'état dans l'IS
   introduirait une dépendance de la SÉLECTION au chemin OOS, plus dangereuse que l'écart.
3. **Contiguïté vérifiée, jamais supposée.** Le portage n'a lieu que si la première bougie OOS de
   la fenêtre k+1 est EXACTEMENT la bougie calendrier suivante de la dernière bougie OOS de la
   fenêtre k (`oos_start_idx[k+1] == oos_end_idx[k] + 1` sur le MÊME calendrier). Sinon :
   démarrage à plat + évènement journalisé dans le résultat (jamais silencieux).
4. **Pessimisme conservé.** Aucune règle de l'extension ne peut rendre un chiffre de décision
   plus favorable par artefact comptable : le comptage des trades clos ne peut jamais être
   GONFLÉ par le découpage en fenêtres (cf. §4 — anti-gaming du seuil « ≥ 80 trades OOS »).
5. **Hors périmètre.** `bot/risk/`, `bot/config.py:WALLETS`, circuit breakers : intouchés (§4.3
   des règles). L'extension ne modifie ni la surcouche vol targeting ni l'ordre des étapes de
   `simulate_segment`.

## 2. API

- `simulate_segment(..., carry_in: Optional[CarryState] = None)` — défaut `None` = comportement
  historique bit-identique.
- `SegmentResult.carry_out: Optional[CarryState]` — TOUJOURS renseigné par `simulate_segment`
  (même sans `carry_in`) décrivant l'état de fin de segment ; `None` uniquement si le segment
  n'a aucune position ouverte en fin de fenêtre. Ne modifie aucun champ existant.
- `CarryState` (dataclass) contient au minimum :
  - `last_idx` : position (index entier) de la dernière bougie du segment dans le calendrier,
    pour la vérification de contiguïté §1.3 ;
  - `weights_at_last_close` : poids par symbole RÉELLEMENT détenus, marqués à la dernière
    clôture (`shares * close / equity`), SIGNÉS pour les colonnes perp ;
  - `last_close` : prix de clôture par symbole porté (nécessaire à la reconstruction §3) ;
  - `open_lines` : métadonnées des lignes ouvertes portées (cf. §4) : symbole, jambe
    (`spot`/`perp`), identifiant de ligne stable, PnL accumulé NORMALISÉ (fraction du capital
    de base de chaque fenêtre où il a été accru, sommé — cohérent avec la concaténation des
    rendements), coûts accumulés (même unité), timestamp d'entrée d'origine.
- Orchestration (runners `backtest/run_*.py`) : quand l'option (nommée explicitement, ex.
  `--carry-across-windows`) est activée, le runner enchaîne
  `carry_in(fenêtre k+1) = carry_out(fenêtre OOS k)` si contiguïté §1.3, sinon `None` + journal.
  Le walk-forward IS/OOS, la grille, les coûts, les benchmarks : inchangés.

## 3. Sémantique de reconstruction (début de fenêtre k+1)

But : répliquer l'exécution continue de production, modulo la renormalisation du capital à
`initial_capital` par fenêtre (nécessaire à la concaténation des rendements, inchangée).

1. À l'entrée du segment, si `carry_in` est fourni et la contiguïté vérifiée :
   `shares_0[s] = weights_at_last_close[s] * initial_capital / last_close[s]` pour chaque
   symbole porté (signé pour perp), `cash_0 = initial_capital - Σ_spot shares_0*last_close`
   (pour le perp : aucune valeur détenue, marge en cash — le notionnel porté n'est PAS déduit du
   cash, cohérent avec la comptabilité perp existante ; la contrainte de faisabilité de marge
   §3.2 de PERP-EXTENSION-SPEC est re-testée à la PREMIÈRE exécution comme pour tout ordre).
   Ainsi l'équity de départ vaut exactement `initial_capital` et la dérive clôture→ouverture de
   la première bougie est vécue par la position portée, comme en continu.
2. **Aucun coût d'entrée** sur la position reconstruite (c'est la MÊME position, pas un ordre).
   Les coûts ne s'appliquent qu'aux ordres réellement émis ensuite (`trade_shares != 0`).
3. La première décision exécutée du segment (`weights_decided.iloc[oos_start_idx-1]`) passe par
   la surcouche normale (vol targeting puis bande) : la bande compare désormais le poids cible
   au poids PORTÉ (plus jamais à zéro) — c'est tout l'objet de l'extension.
4. Prix manquant à la reconstruction : `NaN` sur un symbole porté ⇒ `ValueError` (aligné sur la
   politique perp F1 du 2026-08-31 : jamais un prix 0 silencieux). Un symbole porté à poids
   exactement 0 est simplement omis.
5. Perp : le funding, la mise au marché et le test de liquidation s'appliquent dès la première
   bougie du segment à la position reconstruite (aucune « immunité » de première bougie).

## 4. Comptage des trades et PnL (anti-gaming)

1. Une position qui traverse une frontière de fenêtre reste UNE SEULE ligne économique :
   elle n'est comptée dans `n_trades_closed` qu'à sa VRAIE sortie (poids ramené sous le seuil de
   clôture de ligne existant, ou liquidation), dans la fenêtre où cette sortie a lieu. Le
   découpage en fenêtres ne crée JAMAIS de trade clos supplémentaire (§1.4). En fin de DERNIÈRE
   fenêtre, une ligne encore ouverte reste non close (comportement actuel inchangé).
2. Le PnL de la ligne = somme de ses composantes accumulées fenêtre par fenêtre, chacune en
   fraction du capital de base de sa fenêtre (approximation cohérente avec la concaténation des
   rendements ; documentée dans le README du moteur).
3. `realized_events`/`trades_closed` gardent leur schéma ; les lignes portées ajoutent des champs
   additifs (`carried_windows: int`, `entry_ts` d'origine) sans retirer aucun champ existant.
4. Les métriques de rendement (`returns`, Sharpe, MaxDD, exposition) restent calculées par
   bougie comme aujourd'hui — l'extension ne les touche que via la trajectoire des positions.

## 5. Bande de non-négociation « par poche » — analyse imposée, pas de nouveau réglage

Constat à VÉRIFIER par un test dédié (et à documenter dans le README) : les poids du moteur sont
exprimés en fraction du capital de la candidate (= la poche), et la bande de production est
`NO_TRADE_BAND × capital_alloc_pct` appliquée à des poids exprimés en fraction du wallet
(`bot/risk/manager.py` étape 6, `no_trade_band_by_symbol`). La transformation poche→wallet étant
l'homothétie de rapport `capital_alloc_pct`, la bande de 0,05 du moteur appliquée aux poids de
poche est ÉQUIVALENTE à la bande de production — il n'y a PAS de facteur d'échelle manquant.
Exigence : test d'homothétie (poids wallet = alloc × poids poche, bande wallet = 0,05 × alloc ⇒
mêmes décisions hold/trade que poids poche avec bande 0,05, pour plusieurs valeurs d'alloc).
Si le test RÉFUTE l'équivalence, l'implémentation s'arrête et le désaccord est documenté pour
la session suivante — jamais de correctif improvisé hors spec.

Observation HORS PÉRIMÈTRE consignée (à instruire au backlog, jamais dans cette extension) : la
production exécute TOUJOURS un flatten (cible 0) même sous la bande (`manager.py` étape 6) ;
le moteur, lui, retient un flatten sous la bande. Écart de fidélité distinct de #16, non traité
ici pour ne pas élargir le périmètre pré-enregistré.

## 6. Preuves exigées avant tout usage par une candidate

1. Rétro-compat bit-à-bit extension désactivée (§1.1a et §1.1b).
2. Zéro fuite IS→OOS : test démontrant que modifier les données IS d'une fenêtre ne change pas
   la position portée entre les OOS (à sélection de paramètres égale), et attaque par
   perturbation des données FUTURES (la position portée à t ne dépend d'aucune bougie > t).
3. Conservation économique : sur un scénario synthétique contigu, la concaténation avec portage
   reproduit (aux renormalisations de capital près) l'équity d'une simulation continue unique de
   la même période — écart borné et documenté.
4. Anti-gaming §4.1 : scénario synthétique où une position traverse 3 fenêtres ⇒ exactement
   1 trade clos.
5. Audit adversarial indépendant (session isolée, remote neutralisé) avec verdict `isSound`
   explicite, AVANT toute Porte 1 utilisant l'extension (PROMOTION-RULES §1.4 par analogie,
   précédent : PERP-EXTENSION-SPEC).
6. Re-runs INFORMATIFS (aucun verdict passé ne change, §3.3 : un re-run décisionnel = nouvel id)
   d'au moins `funding_carry_6majors` avec portage activé, pour quantifier l'artefact F1 ;
   `vol_breakout_6majors` et `quasi_passif_crypto_wf_retest` si le budget de calcul le permet.

## 7. Amendements issus de l'audit (append-only, AVANT tout backtest de candidate)

*(vide à la pré-inscription)*
