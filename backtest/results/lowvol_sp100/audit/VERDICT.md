# VERDICT — audit adversarial indépendant (PROMOTION-RULES §1.4) de `lowvol_sp100`, Porte 1

*Auditeur indépendant (n'a participé ni à la SPEC ni à l'implémentation). Périmètre : copie isolée `/home/claude/audit-copy` (`git remote -v` vide, aucun push), données `/home/claude/mdata/data`, zéro appel réseau. Moteur, candidate, code de production : **non modifiés** (`git diff HEAD` vide sur `backtest/engine.py`, `metrics.py`, `risk_overlay.py`, `data.py`, `bot/`). Tout ce qui suit est reproductible par les scripts de ce dossier (`logs/*.log` = sorties). Les expériences « contrefactuelles » (E1/E2) sont **locales, hors protocole, non décisionnelles** : elles mesurent l'ampleur d'un artefact, jamais un critère de sauvetage.*

## 0. Verdict

# `isSound: false`

**Le code du signal est sain et l'arithmétique est reproductible à l'identique ; c'est la MESURE qui est invalide.** Le backtest de Porte 1 n'évalue pas la stratégie pré-enregistrée (« bottom-N équipondéré, rebalance mensuel ») mais un mécanisme différent, non déclaré : *acheter 1/N à l'égalité exacte de la bande, ne revendre un titre sorti du top-N que si son poids dépasse 5 %, et empiler ainsi 44 titres en moyenne (max 62) à 1,25× l'equity (max 1,80×) avec cash négatif 98 % des jours*. Deux des cinq seuils §1.2 (profit factor, nombre de trades) sont des artefacts de ce mécanisme, et le contrôle apparié obligatoire (SPEC §2/§5) n'est pas évaluable. Par la sémantique SPEC §5, `isSound: false` ⇒ rejet automatique.

La cause n'est **pas un bug de `lowvol.py`** : c'est une incohérence du **design pré-enregistré** (bande plate 0,05 par titre × poids 1/N, N ≥ 20) qui est **identique en production** (preuve par exécution du vrai `RiskManager`, §3.3). Le seul volet propre au **moteur** est l'absence de contrainte `cash ≥ 0` sur le spot (que le `Ledger` de production impose).

## 1. Findings

### CRITIQUE

**C1 — Le chiffre de décision ne mesure pas la stratégie déclarée (A3 confirmé et aggravé par exécution).**
Réplication indépendante (sans `engine`/`lowvol`/`metrics`) : Sharpe 0,8127136874221914 (candidat 0,8127136874221905), MaxDD 0,518799, PF 11,12447880195, 371 trades clos, expo brute moy 1,2476 / max 1,7980, 44,41 titres moy / max 62 : **égalité bit-à-bit**. Mécanique démontrée (`a3_mechanics`, `logs/a3_mechanics.log`) :
- 801 ordres : 430 entrées + 371 sorties complètes, **zéro** réduction/renfort. Les 430 entrées ont une cible **exactement égale** à 0,05 (égalité stricte avec la bande, condition `|Δ| < 0,05` fausse) ; les **371/371 sorties** ont lieu avec un poids courant ≥ 0,05 (min 0,0500, médiane 0,0511, max 0,0812). Un titre sorti du top-20 dont le poids est tombé sous 5 % n'est **jamais** vendu : règle effective = « vendre les gagnants, garder les perdants » (effet de disposition mécanique).
- Poids des **orphelins** (titres détenus à cible nulle) : **56,7 % de l'equity en moyenne** (max 90,8 %). Seule la jambe « cibles » (≈ 0,68 de l'equity) est la stratégie déclarée.
- Cash/equity : moyenne −0,248, **minimum −0,798, négatif 98,0 % des jours** : levier implicite non financé, sans coût.
- **PF 11,12 / 307 gagnants vs 64 perdants** : artefact de non-réalisation. Sous la mécanique voulue (E1, rebalance mensuel réel, sorties exécutées) : **PF 2,71, 1 003 trades clos**. Le critère « trades ≥ 80 » (371) et « PF > 1,15 » (11,12 ; 141,1 en S2) sont donc passés pour une mauvaise raison.
- Variante financée (E2 : contrainte `cash ≥ 0` comme le `Ledger`, ventes puis achats alphabétiques) : Sharpe 0,796, MaxDD 45,5 %, expo 0,90, 52 titres en moyenne (max 70), **1 120 achats bloqués faute de cash** — la rotation de portefeuille est gelée dès que le cash est épuisé.

