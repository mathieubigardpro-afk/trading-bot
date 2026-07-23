"""bot/tests/test_labo.py — wallet labo 🧪 (incubateur de stratégies candidates, docs/
ARCHITECTURE.md § Labo, mission auto-amélioration continue).

Couvre :
  - isolation TOUT-OU-RIEN / breakers du labo comme n'importe quel autre wallet
    (`bot.runner.process_wallet` traite le labo par le MÊME chemin de code, aucune branche
    spéciale) ;
  - migration propre : le labo naît NON INITIALISÉ comme les 3 autres à leur naissance ;
  - test NÉGATIF explicite (mission point 5) : une stratégie incubée dans le labo ne peut
    JAMAIS émettre de cibles pour un autre wallet -- vérifié au niveau du mécanisme structurel
    qui le garantit (`bot.runner._combine_pockets` n'appelle une stratégie que si le
    `strategy_ref` d'une poche DE CE WALLET la référence explicitement).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

import pandas as pd
import pytest

import bot.runner as runner
from bot import config
from bot.feeds.fx import FxRate
from bot.feeds.types import Quote
from bot.persist.state import init_state, validate_schema
from bot.strategies import StrategyBase
from tools.migrate_to_wallets import migrate

NOW = datetime(2026, 7, 23, 20, 0, 0, tzinfo=timezone.utc)


class _FakeCandidateStrategy(StrategyBase):
    """Candidate factice : si JAMAIS appelée, retourne un poids massif sur un symbole -- sert de
    détecteur (`self.calls`) pour prouver qu'un wallet qui ne la référence pas dans ses poches ne
    l'invoque jamais (test négatif d'isolation, mission point 5)."""

    name = "candidate_x"

    def __init__(self, symbol: str = "ETH", weight: float = 0.9):
        self.symbol = symbol
        self.weight = weight
        self.calls: list = []

    def target_weights(
        self,
        history: Dict[str, pd.DataFrame],
        state: dict,
        profile: Optional[dict] = None,
    ) -> Dict[str, float]:
        self.calls.append(profile.get("id") if profile else None)
        return {self.symbol: self.weight}


def _labo_wallet_cfg_with_candidate(symbol: str = "ETH", alloc: float = 0.5) -> dict:
    base = config.wallet_config(config.LABO_WALLET_ID)
    cfg = dict(base)
    cfg["pockets"] = [
        {"asset_class": "crypto", "capital_alloc_pct": alloc, "strategy_ref": "candidate_x", "univers": [symbol]},
    ]
    cfg["univers_crypto"] = [symbol]
    return cfg


# ------------------------------------------------------------------------------------------
# Migration : le labo naît non initialisé, comme les 3 autres à leur naissance
# ------------------------------------------------------------------------------------------


def test_migration_creates_labo_wallet_uninitialized(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "state").mkdir()

    report = migrate(str(repo))

    assert config.LABO_WALLET_ID in report.wallets_created
    labo_state_path = repo / config.wallet_state_json(config.LABO_WALLET_ID)
    assert labo_state_path.exists()

    import json

    state = json.loads(labo_state_path.read_text(encoding="utf-8"))
    validate_schema(state)
    assert state["wallet_id"] == config.LABO_WALLET_ID
    assert state["initial_eur"] == pytest.approx(1000.0)
    assert state["cash_usd"] == 0.0
    assert state["positions"] == {}
    assert state["fx"]["initial_rate"] is None
    assert state["last_run_id"] is None

    for name in ("trades.jsonl", "equity.jsonl", "decisions.jsonl"):
        assert (repo / config.wallet_state_dir(config.LABO_WALLET_ID) / name).exists()

    cycle = json.loads((repo / config.CYCLE_JSON).read_text(encoding="utf-8"))
    assert config.LABO_WALLET_ID in cycle["wallet_ids"]


def test_migration_labo_included_alongside_the_three_production_wallets(tmp_path):
    repo = tmp_path / "repo2"
    repo.mkdir()
    (repo / "state").mkdir()

    report = migrate(str(repo))

    assert set(report.wallets_created) == set(config.WALLET_IDS)
    assert set(report.wallets_created) == {"prudent", "equilibre", "agressif", "labo"}


# ------------------------------------------------------------------------------------------
# Le labo vide (état actuel) : reste intégralement en cash, aucun crash
# ------------------------------------------------------------------------------------------


def test_process_wallet_labo_empty_stays_in_cash_no_crash():
    labo_cfg = config.wallet_config(config.LABO_WALLET_ID)
    assert labo_cfg["pockets"] == []
    assert labo_cfg["univers_crypto"] == []

    state = init_state(config.LABO_WALLET_ID, labo_cfg["capital_initial_eur"])
    fx_resolved = FxRate(rate=1.08, ts=NOW.isoformat(), source="frankfurter", stale=False)

    result = runner.process_wallet(
        labo_cfg, state, "2026-07-23T20", NOW,
        prices_all={}, history_all={}, history_failed_all=set(), fx_resolved=fx_resolved,
    )

    assert result.initialized is True
    assert result.new_state["cash_usd"] == pytest.approx(1000.0 * 1.08)
    assert result.new_state["positions"] == {}
    assert result.n_trades == 0
    assert result.decision_records == []  # univers vide -> rien à journaliser par symbole


def test_process_wallet_labo_respects_breakers_like_any_other_wallet():
    """Le labo utilise EXACTEMENT le même `RiskManager`/breakers que les wallets réels (mission
    point 1 : "les candidates s'y battent à armes égales et encadrées") -- vérifié ici via un
    drawdown sévère déjà engagé qui déclenche flatten_mode, exactement comme
    `test_process_wallet_circuit_breaker_isolation_between_wallets` pour agressif/prudent."""
    labo_cfg = _labo_wallet_cfg_with_candidate()
    strategies_by_name = {"candidate_x": _FakeCandidateStrategy(symbol="ETH", weight=0.05)}

    state = init_state(config.LABO_WALLET_ID, 1000.0)
    state["fx"]["initial_rate"] = 1.08
    state["fx"]["last_rate"] = 1.08
    state["cash_usd"] = 250.0
    state["equity_peak_usd"] = 1080.0
    state["equity_peak_ts"] = NOW.isoformat()

    prices = {"ETH": Quote(bid=99.9, ask=100.1, mid=100.0, ts=NOW.isoformat(), source="fake")}
    fx_resolved = FxRate(rate=1.08, ts=NOW.isoformat(), source="frankfurter", stale=False)

    result = runner.process_wallet(
        labo_cfg, state, "2026-07-23T20", NOW,
        prices_all=prices, history_all={}, history_failed_all=set(), fx_resolved=fx_resolved,
        strategies_by_name=strategies_by_name,
    )

    # equity ~250$ vs pic 1080$ -> drawdown ~76.9%, très au-delà du seuil flatten labo (25%).
    assert result.new_state["circuit_breakers"]["flatten_mode"] is True
    assert result.new_state["circuit_breakers"]["manual_review_required"] is True


# ------------------------------------------------------------------------------------------
# Test NÉGATIF explicite (mission point 5) : une candidate incubée ne trade JAMAIS pour un
# autre wallet que le labo.
# ------------------------------------------------------------------------------------------


def test_incubating_strategy_never_emits_targets_for_a_production_wallet():
    """`candidate_x` n'est référencée par AUCUNE poche des 3 wallets réels -- `_combine_pockets`
    ne doit donc jamais l'appeler pour eux, même si elle est présente dans `strategies_by_name`
    (le même dict partagé que `bot.runner.main()` construit une seule fois par cycle pour TOUS
    les wallets, labo compris)."""
    candidate = _FakeCandidateStrategy(symbol="ETH", weight=0.9)
    strategies_by_name = {"candidate_x": candidate, "quasi_passif_crypto": None, "dual_momentum_etf": None}
    # `strategies_by_name` volontairement incomplet pour prudent (None n'est pas un StrategyBase)
    # : ce test porte uniquement sur candidate_x, pas sur le comportement des 2 stratégies de
    # production réelles (couvert ailleurs) -- on retire les entrées None pour rester réaliste
    # (une entrée absente est traitée comme "poche ignorée", jamais une exception).
    strategies_by_name = {k: v for k, v in strategies_by_name.items() if v is not None}

    prudent_cfg = config.wallet_config("prudent")
    working_state: dict = {"positions": {}, "cash_usd": 1080.0, "strategy_state": {}}

    cibles, signals = runner._combine_pockets(
        prudent_cfg, history_hourly={}, daily_history={}, working_state=working_state,
        strategies_by_name=strategies_by_name,
    )

    assert candidate.calls == [], "candidate_x a été appelée pour le wallet prudent -- fuite d'isolation"
    assert "ETH" not in cibles, "poids d'une candidate d'incubation retrouvé dans les cibles d'un wallet réel"
    assert "candidate_x" not in signals


def test_incubating_strategy_never_emits_targets_for_any_production_wallet_full_cycle():
    """Même garantie que ci-dessus, mais via `bot.runner.process_wallet()` complet (pas
    seulement `_combine_pockets`), pour chacun des 3 wallets réels : `decisions.jsonl` ne doit
    JAMAIS porter `candidate_x` dans `strategy_signals`, et aucun ordre ne doit être exécuté sur
    un symbole qu'elle seule ciblerait (ici `ETH`, hors univers du wallet prudent)."""
    candidate = _FakeCandidateStrategy(symbol="ETH", weight=0.9)

    for wallet_id in config.PRODUCTION_WALLET_IDS:
        wallet_cfg = config.wallet_config(wallet_id)
        state = init_state(wallet_id, wallet_cfg["capital_initial_eur"])
        state["fx"]["initial_rate"] = 1.08
        state["fx"]["last_rate"] = 1.08
        state["cash_usd"] = 1080.0
        fx_resolved = FxRate(rate=1.08, ts=NOW.isoformat(), source="frankfurter", stale=False)

        strategies_by_name = {"candidate_x": candidate}  # AUCUNE des 3 stratégies réelles

        result = runner.process_wallet(
            wallet_cfg, state, "2026-07-23T20", NOW,
            prices_all={}, history_all={}, history_failed_all=set(), fx_resolved=fx_resolved,
            strategies_by_name=strategies_by_name,
        )

        for d in result.decision_records:
            assert "candidate_x" not in (d.get("strategy_signals") or {})

    assert candidate.calls == [], f"candidate_x a été appelée pour {candidate.calls} -- fuite d'isolation"


