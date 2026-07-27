"""bot/tests/test_runner_asset_class_security.py — F2 (MAJEUR, audit adversarial) :
`bot/runner.py` forçait `market_open=True` pour tout symbole de `wallet_cfg["univers_crypto"]`,
lequel dérive (pour le wallet labo) du champ déclaratif `asset_class` d'une entrée
`INCUBATING_STRATEGIES` (`bot.config.labo_crypto_universe()`). Une candidate labo déclarant
`asset_class="crypto"` tout en listant un symbole action réel (ex. AAPL) dans son `univers`
contournait ainsi le gate horaires NYSE (`is_us_market_open`, ARCHITECTURE.md §7) pour ce
symbole -- scénario reproduit ci-dessous : marché fermé, la candidate ne doit JAMAIS pouvoir
faire exécuter un ordre sur AAPL.
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
from bot.persist.state import init_state
from bot.strategies import StrategyBase

NOW_MARKET_CLOSED = datetime(2026, 7, 25, 3, 0, 0, tzinfo=timezone.utc)  # samedi, NYSE fermé


class _FakeCryptoDeclaredEquityCandidate(StrategyBase):
    """Candidate malveillante/mal configurée : déclare `asset_class="crypto"` (poche du wallet
    labo) mais cible AAPL (une action réelle) -- scénario d'attaque F2."""

    name = "evil_crypto_declared_aapl"

    def target_weights(
        self,
        history: Dict[str, pd.DataFrame],
        state: dict,
        profile: Optional[dict] = None,
    ) -> Dict[str, float]:
        return {"AAPL": 0.9}


def _labo_cfg_with_crypto_declared_aapl_candidate() -> dict:
    base = config.wallet_config(config.LABO_WALLET_ID)
    cfg = dict(base)
    cfg["pockets"] = [
        {
            "asset_class": "crypto",  # déclaration mensongère : AAPL n'est pas une crypto
            "capital_alloc_pct": 0.5,
            "strategy_ref": "evil_crypto_declared_aapl",
            "univers": ["AAPL"],
        },
    ]
    # Reproduit EXACTEMENT `bot.config.labo_crypto_universe()` : l'univers crypto du wallet
    # labo est l'union des `univers` des candidates `asset_class == "crypto"` -- ici AAPL,
    # injecté par la candidate malveillante.
    cfg["univers_crypto"] = ["AAPL"]
    return cfg


def test_asset_class_of_never_classifies_a_known_equity_as_crypto_even_if_declared_so():
    """Test unitaire direct sur le choke point corrigé : `_asset_class_of` doit classer AAPL
    comme "equities", jamais "crypto", même si `crypto_universe` le contient (poche
    malveillante/mal configurée)."""
    assert runner._asset_class_of("AAPL", crypto_universe=["AAPL"]) == "equities"
    # Non-régression : un symbole crypto légitime, absent des ensembles actions/ETF connus,
    # reste bien classé "crypto".
    assert runner._asset_class_of("BTC", crypto_universe=["BTC", "ETH"]) == "crypto"


def test_labo_candidate_declaring_crypto_with_aapl_cannot_trade_when_nyse_is_closed():
    """Scénario d'attaque complet (F2) : candidate labo `asset_class="crypto"` ciblant AAPL à
    90%, marché NYSE FERMÉ (`NOW_MARKET_CLOSED`) -- le correctif doit produire NO_TRADE sur
    AAPL avec `market_open=False`, jamais un ordre exécuté."""
    labo_cfg = _labo_cfg_with_crypto_declared_aapl_candidate()
    strategies_by_name = {"evil_crypto_declared_aapl": _FakeCryptoDeclaredEquityCandidate()}

    state = init_state(config.LABO_WALLET_ID, labo_cfg["capital_initial_eur"])
    fx_resolved = FxRate(rate=1.08, ts=NOW_MARKET_CLOSED.isoformat(), source="frankfurter", stale=False)
    prices = {
        "AAPL": Quote(bid=199.9, ask=200.1, mid=200.0, ts=NOW_MARKET_CLOSED.isoformat(), source="fake"),
    }

    result = runner.process_wallet(
        labo_cfg, state, "2026-07-25T03", NOW_MARKET_CLOSED,
        prices_all=prices, history_all={}, history_failed_all=set(), fx_resolved=fx_resolved,
        market_open=False,  # is_us_market_open(NOW_MARKET_CLOSED) serait également False (samedi)
        strategies_by_name=strategies_by_name,
    )

    assert result.n_trades == 0, "un ordre a été exécuté sur AAPL alors que le marché NYSE est fermé"
    aapl_decisions = [d for d in result.decision_records if d["symbol"] == "AAPL"]
    assert len(aapl_decisions) == 1
    decision = aapl_decisions[0]
    assert decision["asset_class"] == "equities", (
        "AAPL classé comme autre chose que 'equities' malgré la déclaration crypto mensongère "
        "de la poche -- régression du correctif F2"
    )
    assert decision["market_open"] is False, (
        "market_open forcé à True pour AAPL via une poche 'crypto' déclarative -- régression F2"
    )
    assert decision["decision"] == "NO_TRADE"
    assert "marché actions/ETF fermé" in decision["reason"]
    assert "AAPL" not in (state.get("positions") or {})


def test_labo_candidate_declaring_crypto_with_aapl_uninitialized_wallet_branch():
    """Même scénario d'attaque, mais sur le chemin \"wallet pas encore initialisé\" (FX
    indisponible, cf. `process_wallet` ~ligne 649) -- l'autre point où `market_open` était
    forcé à `True` pour tout symbole de `crypto_universe` avant correctif."""
    labo_cfg = _labo_cfg_with_crypto_declared_aapl_candidate()
    state = init_state(config.LABO_WALLET_ID, labo_cfg["capital_initial_eur"])

    result = runner.process_wallet(
        labo_cfg, state, "2026-07-25T03", NOW_MARKET_CLOSED,
        prices_all={}, history_all={}, history_failed_all=set(), fx_resolved=None,
        market_open=False,
    )

    assert result.initialized is False
    aapl_decisions = [d for d in result.decision_records if d["symbol"] == "AAPL"]
    assert len(aapl_decisions) == 1
    assert aapl_decisions[0]["asset_class"] == "equities"
    assert aapl_decisions[0]["market_open"] is False
