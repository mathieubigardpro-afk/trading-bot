"""backtest/tests/test_engine.py — tests synthétiques du MOTEUR (jamais de données réelles ici,
cf. `docs/PROMOTION-RULES.md` §1.1 : le moteur doit être validé indépendamment de toute
candidate). Quatre garanties non négociables couvertes (cf. docstring de `backtest/engine.py`) :

  1. anti-look-ahead (une stratégie "cheat" qui voit le rendement de demain s'effondre quand le
     moteur applique correctement le décalage d'exécution close(t) -> open(t+1)) ;
  2. les coûts réduisent l'équity proportionnellement au turnover réel ;
  3. les fenêtres walk-forward ont des bornes correctes et IS/OOS ne se chevauchent jamais ;
  4. le DSR se comporte correctement aux cas limites (K=1 -> DSR≈PSR(0) ; K grand -> DSR décroît).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest import engine, metrics


# ------------------------------------------------------------------------------------------
# 1. Anti-look-ahead
# ------------------------------------------------------------------------------------------


def _build_lookahead_fixture(n_days: int = 4000, seed: int = 7):
    """Prix synthétiques construits pour qu'une trahison de causalité soit détectable :
    - `overnight_jump[t]` (gros mouvement, réalisé ENTRE la clôture de t-1 et l'ouverture de t)
      porte la quasi-totalité de l'information "prévisible" utilisée par le signal "cheat".
    - `intraday_noise[t]` (petit mouvement indépendant, réalisé ENTRE l'ouverture et la clôture
      de t) n'est CONNU qu'à la clôture de t -- non corrélé au signe de `overnight_jump`.

    Le signal "cheat" (`weights_decided`, construit ici EXPRÈS avec une fuite : il utilise
    `overnight_jump[t+1]`, une donnée non disponible à la clôture de `t`) parie 100% cash/long
    sur le signe du GAP DE DEMAIN. Un moteur qui exécute correctement à `open[t+1]` ne capture
    QUE le mouvement intrajournalier de `t+1` (indépendant du signal, donc pas d'edge) ; un
    moteur biaisé qui exécuterait au même prix que celui utilisé pour décider capturerait le
    gap au complet (fortement corrélé au signal par construction) -- edge énorme et artificiel.
    """
    rng = np.random.default_rng(seed)
    overnight_jump = rng.normal(0.0, 0.02, n_days)
    intraday_noise = rng.normal(0.0, 0.002, n_days)

    close = np.empty(n_days)
    open_ = np.empty(n_days)
    close[0] = 100.0
    open_[0] = 100.0
    for t in range(1, n_days):
        open_[t] = close[t - 1] * (1.0 + overnight_jump[t])
        close[t] = open_[t] * (1.0 + intraday_noise[t])

    calendar = pd.bdate_range("2000-01-03", periods=n_days)
    opens = pd.DataFrame({"X": open_}, index=calendar)
    closes = pd.DataFrame({"X": close}, index=calendar)

    # Signal "cheat" : weights_decided[t] utilise overnight_jump[t+1] (FUTUR relatif à t).
    cheat_signal = pd.Series(overnight_jump, index=calendar).shift(-1)
    weights_decided = pd.DataFrame({"X": (cheat_signal > 0).astype(float)})
    weights_decided.iloc[-1] = 0.0  # dernière ligne jamais exécutée dans les segments testés

    return calendar, opens, closes, weights_decided


def _biased_same_price_execution_returns(weights_decided: pd.DataFrame, closes: pd.DataFrame) -> pd.Series:
    """Référence délibérément BIAISÉE (PAS le moteur du projet, seulement un outil de test) :
    exécute la décision de clôture de `t` au MÊME prix de clôture de `t` (aucun décalage), tenue
    jusqu'à la clôture de `t+1` -- ce qu'un moteur bogué "look-ahead" produirait. Sert de
    contraste pour prouver que le vrai moteur (`engine.simulate_segment`) NE reproduit PAS cette
    performance artificielle."""
    w = weights_decided["X"].to_numpy()[:-1]
    px = closes["X"].to_numpy()
    close_to_close_ret = px[1:] / px[:-1] - 1.0
    return pd.Series(w * close_to_close_ret)


def test_lookahead_cheat_collapses_under_correct_engine_but_not_under_biased_reference():
    calendar, opens, closes, weights_decided = _build_lookahead_fixture()

    seg = engine.simulate_segment(
        calendar, weights_decided, opens, closes, start_idx=1, end_idx=len(calendar) - 1, cost_bps=0.0
    )
    correct_sharpe = metrics.sharpe_ratio(seg.returns)

    biased_returns = _biased_same_price_execution_returns(weights_decided, closes)
    biased_sharpe = metrics.sharpe_ratio(biased_returns)

    # Le signal "cheat" est construit pour avoir un edge ÉNORME et artificiel s'il pouvait
    # trader au prix ayant servi à la décision (quasi 100% des jours investis gagnants).
    assert biased_sharpe > 8.0, f"référence biaisée attendue très rentable, obtenu {biased_sharpe}"
    # Le moteur correctement décalé ne doit PLUS capturer cet edge -- il ne reste que le bruit
    # intrajournalier, indépendant du signal.
    assert abs(correct_sharpe) < 1.5, f"moteur correct attendu ~plat, obtenu Sharpe={correct_sharpe}"
    assert biased_sharpe > correct_sharpe + 5.0, "l'écart entre les deux doit être massif et net"


# ------------------------------------------------------------------------------------------
# 2. Coûts proportionnels au turnover
# ------------------------------------------------------------------------------------------


def _build_cost_fixture(n_days: int = 41, switch_every: int = 5):
    """Prix CONSTANTS (aucun mouvement de marché) pour isoler complètement l'effet des coûts :
    toute variation d'équity observée ne peut provenir QUE des coûts de transaction. Le
    portefeuille bascule 100% A / 100% B tous les `switch_every` jours (turnover ~ 2x l'équity à
    chaque bascule : vente totale + achat total)."""
    calendar = pd.bdate_range("2001-01-02", periods=n_days)
    opens = pd.DataFrame({"A": 100.0, "B": 50.0}, index=calendar)
    closes = pd.DataFrame({"A": 100.0, "B": 50.0}, index=calendar)

    weights = []
    for i in range(n_days):
        cycle = i // switch_every
        weights.append((1.0, 0.0) if cycle % 2 == 0 else (0.0, 1.0))
    weights_decided = pd.DataFrame(weights, columns=["A", "B"])
    return calendar, opens, closes, weights_decided


def test_costs_reduce_equity_proportionally_to_turnover():
    calendar, opens, closes, weights_decided = _build_cost_fixture()
    start_idx, end_idx = 1, len(calendar) - 1

    seg_zero_cost = engine.simulate_segment(calendar, weights_decided, opens, closes, start_idx, end_idx, cost_bps=0.0)
    seg_low_cost = engine.simulate_segment(calendar, weights_decided, opens, closes, start_idx, end_idx, cost_bps=25.0)
    seg_high_cost = engine.simulate_segment(calendar, weights_decided, opens, closes, start_idx, end_idx, cost_bps=100.0)

    final_zero = seg_zero_cost.equity.iloc[-1]
    final_low = seg_low_cost.equity.iloc[-1]
    final_high = seg_high_cost.equity.iloc[-1]

    # Prix constants + coût nul -> équity strictement inchangée (aucun autre effet possible).
    assert final_zero == pytest.approx(1.0, abs=1e-9)
    # Plus le coût par côté augmente, plus l'équity finale doit être PLUS BASSE (monotone).
    assert final_zero > final_low > final_high

    # Vérification quantitative : nombre de bascules complètes (100% turnover A<->B, 2 côtés
    # payés) dans la fenêtre testée, coût attendu ~ (1 - 2*cost_rate)^n_switches (approximation
    # au 1er ordre du recomposé, tolérance large pour absorber la composition exacte).
    n_switches = sum(
        1
        for i in range(start_idx, end_idx + 1)
        if not weights_decided.iloc[i - 1].equals(weights_decided.iloc[max(i - 2, 0)])
    )
    cost_rate = 100.0 / 10000.0
    expected_high = (1.0 - 2.0 * cost_rate) ** n_switches
    assert final_high == pytest.approx(expected_high, rel=0.05)


# ------------------------------------------------------------------------------------------
# 3. Fenêtres walk-forward
# ------------------------------------------------------------------------------------------


def test_walk_forward_windows_bounds_and_no_overlap():
    calendar = pd.bdate_range("1990-01-01", "2005-12-31")
    windows = engine.generate_walk_forward_windows(calendar, is_months=36, oos_months=12, step_months=12)

    assert len(windows) >= 10, f"attendu au moins 10 fenêtres sur 16 ans, obtenu {len(windows)}"

    for w in windows:
        assert w.is_start_idx < w.is_end_idx
        assert w.is_end_idx < w.oos_start_idx, "IS doit se terminer strictement avant le début de l'OOS"
        assert w.oos_start_idx == w.is_end_idx + 1, "OOS doit commencer au jour de bourse suivant l'IS (pas de trou)"
        assert w.oos_start_idx <= w.oos_end_idx
        # ~36 mois IS / ~12 mois OOS en jours de bourse (tolérance pour jours fériés/mois courts)
        is_days = w.is_end_idx - w.is_start_idx + 1
        oos_days = w.oos_end_idx - w.oos_start_idx + 1
        assert 700 <= is_days <= 830, f"IS de {is_days} jours de bourse hors plage attendue"
        assert 240 <= oos_days <= 275, f"OOS de {oos_days} jours de bourse hors plage attendue"

    for w1, w2 in zip(windows, windows[1:]):
        assert w2.is_start_idx > w1.is_start_idx, "les fenêtres doivent glisser en avant"
        # step_months == oos_months ici -> carrelage parfait, sans chevauchement ni trou OOS.
        assert w2.oos_start_idx == w1.oos_end_idx + 1, "fenêtres OOS attendues adjacentes (step == oos)"
        assert w2.oos_start_idx > w1.oos_end_idx, "aucun chevauchement OOS entre fenêtres consécutives"


def test_walk_forward_windows_step_smaller_than_oos_no_oos_overlap_when_deduped():
    """Cas générique (step < oos) : les fenêtres se chevauchent bien en IS (attendu), mais les
    bornes de chaque fenêtre individuelle restent internement cohérentes (IS avant OOS, pas de
    trou IS->OOS)."""
    calendar = pd.bdate_range("1990-01-01", "2005-12-31")
    windows = engine.generate_walk_forward_windows(calendar, is_months=36, oos_months=12, step_months=6)
    assert len(windows) >= 15
    for w in windows:
        assert w.oos_start_idx == w.is_end_idx + 1


# ------------------------------------------------------------------------------------------
# 4. DSR — cas limites
# ------------------------------------------------------------------------------------------


def test_dsr_k1_equals_psr_against_zero():
    rng = np.random.default_rng(42)
    returns = rng.normal(0.0006, 0.01, 500)

    result = metrics.deflated_sharpe_ratio(returns, trials_k=1)
    assert result.sr0_benchmark == pytest.approx(0.0)

    manual_psr = metrics.probabilistic_sharpe_ratio(
        result.sharpe_hat_period, 0.0, result.n_obs, result.skew, result.kurtosis_excess
    )
    assert result.dsr == pytest.approx(manual_psr, rel=1e-9)


def test_dsr_decreases_as_trials_k_grows():
    rng = np.random.default_rng(123)
    returns = rng.normal(0.0008, 0.01, 800)  # Sharpe positif net pour un test non-dégénéré

    dsr_k1 = metrics.deflated_sharpe_ratio(returns, trials_k=1).dsr
    dsr_k10 = metrics.deflated_sharpe_ratio(returns, trials_k=10).dsr
    dsr_k1000 = metrics.deflated_sharpe_ratio(returns, trials_k=1000).dsr

    assert dsr_k1 >= dsr_k10 >= dsr_k1000
    assert dsr_k1 > dsr_k1000 + 1e-6, "K=1000 doit être une pénalité nettement plus sévère que K=1"


def test_expected_max_sharpe_zero_for_k_le_1():
    assert metrics.expected_max_sharpe(1, sr_std=0.05) == 0.0
    assert metrics.expected_max_sharpe(0, sr_std=0.05) == 0.0
    assert metrics.expected_max_sharpe(50, sr_std=0.05) > 0.0
