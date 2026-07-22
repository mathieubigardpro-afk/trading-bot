"""Tests d'intégration de bot/risk/manager.py — RiskManager.apply().

Couvre : caps par actif (crypto 25% / action 15%), cap d'exposition brute (80%), no-trade band
(±5%), garde-fou prix indisponible (poids figé), flatten_mode (tout à plat), gel des nouvelles
entrées (bloque le renforcement, pas les sorties), demi-taille sur drawdown, et bout-en-bout du
pipeline avec un scénario réaliste.
"""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from bot.risk import RiskManager


NOW = datetime(2026, 7, 22, 14, 0, 0, tzinfo=timezone.utc)


class FakeQuote:
    def __init__(self, bid, ask, ts=None, source="binance"):
        self.bid = bid
        self.ask = ask
        self.mid = (bid + ask) / 2
        self.ts = ts or NOW.isoformat()
        self.source = source


def flat_history(symbol_prices, n=200, vol=0.001, seed=0):
    """Historique synthétique avec peu de vol pour que le vol_scalar reste proche de 1.0 et
    n'interfère pas avec les tests qui portent sur autre chose que le vol targeting."""
    out = {}
    rng = np.random.default_rng(seed)
    for i, (symbol, price) in enumerate(symbol_prices.items()):
        rets = rng.normal(0.0, vol, size=n)
        prices = price * np.cumprod(1 + rets)
        out[symbol] = pd.DataFrame({"close": prices})
    return out


def make_state(cash_usd=100_000.0, positions=None, equity_peak_usd=None):
    return {
        "cash_usd": cash_usd,
        "positions": positions or {},
        "equity_peak_usd": equity_peak_usd,
        "circuit_breakers": {},
    }


# ---------------------------------------------------------------------------
# Caps par actif : 25% crypto / 15% action
# ---------------------------------------------------------------------------

def test_cap_per_asset_crypto_25pct():
    rm = RiskManager(vol_target_annualized=10.0)  # cible énorme -> vol_scalar ~1, isole le cap
    state = make_state()
    prices = {"BTC": FakeQuote(60_000, 60_010)}
    history = flat_history({"BTC": 60_000})
    cibles_brutes = {"BTC": 0.60}  # bien au-dessus du cap 25%
    finales, reasons = rm.apply(cibles_brutes, state, prices, history, now=NOW)
    assert finales["BTC"] == pytest.approx(0.25, abs=1e-6)
    assert "cap par actif" in reasons["BTC"]


def test_cap_per_asset_equity_15pct():
    rm = RiskManager(vol_target_annualized=10.0)
    state = make_state()
    prices = {"AAPL": FakeQuote(200.0, 200.05)}
    history = flat_history({"AAPL": 200.0})
    cibles_brutes = {"AAPL": 0.50}  # au-dessus du cap action 15%
    finales, reasons = rm.apply(cibles_brutes, state, prices, history, now=NOW)
    assert finales["AAPL"] == pytest.approx(0.15, abs=1e-6)


def test_asset_below_cap_not_clipped():
    rm = RiskManager(vol_target_annualized=10.0)
    state = make_state()
    prices = {"BTC": FakeQuote(60_000, 60_010)}
    history = flat_history({"BTC": 60_000})
    cibles_brutes = {"BTC": 0.10}
    finales, reasons = rm.apply(cibles_brutes, state, prices, history, now=NOW)
    assert finales["BTC"] == pytest.approx(0.10, abs=1e-3)


# ---------------------------------------------------------------------------
# Cap d'exposition brute totale (80%)
# ---------------------------------------------------------------------------

def test_gross_exposure_cap_scales_down_proportionally():
    rm = RiskManager(vol_target_annualized=10.0)
    state = make_state()
    symbols = ["BTC", "ETH", "SOL", "DOGE"]
    prices = {s: FakeQuote(100.0, 100.01) for s in symbols}
    history = flat_history({s: 100.0 for s in symbols})
    # 4 x 25% (cap individuel respecté) = 100% brut > 80% max -> réduction au prorata
    cibles_brutes = {s: 0.25 for s in symbols}
    finales, reasons = rm.apply(cibles_brutes, state, prices, history, now=NOW)
    gross = sum(finales.values())
    assert gross == pytest.approx(0.80, abs=1e-6)
    # Réduction proportionnelle -> tous les poids finaux doivent être égaux entre eux
    values = list(finales.values())
    assert max(values) - min(values) < 1e-9
    assert all("cap d'exposition brute" in reasons[s] for s in symbols)


