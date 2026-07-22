"""Tests de bot/risk/vol_targeting.py — le vol targeting réduit bien les cibles quand la vol
monte, et le cold-start scalar (0.5) s'applique sous 30 points d'historique, pas au-dessus."""

import numpy as np
import pandas as pd
import pytest

from bot.risk.vol_targeting import (
    PERIODS_PER_YEAR,
    annualize_vol,
    compute_vol_scalar,
    ewma_vol_per_period,
    hourly_returns,
    portfolio_vol_annualized,
)


def make_history(n, daily_vol_per_period, seed=0, start_price=100.0):
    """DataFrame `close` synthétique : marche aléatoire de rendements gaussiens iid d'écart-
    type constant `daily_vol_per_period` par période — sert à contrôler précisément la vol
    EWMA résultante dans les tests."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(loc=0.0, scale=daily_vol_per_period, size=n)
    prices = start_price * np.cumprod(1 + rets)
    return pd.DataFrame({"close": prices})


# ---------------------------------------------------------------------------
# annualisation / EWMA de base
# ---------------------------------------------------------------------------

def test_annualize_vol_uses_sqrt_8760():
    assert annualize_vol(0.01) == pytest.approx(0.01 * (PERIODS_PER_YEAR ** 0.5))


def test_ewma_vol_per_period_none_below_two_points():
    assert ewma_vol_per_period(pd.Series([0.01]), halflife_hours=60) is None
    assert ewma_vol_per_period(pd.Series(dtype=float), halflife_hours=60) is None


def test_ewma_vol_higher_for_higher_input_vol():
    calm = make_history(200, 0.001, seed=1)
    turbulent = make_history(200, 0.05, seed=1)
    r_calm = hourly_returns(calm)
    r_turb = hourly_returns(turbulent)
    v_calm = ewma_vol_per_period(r_calm, halflife_hours=60)
    v_turb = ewma_vol_per_period(r_turb, halflife_hours=60)
    assert v_turb > v_calm


# ---------------------------------------------------------------------------
# proxy portefeuille + scalaire : réduit bien les cibles quand la vol monte
# ---------------------------------------------------------------------------

def test_vol_scalar_lower_when_portfolio_vol_higher():
    weights = {"BTC": 0.20, "ETH": 0.20}
    calm_hist = {
        "BTC": make_history(200, 0.001, seed=1),
        "ETH": make_history(200, 0.001, seed=2),
    }
    turbulent_hist = {
        "BTC": make_history(200, 0.05, seed=1),
        "ETH": make_history(200, 0.05, seed=2),
    }

    vol_calm, cold_calm = portfolio_vol_annualized(calm_hist, weights, 60, 30)
    vol_turb, cold_turb = portfolio_vol_annualized(turbulent_hist, weights, 60, 30)
    assert cold_calm is False and cold_turb is False
    assert vol_turb > vol_calm

    scalar_calm = compute_vol_scalar(vol_calm, target_vol_annual=0.275, coldstart=cold_calm, coldstart_scalar=0.5)
    scalar_turb = compute_vol_scalar(vol_turb, target_vol_annual=0.275, coldstart=cold_turb, coldstart_scalar=0.5)

    assert scalar_turb < scalar_calm
    assert scalar_calm <= 1.0
    assert scalar_turb <= 1.0
    assert scalar_turb >= 0.0


def test_vol_scalar_never_exceeds_one_even_with_very_low_vol():
    weights = {"BTC": 0.20}
    hist = {"BTC": make_history(200, 0.0001, seed=5)}
    vol, coldstart = portfolio_vol_annualized(hist, weights, 60, 30)
    scalar = compute_vol_scalar(vol, target_vol_annual=0.275, coldstart=coldstart, coldstart_scalar=0.5)
    assert scalar <= 1.0


# ---------------------------------------------------------------------------
# cold-start : scalar 0.5 sous 30 points, pas au-dessus
# ---------------------------------------------------------------------------

def test_coldstart_flag_true_below_30_points():
    weights = {"BTC": 0.20}
    hist = {"BTC": make_history(20, 0.01, seed=3)}  # 20 bougies -> 19 rendements < 30
    vol, coldstart = portfolio_vol_annualized(hist, weights, halflife_hours=60, min_points=30)
    assert coldstart is True


def test_coldstart_flag_false_at_or_above_30_points():
    weights = {"BTC": 0.20}
    hist = {"BTC": make_history(35, 0.01, seed=3)}  # 34 rendements >= 30
    vol, coldstart = portfolio_vol_annualized(hist, weights, halflife_hours=60, min_points=30)
    assert coldstart is False


def test_coldstart_scalar_halves_result():
    scalar_normal = compute_vol_scalar(0.20, target_vol_annual=0.275, coldstart=False, coldstart_scalar=0.5)
    scalar_coldstart = compute_vol_scalar(0.20, target_vol_annual=0.275, coldstart=True, coldstart_scalar=0.5)
    assert scalar_coldstart == pytest.approx(scalar_normal * 0.5)


def test_missing_history_for_weighted_symbol_marks_coldstart_not_silently_riskfree():
    """Un actif à poids non nul sans historique DU TOUT doit déclencher coldstart (jamais
    traité comme 'vol nulle donc sans risque' de façon silencieuse)."""
    weights = {"BTC": 0.20}
    vol, coldstart = portfolio_vol_annualized({}, weights, halflife_hours=60, min_points=30)
    assert coldstart is True
    assert vol == 0.0


def test_zero_weight_symbols_ignored():
    weights = {"BTC": 0.0, "ETH": 0.20}
    hist = {
        "BTC": make_history(5, 10.0, seed=9),  # vol énorme mais poids nul -> ignoré
        "ETH": make_history(200, 0.01, seed=2),
    }
    vol, coldstart = portfolio_vol_annualized(hist, weights, 60, 30)
    # Pas de coldstart déclenché par BTC (poids nul, jamais examiné)
    assert coldstart is False
