# DOSSIER DE GOUVERNANCE — Antécédent `quasi_passif_crypto` (backlog #14) + amendements #12

*Préparé par la session hebdomadaire #4 (2026-08-24). **Ce document ne décide RIEN** : c'est un
dossier d'instruction pour la décision humaine requise par `docs/PROMOTION-RULES.md` §4.3 et par
la sémantique pré-enregistrée du retest (SPEC commit fb5b8b5) — la boucle de recherche ne touche
pas à la composition des wallets réels de sa propre initiative. La session #4 n'a jugé aucune
candidate (aucune Porte 1/Porte 2 dans cette session), conformément à l'esprit du §0 : ce dossier
peut donc être instruit sans mélange gouvernance/jugement. L'adoption de toute option ci-dessous
doit suivre la procédure du §0 (session dédiée, justification écrite au RESEARCH-LOG, commits
séparés de toute action de promotion/rétrogradation/mort).*

---

## 1. La question à trancher

La **seule brique crypto** des 3 wallets réels (`quasi_passif_crypto`, poche crypto de 🛡️/⚖️/🔥)
repose sur un backtest d'origine explicitement non audité (2026-07-23), dont le retest complet
selon le protocole Porte 1 (session #3, 2026-08-10, audit adversarial `isSound: true`) a été un
**échec 3/3 variantes**. Les chiffres d'origine (Sharpe 1,24/1,47/1,49) ne sont pas reproduits
et ne doivent plus servir de référence. Que fait-on de cette brique ?

## 2. Faits chiffrés (sources : registre, retest #3, DRIFT-REPORT 2026-08-23)

**Retest walk-forward (14 fenêtres 9m/3m, 2022-10→2026-03, coûts pessimistes, moteur audité) :**

| Variante (wallet) | Sharpe OOS | Bench B&H | PF | MaxDD | DSR | Verdict §1.2 |
|---|---|---|---|---|---|---|
| prudent (BTC+ETH) | 0,808 | 0,761 | 1,080 | 8,4% | 0,215 | ÉCHEC 2/5 (PF, DSR) |
| équilibré (6 majors) | 0,283 | 0,653 | 0,718 | 27,3% | 0,038 | ÉCHEC 3/5 |
| agressif (11 divers.) | 0,069 | 0,537 | 0,771 | 56,4% | 0,015 | ÉCHEC 3/5 |

Points saillants : équilibré et agressif sont SOUS leur benchmark buy & hold ET perdants nets aux
coûts nominaux ; tout l'edge apparent vient de 2022-2023 (Sharpe depuis 2024 : +0,61 / **−0,17** /
**−0,55**) ; corrélation inter-variantes 0,75-0,83 (~1 pari corrélé). La variante prudente est la
moins loin des seuils : au-dessus de son benchmark (0,81 > 0,76), MaxDD 8,4%, mais PF 1,08 ≤ 1,15
et DSR 0,215 < 0,50.

**Vécu en production (32 jours au 2026-08-23 — TROP COURT pour être significatif, n < 60j) :**
Sharpe vécu prudent −2,57, équilibré +5,41, agressif +0,68. Bruit d'échantillon court ; aucun
diagnostic fiable avant ~60 jours de vécu. À noter : le DRIFT-REPORT affiche encore comme
« Sharpe attendu » les chiffres discrédités (1,24/1,47/1,49 lus dans le registre) — point
d'hygiène signalé au backlog (la référence attendue devrait devenir celle du retest audité).

**Garde-fou déjà pré-enregistré** : `docs/SELECTION-FINALE.md` §5 — bascule (vers cash/60-40
selon la poche) si sous-performance vécue sur 3 mois réels, soit une échéance naturelle vers
**fin octobre 2026** (premiers cycles réels ~2026-07-22).

## 3. Options instruites

**(a) Statu quo sous le critère vécu §5 (ne rien changer avant fin octobre).**
Pour : c'est le critère PRÉ-enregistré à l'origine du déploiement — le respecter est exactement
la discipline que le projet s'impose ; 32 jours de vécu ne fournissent aucune base statistique
pour agir mieux que le plan initial ; le vol-targeting + SMA200 borne structurellement le risque
(exposition brute réalisée historiquement faible, MaxDD vécu ≤ 1,2% à ce jour).
Contre : pendant ~2 mois, ⚖️ et 🔥 portent une poche dont la validation protocolaire a échoué
nettement (sous-benchmark, PF < 1) ; « attendre le critère vécu » sur un échantillon de 3 mois
reste un test faible (bruit élevé) — le risque est de reconduire par inertie.

