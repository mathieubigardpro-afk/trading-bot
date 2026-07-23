"""Tests d'intégration de `bot/runner.py` (multi-wallets) : idempotence GLOBALE du cycle,
reprise après crash, comportement tout-ou-rien, initialisation FX/capital, et isolation des
wallets — cf. docs/ARCHITECTURE.md §9.

Ces tests exécutent `bot.runner.main()` pour de vrai (pas de mock du runner lui-même), contre
un dépôt git jetable (bare `origin` + clone) déjà migré (`state/cycle.json` +
`state/wallets/<id>/*`, cf. `tools/migrate_to_wallets.py`), avec `bot.runner.get_prices` /
`bot.runner.get_fx_rate` substitués par des doublures déterministes (aucun appel réseau réel :
le proxy du bac à sable bloque de toute façon l'accès public Binance/Yahoo/FX, et ces tests
doivent rester rapides et déterministes indépendamment de la disponibilité réseau)."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import bot.runner as runner
from bot import config
from bot.feeds.fx import FxRate
from bot.feeds.types import HistoryUnavailableError, Quote
from bot.persist.cycle import save_cycle_state
from bot.persist.journal import append_journal
from bot.persist.state import save_state
from tools.migrate_to_wallets import migrate

FIXED_NOW = datetime(2026, 7, 22, 14, 3, 0, tzinfo=timezone.utc)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


def _git_ok(repo: Path, *args: str) -> str:
    result = _git(repo, *args)
    assert result.returncode == 0, f"git {args} a échoué : {result.stderr}"
    return result.stdout


@pytest.fixture
def origin_and_clone(tmp_path):
    """Bare `origin` + clone déjà migré (3 wallets non initialisés + cycle.json), committé et
    poussé — reproduit fidèlement le point de départ d'un run réel post-migration."""
    origin = tmp_path / "origin.git"
    _git_ok(tmp_path, "init", "--bare", "-b", "main", str(origin))

    clone = tmp_path / "clone"
    _git_ok(tmp_path, "clone", str(origin), str(clone))
    (clone / "state").mkdir(parents=True, exist_ok=True)
    migrate(str(clone))
    _git_ok(clone, "add", "state")
    _git_ok(clone, "commit", "-m", "Migration multi-wallets")
    _git_ok(clone, "push", "origin", "main")

    return origin, clone


@pytest.fixture(autouse=True)
def _no_network_and_repo_override(monkeypatch, origin_and_clone):
    """Par défaut : aucun appel réseau ne renvoie quoi que ce soit d'exploitable (reproduit le
    réseau bloqué du bac à sable) — chaque test qui a besoin de prix/FX les fournit lui-même
    via un monkeypatch supplémentaire ciblé. `prefetch_daily_history`/`get_daily_history`
    (poches actions/ETF, docs/ARCHITECTURE.md §11) sont également neutralisés ici : sans ce
    monkeypatch, ces tests déclencheraient de VRAIS appels réseau (yfinance/stooq) à chaque
    `runner.main()`, lents et non déterministes dans ce bac à sable (proxy bloqué)."""
    _origin, clone = origin_and_clone
    monkeypatch.setattr(runner, "repo_dir", lambda: str(clone))
    monkeypatch.setattr(runner, "get_prices", lambda symbols: {sym: None for sym in symbols})
    monkeypatch.setattr(runner, "get_fx_rate", lambda pair, last_known=None: None)
    monkeypatch.setattr(runner, "prefetch_daily_history", lambda symbols, asset_class, n_days=None: {})
    monkeypatch.setattr(
        runner, "get_daily_history",
        lambda symbol, n_days, asset_class: (_ for _ in ()).throw(
            HistoryUnavailableError("pas de fixture réseau (réseau bloqué du bac à sable)")
        ),
    )
    monkeypatch.chdir(clone)


def _wallet_state(clone: Path, wallet_id: str) -> dict:
    path = clone / config.wallet_state_json(wallet_id)
    return json.loads(path.read_text(encoding="utf-8"))


def _remote_wallet_state(origin: Path, wallet_id: str) -> dict:
    return json.loads(_git_ok(origin, "show", f"main:{config.wallet_state_json(wallet_id)}"))