**C2 — Contrôle apparié inerte ⇒ critère de domination (SPEC §5) non évaluable ; défaut du design pré-enregistré, pas de l'implémentation.**
Le contrôle (univers éligible équipondéré) n'émet **0 ordre sur les 30 fenêtres** (éligibles aux dates OOS : min 73, médiane 95, max 103 ⇒ poids ≤ 0,0137 < 0,05) ; equity constante 1,0, 100 % cash, Sharpe/Sortino NaN. La SPEC prescrit simultanément `no_trade_band = 0,05` par titre (§2) et un contrôle ~1/100 (§2) : les deux ne sont pas compatibles, et l'inévaluabilité de la branche « non dominée par le contrôle » (§5) était prévisible avant exécution. **Un critère non évaluable n'est pas un critère passé** : la branche d'incubation « 5/5 + isSound + non dominée » est inatteignable par construction. (Le REPORT a correctement refusé de conclure `dominated = False`.)

### MAJEUR

**M1 — Dépendance à une égalité flottante, et grille effective réduite de moitié.**
`1/20 == 0.05` en flottant ⇒ entrée exécutée ; poids × (1 − 1e-12) ⇒ **0 ordre, Sharpe NaN** (`logs/a5_dsr_misc.log`). N = 30 (1/30 = 0,033) est structurellement inentrable : 60 couples (fenêtre, combo) sur 120 ont un Sharpe IS NaN. La grille pré-enregistrée de 4 combinaisons n'en contient que **2 actives** (63,20) et (252,20), dont les Sharpe OOS pleine période à combo fixe sont quasi identiques (0,829 / 0,835) : la sélection IS est non informative (K_total = 166 compte 4 combos, donc conservateur côté DSR, mais le « walk-forward à 4 combos » du protocole n'existe pas).

**M2 — Fidélité du sizing (SIZING-FIDELITY §3.4) : S1 est plus généreux que la production sur le cash, la production reproduit le reste.**
- *Interdit en production* : `Ledger.apply_fill` lève `ValueError` sur un achat sans cash (`bot/sim/ledger.py` l.55-59, invariant cash ≥ 0 l.37 ; exécuté dans `a3c_production`). Le cash négatif (jusqu'à −80 % de l'equity de poche) de S1 n'est pas réalisable dans une poche autonome.
- *Reproduit à l'identique en production* (vrai `RiskManager.apply`, bande `0,05·a`, `a = 0,30`) : entrée depuis plat à `0,05·a` exécutée ; cible `(1/30)·a` bloquée ; contrôle `(1/100)·a` bloqué ; orphelins à 0,0145 / 0,0120 / 0,0149 conservés, 0,0151 / 0,0300 vendus. Les commentaires de `bot/runner.py` (`_no_trade_band_scale_by_symbol`) le confirment : « une position sortie du top-k reste soumise à la bande de SA poche ».
- *Conséquence en wallet* (S2, `a6_s2_cb`) : le cash n'est jamais négatif (min 43 % de l'equity), l'accumulation est financée par le cash **non alloué** du wallet : poids de la poche en début de fenêtre **0,32 à 0,52 (médiane 0,42) pour `a = 0,30`**, expo brute wallet 0,43 (max 0,55) = 1,42× (max 1,85×) l'allocation. `capital_alloc_pct` ne borne donc plus le risque de la poche ; `RiskManager` n'a aucun plafond par poche (cap brut wallet 1,0 trivial, SIZING §3.6). Le « levier non financé » de S1 est donc, en production, un **dépassement d'allocation** — pas une impossibilité, mais pas non plus la poche à 30 % que la SPEC dimensionne.

**M3 — Biais du survivant documenté mais non quantifié, et l'avantage low-vol n'est pas démontré face à l'équipondéré.**
Le contrôle équipondéré des constituants actuels (E1, mécanique voulue) fait CAGR 17,1 % / Sharpe 0,92 contre SPY 10,4 % / 0,61 : signature de survivorship de ~+6,7 pts/an et +0,31 de Sharpe, du même ordre que l'avance de la candidate sur SPY (+0,22). Sous la mécanique voulue, la candidate (Sharpe 0,83, Sortino 1,19, MaxDD 37,7 %, CAGR 11,2 %) est **battue par l'équipondéré sur Sharpe, Sortino et CAGR** (IR candidate vs contrôle −0,58 ; corrélation 0,84) et ne gagne que sur MaxDD (50,5 %). Elle n'est donc pas « dominée » au sens strict à 3 critères de la SPEC §5, mais l'anomalie low-vol n'est pas établie sur cet univers.

