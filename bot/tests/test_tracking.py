"""bot/tests/test_tracking.py — bot/reporting/tracking.py:compute_live_metrics()
(suivi de confrontation backtest-vs-vécu, mission point 4, § Labo)."""

from __future__ import annotations

import pytest

from bot.reporting.tracking import compute_live_metrics, update_strategy_state_live_metrics


def _decision(run_id, symbol, strategy_signals):
    return {"run_id": run_id, "symbol": symbol, "strategy_signals": strategy_signals}


def _trade(run_id, symbol, side, realized_pnl_usd=None):
    rec = {"run_id": run_id, "symbol": symbol, "side": side}
    if realized_pnl_usd is not None:
        rec["realized_pnl_usd"] = realized_pnl_usd
    return rec


def _equity(run_id, exposures):
    return {"run_id": run_id, "exposures": exposures}


# ------------------------------------------------------------------------------------------
# Cas de base : aucune donnée
# ------------------------------------------------------------------------------------------


def test_compute_live_metrics_empty_journaux_returns_zeroed_metrics():
    metrics = compute_live_metrics(None, None, "quasi_passif_crypto")
    assert metrics["n_trades"] == 0
    assert metrics["realized_pnl_cumulative_usd"] == 0.0
    assert metrics["avg_exposure_pct"] == 0.0
    assert metrics["n_cycles_observed"] == 0
    assert metrics["first_run_id"] is None
    assert metrics["last_run_id"] is None
    assert metrics["wallet_id"] is None


def test_compute_live_metrics_unknown_strategy_id_returns_zeroed_metrics():
    journaux = {
        "decisions": [_decision("2026-07-23T10", "BTC", {"quasi_passif_crypto": 0.2})],
        "trades": [_trade("2026-07-23T10", "BTC", "BUY")],
        "equity": [_equity("2026-07-23T10", {"BTC": 0.2})],
    }
    metrics = compute_live_metrics({"wallet_id": "agressif"}, journaux, "strategy_inconnue")
    assert metrics["n_trades"] == 0
    assert metrics["n_cycles_observed"] == 0


# ------------------------------------------------------------------------------------------
# Attribution trades/exposition par (run_id, symbole) via decisions.strategy_signals
# ------------------------------------------------------------------------------------------


def test_compute_live_metrics_counts_trades_and_pnl_attributed_via_decisions():
    journaux = {
        "decisions": [
            _decision("2026-07-23T10", "BTC", {"quasi_passif_crypto": 0.20}),
            _decision("2026-07-23T10", "ETH", {"quasi_passif_crypto": 0.10}),
            # AAPL ce run-là : attribué à une AUTRE stratégie -- ne doit jamais être compté.
            _decision("2026-07-23T10", "AAPL", {"xs_momentum_sp100": 0.05}),
            _decision("2026-07-23T11", "BTC", {"quasi_passif_crypto": 0.0}),
        ],
        "trades": [
            _trade("2026-07-23T10", "BTC", "BUY"),
            _trade("2026-07-23T10", "AAPL", "BUY"),  # pas attribué à quasi_passif_crypto ce run
            _trade("2026-07-23T11", "BTC", "SELL", realized_pnl_usd=-4.5),
        ],
        "equity": [
            _equity("2026-07-23T10", {"BTC": 0.18, "ETH": 0.09, "AAPL": 0.05}),
            _equity("2026-07-23T11", {"ETH": 0.09}),  # BTC vendu, plus d'exposition ce run
        ],
    }

    metrics = compute_live_metrics({"wallet_id": "agressif"}, journaux, "quasi_passif_crypto")

    # 2 trades imputables : BTC BUY à T10, BTC SELL à T11 (AAPL exclu -- attribué à une autre
    # stratégie ce run précis).
    assert metrics["n_trades"] == 2
    assert metrics["realized_pnl_cumulative_usd"] == pytest.approx(-4.5)
    assert metrics["n_cycles_observed"] == 2  # T10 (BTC+ETH) et T11 (BTC)
    assert metrics["first_run_id"] == "2026-07-23T10"
    assert metrics["last_run_id"] == "2026-07-23T11"
    assert metrics["wallet_id"] == "agressif"

    # exposition moyenne : T10 -> BTC+ETH = 0.27 ; T11 -> BTC(absent, 0) = 0.0 -> moyenne 0.135
    assert metrics["avg_exposure_pct"] == pytest.approx((0.27 + 0.0) / 2)
    assert metrics["n_cycles_with_exposure"] == 2


