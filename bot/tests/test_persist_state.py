"""Tests de bot/persist/state.py : init_state, load_state/save_state, validation de schéma
stricte, chaîne de hash, idempotence."""

from __future__ import annotations

import copy
import json
import os

import pytest

from bot.persist.state import (
    GENESIS_HASH,
    StateValidationError,
    compute_state_hash,
    init_state,
    is_run_already_done,
    load_state,
    save_state,
    validate_schema,
)


# --------------------------------------------------------------------------------------
# init_state / valid round-trip
# --------------------------------------------------------------------------------------


def test_init_state_shape():
    state = init_state()
    validate_schema(state)  # ne doit jamais lever
    # Multi-wallets : un wallet naît NON INITIALISÉ (cash_usd=0.0, fx.initial_rate=None) —
    # le capital réel n'est fixé qu'au premier cycle où un taux EUR/USD est disponible
    # (jamais inventé ici), voir docs/ARCHITECTURE.md §9.1.
    assert state["cash_usd"] == 0.0
    assert state["positions"] == {}
    assert state["last_run_id"] is None
    assert state["equity_peak_usd"] == 0.0
    assert state["state_hash_prev"] == GENESIS_HASH
    assert state["wallet_id"] == "default"
    assert state["initial_eur"] == 1000.0
    assert state["fx"]["initial_rate"] is None
    assert state["fx"]["last_rate"] is None
    cb = state["circuit_breakers"]
    assert cb["flatten_mode"] is False
    assert cb["manual_review_required"] is False
    assert cb["consecutive_losses"] == 0
    assert state["trade_history_for_breakers"] == []


def test_init_state_with_wallet_id_and_capital():
    state = init_state("prudent", 1000.0)
    validate_schema(state)
    assert state["wallet_id"] == "prudent"
    assert state["initial_eur"] == 1000.0
    assert state["cash_usd"] == 0.0
    assert state["fx"] == {
        "initial_rate": None, "last_rate": None, "last_rate_ts": None,
        "last_rate_source": None, "last_rate_stale": False,
    }


def test_load_state_absent_file_returns_init_state(tmp_path):
    path = tmp_path / "state.json"
    assert not path.exists()
    state = load_state(str(path))
    assert state == init_state()


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = init_state()
    state["cash_usd"] = 41235.77
    state["positions"] = {"BTC": {"qty": 0.5, "prix_moyen": 61000.0}}
    state["last_run_id"] = "2026-07-22T14"
    save_state(state, str(path))

    reloaded = load_state(str(path))
    assert reloaded == state


def test_save_state_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "state.json"
    save_state(init_state(), str(path))
    assert path.exists()


def test_save_state_rejects_invalid_state(tmp_path):
    path = tmp_path / "state.json"
    bad = init_state()
    del bad["cash_usd"]
    with pytest.raises(StateValidationError):
        save_state(bad, str(path))
    # rien n'a été écrit sur disque
    assert not path.exists()


# --------------------------------------------------------------------------------------
# Atomicité de save_state (tmp + os.replace)
# --------------------------------------------------------------------------------------