**(b) Alignement formel de l'antécédent sur les règles de mort §3.**
Pour : met fin au statut d'exception « hors cadre » ; donne des seuils chiffrés automatiques
(DD > 2× attendu, Sharpe roulant 60j < 0 pendant 30j) là où le §5 actuel demande une lecture
humaine ; ne retire rien tant que les seuils ne se déclenchent pas.
Contre : exige de fixer la « référence attendue » (le §3 se réfère au MaxDD/Sharpe OOS ayant
justifié la promotion — pour cet antécédent, il faut décider explicitement que la référence
devient LE RETEST AUDITÉ : Sharpe 0,808/0,283/0,069, MaxDD 8,4/27,3/56,4%, jamais les chiffres
discrédités) ; avec un Sharpe attendu déjà proche de 0 pour ⚖️/🔥, « Sharpe vécu ≥ 50% de
l'attendu » devient un critère quasi vide — l'alignement mécanique ne règle pas tout.

**(c) Réduction/retrait ciblé de la poche crypto de ⚖️ et 🔥 (conserver 🛡️).**
Pour : c'est l'option la plus fidèle aux chiffres — les deux variantes en échec net (sous
benchmark ET perdantes aux coûts) sont retirées (poche → cash du wallet), la variante prudente
(la seule au-dessus de son benchmark, MaxDD 8,4%) est conservée sous surveillance §5 ; réversible
(une réintroduction repasserait par Porte 1/Porte 2 avec un nouvel id).
Contre : agit AVANT l'échéance du critère pré-enregistré, sur la base d'un backtest — c'est
précisément le genre de décision hâtive que le pré-enregistrement veut éviter ; laisse ⚖️/🔥 sans
exposition crypto (acceptable en paper trading, mais change le profil de diversification voulu
par `SELECTION-FINALE.md`).

## 4. Lecture de la session (sans force décisionnelle)

Le minimum défendable est (a) : le critère §5 est pré-enregistré, l'échéance est proche, et rien
dans le vécu (32j) ne justifie de le court-circuiter. Si une action anticipée est souhaitée, (c)
est la mieux étayée par les chiffres audités, et (b) peut être adopté EN COMPLÉMENT de (a) ou (c)
pour mettre fin au statut d'exception — à condition de fixer explicitement la référence attendue
sur les chiffres du retest audité. Quelle que soit l'option : décision humaine, session dédiée,
commit séparé, entrée au RESEARCH-LOG.

## 5. Amendements #12 à PROMOTION-RULES.md (propositions de texte, à adopter en session dédiée)

**(12a) Critère de valeur marginale vs incumbent (nouveau §1.6 proposé).** « Si la candidate est
une variante d'une stratégie déjà en production, le backtest DOIT inclure un contrôle : la
version incumbent exécutée sur les MÊMES fenêtres walk-forward et le même moteur. Si la candidate
ne domine pas l'incumbent sur au moins un axe majeur (Sharpe, Sortino, MaxDD) sans être dominée
sur les autres, ou si son Information Ratio vs incumbent est négatif de façon significative, elle
est écartée même si tous les seuils §1.2 sont passés (statut `ecartee`, comptée dans K_total). »
— Formalise le précédent `xs_momentum_invvol_sp100` (session #1, option conservatrice déjà
appliquée).

**(12b) Discriminance du DSR sur OOS longs (complément §1.2 proposé).** Constat chiffré (audits
#1 et #2) : sur un OOS concaténé très long (n ≈ 7 500 obs. quotidiennes), DSR ≥ 0,50 est quasi
automatiquement satisfait quel que soit K ; sur OOS horaire (n ≈ 30 000), il est au contraire
très discriminant. Options instruites : (i) ajouter un test de significativité de l'écart vs
benchmark (Jobson-Korkie/Memmel, p ≤ 0,10, déjà utilisé en vague 1) pour les stratégies à OOS
quotidien long ; (ii) PSR avec SR* = 0,25 au lieu de 0 ; (iii) DSR calculé par fenêtre puis
agrégé (médiane). La session recommande d'instruire (i) en priorité : c'est l'outil déjà utilisé
par le projet, il cible exactement le cas observé (candidate au-dessus des seuils absolus mais
non distinguable de son benchmark).

*Rappel procédural : l'adoption de 12a/12b modifie un document gravé — session dédiée, aucune
candidate jugée dans la même session, commit séparé référençant PROMOTION-RULES.md §0.*
