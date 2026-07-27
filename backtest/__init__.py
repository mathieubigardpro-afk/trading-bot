"""backtest/ — MOTEUR COMMUN de backtest walk-forward du projet (docs/PROMOTION-RULES.md §1.1).

Avant ce module, aucun moteur de backtest partagé n'existait dans le dépôt : les métriques
citées en référence pour `xs_momentum_sp100` (Sharpe OOS 0,82, DSR 0,92, cf.
`docs/RESEARCH-REGISTRY.json`) proviennent d'un script ad hoc externe (`bt-final/xs-momentum-
sp100/`, absent de ce dépôt) — c'était le finding critique d'audit qui a motivé la création de
ce paquet. **Toute nouvelle candidate, à partir de maintenant, doit passer par
`backtest/engine.py`** — PROMOTION-RULES §1.1 l'exige explicitement ("jamais un script ad hoc
parallèle").

Sous-modules :
  - `backtest.data`        : chargement des CSV de prix quotidiens, calendrier de trading.
  - `backtest.metrics`      : Sharpe/Sortino/profit factor/MaxDD/CAGR/exposition/DSR-PSR.
  - `backtest.engine`       : moteur walk-forward générique (fenêtres IS/OOS, simulation de
                              portefeuille avec coûts, sélection de paramètres IS-only,
                              concaténation OOS).
  - `backtest.strategies.*` : logiques de signal spécifiques à chaque stratégie candidate,
                              réutilisant le moteur ci-dessus (jamais l'inverse).

Principes non négociables (cf. docstring de `backtest/engine.py` pour le détail) :
  1. Aucun look-ahead : tout signal utilisé pour décider les poids à la clôture de `t` ne lit
     que des données `<= t` ; l'exécution effective a lieu à l'OUVERTURE de `t+1`.
  2. Coûts en bps/côté appliqués sur le turnover réel à chaque rebalance (achats ET ventes).
  3. Sélection de paramètres (si grille) uniquement sur la fenêtre IS ; les métriques de
     décision sont calculées sur l'équity OOS **concaténée** de toutes les fenêtres.
  4. Aucune donnée de marché n'est copiée dans ce dépôt — ce paquet lit les CSV depuis un
     `--data-dir` fourni par l'appelant (jamais un chemin en dur pointant hors du dépôt dans le
     code, toujours un paramètre).
"""