def test_save_state_atomic_crash_between_tmp_and_replace(tmp_path, monkeypatch):
    """Simule un crash après l'écriture du fichier tmp mais avant os.replace() : le fichier
    final ne doit pas exister/être modifié, et le tmp doit contenir l'écriture complète."""
    path = tmp_path / "state.json"

    # Un état "précédent" légitime déjà présent sur disque.
    previous = init_state()
    previous["cash_usd"] = 99000.0
    save_state(previous, str(path))
    assert path.exists()

    original_replace = os.replace

    def _boom(*args, **kwargs):
        raise OSError("crash simulé entre tmp et rename")

    monkeypatch.setattr(os, "replace", _boom)

    new_state = init_state()
    new_state["cash_usd"] = 12345.0
    with pytest.raises(OSError):
        save_state(new_state, str(path))

    monkeypatch.setattr(os, "replace", original_replace)

    # Le fichier final n'a jamais été touché par l'écriture avortée.
    with open(path, "r", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["cash_usd"] == 99000.0

    # Le fichier tmp, lui, contient bien l'écriture complète et valide (preuve que le crash a
    # eu lieu APRÈS l'écriture complète des données, pas au milieu).
    tmp_path_file = str(path) + ".tmp"
    assert os.path.exists(tmp_path_file)
    with open(tmp_path_file, "r", encoding="utf-8") as f:
        tmp_content = json.load(f)
    assert tmp_content["cash_usd"] == 12345.0
    validate_schema(tmp_content)

    # Un run suivant qui réussit peut terminer la bascule normalement.
    save_state(new_state, str(path))
    with open(path, "r", encoding="utf-8") as f:
        on_disk_final = json.load(f)
    assert on_disk_final["cash_usd"] == 12345.0


def test_save_state_atomic_never_leaves_partial_final_file(tmp_path, monkeypatch):
    """Le contenu passé à open(tmp,'w') est écrit intégralement (flush+fsync) avant tout appel
    à os.replace : si os.replace échoue, le fichier final original doit rester lisible et
    valide (jamais tronqué / jamais à moitié écrit)."""
    path = tmp_path / "state.json"
    save_state(init_state(), str(path))

    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    huge_state = init_state()
    huge_state["positions"] = {
        f"SYM{i}": {"qty": 1.0 + i, "prix_moyen": 10.0 + i} for i in range(200)
    }
    with pytest.raises(OSError):
        save_state(huge_state, str(path))

    # Le fichier final original doit toujours être intact et parseable.
    reloaded = load_state(str(path))
    assert reloaded["positions"] == {}


# --------------------------------------------------------------------------------------
# Validation de schéma stricte — états corrompus variés
# --------------------------------------------------------------------------------------


def _valid_state() -> dict:
    s = init_state()
    s["positions"] = {"BTC": {"qty": 0.1, "prix_moyen": 60000.0}}
    return s


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.pop("cash_usd"),
        lambda s: s.pop("positions"),
        lambda s: s.pop("circuit_breakers"),
        lambda s: s.pop("schema_version"),
        lambda s: s.update(schema_version=2),
        lambda s: s.update(cash_usd="100000"),
        lambda s: s.update(cash_usd=True),
        lambda s: s.update(cash_usd=float("nan")),
        lambda s: s.update(cash_usd=float("inf")),
        lambda s: s.update(positions={"BTC": {"qty": 0.1}}),  # prix_moyen manquant
        lambda s: s.update(positions={"BTC": {"qty": 0, "prix_moyen": 100.0}}),  # qty<=0
        lambda s: s.update(positions={"BTC": {"qty": -1.0, "prix_moyen": 100.0}}),
        lambda s: s.update(positions={"BTC": "not_a_dict"}),
        lambda s: s.update(positions="not_a_dict"),
        # equity_peak_usd=0.0 N'EST PLUS rejeté (multi-wallets : légitime tant que le wallet
        # n'est pas initialisé, cf. test_validate_schema_accepts_equity_peak_zero ci-dessous) —
        # seule une valeur négative reste invalide.
        lambda s: s.update(equity_peak_usd=-5.0),
        lambda s: s.pop("wallet_id"),
        lambda s: s.update(wallet_id=""),
        lambda s: s.update(wallet_id=123),
        lambda s: s.pop("initial_eur"),
        lambda s: s.update(initial_eur=0.0),
        lambda s: s.update(initial_eur=-1000.0),
        lambda s: s.pop("fx"),
        lambda s: s.update(fx={"initial_rate": None, "last_rate": None, "last_rate_ts": None, "last_rate_source": None}),  # last_rate_stale manquant
        lambda s: s.update(fx={"initial_rate": 0.0, "last_rate": None, "last_rate_ts": None, "last_rate_source": None, "last_rate_stale": False}),
        lambda s: s.update(fx={"initial_rate": -1.1, "last_rate": None, "last_rate_ts": None, "last_rate_source": None, "last_rate_stale": False}),
        lambda s: s.update(fx={"initial_rate": None, "last_rate": None, "last_rate_ts": None, "last_rate_source": None, "last_rate_stale": "false"}),
        lambda s: s.update(fx={"initial_rate": None, "last_rate": None, "last_rate_ts": None, "last_rate_source": None, "last_rate_stale": False, "extra": 1}),
        lambda s: s.update(state_hash_prev="not_a_valid_hash"),
        lambda s: s.update(state_hash_prev=123),
        lambda s: s.update(last_run_id=20260722),
        lambda s: s.update(circuit_breakers={"flatten_mode": "false"}),  # string, pas bool
        lambda s: s.update(circuit_breakers={
            "flatten_mode": False, "manual_review_required": False,
            "daily_loss_freeze_until": None, "cooldown_until": None,
            "consecutive_losses": -1, "dd_half_size_active": False,
        }),
        lambda s: s.update(circuit_breakers={
            "flatten_mode": False, "manual_review_required": False,
            "daily_loss_freeze_until": None, "cooldown_until": None,
            "consecutive_losses": 1.5, "dd_half_size_active": False,
        }),
        lambda s: s.update(circuit_breakers={
            "flatten_mode": False, "manual_review_required": False,
            "daily_loss_freeze_until": None, "cooldown_until": None,
            "consecutive_losses": 0, "dd_half_size_active": False,
            "unexpected_extra_field": True,
        }),
        lambda s: s.update(trade_history_for_breakers=[{"ts": "x", "symbol": "BTC"}]),  # pnl manquant
        lambda s: s.update(trade_history_for_breakers=[{"ts": "x", "symbol": "BTC", "realized_pnl_usd": "oops"}]),
        lambda s: s.update(trade_history_for_breakers="not_a_list"),
    ],
)
def test_validate_schema_rejects_corrupted_states(mutate):
    state = _valid_state()
    mutate(state)
    with pytest.raises(StateValidationError):
        validate_schema(state)


