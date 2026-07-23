"""Tests de tools/weekly_maintenance.py -- fixtures synthétiques uniquement (réseau bloqué en
développement, cf. docs/ARCHITECTURE.md §0.2/§0.3). Couvre :
  - le moniteur de dérive (classification OK/SURVEILLER/ALERTE, cas construits) ;
  - le recalibrage encadré (refus hors-grille, refus <10% d'amélioration, simulateur walk-forward
    sur données synthétiques) ;
  - le rendu du rapport et l'orchestration main() en mode dry-run (--skip-push).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

import tools.weekly_maintenance as wm


# ============================================================================================
# --- Fonctions statistiques pures ---
# ============================================================================================


def test_sharpe_from_daily_returns_none_below_two_points():
    assert wm.sharpe_from_daily_returns([]) is None
    assert wm.sharpe_from_daily_returns([0.01]) is None


def test_sharpe_from_daily_returns_none_on_zero_variance():
    assert wm.sharpe_from_daily_returns([0.01, 0.01, 0.01]) is None


def test_sharpe_from_daily_returns_positive_for_positive_drift():
    returns = [0.01, -0.002, 0.015, 0.003, -0.001] * 20
    sharpe = wm.sharpe_from_daily_returns(returns)
    assert sharpe is not None
    assert sharpe > 0


def test_max_drawdown_from_daily_returns_empty_is_none():
    assert wm.max_drawdown_from_daily_returns([]) is None


def test_max_drawdown_from_daily_returns_simple_case():
    # +10%, -20%, +5% : pic à 1.10, creux à 0.88 -> DD = (1.10-0.88)/1.10 = 20%
    dd = wm.max_drawdown_from_daily_returns([0.10, -0.20, 0.05])
    assert dd is not None
    assert abs(dd - 20.0) < 1e-9


def test_max_drawdown_never_negative_on_all_positive_returns():
    dd = wm.max_drawdown_from_daily_returns([0.01, 0.02, 0.005])
    assert dd == 0.0


def test_rolling_sharpe_negative_streak_none_if_not_enough_data():
    assert wm.rolling_sharpe_negative_streak([0.01] * 10, window=60) is None


def test_rolling_sharpe_negative_streak_detects_trailing_negative_run():
    # 100 jours de rendements positifs/bruit, suivis de 40 jours nettement négatifs.
    rng = np.random.default_rng(7)
    good = list(rng.normal(0.01, 0.005, 100))
    bad = list(rng.normal(-0.02, 0.005, 40))
    returns = good + bad
    streak = wm.rolling_sharpe_negative_streak(returns, window=60)
    assert streak is not None
    # Sharpe roulant 60j négatif doit se déclencher une fois la fenêtre roulante suffisamment
    # "contaminée" par la période négative (marge large pour rester robuste au bruit aléatoire).
    assert streak >= 15


def test_rolling_sharpe_negative_streak_zero_when_recent_window_positive():
    rng = np.random.default_rng(3)
    returns = list(rng.normal(0.01, 0.004, 120))
    streak = wm.rolling_sharpe_negative_streak(returns, window=60)
    assert streak == 0


# ============================================================================================
# --- Construction de la série de rendements attribués à une stratégie ---
# ============================================================================================


def test_build_strategy_cycle_returns_attributes_only_matching_symbols():
    decisions = [
        {"run_id": "2026-01-01T00", "symbol": "BTC", "strategy_signals": {"quasi_passif_crypto": 0.1}},
        {"run_id": "2026-01-01T00", "symbol": "AAPL", "strategy_signals": {"xs_momentum_sp100": 0.2}},
        {"run_id": "2026-01-01T01", "symbol": "BTC", "strategy_signals": {"quasi_passif_crypto": 0.1}},
    ]
    equity = [
        {"run_id": "2026-01-01T00", "equity_usd": 1000.0, "exposures": {"BTC": 0.1, "AAPL": 0.2}},
        {"run_id": "2026-01-01T01", "equity_usd": 1010.0, "exposures": {"BTC": 0.1, "AAPL": 0.2}},
    ]
    returns = wm.build_strategy_cycle_returns(decisions, equity, "quasi_passif_crypto")
    assert len(returns) == 1
    run_id, r = returns[0]
    assert run_id == "2026-01-01T01"
    wallet_return = (1010.0 - 1000.0) / 1000.0
    assert abs(r - (0.1 * wallet_return)) < 1e-12


def test_build_strategy_cycle_returns_empty_if_never_attributed():
    decisions = [
        {"run_id": "2026-01-01T00", "symbol": "AAPL", "strategy_signals": {"xs_momentum_sp100": 0.2}},
    ]
    equity = [
        {"run_id": "2026-01-01T00", "equity_usd": 1000.0, "exposures": {}},
        {"run_id": "2026-01-01T01", "equity_usd": 1010.0, "exposures": {}},
    ]
    assert wm.build_strategy_cycle_returns(decisions, equity, "quasi_passif_crypto") == []


def test_aggregate_daily_returns_compounds_within_a_day():
    cycle_returns = [
        ("2026-01-01T00", 0.01),
        ("2026-01-01T01", 0.02),
        ("2026-01-02T00", -0.01),
    ]
    daily = wm.aggregate_daily_returns(cycle_returns)
    assert [d for d, _ in daily] == ["2026-01-01", "2026-01-02"]
    expected_day1 = (1.01 * 1.02) - 1.0
    assert abs(daily[0][1] - expected_day1) < 1e-12
    assert abs(daily[1][1] - (-0.01)) < 1e-12


# ============================================================================================
# --- Classification du verdict : stratégie ACTIVE ---
# ============================================================================================


def test_classify_active_insufficient_history_gives_surveiller():
    v = wm.classify_active_strategy_drift(
        sharpe_live=1.0, dd_live_pct=5.0, sharpe_ref=1.2, dd_ref_pct=8.0,
        n_days_observed=10, negative_rolling_streak_days=None,
    )
    assert v["verdict"] == "SURVEILLER"


def test_classify_active_ok_case():
    v = wm.classify_active_strategy_drift(
        sharpe_live=1.1, dd_live_pct=9.0, sharpe_ref=1.24, dd_ref_pct=8.0,
        n_days_observed=90, negative_rolling_streak_days=0,
    )
    assert v["verdict"] == "OK"


def test_classify_active_dd_watch_between_1_5_and_2x():
    # dd_live = 13% vs dd_ref = 8% -> ratio 1.625 -> SURVEILLER (entre 1.5x et 2.0x)
    v = wm.classify_active_strategy_drift(
        sharpe_live=1.0, dd_live_pct=13.0, sharpe_ref=1.24, dd_ref_pct=8.0,
        n_days_observed=90, negative_rolling_streak_days=0,
    )
    assert v["verdict"] == "SURVEILLER"
    assert any("drawdown" in r for r in v["reasons"])


def test_classify_active_dd_alerte_above_2x():
    # dd_live = 20% vs dd_ref = 8% -> ratio 2.5 -> ALERTE
    v = wm.classify_active_strategy_drift(
        sharpe_live=1.0, dd_live_pct=20.0, sharpe_ref=1.24, dd_ref_pct=8.0,
        n_days_observed=90, negative_rolling_streak_days=0,
    )
    assert v["verdict"] == "ALERTE"


def test_classify_active_rolling_sharpe_alerte_at_30_consecutive_days():
    v = wm.classify_active_strategy_drift(
        sharpe_live=-0.5, dd_live_pct=8.0, sharpe_ref=1.24, dd_ref_pct=8.0,
        n_days_observed=120, negative_rolling_streak_days=35,
    )
    assert v["verdict"] == "ALERTE"


def test_classify_active_rolling_sharpe_surveiller_below_30_consecutive_days():
    v = wm.classify_active_strategy_drift(
        sharpe_live=0.2, dd_live_pct=8.0, sharpe_ref=1.24, dd_ref_pct=8.0,
        n_days_observed=120, negative_rolling_streak_days=5,
    )
    assert v["verdict"] == "SURVEILLER"


def test_classify_active_alerte_dominates_surveiller():
    # DD watch (SURVEILLER) + rolling sharpe alerte (ALERTE) -> verdict global ALERTE.
    v = wm.classify_active_strategy_drift(
        sharpe_live=-0.5, dd_live_pct=13.0, sharpe_ref=1.24, dd_ref_pct=8.0,
        n_days_observed=120, negative_rolling_streak_days=31,
    )
    assert v["verdict"] == "ALERTE"
    assert len(v["reasons"]) >= 2


# ============================================================================================
# --- Classification du verdict : candidate en INCUBATION ---
# ============================================================================================


def test_classify_incubating_missing_entered_at_is_alerte():
    v = wm.classify_incubating_drift(
        age_days=None, sharpe_live=1.0, dd_live_pct=5.0, sharpe_ref=1.0, dd_ref_pct=10.0,
        n_days_observed=30,
    )
    assert v["verdict"] == "ALERTE"


def test_classify_incubating_ok_case():
    v = wm.classify_incubating_drift(
        age_days=30, sharpe_live=0.7, dd_live_pct=6.0, sharpe_ref=1.0, dd_ref_pct=10.0,
        n_days_observed=30,
    )
    assert v["verdict"] == "OK"


def test_classify_incubating_below_min_observation_is_surveiller():
    v = wm.classify_incubating_drift(
        age_days=10, sharpe_live=None, dd_live_pct=None, sharpe_ref=1.0, dd_ref_pct=10.0,
        n_days_observed=10,
    )
    assert v["verdict"] == "SURVEILLER"


def test_classify_incubating_age_over_56_days_is_alerte():
    v = wm.classify_incubating_drift(
        age_days=60, sharpe_live=0.9, dd_live_pct=5.0, sharpe_ref=1.0, dd_ref_pct=10.0,
        n_days_observed=59,
    )
    assert v["verdict"] == "ALERTE"
    assert any("56" in r for r in v["reasons"])


def test_classify_incubating_age_watch_heuristic_at_45_days():
    v = wm.classify_incubating_drift(
        age_days=45, sharpe_live=0.9, dd_live_pct=5.0, sharpe_ref=1.0, dd_ref_pct=10.0,
        n_days_observed=44,
    )
    assert v["verdict"] == "SURVEILLER"


def test_classify_incubating_porte2_sharpe_ratio_fails_is_alerte():
    # sharpe_live=0.3, sharpe_ref=1.0 -> ratio 0.3 < 0.5 (§2.2) -> ALERTE
    v = wm.classify_incubating_drift(
        age_days=30, sharpe_live=0.3, dd_live_pct=5.0, sharpe_ref=1.0, dd_ref_pct=10.0,
        n_days_observed=30,
    )
    assert v["verdict"] == "ALERTE"
    assert any("Porte 2" in r for r in v["reasons"])


def test_classify_incubating_porte2_dd_ratio_fails_is_alerte():
    # dd_live=16 vs dd_ref=10 -> ratio 1.6 > 1.5 (§2.2) -> ALERTE
    v = wm.classify_incubating_drift(
        age_days=30, sharpe_live=0.9, dd_live_pct=16.0, sharpe_ref=1.0, dd_ref_pct=10.0,
        n_days_observed=30,
    )
    assert v["verdict"] == "ALERTE"


# ============================================================================================
# --- Reference metrics resolution (registre) ---
# ============================================================================================


def test_reference_metrics_for_quasi_passif_uses_wallet_variant():
    entry = {
        "id": "quasi_passif_crypto",
        "sharpe_backtest_non_audite": {"prudent_btc_eth": 1.24, "equilibre_6majors": 1.47},
        "max_drawdown_pct_backtest": {"prudent_btc_eth": 8.0, "equilibre_6majors": 16.4},
    }
    ref_prudent = wm.reference_metrics_for("quasi_passif_crypto", "prudent", entry)
    assert ref_prudent["sharpe_ref"] == 1.24
    assert ref_prudent["dd_ref_pct"] == 8.0
    ref_equilibre = wm.reference_metrics_for("quasi_passif_crypto", "equilibre", entry)
    assert ref_equilibre["sharpe_ref"] == 1.47


def test_reference_metrics_for_standard_strategy_uses_single_values():
    entry = {"id": "xs_momentum_sp100", "sharpe_oos": 0.8227, "max_drawdown_oos_pct": 50.29}
    ref = wm.reference_metrics_for("xs_momentum_sp100", "equilibre", entry)
    assert ref["sharpe_ref"] == 0.8227
    assert ref["dd_ref_pct"] == 50.29


def test_reference_metrics_for_missing_entry_returns_none():
    ref = wm.reference_metrics_for("inconnue", "equilibre", None)
    assert ref["sharpe_ref"] is None
    assert ref["dd_ref_pct"] is None


def test_registry_entry_lookup_real_registry_file():
    registry = wm.load_registry(wm._REPO_ROOT)
    entry = wm.registry_entry(registry, "quasi_passif_crypto")
    assert entry is not None
    entry_missing = wm.registry_entry(registry, "ne_existe_pas")
    assert entry_missing is None


# ============================================================================================
# --- Recalibrage : refus hors-grille, seuil 10% ---
# ============================================================================================


def test_validate_param_in_grid_accepts_grid_value():
    wm.validate_param_in_grid("quasi_passif_crypto", "regime_sma_days", 200)  # ne lève pas


def test_validate_param_in_grid_refuses_out_of_grid_value():
    with pytest.raises(ValueError, match="hors de la grille"):
        wm.validate_param_in_grid("quasi_passif_crypto", "regime_sma_days", 999)


def test_decide_recalibration_refuses_out_of_grid_current_value():
    with pytest.raises(ValueError):
        wm.decide_recalibration("quasi_passif_crypto", "regime_sma_days", 999, {200: 1.0})


def test_decide_recalibration_refuses_out_of_grid_candidate_value():
    with pytest.raises(ValueError):
        wm.decide_recalibration(
            "quasi_passif_crypto", "regime_sma_days", 200, {200: 1.0, 999: 5.0}
        )


def test_decide_recalibration_no_change_when_current_is_best():
    result = wm.decide_recalibration(
        "quasi_passif_crypto", "regime_sma_days", 200, {200: 1.5, 175: 1.0, 225: 1.2}
    )
    assert result["changed"] is False
    assert result["best_value"] == 200


def test_decide_recalibration_no_change_below_10_percent_improvement():
    # (1.09 - 1.00) / 1.00 = 9% < seuil 10%
    result = wm.decide_recalibration(
        "quasi_passif_crypto", "regime_sma_days", 200, {200: 1.00, 175: 1.09}
    )
    assert result["changed"] is False
    assert result["relative_improvement"] is not None
    assert result["relative_improvement"] < 0.10


def test_decide_recalibration_applies_change_above_10_percent_improvement():
    # (1.16 - 1.00) / 1.00 = 16% > seuil 10%
    result = wm.decide_recalibration(
        "quasi_passif_crypto", "regime_sma_days", 200, {200: 1.00, 225: 1.16}
    )
    assert result["changed"] is True
    assert result["best_value"] == 225
    assert result["relative_improvement"] > 0.10


def test_decide_recalibration_refuses_when_current_sharpe_non_positive():
    result = wm.decide_recalibration(
        "quasi_passif_crypto", "regime_sma_days", 200, {200: -0.2, 225: 1.5}
    )
    assert result["changed"] is False
    assert "non positif" in result["reason"]


def test_decide_recalibration_raises_if_current_value_missing_from_results():
    with pytest.raises(ValueError):
        wm.decide_recalibration("quasi_passif_crypto", "regime_sma_days", 200, {175: 1.0})


# ============================================================================================
# --- Simulateur walk-forward (données synthétiques) ---
# ============================================================================================


def _synthetic_hourly_history(symbols, n_days, seed):
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2022-01-01", periods=n_days * 24, freq="h", tz="UTC")
    history = {}
    for sym in symbols:
        drift = rng.uniform(-0.0002, 0.0005)
        rets = rng.normal(drift / 24, 0.01, len(dates))
        prices = 100.0 * np.cumprod(1.0 + rets)
        history[sym] = pd.DataFrame(
            {"open": prices, "high": prices, "low": prices, "close": prices, "volume": 1.0},
            index=dates,
        )
    return history


RISQUE_TEST = {
    "vol_target_annualized": 0.20,
    "gross_exposure_max": 0.70,
    "cap_per_asset": 0.25,
    "vol_ewma_halflife_hours": 60,
}


def test_simulate_daily_returns_produces_clean_finite_series():
    history = _synthetic_hourly_history(["BTC", "ETH"], n_days=400, seed=42)
    fee = {"BTC": 15.0, "ETH": 15.0}
    series = wm.simulate_daily_returns(["BTC", "ETH"], history, 200, RISQUE_TEST, fee)
    assert isinstance(series, pd.Series)
    assert len(series) > 0
    assert not series.isna().any()
    assert np.isfinite(series.to_numpy()).all()


def test_simulate_daily_returns_empty_when_no_history():
    series = wm.simulate_daily_returns(["BTC"], {}, 200, RISQUE_TEST, {"BTC": 15.0})
    assert series.empty


def test_simulate_daily_returns_varies_with_sma_days():
    history = _synthetic_hourly_history(["BTC", "ETH", "SOL"], n_days=400, seed=11)
    fee = {s: 15.0 for s in history}
    s150 = wm.simulate_daily_returns(list(history), history, 150, RISQUE_TEST, fee)
    s250 = wm.simulate_daily_returns(list(history), history, 250, RISQUE_TEST, fee)
    common = s150.index.intersection(s250.index)
    assert len(common) > 100
    # Deux fenêtres de tendance différentes ne doivent (presque) jamais produire une série
    # identique au bit près sur des données bruitées.
    assert not np.allclose(s150.reindex(common).to_numpy(), s250.reindex(common).to_numpy())


def test_walk_forward_windows_non_overlapping_and_bounded():
    index = pd.RangeIndex(0, 500)
    windows = wm.walk_forward_windows(index, is_days=200, oos_days=100)
    assert len(windows) == 3  # (0-300), (100-400), (200-500)
    for is_idx, oos_idx in windows:
        assert len(is_idx) == 200
        assert len(oos_idx) == 100
        assert is_idx[-1] < oos_idx[0]


def test_walk_forward_windows_empty_when_too_short():
    index = pd.RangeIndex(0, 50)
    assert wm.walk_forward_windows(index, is_days=200, oos_days=100) == []


def test_walk_forward_select_and_compare_insufficient_data():
    short_series = pd.Series([0.01] * 10, index=pd.date_range("2022-01-01", periods=10))
    result = wm.walk_forward_select_and_compare({200: short_series, 175: short_series}, 200)
    assert result["status"] == "DONNEES_INSUFFISANTES"


def test_walk_forward_select_and_compare_ok_status_with_synthetic_data():
    history = _synthetic_hourly_history(["BTC", "ETH", "SOL", "DOGE", "LINK", "AVAX"], n_days=500, seed=99)
    fee = {s: 15.0 for s in history}
    grid = [150, 175, 200, 225, 250]
    returns_by_value = {
        v: wm.simulate_daily_returns(list(history), history, v, RISQUE_TEST, fee) for v in grid
    }
    result = wm.walk_forward_select_and_compare(returns_by_value, 200, is_days=270, oos_days=90)
    assert result["status"] == "OK"
    assert result["windows"] >= 1
    assert set(result["oos_sharpe_by_value"].keys()) <= set(grid)
    assert result["modal_value"] in grid


# ============================================================================================
# --- Application du changement dans les fichiers source ---
# ============================================================================================


def test_apply_recalibration_to_files_replaces_both_files(tmp_path):
    repo = tmp_path / "repo"
    (repo / "bot" / "strategies").mkdir(parents=True)
    (repo / "bot" / "config.py").write_text(
        "SOME_CONST = 1\nREGIME_SMA_DAYS = 200\nOTHER = 2\n", encoding="utf-8"
    )
    (repo / "bot" / "strategies" / "quasi_passif_crypto.py").write_text(
        "REGIME_SMA_DAYS = 200\nX = 1\n", encoding="utf-8"
    )

    changed = wm.apply_recalibration_to_files(str(repo), 200, 225)
    assert set(changed) == {"bot/config.py", "bot/strategies/quasi_passif_crypto.py"}

    config_text = (repo / "bot" / "config.py").read_text(encoding="utf-8")
    strat_text = (repo / "bot" / "strategies" / "quasi_passif_crypto.py").read_text(encoding="utf-8")
    assert "REGIME_SMA_DAYS = 225" in config_text
    assert "SOME_CONST = 1" in config_text  # reste inchangé
    assert "REGIME_SMA_DAYS = 225" in strat_text


def test_apply_recalibration_to_files_refuses_ambiguous_pattern(tmp_path):
    repo = tmp_path / "repo"
    (repo / "bot" / "strategies").mkdir(parents=True)
    (repo / "bot" / "config.py").write_text("REGIME_SMA_DAYS = 150\n", encoding="utf-8")  # pas 200
    (repo / "bot" / "strategies" / "quasi_passif_crypto.py").write_text(
        "REGIME_SMA_DAYS = 200\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="trouvé 0 fois"):
        wm.apply_recalibration_to_files(str(repo), 200, 225)


def test_apply_recalibration_to_files_real_repo_pattern_matches_exactly_once():
    # Sanity : le motif recherché existe bien exactement une fois dans les DEUX fichiers réels du
    # dépôt aujourd'hui (REGIME_SMA_DAYS = 200) -- si ce test casse, c'est que le motif attendu par
    # apply_recalibration_to_files() a dérivé du code réel, ce qui doit être visible immédiatement.
    import re

    for relpath in ("bot/config.py", "bot/strategies/quasi_passif_crypto.py"):
        full = os.path.join(wm._REPO_ROOT, relpath)
        text = open(full, encoding="utf-8").read()
        matches = re.findall(r"(?m)^REGIME_SMA_DAYS = 200\s*$", text)
        assert len(matches) == 1, relpath


# ============================================================================================
# --- Rendu du rapport ---
# ============================================================================================


def test_render_drift_report_smoke():
    rows = [
        {
            "categorie": "active", "strategy_id": "quasi_passif_crypto", "wallet_id": "prudent",
            "n_days_observed": 90, "sharpe_live": 1.1, "sharpe_ref": 1.24,
            "dd_live_pct": 9.0, "dd_ref_pct": 8.0, "verdict": "SURVEILLER",
            "reasons": ["drawdown vécu 9.0% > 1.5x le DD attendu (8.0%)"],
            "antecedent_hors_promotion_rules": True,
        },
    ]
    now = datetime(2026, 7, 27, 22, 0, tzinfo=timezone.utc)
    report = wm.render_drift_report(rows, now, None, "sauté (--skip-recalibration)")
    assert "DRIFT-REPORT.md" in report
    assert "quasi_passif_crypto" in report
    assert "SURVEILLER" in report
    assert "PROMOTION-RULES.md" in report
    assert "Recalibrage non exécuté" in report


def test_render_drift_report_empty_rows():
    now = datetime(2026, 7, 27, 22, 0, tzinfo=timezone.utc)
    report = wm.render_drift_report([], now, None, "OK")
    assert "Aucune stratégie active" in report


def test_render_drift_report_with_applied_recalibration():
    now = datetime(2026, 7, 27, 22, 0, tzinfo=timezone.utc)
    recalibration = {
        "status": "OK", "windows": 3, "current_value": 200, "current_oos_sharpe": 1.0,
        "best_value": 225, "best_oos_sharpe": 1.2, "modal_value": 225,
        "relative_improvement": 0.20, "changed": True, "reason": "amélioration OOS relative 20.0% > seuil 10%",
    }
    report = wm.render_drift_report([], now, recalibration, "OK")
    assert "CHANGEMENT APPLIQUÉ" in report
    assert "225" in report


# ============================================================================================
# --- Orchestration main() en mode dry-run (--skip-push) ---
# ============================================================================================


@pytest.fixture
def fake_repo(tmp_path):
    """Un répertoire minimal (state/wallets/*/*.jsonl vides + un registre synthétique) --
    suffisant pour exercer main() sans toucher au vrai dépôt ni au réseau."""
    repo = tmp_path / "repo"
    for wallet_id in ("prudent", "equilibre", "agressif", "labo"):
        d = repo / "state" / "wallets" / wallet_id
        d.mkdir(parents=True)
        for name in ("decisions.jsonl", "trades.jsonl", "equity.jsonl"):
            (d / name).write_text("", encoding="utf-8")
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    registry = {
        "strategies": [
            {"id": "quasi_passif_crypto", "sharpe_backtest_non_audite": {"prudent_btc_eth": 1.24}, "max_drawdown_pct_backtest": {"prudent_btc_eth": 8.0}},
            {"id": "xs_momentum_sp100", "sharpe_oos": 0.82, "max_drawdown_oos_pct": 50.3},
            {"id": "dual_momentum_multiclasse_etf", "sharpe_oos": 0.65, "max_drawdown_oos_pct": 27.6},
        ]
    }
    (repo / "docs" / "RESEARCH-REGISTRY.json").write_text(json.dumps(registry), encoding="utf-8")
    return repo


def test_main_dry_run_writes_report_and_returns_zero(fake_repo):
    rc = wm.main(
        [
            "--repo-dir", str(fake_repo),
            "--skip-push",
            "--skip-pull",
            "--skip-recalibration",
        ]
    )
    assert rc == 0
    report_path = fake_repo / "docs" / "DRIFT-REPORT.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "quasi_passif_crypto" in content
    assert "xs_momentum_sp100" in content


def test_main_dry_run_skips_recalibration_without_data(fake_repo):
    # --skip-data-refresh (mais PAS --skip-recalibration) : le recalibrage doit se signaler
    # comme sauté faute de données, sans jamais lever d'exception.
    rc = wm.main(
        [
            "--repo-dir", str(fake_repo),
            "--skip-push",
            "--skip-pull",
            "--skip-data-refresh",
        ]
    )
    assert rc == 0
    content = (fake_repo / "docs" / "DRIFT-REPORT.md").read_text(encoding="utf-8")
    assert "DONNEES_INSUFFISANTES" in content or "sauté" in content.lower() or "SAUTÉ" in content


def test_main_handles_missing_registry_gracefully(tmp_path):
    repo = tmp_path / "repo_no_registry"
    for wallet_id in ("prudent", "equilibre", "agressif", "labo"):
        (repo / "state" / "wallets" / wallet_id).mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)
    rc = wm.main(
        [
            "--repo-dir", str(repo),
            "--skip-push",
            "--skip-pull",
            "--skip-recalibration",
        ]
    )
    assert rc == 0
    assert (repo / "docs" / "DRIFT-REPORT.md").exists()


# ============================================================================================
# --- Cohérence avec bot.config (pas de dérive silencieuse de la grille par rapport à la
# --- valeur réellement en production) ---
# ============================================================================================


def test_current_production_value_is_always_in_the_registered_grid():
    import bot.config as config

    grid = wm.RECALIBRATION_GRIDS["quasi_passif_crypto"]["regime_sma_days"]
    assert config.REGIME_SMA_DAYS in grid


def test_recal_wallet_universe_matches_spec():
    from bot.strategies.quasi_passif_crypto import SPEC_UNIVERSE_BY_WALLET

    assert wm.RECAL_WALLET_ID in SPEC_UNIVERSE_BY_WALLET