def test_gross_exposure_within_limit_not_scaled():
    rm = RiskManager(vol_target_annualized=10.0)
    state = make_state()
    prices = {"BTC": FakeQuote(100.0, 100.01), "ETH": FakeQuote(100.0, 100.01)}
    history = flat_history({"BTC": 100.0, "ETH": 100.0})
    cibles_brutes = {"BTC": 0.20, "ETH": 0.20}
    finales, reasons = rm.apply(cibles_brutes, state, prices, history, now=NOW)
    assert finales["BTC"] == pytest.approx(0.20, abs=1e-3)
    assert finales["ETH"] == pytest.approx(0.20, abs=1e-3)


# ---------------------------------------------------------------------------
# No-trade band (±5%)
# ---------------------------------------------------------------------------

def test_no_trade_band_filters_micro_adjustment():
    rm = RiskManager(vol_target_annualized=10.0)
    # Position actuelle : 0.10 (1000 BTC-equiv usd sur 100k, via positions/qty)
    positions = {"BTC": {"qty": 10.0, "prix_moyen": 1000.0}}  # 10*1000=10_000 -> poids 10%
    state = make_state(cash_usd=90_000.0, positions=positions)
    prices = {"BTC": FakeQuote(999.5, 1000.5)}
    history = flat_history({"BTC": 1000.0})
    cibles_brutes = {"BTC": 0.13}  # écart de 3 points < bande 5% -> pas de trade
    finales, reasons = rm.apply(cibles_brutes, state, prices, history, now=NOW)
    assert finales["BTC"] == pytest.approx(0.10, abs=1e-6)
    assert "no-trade band" in reasons["BTC"]


def test_beyond_no_trade_band_trades():
    rm = RiskManager(vol_target_annualized=10.0)
    positions = {"BTC": {"qty": 10.0, "prix_moyen": 1000.0}}
    state = make_state(cash_usd=90_000.0, positions=positions)
    prices = {"BTC": FakeQuote(999.5, 1000.5)}
    history = flat_history({"BTC": 1000.0})
    cibles_brutes = {"BTC": 0.20}  # écart de 10 points > bande 5%
    finales, reasons = rm.apply(cibles_brutes, state, prices, history, now=NOW)
    assert finales["BTC"] == pytest.approx(0.20, abs=1e-3)


# ---------------------------------------------------------------------------
# Garde-fou prix indisponible
# ---------------------------------------------------------------------------

def test_missing_price_freezes_weight():
    rm = RiskManager(vol_target_annualized=10.0)
    positions = {"AVAX": {"qty": 100.0, "prix_moyen": 20.0}}  # 2000$ -> poids 2%
    state = make_state(cash_usd=98_000.0, positions=positions)
    prices = {"AVAX": None}  # prix indisponible ce cycle
    history = {}
    cibles_brutes = {"AVAX": 0.20}  # signal fort, mais prix absent -> aucune action
    finales, reasons = rm.apply(cibles_brutes, state, prices, history, now=NOW)
    assert finales["AVAX"] == pytest.approx(0.02, abs=1e-3)
    assert "indisponible" in reasons["AVAX"]


def test_missing_price_key_entirely_also_freezes():
    rm = RiskManager(vol_target_annualized=10.0)
    positions = {"AVAX": {"qty": 100.0, "prix_moyen": 20.0}}
    state = make_state(cash_usd=98_000.0, positions=positions)
    prices = {}  # symbole absent du dict prices
    history = {}
    cibles_brutes = {"AVAX": 0.20}
    finales, reasons = rm.apply(cibles_brutes, state, prices, history, now=NOW)
    assert finales["AVAX"] == pytest.approx(0.02, abs=1e-3)


# ---------------------------------------------------------------------------
# Circuit breakers intégrés au pipeline complet
# ---------------------------------------------------------------------------

def test_flatten_mode_zeroes_all_targets():
    rm = RiskManager(vol_target_annualized=10.0)
    positions = {
        "BTC": {"qty": 1.0, "prix_moyen": 60_000.0},
        "ETH": {"qty": 10.0, "prix_moyen": 3_000.0},
    }
    # equity actuelle très inférieure au pic -> drawdown > 30%
    state = make_state(cash_usd=0.0, positions=positions, equity_peak_usd=200_000.0)
    prices = {"BTC": FakeQuote(59_990, 60_010), "ETH": FakeQuote(2_995, 3_005)}
    history = flat_history({"BTC": 60_000, "ETH": 3_000})
    cibles_brutes = {"BTC": 0.30, "ETH": 0.30}
    finales, reasons = rm.apply(cibles_brutes, state, prices, history, now=NOW)
    assert finales["BTC"] == 0.0
    assert finales["ETH"] == 0.0
    assert state["circuit_breakers"]["flatten_mode"] is True
    assert state["circuit_breakers"]["manual_review_required"] is True