def test_validate_schema_accepts_well_formed_state():
    validate_schema(_valid_state())  # ne doit pas lever


def test_validate_schema_accepts_equity_peak_zero_when_uninitialized():
    """Multi-wallets : un wallet non initialisé (aucun taux EUR/USD résolu encore) a
    legitimement equity_peak_usd=0.0 — ce n'est plus une anomalie de schéma."""
    state = init_state("prudent", 1000.0)
    validate_schema(state)  # ne doit pas lever
    assert state["equity_peak_usd"] == 0.0


def test_validate_schema_accepts_wallet_fx_fully_populated():
    state = init_state("agressif", 1000.0)
    state["fx"] = {
        "initial_rate": 1.08, "last_rate": 1.081, "last_rate_ts": "2026-07-23T10:00:00+00:00",
        "last_rate_source": "frankfurter", "last_rate_stale": False,
    }
    validate_schema(state)  # ne doit pas lever


def test_load_state_rejects_invalid_json(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json,,,", encoding="utf-8")
    with pytest.raises(StateValidationError):
        load_state(str(path))


def test_load_state_rejects_incomplete_schema_on_disk(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"cash_usd": 100000.0}), encoding="utf-8")
    with pytest.raises(StateValidationError):
        load_state(str(path))


def test_load_state_never_silently_defaults_when_file_exists_but_corrupt(tmp_path):
    """Contrat central : un fichier PRÉSENT mais invalide ne doit jamais retomber sur
    init_state() en silence — contrairement au cas d'absence de fichier."""
    path = tmp_path / "state.json"
    corrupt = _valid_state()
    del corrupt["equity_peak_usd"]
    path.write_text(json.dumps(corrupt), encoding="utf-8")

    with pytest.raises(StateValidationError):
        load_state(str(path))

    # Bien distinct du comportement "fichier absent" :
    missing_path = tmp_path / "does_not_exist.json"
    assert load_state(str(missing_path)) == init_state()


# --------------------------------------------------------------------------------------
# Chaîne de hash — compute_state_hash
# --------------------------------------------------------------------------------------


def test_compute_state_hash_deterministic_regardless_of_key_order():
    state = _valid_state()
    reordered = dict(reversed(list(state.items())))
    assert compute_state_hash(state) == compute_state_hash(reordered)


def test_compute_state_hash_changes_on_any_mutation():
    state = _valid_state()
    h1 = compute_state_hash(state)
    mutated = copy.deepcopy(state)
    mutated["cash_usd"] += 0.01
    h2 = compute_state_hash(mutated)
    assert h1 != h2


def test_compute_state_hash_is_sha256_hex():
    h = compute_state_hash(_valid_state())
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


# --------------------------------------------------------------------------------------
# Idempotence
# --------------------------------------------------------------------------------------


def test_is_run_already_done_true_on_match():
    state = init_state()
    state["last_run_id"] = "2026-07-22T14"
    assert is_run_already_done(state, "2026-07-22T14") is True


def test_is_run_already_done_false_on_new_hour():
    state = init_state()
    state["last_run_id"] = "2026-07-22T14"
    assert is_run_already_done(state, "2026-07-22T15") is False


def test_is_run_already_done_false_when_never_run():
    state = init_state()
    assert is_run_already_done(state, "2026-07-22T14") is False