**M4 — Annexe `band_fidelity` : lecture du wallet pour le bloc `cb` mal identifiée (BAND §3.7).**
Les chiffres S2 sont **reproduits** (Sharpe 0,7703 ; MaxDD 0,5741 ; PF 141,13 ; 716 trades ; DSR 0,9339 ; D2 = 619 barres / 1 059 cellules ; ordres 801 / 1 468). Mais la lecture du wallet pertinente est mal étiquetée (cf. §6 point 6).

### MINEUR

- **m1 — Reconversion `P = 1 + (E−1)/a` non valide sous portage** : elle suppose un poids de poche = `a` en début de fenêtre ; avec portage il vaut 0,32-0,52. Effet : S2 en unités de poche Sharpe 0,770 (SPEC) vs 0,812 (`r_E/a` quotidien) vs 0,827 (diviseur = poids réel) ; MaxDD 57,4 % vs 57,0 % vs 45,4 %. Aucun seuil ne bascule, `band_fragile = false` tient sous toutes les variantes, mais le ΔSharpe S2−S1 = −0,042 rapporté est en grande partie un artefact du diviseur (wallet S2 brut : 0,8117 ≈ S1 0,8127).
- **m2 — A1 (BK absent de `UNIVERSE_SP100`, univers effectif 103 ≠ 102 de la SPEC)** : vérifié (103/103 fichiers présents, aucun `BK`), documenté, sans effet.
- **m3 — Sensibilité aux coûts non informative en S1** : le turnover réalisé (6,1 %/mois) est moitié du turnover signal (13,4 %) parce que la bande retient les sorties ; Sharpe 0,813 → 0,808 à 25 bps est un artefact. Sous la mécanique voulue : Sharpe 0,843 (0 bps) → 0,830 (5) → 0,777 (25) → **0,710 (50 bps)**, PF 2,76 → 2,29 : réelle mais encore ≥ 0,70 à 5× .
- **m4 — Implémentation non committée** (`lowvol.py`, `run_lowvol_sp100.py`, `test_lowvol.py`, `results/` sont `??` dans git) : « une seule exécution, aucune retouche » est invérifiable par l'historique git. Aucun indice contraire (grille = SPEC à la lettre, `PARAM_GRID` figé, aucun argument CLI de recherche, `runtime_seconds` unique).
- **m5 — Hygiène DSR** : la convention projet (écart-type d'estimation du Sharpe comme proxy de la dispersion inter-essais de B&LdP) est appliquée telle quelle ; non contestée ici car conservatrice quand la dispersion inter-essais est plus faible (c'est le cas : 2 combos actifs à 0,829/0,835).
- **m6 — Qualité de données** (sans effet sur la sélection) : rendements exactement nuls fréquents (2-10 %, USB 21 %) avant la décimalisation ; sauts > 40 % sur AIG (6), MCD (2), NFLX (2)… (splits/ajustements) concernant des titres non sélectionnés ; vol 63 j des titres choisis plausible (médiane 16,5 %, min 6,4 %). Titres les plus sélectionnés : PG, JNJ, PEP, SO, KO (profil défensif attendu).

## 2. Point 1 — Look-ahead : **aucun** (`a1_lookahead`, `logs/a1_lookahead.log`)

Attaque par perturbation : 18 dates de décision tirées aléatoirement (≥ 1995) ; toutes les clôtures **strictement postérieures** à D sont (a) multipliées par un bruit log-normal σ = 0,5, (b) ×10, (c) mises à NaN ; poids recalculés avec le code de la candidate (4 combos + contrôle) **et** avec ma réimplémentation. **378 comparaisons, 0 écart : lignes ≤ D égales bit-à-bit.** Contrôle de puissance : perturber la clôture de D change la ligne D dans 18/18 cas ; perturber la seule clôture D+1 laisse les lignes ≤ D inchangées dans 18/18 cas. Les poids de la candidate et de ma réimplémentation sont identiques bit-à-bit sur toute la série. Le moteur exécute `weights_decided.iloc[i-1]` à l'open `i` (reproduit par mon simulateur à 1e-15). Réserve non-temporelle : constituants actuels (survivorship, M3), qui est un biais de sélection de l'univers, pas un look-ahead de signal.

