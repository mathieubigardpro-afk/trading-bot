"""Garde-fou de synchronisation entre `bot/config.py` (univers actions/ETF, dupliqués pour que
`bot.config` reste sans dépendance sur `bot.strategies`, cf. bandeau de tête de ce bloc dans
`bot/config.py`) et les constantes de SPEC portées par les modules de stratégie concrets
(`bot/strategies/xs_momentum_sp100.py`, `bot/strategies/dual_momentum_etf.py`).

Un désalignement entre les deux romprait silencieusement le routage `bot.feeds.get_prices()`
(un symbole suivi par une stratégie mais absent de `bot.config.SYMBOLS_EQUITY` ne serait jamais
reconnu comme "actions/ETF" par `bot.feeds._config_fallback`, donc jamais coté) — ce test rend
cette classe de bug impossible à introduire sans le remarquer."""

from __future__ import annotations

from bot import config
from bot.strategies.dual_momentum_etf import BOND_BOGEY, RISKY_UNIVERSE
from bot.strategies.xs_momentum_sp100 import MARKET_FILTER_SYMBOL, UNIVERSE_SP100


def test_equities_sp100_universe_matches_strategy_module():
    assert set(config.EQUITIES_SP100_UNIVERSE) == set(UNIVERSE_SP100)
    assert len(config.EQUITIES_SP100_UNIVERSE) == len(UNIVERSE_SP100) == 103


def test_market_filter_symbol_matches_strategy_module():
    assert config.EQUITIES_MARKET_FILTER_SYMBOL == MARKET_FILTER_SYMBOL


def test_etf_universe_matches_strategy_module():
    assert set(config.ETF_RISKY_UNIVERSE) == set(RISKY_UNIVERSE)
    assert config.ETF_BOND_BOGEY == BOND_BOGEY


def test_symbols_equity_is_a_superset_needed_for_price_routing():
    needed = (
        set(config.EQUITIES_SP100_UNIVERSE)
        | {config.EQUITIES_MARKET_FILTER_SYMBOL}
        | set(config.ETF_RISKY_UNIVERSE)
        | {config.ETF_BOND_BOGEY}
    )
    assert needed <= set(config.SYMBOLS_EQUITY)


def test_runner_pocket_universes_match_config():
    import bot.runner as runner

    assert set(runner.EQUITIES_TRADABLE_SYMBOLS) == set(config.EQUITIES_SP100_UNIVERSE)
    assert set(runner.ETF_TRADABLE_SYMBOLS) == set(config.ETF_RISKY_UNIVERSE) | {config.ETF_BOND_BOGEY}


def test_agressif_crypto_universe_matches_quasi_passif_spec_variant():
    from bot.strategies.quasi_passif_crypto import SPEC_UNIVERSE_BY_WALLET

    for wallet_cfg in config.WALLETS:
        spec_universe = SPEC_UNIVERSE_BY_WALLET.get(wallet_cfg["id"])
        assert spec_universe is not None
        assert set(wallet_cfg["univers_crypto"]) == set(spec_universe), (
            f"wallet {wallet_cfg['id']}: bot.config univers_crypto désynchronisé de "
            "quasi_passif_crypto.SPEC_UNIVERSE_BY_WALLET"
        )


def test_pockets_capital_alloc_pct_sums_to_at_most_one_per_wallet():
    for wallet_cfg in config.WALLETS:
        total = sum(float(p["capital_alloc_pct"]) for p in wallet_cfg.get("pockets", []))
        assert 0.99 <= total <= 1.0 + 1e-9, (
            f"wallet {wallet_cfg['id']}: somme des capital_alloc_pct = {total} (attendu ~1.0)"
        )


def test_pockets_strategy_ref_resolves_to_a_known_strategy_or_cash():
    known_strategy_names = {"quasi_passif_crypto", "xs_momentum_sp100", "dual_momentum_etf"}
    for wallet_cfg in config.WALLETS:
        for pocket in wallet_cfg.get("pockets", []):
            ref = pocket.get("strategy_ref")
            if pocket["asset_class"] == "cash":
                assert ref is None
            else:
                assert ref in known_strategy_names
