"""Tests de `bot.feeds.fx` : get_fx_rate() — deux sources gratuites + repli dernier taux
connu, jamais de taux inventé."""

from __future__ import annotations

import requests

import bot.feeds.fx as fx_mod


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json_data


def _boom(*a, **k):
    raise requests.ConnectionError("réseau bloqué (bac à sable)")


def test_get_fx_rate_uses_frankfurter_when_available(monkeypatch):
    monkeypatch.setattr(
        fx_mod._session, "get",
        lambda url, **k: FakeResponse({"amount": 1.0, "base": "EUR", "rates": {"USD": 1.0842}}),
    )
    rate = fx_mod.get_fx_rate("EURUSD")
    assert rate is not None
    assert rate.rate == 1.0842
    assert rate.source == "frankfurter"
    assert rate.stale is False


def test_get_fx_rate_falls_back_to_open_er_api_on_frankfurter_failure(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, **k):
        calls["n"] += 1
        if "frankfurter" in url:
            raise requests.ConnectionError("boom")
        return FakeResponse({"result": "success", "rates": {"USD": 1.09, "EUR": 1.0}})

    monkeypatch.setattr(fx_mod._session, "get", fake_get)
    rate = fx_mod.get_fx_rate("EURUSD")
    assert rate is not None
    assert rate.rate == 1.09
    assert rate.source == "open_er_api"
    assert rate.stale is False
    assert calls["n"] == 2


def test_get_fx_rate_falls_back_to_last_known_when_both_sources_fail(monkeypatch):
    monkeypatch.setattr(fx_mod._session, "get", _boom)
    rate = fx_mod.get_fx_rate("EURUSD", last_known={"rate": 1.075, "ts": "2026-07-21T10:00:00+00:00"})
    assert rate is not None
    assert rate.rate == 1.075
    assert rate.ts == "2026-07-21T10:00:00+00:00"
    assert rate.source == "dernier_taux_connu"
    assert rate.stale is True


def test_get_fx_rate_returns_none_when_everything_fails_and_no_last_known(monkeypatch):
    monkeypatch.setattr(fx_mod._session, "get", _boom)
    assert fx_mod.get_fx_rate("EURUSD", last_known=None) is None
    assert fx_mod.get_fx_rate("EURUSD", last_known={"rate": None}) is None
    assert fx_mod.get_fx_rate("EURUSD", last_known={}) is None


def test_get_fx_rate_rejects_invalid_or_negative_rates(monkeypatch):
    monkeypatch.setattr(
        fx_mod._session, "get",
        lambda url, **k: FakeResponse({"rates": {"USD": -1.0}}) if "frankfurter" in url
        else FakeResponse({"result": "success", "rates": {"USD": "not_a_number"}}),
    )
    assert fx_mod.get_fx_rate("EURUSD") is None


def test_get_fx_rate_rejects_open_er_api_non_success_result(monkeypatch):
    def fake_get(url, **k):
        if "frankfurter" in url:
            raise requests.ConnectionError("boom")
        return FakeResponse({"result": "error"})

    monkeypatch.setattr(fx_mod._session, "get", fake_get)
    assert fx_mod.get_fx_rate("EURUSD") is None


def test_get_fx_rate_unsupported_pair_raises_value_error():
    import pytest
    with pytest.raises(ValueError):
        fx_mod.get_fx_rate("GBPUSD")


def test_get_fx_rate_http_error_status_treated_as_failure(monkeypatch):
    monkeypatch.setattr(
        fx_mod._session, "get",
        lambda url, **k: FakeResponse({}, status_code=503),
    )
    monkeypatch.setattr(fx_mod, "_fetch_open_er_api", lambda: None)
    assert fx_mod.get_fx_rate("EURUSD") is None