def test_compute_live_metrics_sums_realized_pnl_only_on_sell_side():
    journaux = {
        "decisions": [_decision("2026-07-23T10", "BTC", {"s": 0.2})],
        "trades": [
            _trade("2026-07-23T10", "BTC", "BUY", realized_pnl_usd=999.0),  # jamais compté (BUY)
            _trade("2026-07-23T10", "BTC", "SELL", realized_pnl_usd=10.0),
            _trade("2026-07-23T10", "BTC", "SELL", realized_pnl_usd=-3.0),
        ],
        "equity": [],
    }
    metrics = compute_live_metrics(None, journaux, "s")
    assert metrics["n_trades"] == 3
    assert metrics["realized_pnl_cumulative_usd"] == pytest.approx(7.0)
    assert metrics["avg_exposure_pct"] == 0.0
    assert metrics["n_cycles_with_exposure"] == 0


def test_compute_live_metrics_missing_journal_keys_are_tolerated():
    metrics = compute_live_metrics({"wallet_id": "prudent"}, {}, "quasi_passif_crypto")
    assert metrics["n_trades"] == 0
    assert metrics["avg_exposure_pct"] == 0.0
    metrics2 = compute_live_metrics({"wallet_id": "prudent"}, {"decisions": None}, "quasi_passif_crypto")
    assert metrics2["n_trades"] == 0


def test_compute_live_metrics_reusable_for_an_incubating_candidate_id():
    """Même fonction, aucune hypothèse sur le nom -- utilisable telle quelle pour une candidate
    en incubation (`bot.config.INCUBATING_STRATEGIES[*]['id']`) comme pour une stratégie de
    production."""
    journaux = {
        "decisions": [_decision("2026-07-23T20", "ETH", {"candidate_x": 0.4})],
        "trades": [_trade("2026-07-23T20", "ETH", "BUY")],
        "equity": [_equity("2026-07-23T20", {"ETH": 0.4})],
    }
    metrics = compute_live_metrics({"wallet_id": "labo"}, journaux, "candidate_x")
    assert metrics["strategy_id"] == "candidate_x"
    assert metrics["wallet_id"] == "labo"
    assert metrics["n_trades"] == 1
    assert metrics["avg_exposure_pct"] == pytest.approx(0.4)


# ------------------------------------------------------------------------------------------
# update_strategy_state_live_metrics : mutation en place, convention strategy_state
# ------------------------------------------------------------------------------------------


def test_update_strategy_state_live_metrics_persists_in_place():
    state = {"strategy_state": {}}
    journaux = {
        "decisions": [_decision("2026-07-23T20", "ETH", {"candidate_x": 0.4})],
        "trades": [_trade("2026-07-23T20", "ETH", "BUY")],
        "equity": [_equity("2026-07-23T20", {"ETH": 0.4})],
    }

    metrics = update_strategy_state_live_metrics(state, journaux, "candidate_x")

    assert state["strategy_state"]["candidate_x"]["live_metrics"] == metrics
    assert metrics["n_trades"] == 1


def test_update_strategy_state_live_metrics_creates_missing_strategy_state_key():
    state: dict = {}  # pas de "strategy_state" du tout (ex. state.json ancien schéma)
    metrics = update_strategy_state_live_metrics(state, {}, "quasi_passif_crypto")
    assert state["strategy_state"]["quasi_passif_crypto"]["live_metrics"] == metrics


def test_update_strategy_state_live_metrics_preserves_other_strategy_state_keys():
    state = {
        "strategy_state": {
            "quasi_passif_crypto": {"missing_data_cycles": {"XLM": 3}},
        }
    }
    update_strategy_state_live_metrics(state, {}, "quasi_passif_crypto")
    assert state["strategy_state"]["quasi_passif_crypto"]["missing_data_cycles"] == {"XLM": 3}
    assert "live_metrics" in state["strategy_state"]["quasi_passif_crypto"]


def test_update_strategy_state_live_metrics_tolerates_non_dict_state():
    metrics = update_strategy_state_live_metrics(None, {}, "quasi_passif_crypto")  # ne lève pas
    assert metrics["strategy_id"] == "quasi_passif_crypto"
