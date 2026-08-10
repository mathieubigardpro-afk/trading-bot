"""backtest/tests/test_quasi_passif_backtest.py — tests de la candidate
`quasi_passif_crypto_wf_retest` (`backtest/results/quasi_passif_crypto_wf_retest/SPEC.md`).

Quatre garanties couvertes, directement issues de la mission :
  1. Égalité numérique entre la voie "vectorisée" de `backtest/strategies/quasi_passif.py`
     (agrégation journalière précalculée UNE FOIS puis tronquée par date) et un appel DIRECT
     des helpers de production (`bot.strategies.quasi_passif_crypto`) sur des données brutes
     tronquées à la même date causale -- sur un ÉCHANTILLON de dates de données RÉELLES
     (`/tmp/md/data/crypto`, skip si absent, jamais de dépendance réseau).
  2. Causalité : modifier les données STRICTEMENT APRÈS une date de décision ne change AUCUN
     poids décidé à cette date ou avant (fixture synthétique, aucune donnée réelle requise).
  3. Constance intra-journalière : le poids décidé reste rigoureusement constant entre deux
     instants de décision quotidienne consécutifs (fixture synthétique).
  4. Sanity du runner : `generate_weight_decisions` -> `engine.simulate_segment` ->
     `engine.concatenate_segments` -> `backtest.metrics` s'enchaînent sans erreur ni NaN sur un
     petit sous-ensemble walk-forward synthétique (2 fenêtres 9m IS/3m OOS).
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest import data_hourly as bt_data
from backtest import engine, metrics as bt_metrics, risk_overlay
from backtest.strategies import quasi_passif as qp
from bot.strategies.quasi_passif_crypto import (
    REGIME_SMA_DAYS,
    _basket_vol_annualized,
    _daily_closes,
    _is_trend_on,
)

REAL_DATA_DIR = Path(os.environ.get("QUASI_PASSIF_DATA_DIR", "/tmp/md/data/crypto"))
_REAL_DATA_AVAILABLE = REAL_DATA_DIR.exists() and any(REAL_DATA_DIR.glob("*.csv.gz"))

RISK_PROFILE_TEST = {
    "vol_target_annualized": 0.20,
    "gross_exposure_max": 0.70,
    "cap_per_asset": 0.25,
    "vol_ewma_halflife_hours": 60.0,
}


# ------------------------------------------------------------------------------------------
# Fixtures synthétiques (aucune dépendance réseau/disque)
# ------------------------------------------------------------------------------------------


def _synthetic_universe(n_days: int = 260, seed: int = 42) -> dict:
    """Univers synthétique à 2 symboles couvrant > REGIME_SMA_DAYS (200) jours calendaires
    COMPLETS, avec une tendance haussière franche pour "A" (garantit `trend_on=True` une fois
    le warm-up passé) et un bruit pur pour "B" (contrôle, souvent flat/off)."""
    rng = np.random.default_rng(seed)
    n_hours = n_days * 24
    idx = pd.date_range("2022-01-01", periods=n_hours, freq="h")
    trend_a = np.linspace(0, 40, n_hours)
    close_a = 100 + trend_a + rng.normal(0, 0.5, n_hours)
    close_b = 50 + rng.normal(0, 1.0, n_hours)
    raw = {
        "A": pd.DataFrame(
            {
                "open": close_a,
                "high": close_a * 1.001,
                "low": close_a * 0.999,
                "close": close_a,
                "volume": 1.0,
            },
            index=idx,
        ),
        "B": pd.DataFrame(
            {
                "open": close_b,
                "high": close_b * 1.001,
                "low": close_b * 0.999,
                "close": close_b,
                "volume": 1.0,
            },
            index=idx,
        ),
    }
    calendar = pd.DatetimeIndex(idx)
    return {"raw": raw, "calendar": calendar}


# ------------------------------------------------------------------------------------------
# 1. Égalité numérique voie vectorisée vs helper de production (données réelles, skip si absentes)
# ------------------------------------------------------------------------------------------


@pytest.mark.skipif(
    not _REAL_DATA_AVAILABLE,
    reason=f"données horaires réelles absentes sous {REAL_DATA_DIR} (extraction market-data non disponible dans cet environnement)",
)
def test_vectorized_daily_matches_production_helper_on_real_data_sample():
    universe = ["BTC", "ETH", "SOL", "BNB", "XRP", "XLM", "HBAR", "ICP", "OP", "UNI", "FIL"]
    raw = bt_data.load_universe_raw(REAL_DATA_DIR, universe)
    calendar = bt_data.build_calendar(raw)
    risk_profile = {
        "vol_target_annualized": 0.35,
        "gross_exposure_max": 0.90,
        "cap_per_asset": 0.30,
        "vol_ewma_halflife_hours": 60.0,
    }

    weights_fast = qp.generate_weight_decisions(raw, calendar, universe, risk_profile)

    positions = qp.decision_positions(calendar)
    rng = np.random.default_rng(0)
    # échantillon de 20 dates de décision, hors tout début de warm-up (pour couvrir des cas où
    # l'éligibilité est réellement non triviale).
    sample_positions = rng.choice(positions[250:], size=20, replace=False)

    for pos in sorted(sample_positions):
        t = calendar[pos]
        # --- appel DIRECT des helpers de production, sur données brutes tronquées < t --------
        eligible = []
        for s in universe:
            history_cutoff = raw[s][raw[s].index < t]
            daily = _daily_closes(history_cutoff)
            if _is_trend_on(daily, REGIME_SMA_DAYS) is True:
                eligible.append(s)

        expected = {s: 0.0 for s in universe}
        if eligible:
            history_cutoff_eligible = {s: raw[s][raw[s].index < t] for s in eligible}
            vol_annual = _basket_vol_annualized(
                eligible, history_cutoff_eligible, risk_profile["vol_ewma_halflife_hours"]
            )
            if vol_annual is not None:
                gross = max(
                    0.0,
                    min(risk_profile["gross_exposure_max"], risk_profile["vol_target_annualized"] / vol_annual),
                )
                per_asset = min(gross / len(eligible), risk_profile["cap_per_asset"])
                for s in eligible:
                    expected[s] = per_asset

        actual = weights_fast.loc[t]
        for s in universe:
            assert actual[s] == pytest.approx(expected[s], abs=1e-12), (
                f"écart à {t} pour {s} : voie vectorisée={actual[s]!r} vs helper de "
                f"production direct={expected[s]!r}"
            )


# ------------------------------------------------------------------------------------------
# 2. Causalité : perturber les données APRÈS une date de décision ne change pas les poids
#    décidés à cette date (ou avant).
# ------------------------------------------------------------------------------------------


def test_causality_future_perturbation_does_not_change_past_decisions():
    fixture = _synthetic_universe(n_days=280, seed=11)
    raw = fixture["raw"]
    calendar = fixture["calendar"]
    universe = ["A", "B"]

    weights_original = qp.generate_weight_decisions(raw, calendar, universe, RISK_PROFILE_TEST)

    # coupure à mi-parcours (largement après le warm-up SMA200 -- 200 jours = 4800 heures).
    positions = qp.decision_positions(calendar)
    cut_pos = int(positions[len(positions) // 2])
    t_cut = calendar[cut_pos]

    perturbed_raw = {}
    rng = np.random.default_rng(99)
    for sym, df in raw.items():
        df2 = df.copy()
        mask_future = df2.index > t_cut
        n_future = int(mask_future.sum())
        # Perturbation énorme et déstabilisante, strictement APRÈS t_cut uniquement.
        df2.loc[mask_future, "close"] = df2.loc[mask_future, "close"] * (
            1.0 + rng.normal(0, 0.9, size=n_future)
        )
        perturbed_raw[sym] = df2

    weights_perturbed = qp.generate_weight_decisions(perturbed_raw, calendar, universe, RISK_PROFILE_TEST)

    pd.testing.assert_frame_equal(
        weights_original.loc[:t_cut], weights_perturbed.loc[:t_cut]
    )
    # Contrôle de non-trivialité : la perturbation doit réellement changer quelque chose après
    # t_cut, sinon le test passerait même avec un moteur cassé qui ignorerait tout, tout le temps.
    assert not weights_original.loc[calendar[cut_pos + 1] :].equals(
        weights_perturbed.loc[calendar[cut_pos + 1] :]
    )


# ------------------------------------------------------------------------------------------
# 3. Constance intra-journalière des poids décidés
# ------------------------------------------------------------------------------------------


def test_weights_constant_between_daily_decisions():
    fixture = _synthetic_universe(n_days=260, seed=7)
    raw = fixture["raw"]
    calendar = fixture["calendar"]
    universe = ["A", "B"]

    weights = qp.generate_weight_decisions(raw, calendar, universe, RISK_PROFILE_TEST)
    positions = qp.decision_positions(calendar)

    assert len(positions) > 5, "fixture trop courte pour couvrir plusieurs jours de décision"

    for k in range(len(positions) - 1):
        start = positions[k]
        end = positions[k + 1]  # exclusif : les heures [start, end) doivent porter le MÊME poids
        block = weights.iloc[start:end]
        for sym in universe:
            assert block[sym].nunique() == 1, (
                f"poids de {sym!r} non constant entre deux décisions quotidiennes "
                f"(positions {start}:{end})"
            )

    # Aucun NaN nulle part (poids 0.0 explicite pour "inéligible", jamais NaN).
    assert not bool(weights.isna().any().any())


# ------------------------------------------------------------------------------------------
# 4. Sanity du runner walk-forward sur un petit sous-ensemble synthétique
# ------------------------------------------------------------------------------------------


def test_runner_walkforward_sanity_small_subset():
    fixture = _synthetic_universe(n_days=560, seed=3)  # >= 9m IS + 3m OOS x 2 fenêtres
    raw = fixture["raw"]
    calendar = fixture["calendar"]
    universe = ["A", "B"]

    weights_decided = qp.generate_weight_decisions(raw, calendar, universe, RISK_PROFILE_TEST)
    assert weights_decided.shape == (len(calendar), len(universe))
    assert not bool(weights_decided.isna().any().any())

    aligned = {sym: raw[sym] for sym in universe}
    opens = bt_data.opens_panel(aligned, universe)
    closes = bt_data.closes_panel(aligned, universe)

    windows = engine.generate_walk_forward_windows(calendar, is_months=9, oos_months=3, step_months=3)
    assert len(windows) >= 1

    sim_kwargs = dict(
        vol_ewma_halflife_days=risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
        vol_periods_per_year=risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR,
        vol_target_annualized=RISK_PROFILE_TEST["vol_target_annualized"],
        no_trade_band=0.05,
    )

    segments = []
    for w in windows:
        is_start_idx_safe = max(1, w.is_start_idx)
        sel = engine.select_params_via_is(
            lambda params: weights_decided,
            calendar,
            opens,
            closes,
            cost_bps=25.0,
            is_start_idx=is_start_idx_safe,
            is_end_idx=w.is_end_idx,
            param_grid=[{}],
            sim_kwargs=sim_kwargs,
        )
        assert sel.chosen_params == {}
        seg = engine.simulate_segment(
            calendar, weights_decided, opens, closes, w.oos_start_idx, w.oos_end_idx, cost_bps=25.0, **sim_kwargs
        )
        assert not bool(seg.returns.isna().any())
        segments.append(seg)

    concatenated = engine.concatenate_segments(segments)
    dsr = bt_metrics.deflated_sharpe_ratio(concatenated.returns, trials_k=53)
    assert isinstance(dsr.dsr, float)
    summary = {
        "sharpe": bt_metrics.sharpe_ratio(concatenated.returns, periods_per_year=8760.0),
        "n_trades_closed": len(concatenated.trades_closed),
    }
    assert not np.isnan(summary["sharpe"]) or len(concatenated.returns) < 2
    assert summary["n_trades_closed"] >= 0