def test_half_size_breaker_halves_targets_in_full_pipeline():
    rm = RiskManager(vol_target_annualized=10.0)
    positions = {"BTC": {"qty": 1.0, "prix_moyen": 60_000.0}}
    # drawdown ~25% (entre 20% et 30%) -> demi-taille, pas de flatten
    state = make_state(cash_usd=15_000.0, positions=positions, equity_peak_usd=100_000.0)
    prices = {"BTC": FakeQuote(59_990, 60_010)}
    history = flat_history({"BTC": 60_000})
    cibles_brutes = {"BTC": 0.40}
    finales, reasons = rm.apply(cibles_brutes, state, prices, history, now=NOW)
    assert state["circuit_breakers"]["dd_half_size_active"] is True
    assert state["circuit_breakers"]["flatten_mode"] is False
    # 0.40 * vol_scalar(~1) * 0.5(half-size) = 0.20, sous le cap 25% donc pas re-clippé
    assert finales["BTC"] == pytest.approx(0.20, abs=1e-2)


def test_freeze_entries_blocks_increase_but_allows_decrease():
    rm = RiskManager(vol_target_annualized=10.0)
    # Position actuelle : 20_000$ de BTC sur 100_000$ d'équity -> poids actuel 20% (sous le
    # cap 25%, pour isoler l'effet du gel des nouvelles entrées de celui du cap par actif).
    positions = {"BTC": {"qty": 1 / 3, "prix_moyen": 60_000.0}}
    state = make_state(cash_usd=80_000.0, positions=positions)
    equity_now = 80_000.0 + (1 / 3) * 60_000.0  # ~100_000
    # Pré-remplit une fenêtre d'équity montrant une perte de 5% sur 24h -> gel des entrées
    state["circuit_breakers"] = {
        "equity_window_24h": [
            {"ts": NOW.isoformat(), "equity": equity_now / (1 - 0.05)},
        ]
    }
    prices = {"BTC": FakeQuote(59_990, 60_010)}
    history = flat_history({"BTC": 60_000})

    # Tentative de RENFORCEMENT (cible 24% > poids actuel ~20%, toujours sous le cap 25%)
    # -> doit être bloquée par le gel, pas par le cap.
    finales_up, reasons_up = rm.apply({"BTC": 0.24}, state, prices, history, now=NOW)
    assert state["circuit_breakers"]["daily_loss_freeze_until"] is not None
    assert finales_up["BTC"] == pytest.approx(0.20, abs=1e-3)  # inchangé, renforcement bloqué
    assert "gel des nouvelles entrées" in reasons_up["BTC"]

    # Tentative de RÉDUCTION (cible très inférieure) -> doit être autorisée malgré le gel
    state2 = make_state(cash_usd=80_000.0, positions=positions)
    state2["circuit_breakers"] = {
        "equity_window_24h": [
            {"ts": NOW.isoformat(), "equity": equity_now / (1 - 0.05)},
        ]
    }
    finales_down, reasons_down = rm.apply({"BTC": 0.05}, state2, prices, history, now=NOW)
    assert finales_down["BTC"] == pytest.approx(0.05, abs=1e-3)


# ---------------------------------------------------------------------------
# Bout-en-bout
# ---------------------------------------------------------------------------

def test_end_to_end_realistic_scenario():
    rm = RiskManager()
    positions = {
        "BTC": {"qty": 0.5, "prix_moyen": 55_000.0},
        "AAPL": {"qty": 50.0, "prix_moyen": 190.0},
    }
    state = make_state(cash_usd=30_000.0, positions=positions)
    prices = {
        "BTC": FakeQuote(59_990, 60_010),
        "ETH": FakeQuote(2_995, 3_005),
        "AAPL": FakeQuote(199.9, 200.1),
    }
    history = flat_history({"BTC": 60_000, "ETH": 3_000, "AAPL": 200.0}, n=200, vol=0.01)
    cibles_brutes = {"BTC": 0.20, "ETH": 0.15, "AAPL": 0.10}
    finales, reasons = rm.apply(cibles_brutes, state, prices, history, now=NOW)

    assert set(finales.keys()) == {"BTC", "ETH", "AAPL"}
    assert finales["AAPL"] <= 0.15 + 1e-9  # cap action respecté
    assert finales["BTC"] <= 0.25 + 1e-9  # cap crypto respecté
    assert sum(finales.values()) <= 0.80 + 1e-9  # cap brut respecté
    for symbol in finales:
        assert reasons[symbol]  # une raison est toujours renseignée