def test_labo_pockets_do_call_the_incubating_strategy_and_scale_by_capital_alloc_pct():
    """Contrepartie positive : quand une candidate EST référencée par une poche du labo, elle
    est bien appelée (comme n'importe quelle stratégie de production, mission point 2 : "le
    runner les charge comme les autres"), et son poids est mis à l'échelle par
    `capital_alloc_pct`, exactement comme `_combine_pockets` le fait pour les 3 wallets réels."""
    candidate = _FakeCandidateStrategy(symbol="ETH", weight=0.8)
    labo_cfg = _labo_wallet_cfg_with_candidate(symbol="ETH", alloc=0.5)
    working_state: dict = {"positions": {}, "cash_usd": 1080.0, "strategy_state": {}}

    cibles, signals = runner._combine_pockets(
        labo_cfg, history_hourly={}, daily_history={}, working_state=working_state,
        strategies_by_name={"candidate_x": candidate},
    )

    assert candidate.calls == [config.LABO_WALLET_ID]
    assert cibles["ETH"] == pytest.approx(0.8 * 0.5)
    assert signals["candidate_x"] == {"ETH": 0.8}


def test_no_trade_band_scale_and_tradable_symbols_use_generic_pocket_univers_fallback():
    """`_no_trade_band_scale_by_symbol`/`_noncrypto_tradable_symbols` (docs/ARCHITECTURE.md §
    Labo) doivent retomber sur `pocket["univers"]` pour une candidate actions/ETF inconnue de
    `POCKET_STRATEGY_TRADABLE_SYMBOLS` (mapping des 3 stratégies de production uniquement)."""
    labo_cfg = dict(config.wallet_config(config.LABO_WALLET_ID))
    labo_cfg["pockets"] = [
        {
            "asset_class": "equities", "capital_alloc_pct": 0.3, "strategy_ref": "candidate_equities",
            "univers": ["AAPL", "MSFT"],
        },
    ]
    labo_cfg["univers_crypto"] = []

    tradable = runner._noncrypto_tradable_symbols(labo_cfg)
    assert tradable == ["AAPL", "MSFT"]

    scale = runner._no_trade_band_scale_by_symbol(labo_cfg)
    assert scale["AAPL"] == pytest.approx(0.3)
    assert scale["MSFT"] == pytest.approx(0.3)