## 3. Point 3 — A3 par exécution et fidélité production

3.1 *Mécanique exacte* : entrée depuis plat = exécutée uniquement à l'égalité `|w_cible − 0| = 0,05` ; sortie = exécutée uniquement si le poids **courant** ≥ 0,05 (poids dérivé des prix, relatif à l'equity y compris cash négatif). Pas de réduction partielle, pas de renforcement (la bande les absorbe). 601/801 ordres au lendemain d'une décision, 200 hors (franchissement de bande par dérive).
3.2 *Exposition brute / titres / cash* : voir C1 (1,2476 ; max 1,7980 ; 91,9 % des jours > 1,05 ; 44,4 / 62 titres ; cash min −0,798).
3.3 *La production peut-elle produire ça ?* — **Le comportement « jamais vendu sous la bande » : oui, identique** (RiskManager réel exécuté, §M2 ; BAND-FIDELITY §1.3). **Le cash négatif : non** (Ledger). **L'exposition 1,25-1,80× la poche : oui en wallet**, financée par le cash non alloué (M2), au prix d'un dépassement de `capital_alloc_pct` non plafonné. Conclusion vis-à-vis de §1.4 : S1 n'est pas « plus généreux que la production » sur la bande, mais **l'est sur le cash** (pas de contrainte de financement de poche) ; et surtout le chiffre de décision mesure un portefeuille qui n'est ni la stratégie SPEC ni une poche à 30 %.

## 4. Point 4 — Contrôle apparié

Vérifié : 100 % cash, 0 ordre, equity ≡ 1 sur 30 fenêtres (C2). SPEC §5 : la branche « non dominée » est **inévaluable**, pas « passée ». Même en production le contrôle ne pourrait pas être incubé (`(1/100)·a` bloqué). Sous la mécanique voulue (E1, mon simulateur, rebalance mensuel réel, sans bande), le contrôle est évaluable et bat la candidate sur Sharpe/Sortino/CAGR (M3).

## 5. Point 5 — DSR (from scratch, B&LdP 2014)

n = 7 549, Sharpe quotidien 0,051196, skew −0,0926 (non biaisé), kurtosis excess 11,3728, SE = 0,011588, SR0(K=166) = 0,031334, z = 1,714, **DSR = 0,9567** (identique au candidat). K_total = 16 + 30×4 + 30×1 = 166 (16 lignes de registre vérifiées ; 30 fenêtres recomptées indépendamment, OOS 1996-01-29 → 2026-01-28, contiguës, 9 mois de données finales non utilisés car fenêtres complètes seulement). Sensibilité : K=216 → 0,948. La méthode est correctement appliquée ; la valeur est « peu discriminante » à n≈7 500 (la SPEC le disait) et porte sur un Sharpe qui n'est pas celui de la stratégie déclarée (C1).

## 6. Point 6 — Annexe `band_fidelity`

