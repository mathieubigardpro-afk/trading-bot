"""Tests de bot/risk/circuit_breakers.py — chaque breaker se déclenche au bon seuil et pas
avant (calibrage AGRESSIF : 4% / 20% / 30%, voir bot/risk/config_fallback.py)."""

from datetime import datetime, timedelta, timezone

import pytest

from bot.risk.circuit_breakers import (
    compute_daily_loss_pct,
    compute_drawdown_pct,
    default_breaker_state,
    evaluate_breakers,
)

NOW = datetime(2026, 7, 22, 14, 0, 0, tzinfo=timezone.utc)

DEFAULTS = dict(
    daily_loss_freeze_pct=0.04,
    daily_loss_freeze_hours=24,
    consecutive_losses_trigger=5,
    cooldown_hours=24,
    dd_half_size_pct=0.20,
    dd_flatten_pct=0.30,
)


def make_window(now, hours_ago, equity):
    return {"ts": (now - timedelta(hours=hours_ago)).isoformat(), "equity": equity}


# ---------------------------------------------------------------------------
# Drawdown : demi-taille à 20%, flatten à 30%, pas avant
# ---------------------------------------------------------------------------

def test_drawdown_pct_formula():
    assert compute_drawdown_pct(90_000, 100_000) == pytest.approx(0.10)
    assert compute_drawdown_pct(100_000, 100_000) == 0.0
    assert compute_drawdown_pct(0, 0) == 0.0  # pas de pic valide


def test_half_size_not_triggered_at_exactly_20pct():
    cb_in = default_breaker_state()
    equity_peak = 100_000.0
    equity_now = equity_peak * (1 - 0.20)  # exactement 20% de DD
    cb_out, flags = evaluate_breakers(cb_in, NOW, equity_now, equity_peak, **DEFAULTS)
    assert flags["half_size"] is False
    assert flags["flatten"] is False


def test_half_size_triggered_just_above_20pct():
    cb_in = default_breaker_state()
    equity_peak = 100_000.0
    equity_now = equity_peak * (1 - 0.201)  # 20.1% de DD
    cb_out, flags = evaluate_breakers(cb_in, NOW, equity_now, equity_peak, **DEFAULTS)
    assert flags["half_size"] is True
    assert flags["flatten"] is False
    assert cb_out["dd_half_size_active"] is True


def test_flatten_not_triggered_at_exactly_30pct():
    cb_in = default_breaker_state()
    equity_peak = 100_000.0
    equity_now = equity_peak * (1 - 0.30)
    cb_out, flags = evaluate_breakers(cb_in, NOW, equity_now, equity_peak, **DEFAULTS)
    assert flags["flatten"] is False
    assert flags["half_size"] is True  # au-dessus de 20%, en dessous/à 30%


def test_flatten_triggered_just_above_30pct():
    cb_in = default_breaker_state()
    equity_peak = 100_000.0
    equity_now = equity_peak * (1 - 0.301)
    cb_out, flags = evaluate_breakers(cb_in, NOW, equity_now, equity_peak, **DEFAULTS)
    assert flags["flatten"] is True
    assert flags["manual_review"] is True
    assert cb_out["flatten_mode"] is True
    assert cb_out["manual_review_required"] is True


def test_flatten_mode_is_sticky_across_cycles_even_if_equity_recovers():
    """Une fois flatten_mode activé, seul un humain peut le lever — même si l'équity remonte
    au-dessus du seuil au cycle suivant, flatten_mode doit rester True."""
    cb_in = default_breaker_state()
    equity_peak = 100_000.0
    equity_now_crash = equity_peak * (1 - 0.35)
    cb_after_crash, flags_crash = evaluate_breakers(
        cb_in, NOW, equity_now_crash, equity_peak, **DEFAULTS
    )
    assert cb_after_crash["flatten_mode"] is True

    # Cycle suivant : équity remonte nettement au-dessus du seuil flatten (mais pas au pic)
    later = NOW + timedelta(hours=1)
    equity_now_recovered = equity_peak * (1 - 0.05)
    cb_after_recovery, flags_recovery = evaluate_breakers(
        cb_after_crash, later, equity_now_recovered, equity_peak, **DEFAULTS
    )
    assert flags_recovery["flatten"] is True
    assert cb_after_recovery["flatten_mode"] is True
    assert cb_after_recovery["manual_review_required"] is True


