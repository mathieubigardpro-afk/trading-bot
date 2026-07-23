"""Garde-fou de synchronisation entre `bot/config.py` (univers actions/ETF, dupliqués pour que
`bot.config` reste sans dépendance sur `bot.strategies`, cf. bandeau de tête de ce bloc dans
`bot/config.py`) et les constantes de SPEC portées par les modules de stratégie concrets
(`bot/strategies/xs_momentum_sp100.py`, `bot/strategies/dual_momentum_etf.py`).

Un désalignement entre les deux romprait silencieusement le routage `bot.feeds.get_prices()`
(un symbole suivi par une stratégie mais absent de `bot.config.SYMBOLS_EQUITY` ne serait jamais
reconnu comme "actions/ETF" par `bot.feeds._config_fallback`, donc jamais coté) — ce test rend
cette classe de bug impossible à introduire sans le remarquer."""

from __future__ import annotations

import pytest

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
        uses_quasi_passif = any(
            p.get("strategy_ref") == "quasi_passif_crypto" for p in wallet_cfg.get("pockets", [])
        )
        if not uses_quasi_passif:
            # Wallet dynamique (labo 🧪, docs/ARCHITECTURE.md § Labo) sans candidate crypto en
            # incubation référençant cette stratégie : rien à synchroniser pour l'instant --
            # l'univers crypto du labo est dérivé de bot.config.INCUBATING_STRATEGIES, jamais
            # de SPEC_UNIVERSE_BY_WALLET (qui ne couvre que les 3 variantes SPEC de production).
            continue
        spec_universe = SPEC_UNIVERSE_BY_WALLET.get(wallet_cfg["id"])
        assert spec_universe is not None
        assert set(wallet_cfg["univers_crypto"]) == set(spec_universe), (
            f"wallet {wallet_cfg['id']}: bot.config univers_crypto désynchronisé de "
            "quasi_passif_crypto.SPEC_UNIVERSE_BY_WALLET"
        )


def test_pockets_capital_alloc_pct_sums_to_at_most_one_per_wallet():
    for wallet_cfg in config.WALLETS:
        total = sum(float(p["capital_alloc_pct"]) for p in wallet_cfg.get("pockets", []))
        if wallet_cfg["id"] == config.LABO_WALLET_ID:
            # Poches DYNAMIQUES (docs/ARCHITECTURE.md § Labo) : 0 tant qu'aucune candidate
            # n'est en incubation (état ATTENDU aujourd'hui, cf. test_labo.py), jusqu'à 1.0 une
            # fois des candidates ajoutées à bot.config.INCUBATING_STRATEGIES -- jamais plus
            # (même invariant "somme <= 1.0, réserve cash implicite" que les 3 wallets réels).
            assert 0.0 <= total <= 1.0 + 1e-9, (
                f"wallet labo: somme des capital_alloc_pct = {total} (attendu 0 <= x <= 1.0)"
            )
            continue
        assert 0.99 <= total <= 1.0 + 1e-9, (
            f"wallet {wallet_cfg['id']}: somme des capital_alloc_pct = {total} (attendu ~1.0)"
        )


def test_pockets_strategy_ref_resolves_to_a_known_strategy_or_cash():
    known_strategy_names = {"quasi_passif_crypto", "xs_momentum_sp100", "dual_momentum_etf"}
    # Candidates actuellement en incubation dans le labo (vide aujourd'hui, cf. § Labo) : leur
    # `strategy_ref` (== leur `id` d'incubation) est également un nom "connu" par construction.
    known_strategy_names |= {c["id"] for c in config.INCUBATING_STRATEGIES}
    for wallet_cfg in config.WALLETS:
        for pocket in wallet_cfg.get("pockets", []):
            ref = pocket.get("strategy_ref")
            if pocket["asset_class"] == "cash":
                assert ref is None
            else:
                assert ref in known_strategy_names


# --- Wallet labo 🧪 (docs/ARCHITECTURE.md § Labo) --------------------------------------


def test_labo_wallet_exists_with_expected_identity_and_capital():
    labo = config.wallet_config(config.LABO_WALLET_ID)
    assert labo["emoji"] == "🧪"
    assert labo["label"] == "Labo — incubateur"
    assert labo["capital_initial_eur"] == pytest.approx(1000.0)
    assert config.LABO_WALLET_ID in config.WALLET_IDS
    assert config.LABO_WALLET_ID not in config.PRODUCTION_WALLET_IDS
    assert set(config.PRODUCTION_WALLET_IDS) == {"prudent", "equilibre", "agressif"}


def test_labo_risk_profile_matches_mission_spec():
    """vol cible 20%, expo max 70%, cap 20%/actif, breakers 3%/15%/25% (mission § Labo)."""
    risque = config.wallet_config(config.LABO_WALLET_ID)["risque"]
    assert risque["vol_target_annualized"] == pytest.approx(0.20)
    assert risque["gross_exposure_max"] == pytest.approx(0.70)
    assert risque["cap_per_asset"] == pytest.approx(0.20)
    assert risque["cb_daily_loss_freeze_pct"] == pytest.approx(0.03)
    assert risque["cb_dd_half_size_pct"] == pytest.approx(0.15)
    assert risque["cb_dd_flatten_pct"] == pytest.approx(0.25)


def test_incubating_strategies_empty_and_labo_pockets_dynamic_and_empty():
    """État attendu aujourd'hui (mission point 3) : aucune candidate en incubation -> le labo
    naît avec des poches/univers vides et reste en cash."""
    assert config.INCUBATING_STRATEGIES == []
    assert config.labo_pockets() == []
    assert config.labo_crypto_universe() == []
    labo = config.wallet_config(config.LABO_WALLET_ID)
    assert labo["pockets"] == []
    assert labo["univers_crypto"] == []
    assert config.incubating_strategy("anything") is None


def test_labo_pockets_and_universe_derive_from_incubating_strategies(monkeypatch):
    fake = [
        {
            "id": "candidate_x",
            "module": "bot.strategies.candidate_x",
            "params": {"lookback": 30},
            "asset_class": "crypto",
            "univers": ["BTC", "ETH"],
            "capital_alloc_pct": 0.4,
            "entered_at": "2026-07-23T00:00:00+00:00",
            "entry_run_id": "2026-07-23T10",
        }
    ]
    monkeypatch.setattr(config, "INCUBATING_STRATEGIES", fake)

    pockets = config.labo_pockets()
    assert pockets == [
        {
            "asset_class": "crypto",
            "capital_alloc_pct": 0.4,
            "strategy_ref": "candidate_x",
            "univers": ["BTC", "ETH"],
        }
    ]
    assert config.labo_crypto_universe() == ["BTC", "ETH"]
    assert config.incubating_strategy("candidate_x") == fake[0]
    assert config.incubating_strategy("does_not_exist") is None