- *S2 reproduit* indépendamment (mon simulateur, poids 0,3·W, bande 0,015, mêmes combos, portage) : Sharpe 0,7703 ; MaxDD 0,5741 ; PF 141,13 ; 716 trades clos ; DSR 0,9339 ; 543 barres avec ordre ; D2 619/1 059 ; réplica = moteur. Chiffres exacts.
- *Reconversion* : voir m1 (formule SPEC appliquée à la lettre mais invalide sous portage ; sans effet sur les bascules de seuils).
- *Bloc `cb` aux seuils du labo* (`bot/config.py` : half-size 0,15 / flatten 0,25 / gel 3 %) : seuils corrects. **Quelle lecture est pertinente ?** BAND §3.2 demande « le MaxDD du *wallet simulé S2* ». Le wallet simulé S2 est la courbe `E` des segments S2 chaînés (positions et cash portés, poche dimensionnée `a·W·equity` chaque mois), c'est-à-dire la lecture que l'implémenteur a étiquetée « *a ré-équilibré à chaque fenêtre* » : numériquement elle égale exactement l'equity S2 chaînée (MaxDD **20,72 %**, 53 jours > half-size, 4 épisodes, 0 flatten, 2 gels) — l'étiquette est fausse (il n'y a aucun rééquilibrage ; mon vrai « a×P chaîné » donne 20,25 %, 25 jours, 4 épisodes). La lecture « formule SPEC `E = 1 + a(P−1)` avec P concaténée » (45,98 %, 7 épisodes de flatten, premier 2008-10-09) suppose une poche jamais redimensionnée face au reste du wallet (poids de poche → 1 en 30 ans) alors que le dimensionnement `a·W×equity` de S2 (donc de la production) rééchelonne chaque mois : elle **surestime** et n'est pas la bonne pour BAND §3.2. **Constat robuste aux deux lectures pertinentes : MaxDD wallet ≈ 20-21 % > half-size 15 %, < flatten 25 %** ⇒ le half-size se déclencherait (≈ 4 épisodes), la prévision a priori de la SPEC (11-14 %) est réfutée, et la marge au flatten n'est que de 4,3 pts avec une poche qui, du fait de C1, déborde son allocation. Les circuits breakers n'étant pas modélisés, ces chiffres sont des bornes, pas des résultats.
- `band_fragile = false` : confirmé (aucune bascule de seuil), mais sans valeur probante puisque S1 et S2 partagent l'artefact C1.

## 7. Point 7 — Autres vérifications demandées

- **Coûts** : voir m3 (le stress du REPORT est valide arithmétiquement mais non informatif ; sous la mécanique voulue le Sharpe reste ≥ 0,70 jusqu'à ~50 bps).
- **Survivant** : documenté (SPEC §1, REPORT §7) mais non chiffré — M3 le chiffre via le contrôle (+6,7 pts de CAGR sur SPY).
- **Retouche post-OOS** : grille conforme (4 combos, `LowVolParams.__post_init__` refuse toute autre valeur), pas de CLI de recherche, une seule exécution déclarée ; non vérifiable par git (m4). 24/24 tests de `test_lowvol.py` passent ; ils documentent l'entrée bloquée sous la bande (N=30, contrôle) mais pas l'asymétrie de SORTIE sous la bande (orphelins jamais vendus), qui est l'origine de C1.
- **Éligibilité 252 j** : ≥ 252 rendements (253 clôtures), indépendante de L (vérifiée : mêmes ensembles éligibles pour L=63 et 252) ; 73-103 éligibles aux dates OOS ; introductions tardives (TSLA 2010, META 2012, DOW 2019…) correctement exclues avant 252 jours. Interprétation signalée par l'implémenteur : acceptable.
- **Nombre de fenêtres** : 30, recalculé from scratch depuis la SPEC (mêmes bornes), contiguës ; K_total cohérent.

## 8. Autres angles d'attaque (point 8)

- Le Sharpe ≈ 0,8 est robuste à la mécanique (0,813 S1 ; 0,830 voulue ; 0,796 financée) — il vient de l'exposition actions diversifiée de grandes capitalisations survivantes, pas du tri low-vol (contrôle 0,92) ; C1/C2 invalident donc surtout PF, trades, MaxDD relatif et la comparaison au contrôle, pas la lecture « Sharpe ≈ 0,8 ».
- Le critère MaxDD relatif (0,946) est défavorisé par le levier de C1 ; sous mécanique voulue il vaut 0,69.
- Le Sharpe annualisé n'a pas de taux sans risque soustrait (réserve REPORT) : sans effet de classement ici (levier gratuit ne change pas r/σ), mais le « coût du levier » est nul par construction.
- Pas de look-ahead dans le portage inter-fenêtres (reconstruction à `last_close`, vérifiée par la réplication exacte 1e-15).

## 9. Expériences locales journalisées (aucun fichier du dépôt modifié)

| Id | Description | Résultat clé |
|---|---|---|
| Réplication | `indep.py` + `a2_reproduce.py` : simulateur from scratch, sélection IS et chaîne OOS avec portage | égalité bit-à-bit avec le candidat (Sharpe, MaxDD, PF, trades, expo, titres, combos par fenêtre) |
| E1 | mon simulateur, rebalance mensuel réel (ordres au lendemain de chaque décision), sans bande, mêmes combos IS : candidate vs contrôle EW vs SPY | cand. Sharpe 0,830 / Sortino 1,194 / MaxDD 37,7 % / PF 2,71 / 1 003 trades / DSR 0,964 ; contrôle 0,920 / 1,330 / 50,5 % ; non dominée (MaxDD) mais IR −0,58 |
| E1b | idem, sélection IS rejouée (N=30 devient sélectionnable) | cand. 0,806 / MaxDD 36,8 % ; même conclusion |
| E2 | S1 avec contrainte `cash ≥ 0` (Ledger) | Sharpe 0,796, MaxDD 45,5 %, 1 120 achats bloqués |
| Fragilité | poids × (1−1e-12) | 0 ordre, Sharpe NaN |

Ces chiffres ne sont **pas** utilisables pour réhabiliter la candidate (SPEC §5 : toute variante = nouvel id, nouveau K_total) ; ils servent à dimensionner l'artefact.

## 10. Recommandation sur le statut registre

1. **Statut : `rejetee`** (SPEC §5 : `isSound: false` ⇒ rejet automatique, indépendamment des 5 seuils). Libellé de `raison` recommandé : *« rejet de PROCÉDURE — mesure invalide (A3 : bande plate 0,05 par titre × poids 1/N, N ≥ 20) ; l'hypothèse low-vol n'a pas été testée équitablement ; aucune conclusion sur l'anomalie »* — distinct d'un rejet par performance (Sharpe > 0 et PF > 1 auraient donné `ecartee`). Annexe `band_fidelity` à consigner avec les corrections m1/M4 (lecture cb pertinente = equity S2 chaînée : 20,7 %).
2. **Le rejet est-il dû à un artefact du MOTEUR générique à toute candidate équipondérée N ≥ 20 ?** Réponse en deux parties, à porter telle quelle sur la fiche de backlog :
   - **Générique (design/sémantique de bande, commun moteur ET production, prouvé par `RiskManager` réel)** : toute poche équipondérée avec N ≥ 20 (poids par titre ≤ 0,05 de la poche) est soit **inentrable** (N > 20 ; le contrôle large équipondéré aussi), soit **à l'égalité flottante** (N = 20), et dans tous les cas les sorties de titres dont le poids est tombé sous la bande ne s'exécutent **jamais** (asymétrie « vendre les gagnants, garder les perdants »). Le piège s'atténue mais subsiste pour N < 20 (un orphelin dont le poids est tombé sous 5 % de la poche n'est jamais vendu : pour top-10, une perte > 50 % depuis l'entrée) — **à vérifier par exécution sur `xs_momentum_sp100` en production** (hors périmètre de cet audit, hypothèse non testée).
   - **Propre au moteur** : absence de contrainte `cash ≥ 0` / brut ≤ 1 sur le spot (la docstring l'admet), alors que le `Ledger` l'impose : ce défaut est générique à toute candidate spot dont les positions cumulées dépassent 1, et il a masqué l'ampleur de l'accumulation.
   - **Non générique** : `lowvol.py` (signal, éligibilité, causalité) est correct ; aucun bug d'implémentation ; le rejet n'est pas un jugement sur la qualité du signal.
3. **Pistes pour la fiche de backlog** (décisions humaines/gouvernance, pas de modification ici) : (a) garde moteur/runner : `min_cash`, `max_gross` publiés dans `summarize_segment` et erreur ou drapeau bloquant si `cash < 0` sur spot long-only ; (b) lint de runner : tout contrôle apparié doit émettre ≥ 1 ordre sinon la SPEC est invalide — pré-vol obligatoire AVANT commit de la SPEC (dry-run du contrôle et de la grille : `max(w_i) > bande·(1+ε)`, sorties exécutables) ; (c) amendement #12 : sémantique de sortie sous bande en production (orphelins) ; (d) un éventuel variant `lowvol_*` (N ≤ 15, ou sémantique de bande documentée, contrôle pré-enregistré dans une sémantique évaluable) = **nouvel id, nouveau K_total**, avec l'indication non décisionnelle de E1 que le gain face à l'équipondéré est négatif en Sharpe/CAGR sur cet univers biaisé.

## 11. Fichiers

`indep.py` (réimplémentation) · `a1_lookahead.py` · `a2_reproduce.py` · `a3_mechanics.py` (+ `a3_orders_s1.csv`) · `a3b_pf.py` · `a3c_production.py` (vrai RiskManager/Ledger) · `a5_dsr_misc.py` · `a6_s2_cb.py` · `a6b_divergence.py` · `a8_data.py` · `e1_counterfactual.py` · `e2_funded.py` · `logs/*.log`. Exécution : `PYTHONPATH=. python3 a2_reproduce.py` d'abord (écrit `a2.pkl`), puis les autres.