# ---------------------------------------------------------------------------
# Perte 24h glissante : gel à 4%, pas avant
# ---------------------------------------------------------------------------

def test_daily_loss_pct_formula():
    window = [make_window(NOW, 24, 100_000), make_window(NOW, 0, 95_000)]
    assert abs(compute_daily_loss_pct(window, 95_000) - 0.05) < 1e-9


def test_daily_loss_freeze_not_triggered_at_exactly_4pct():
    cb_in = default_breaker_state()
    cb_in["equity_window_24h"] = [make_window(NOW, 23, 100_000)]
    equity_now = 100_000 * (1 - 0.04)
    cb_out, flags = evaluate_breakers(cb_in, NOW, equity_now, equity_now, **DEFAULTS)
    assert flags["freeze_entries"] is False
    assert cb_out["daily_loss_freeze_until"] is None


def test_daily_loss_freeze_triggered_just_above_4pct():
    cb_in = default_breaker_state()
    cb_in["equity_window_24h"] = [make_window(NOW, 23, 100_000)]
    equity_now = 100_000 * (1 - 0.041)
    cb_out, flags = evaluate_breakers(cb_in, NOW, equity_now, equity_now, **DEFAULTS)
    assert flags["freeze_entries"] is True
    assert cb_out["daily_loss_freeze_until"] is not None
    freeze_until = datetime.fromisoformat(cb_out["daily_loss_freeze_until"])
    assert freeze_until > NOW
    assert freeze_until <= NOW + timedelta(hours=24, minutes=1)


def test_daily_loss_freeze_expires_after_24h():
    cb_in = default_breaker_state()
    cb_in["daily_loss_freeze_until"] = (NOW - timedelta(minutes=1)).isoformat()
    cb_in["equity_window_24h"] = [make_window(NOW, 1, 100_000)]
    cb_out, flags = evaluate_breakers(cb_in, NOW, 100_000, 100_000, **DEFAULTS)
    assert flags["freeze_entries"] is False
    assert cb_out["daily_loss_freeze_until"] is None


def test_daily_loss_insufficient_window_never_freezes():
    """Moins de 2 points dans la fenêtre -> pas de perte détectable, jamais de gel injustifié."""
    cb_in = default_breaker_state()
    cb_out, flags = evaluate_breakers(cb_in, NOW, 10_000.0, 10_000.0, **DEFAULTS)
    assert flags["freeze_entries"] is False
    assert flags["daily_loss_pct"] == 0.0


# ---------------------------------------------------------------------------
# Cooldown pertes consécutives : déclenché à 5, pas à 4
# ---------------------------------------------------------------------------

def test_cooldown_not_triggered_at_4_consecutive_losses():
    cb_in = default_breaker_state()
    cb_in["consecutive_losses"] = 4
    cb_out, flags = evaluate_breakers(cb_in, NOW, 100_000.0, 100_000.0, **DEFAULTS)
    assert flags["freeze_entries"] is False
    assert cb_out["cooldown_until"] is None


def test_cooldown_triggered_at_5_consecutive_losses():
    cb_in = default_breaker_state()
    cb_in["consecutive_losses"] = 5
    cb_out, flags = evaluate_breakers(cb_in, NOW, 100_000.0, 100_000.0, **DEFAULTS)
    assert flags["freeze_entries"] is True
    assert flags["cooldown_active"] is True
    assert cb_out["cooldown_until"] is not None


def test_cooldown_expires_and_resets_counter():
    cb_in = default_breaker_state()
    cb_in["consecutive_losses"] = 5
    cb_in["cooldown_until"] = (NOW - timedelta(minutes=1)).isoformat()
    cb_out, flags = evaluate_breakers(cb_in, NOW, 100_000.0, 100_000.0, **DEFAULTS)
    assert flags["cooldown_active"] is False
    assert cb_out["cooldown_until"] is None
    assert cb_out["consecutive_losses"] == 0
