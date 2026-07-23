"""Test d'intégration LÉGER (pas de git, pas de disque) de `bot.runner.process_wallet` pour le
correctif ARCHITECTURE.md §12.4 : vérifie que le mécanisme "gel vs liquidation par prudence"
(`bot.strategies.apply_missing_data_policy`, câblé dans les 3 stratégies concrètes) produit bien
le comportement attendu et la journalisation distincte au niveau `decisions.jsonl`, via
`bot.runner.process_wallet` appelé directement en mémoire (contrairement à `bot/tests/
test_integration_full_cycle.py`, qui exerce `bot.runner.main()` bout-en-bout via un dépôt git
temporaire — plus lourd, pas nécessaire ici pour isoler ce comportement précis).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bot import config
from bot.feeds.fx import FxRate
from bot.feeds.types import Quote
from bot.persist.state import init_state
from bot.runner import process_wallet
from bot.strategies import MISSING_DATA_MAX_CYCLES_DEFAULT
from bot.strategies.quasi_passif_crypto import QuasiPassifCrypto
from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 7, 23, 19, 40, 28, tzinfo=timezone.utc)
STRATEGIES = {"quasi_passif_crypto": QuasiPassifCrypto()}


def _xlm_hourly_history(n_days: int = 260) -> pd.DataFrame:
    """Historique horaire XLM en tendance haussière nette (SMA200 "on"), jours calendaires
    COMPLETS jusqu'à `NOW` (aucune bougie du jour en cours), même construction que
    `bot/tests/test_quasi_passif_crypto.py:make_complete_days_history`."""
    n_hours = n_days * 24
    rng = np.random.default_rng(7)
    returns = rng.normal(loc=0.0006, scale=0.002, size=n_hours)
    prices = 0.18 * np.cumprod(1.0 + returns)
    end = NOW.replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(end=end - timedelta(hours=1), periods=n_hours, freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": prices, "high": prices, "low": prices, "close": prices, "volume": 1.0}, index=idx
    )


def _base_state() -> dict:
    wallet_cfg = config.wallet_config("agressif")
    state = init_state("agressif", wallet_cfg["capital_initial_eur"])
    state["fx"] = {
        "initial_rate": 1.08, "last_rate": 1.08, "last_rate_ts": NOW.isoformat(),
        "last_rate_source": "test", "last_rate_stale": False,
    }
    state["cash_usd"] = 800.0
    state["positions"] = {"XLM": {"qty": 1132.0}}
    return wallet_cfg, state


def _run_cycle(wallet_cfg, state, history_all, history_failed_all, run_id):
    fx_resolved = FxRate(rate=1.08, ts=NOW.isoformat(), source="frankfurter", stale=False)
    xlm_quote = Quote(bid=0.1822, ask=0.1832, mid=0.1827, ts=NOW.isoformat(), source="coinbase")
    result = process_wallet(
        wallet_cfg=wallet_cfg,
        state=state,
        run_id=run_id,
        now=NOW,
        prices_all={"XLM": xlm_quote},
        history_all=history_all,
        history_failed_all=history_failed_all,
        fx_resolved=fx_resolved,
        market_open=True,
        strategies_by_name=STRATEGIES,
    )
    decisions_by_symbol = {d["symbol"]: d for d in result.decision_records}
    return result, decisions_by_symbol


def test_transient_history_gap_freezes_position_no_forced_sell():
    """Reproduit le symptôme production (XLM, wallet agressif, 2026-07-23T19) au niveau
    `bot.runner.process_wallet` : l'historique horaire XLM est manquant CE cycle
    (`history_failed_all={"XLM"}`), la cotation XLM reste disponible (comme en production,
    quote_source=coinbase) -- la position ne doit PAS être vendue, et la raison journalisée
    doit distinguer explicitement "position gelée" d'une sortie de tendance."""
    wallet_cfg, state = _base_state()

    result, decisions = _run_cycle(
        wallet_cfg, state, history_all={}, history_failed_all={"XLM"}, run_id="2026-07-23T19",
    )

    assert result.n_trades == 0
    xlm_decision = decisions["XLM"]
    assert xlm_decision["decision"] == "NO_TRADE"
    assert "gelée" in xlm_decision["reason"]
    assert "liquidée" not in xlm_decision["reason"]
    # Position intacte dans le nouvel état (aucune vente déclenchée).
    assert result.new_state["positions"]["XLM"]["qty"] == pytest.approx(1132.0)


def test_history_available_again_next_cycle_gives_normal_signal():
    """Suite du scénario ci-dessus : l'historique redevient disponible au cycle suivant (T20)
    -- le signal redevient normal (pas de trace résiduelle du gel), cohérent avec T18."""
    wallet_cfg, state = _base_state()
    hist = _xlm_hourly_history()

    # T19 : historique manquant (gel).
    result_t19, _ = _run_cycle(
        wallet_cfg, state, history_all={}, history_failed_all={"XLM"}, run_id="2026-07-23T19",
    )
    state = result_t19.new_state

    # T20 : historique de nouveau disponible.
    result_t20, decisions_t20 = _run_cycle(
        wallet_cfg, state, history_all={"XLM": hist}, history_failed_all=set(), run_id="2026-07-23T20",
    )

    xlm_decision = decisions_t20["XLM"]
    assert xlm_decision["strategy_signals"]["quasi_passif_crypto"] > 0.0
    counters = state.get("strategy_state", {}).get("quasi_passif_crypto", {}).get("missing_data_cycles", {})
    # Le compteur de T19 est remis à zéro dès que la donnée redevient disponible.
    assert "XLM" not in (result_t20.new_state.get("strategy_state", {})
                          .get("quasi_passif_crypto", {}).get("missing_data_cycles", {}))


def test_missing_history_for_max_consecutive_cycles_liquidates_with_distinct_reason():
    """Garde-fou N cycles : après `MISSING_DATA_MAX_CYCLES_DEFAULT` cycles consécutifs sans
    historique, la position est liquidée par prudence -- ET la raison journalisée le dit
    explicitement (distincte d'une sortie de tendance normale, cf. ARCHITECTURE.md §12.4)."""
    wallet_cfg, state = _base_state()

    for cycle in range(1, MISSING_DATA_MAX_CYCLES_DEFAULT):
        result, decisions = _run_cycle(
            wallet_cfg, state, history_all={}, history_failed_all={"XLM"}, run_id=f"cycle-{cycle}",
        )
        state = result.new_state
        assert result.n_trades == 0, f"cycle {cycle}: ne devrait pas encore trader"
        assert state["positions"]["XLM"]["qty"] == pytest.approx(1132.0)

    result, decisions = _run_cycle(
        wallet_cfg, state, history_all={}, history_failed_all={"XLM"},
        run_id=f"cycle-{MISSING_DATA_MAX_CYCLES_DEFAULT}",
    )

    assert result.n_trades == 1
    xlm_decision = decisions["XLM"]
    assert xlm_decision["decision"] == "SELL"
    assert "liquidée par prudence" in xlm_decision["reason"]
    assert "cycles consécutifs" in xlm_decision["reason"]
    assert "XLM" not in result.new_state["positions"] or result.new_state["positions"]["XLM"]["qty"] == pytest.approx(0.0)