def _remote_cycle_state(origin: Path) -> dict:
    return json.loads(_git_ok(origin, "show", f"main:{config.CYCLE_JSON}"))


# --------------------------------------------------------------------------------------
# Réseau bloqué (cas réel de ce bac à sable) : cycle propre, 0 wallet initialisé, 1 commit
# --------------------------------------------------------------------------------------


def test_runner_no_fx_and_no_prices_all_wallets_stay_uninitialized_but_cycle_commits(origin_and_clone):
    origin, clone = origin_and_clone
    run_id = runner.compute_run_id(FIXED_NOW)

    exit_code = runner.main(now=FIXED_NOW)
    assert exit_code == 0

    remote_cycle = _remote_cycle_state(origin)
    assert remote_cycle["last_run_id"] == run_id

    for wallet_id in config.WALLET_IDS:
        remote_state = _remote_wallet_state(origin, wallet_id)
        assert remote_state["last_run_id"] == run_id
        assert remote_state["cash_usd"] == 0.0
        assert remote_state["positions"] == {}
        assert remote_state["fx"]["initial_rate"] is None

        equity_lines = _git_ok(
            origin, "show", f"main:{config.wallet_equity_jsonl(wallet_id)}"
        ).splitlines()
        assert len(equity_lines) == 1
        equity_rec = json.loads(equity_lines[0])
        assert equity_rec["equity_usd"] == 0.0
        assert equity_rec["wallet_id"] == wallet_id

    log = _git_ok(origin, "log", "--format=%s")
    assert "init. en attente" in log.splitlines()[0]


# --------------------------------------------------------------------------------------
# Idempotence globale
# --------------------------------------------------------------------------------------


def test_runner_idempotent_second_call_same_hour_is_clean_noop(origin_and_clone):
    origin, _clone = origin_and_clone

    assert runner.main(now=FIXED_NOW) == 0
    log_after_first = _git_ok(origin, "log", "--format=%H")

    assert runner.main(now=FIXED_NOW) == 0
    log_after_second = _git_ok(origin, "log", "--format=%H")

    assert log_after_first == log_after_second  # aucun commit supplémentaire


def test_runner_resumes_git_sync_instead_of_silently_aborting_as_duplicate(origin_and_clone):
    """Si `cycle.json` local porte déjà `last_run_id = run_id` (le cycle a réellement eu lieu)
    mais qu'un crash a empêché tout commit/push, une invocation suivante pour le MÊME run_id
    ne doit pas se contenter de sortir en silence — elle doit reprendre `git_sync()`."""
    origin, clone = origin_and_clone
    run_id = runner.compute_run_id(FIXED_NOW)

    for wallet_id in config.WALLET_IDS:
        state = _wallet_state(clone, wallet_id)
        state["last_run_id"] = run_id
        state["last_run_completed_at"] = FIXED_NOW.isoformat()
        save_state(state, str(clone / config.wallet_state_json(wallet_id)))
    save_cycle_state(
        {"schema_version": 1, "last_run_id": run_id, "last_run_completed_at": FIXED_NOW.isoformat(),
         "wallet_ids": list(config.WALLET_IDS)},
        str(clone / config.CYCLE_JSON),
    )

    status_before = _git_ok(clone, "status", "--porcelain", "--", "state")
    assert status_before.strip() != ""  # bien "sale" : jamais commité

    exit_code = runner.main(now=FIXED_NOW)
    assert exit_code == 0

    remote_cycle = _remote_cycle_state(origin)
    assert remote_cycle["last_run_id"] == run_id

    status_after = _git_ok(clone, "status", "--porcelain", "--", "state")
    assert status_after.strip() == ""


# --------------------------------------------------------------------------------------
# Garde-fou anti-doublon post-crash (orphelin de journalisation)
# --------------------------------------------------------------------------------------


def test_runner_aborts_on_orphaned_run_records_instead_of_duplicating_fills(origin_and_clone):
    origin, clone = origin_and_clone
    run_id = runner.compute_run_id(FIXED_NOW)

    orphan_fill = {
        "run_id": run_id, "ts": FIXED_NOW.isoformat(), "symbol": "BTC", "strategy": "ensemble",
        "wallet_id": "prudent", "side": "BUY", "qty": 0.001, "notional_usd": 50.0,
        "price_fill": 50000.0, "price_mid_ideal": 49990.0, "fees_usd": 0.05, "slippage_usd": 0.02,
        "quote_source": "binance", "quote_ts": FIXED_NOW.isoformat(), "cash_after_usd": 1000.0,
    }
    append_journal(str(clone / config.wallet_trades_jsonl("prudent")), orphan_fill)

    cycle_before = json.loads((clone / config.CYCLE_JSON).read_text(encoding="utf-8"))
    assert cycle_before["last_run_id"] is None

    exit_code = runner.main(now=FIXED_NOW)
    assert exit_code == 1

    # Rien n'a bougé localement, et rien n'a été poussé.
    cycle_after = json.loads((clone / config.CYCLE_JSON).read_text(encoding="utf-8"))
    assert cycle_after == cycle_before
    for wallet_id in config.WALLET_IDS:
        state = _wallet_state(clone, wallet_id)
        assert state["last_run_id"] is None

    remote_cycle = json.loads(_git_ok(origin, "show", "main:state/cycle.json"))
    assert remote_cycle["last_run_id"] is None


# --------------------------------------------------------------------------------------
# Initialisation FX + capital, et trading une fois initialisé
# --------------------------------------------------------------------------------------


def _fake_quotes_for(symbols, now, price=100.0):
    return {
        sym: Quote(bid=price * 0.999, ask=price * 1.001, mid=price, ts=now.isoformat(), source="fake")
        for sym in symbols
    }


def test_runner_initializes_all_wallets_capital_from_eur_when_fx_and_prices_available(origin_and_clone, monkeypatch):
    origin, _clone = origin_and_clone
    run_id = runner.compute_run_id(FIXED_NOW)

    all_symbols = sorted({sym for w in config.WALLETS for sym in w["univers_crypto"]})
    monkeypatch.setattr(runner, "get_prices", lambda symbols: _fake_quotes_for(symbols, FIXED_NOW))
    monkeypatch.setattr(
        runner, "get_fx_rate",
        lambda pair, last_known=None: FxRate(rate=1.08, ts=FIXED_NOW.isoformat(), source="frankfurter", stale=False),
    )
    # get_history lève HistoryUnavailableError par défaut (pas de fixture réseau) : traité
    # comme repli pessimiste habituel (signal désactivé), sans empêcher l'initialisation FX.
    from bot.feeds.types import HistoryUnavailableError
    monkeypatch.setattr(runner, "get_history", lambda sym, n_hours: (_ for _ in ()).throw(HistoryUnavailableError("pas de fixture")))

    exit_code = runner.main(now=FIXED_NOW)
    assert exit_code == 0

    for wallet_cfg in config.WALLETS:
        wallet_id = wallet_cfg["id"]
        remote_state = _remote_wallet_state(origin, wallet_id)
        assert remote_state["last_run_id"] == run_id
        assert remote_state["fx"]["initial_rate"] == pytest.approx(1.08)
        expected_cash = wallet_cfg["capital_initial_eur"] * 1.08
        # Sans stratégie concrète (V1), aucune position n'est ouverte : cash == equity.
        assert remote_state["cash_usd"] == pytest.approx(expected_cash)
        assert remote_state["positions"] == {}

    del all_symbols


# --------------------------------------------------------------------------------------
# Isolation des wallets : un breaker déclenché sur un wallet ne touche pas les autres
# --------------------------------------------------------------------------------------


def test_process_wallet_circuit_breaker_isolation_between_wallets():
    """Un drawdown sévère déjà engagé sur le wallet AGRESSIF (equity très en dessous de son
    pic -> flatten_mode) ne doit avoir AUCUN effet sur l'état/les décisions du wallet PRUDENT
    évalué au même cycle avec les mêmes prix (fonctions pures, aucun état partagé)."""
    from bot.persist.state import init_state

    now = FIXED_NOW
    prices = _fake_quotes_for(config.CRYPTO_SYMBOLS_30, now, price=100.0)
    history = {}
    history_failed = set()
    fx_resolved = FxRate(rate=1.08, ts=now.isoformat(), source="frankfurter", stale=False)

    agressif_cfg = config.wallet_config("agressif")
    prudent_cfg = config.wallet_config("prudent")

    # Wallet agressif : déjà initialisé, en drawdown sévère (equity 40% du pic, largement
    # au-delà du seuil flatten agressif de 35%).
    agressif_state = init_state("agressif", 1000.0)
    agressif_state["fx"]["initial_rate"] = 1.08
    agressif_state["fx"]["last_rate"] = 1.08
    agressif_state["cash_usd"] = 400.0
    agressif_state["equity_peak_usd"] = 1080.0
    agressif_state["equity_peak_ts"] = now.isoformat()

    # Wallet prudent : initialisé, SANS aucun drawdown (equity = pic).
    prudent_state = init_state("prudent", 1000.0)
    prudent_state["fx"]["initial_rate"] = 1.08
    prudent_state["fx"]["last_rate"] = 1.08
    prudent_state["cash_usd"] = 1080.0
    prudent_state["equity_peak_usd"] = 1080.0
    prudent_state["equity_peak_ts"] = now.isoformat()

    agressif_result = runner.process_wallet(
        agressif_cfg, agressif_state, "2026-07-22T14", now, prices, history, history_failed, fx_resolved
    )
    prudent_result = runner.process_wallet(
        prudent_cfg, prudent_state, "2026-07-22T14", now, prices, history, history_failed, fx_resolved
    )

    assert agressif_result.new_state["circuit_breakers"]["flatten_mode"] is True
    assert agressif_result.new_state["circuit_breakers"]["manual_review_required"] is True

    # Le wallet prudent, évalué avec les MÊMES prix au MÊME instant, reste totalement
    # indemne : aucun breaker actif, comme s'il avait été calculé isolément.
    assert prudent_result.new_state["circuit_breakers"]["flatten_mode"] is False
    assert prudent_result.new_state["circuit_breakers"]["manual_review_required"] is False
    assert prudent_result.new_state["circuit_breakers"]["dd_half_size_active"] is False


# --------------------------------------------------------------------------------------
# Tout-ou-rien : un wallet qui échoue empêche le commit des 3 (cohérence globale d'abord)
# --------------------------------------------------------------------------------------


def test_runner_all_or_nothing_when_one_wallet_processing_raises(origin_and_clone, monkeypatch):
    """Si le traitement d'UN wallet lève une exception inattendue, le cycle entier échoue
    proprement : AUCUNE écriture pour AUCUN wallet, AUCUN commit, code de sortie non nul."""
    origin, clone = origin_and_clone

    monkeypatch.setattr(runner, "get_prices", lambda symbols: _fake_quotes_for(symbols, FIXED_NOW))
    monkeypatch.setattr(
        runner, "get_fx_rate",
        lambda pair, last_known=None: FxRate(rate=1.08, ts=FIXED_NOW.isoformat(), source="frankfurter", stale=False),
    )

    original_risk_manager_for_wallet = runner._risk_manager_for_wallet

    def _boom_for_equilibre(wallet_cfg):
        if wallet_cfg["id"] == "equilibre":
            raise RuntimeError("panne simulée du RiskManager pour le wallet équilibré")
        return original_risk_manager_for_wallet(wallet_cfg)

    monkeypatch.setattr(runner, "_risk_manager_for_wallet", _boom_for_equilibre)

    remote_cycle_before = json.loads(_git_ok(origin, "show", "main:state/cycle.json"))
    remote_states_before = {
        wallet_id: _remote_wallet_state(origin, wallet_id) for wallet_id in config.WALLET_IDS
    }

    exit_code = runner.main(now=FIXED_NOW)
    assert exit_code == 1

    # Rien n'a été committé : ni le wallet fautif, ni les deux autres qui, eux, auraient pu
    # réussir individuellement — c'est précisément la garantie tout-ou-rien.
    remote_cycle_after = json.loads(_git_ok(origin, "show", "main:state/cycle.json"))
    assert remote_cycle_after == remote_cycle_before
    for wallet_id in config.WALLET_IDS:
        assert _remote_wallet_state(origin, wallet_id) == remote_states_before[wallet_id]

    # Rien n'a non plus été écrit localement sur les wallets sains (prudent, agressif).
    for wallet_id in ("prudent", "agressif"):
        state = _wallet_state(clone, wallet_id)
        assert state["last_run_id"] is None
