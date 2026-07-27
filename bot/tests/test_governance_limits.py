"""bot/tests/test_governance_limits.py — F3+F4 (validation structurelle, audit adversarial) :
vérifie que `bot/config.py` respecte les LIMITES STRUCTURELLES chiffrées de
`docs/PROMOTION-RULES.md` (§4.1 capacité du labo, §2.4/§4.2 capacité par wallet réel) et que
chaque entrée de `INCUBATING_STRATEGIES` respecte le schéma documenté (bandeau de tête de ce
bloc dans `bot/config.py`).

Ce fichier ne modifie AUCUNE constante -- il ne fait que lire `bot.config` et échouer bruyamment
si une future entrée d'incubation ou une future poche de wallet réel viole une des règles
gravées de `docs/PROMOTION-RULES.md`, plutôt que de laisser une violation passer inaperçue.
"""

from __future__ import annotations

from bot import config

REQUIRED_INCUBATION_FIELDS = {
    "id",
    "module",
    "params",
    "asset_class",
    "univers",
    "capital_alloc_pct",
    "entered_at",
    "entry_run_id",
}
VALID_ASSET_CLASSES = {"crypto", "equities", "etf"}


def _known_equity_etf_symbols() -> set:
    """Union de tout symbole action/ETF connu de `bot/config.py` -- source de vérité la plus
    complète disponible (cf. `bot.config.SYMBOLS_EQUITY`, déjà l'union documentée de
    `EQUITIES_SP100_UNIVERSE`/`EQUITIES_MARKET_FILTER_SYMBOL`/`ETF_RISKY_UNIVERSE`/
    `ETF_BOND_BOGEY`, cf. bandeau de tête de ce bloc dans `bot/config.py`)."""
    return set(config.SYMBOLS_EQUITY)


# ------------------------------------------------------------------------------------------
# F4.1 -- capacité du labo : max 3 candidates simultanées (docs/PROMOTION-RULES.md §4.1)
# ------------------------------------------------------------------------------------------


def test_incubating_strategies_never_exceeds_3_candidates():
    assert len(config.INCUBATING_STRATEGIES) <= 3, (
        f"{len(config.INCUBATING_STRATEGIES)} candidates en incubation simultanée -- dépasse "
        "la limite structurelle §4.1 de docs/PROMOTION-RULES.md (max 3)"
    )


# ------------------------------------------------------------------------------------------
# F4.2/F2.4 -- capacité de chaque wallet réel : max 5 stratégies actives (poches non-cash),
# docs/PROMOTION-RULES.md §2.4/§4.2
# ------------------------------------------------------------------------------------------


def test_each_real_wallet_has_at_most_5_non_cash_pockets():
    for wallet_cfg in config.WALLETS:
        non_cash_pockets = [
            p for p in wallet_cfg.get("pockets", []) or [] if p.get("asset_class") != "cash"
        ]
        assert len(non_cash_pockets) <= 5, (
            f"wallet {wallet_cfg['id']!r} : {len(non_cash_pockets)} poches non-cash -- dépasse "
            "la limite structurelle §2.4/§4.2 de docs/PROMOTION-RULES.md (max 5 stratégies "
            "actives par wallet réel)"
        )


# ------------------------------------------------------------------------------------------
# F3 -- schéma de chaque entrée d'incubation (bandeau INCUBATING_STRATEGIES, bot/config.py)
# ------------------------------------------------------------------------------------------


def test_each_incubating_entry_has_required_schema_fields():
    for entry in config.INCUBATING_STRATEGIES:
        missing = REQUIRED_INCUBATION_FIELDS - set(entry.keys())
        assert not missing, (
            f"entrée d'incubation {entry.get('id', '?')!r} : champs obligatoires manquants "
            f"{sorted(missing)} (schéma bandeau INCUBATING_STRATEGIES, bot/config.py)"
        )


def test_each_incubating_entry_capital_alloc_pct_in_unit_interval():
    for entry in config.INCUBATING_STRATEGIES:
        alloc = float(entry["capital_alloc_pct"])
        assert 0.0 <= alloc <= 1.0, (
            f"entrée d'incubation {entry.get('id', '?')!r} : capital_alloc_pct={alloc} hors de "
            "[0, 1]"
        )


def test_each_incubating_entry_asset_class_is_valid():
    for entry in config.INCUBATING_STRATEGIES:
        assert entry["asset_class"] in VALID_ASSET_CLASSES, (
            f"entrée d'incubation {entry.get('id', '?')!r} : asset_class={entry['asset_class']!r} "
            f"invalide (attendu un de {sorted(VALID_ASSET_CLASSES)})"
        )


def test_each_crypto_incubating_entry_univers_disjoint_from_known_equities_etf():
    """F2/F3 combinés : une candidate déclarant `asset_class == "crypto"` ne doit JAMAIS lister
    un symbole action/ETF CONNU dans son `univers` -- c'est exactement le vecteur d'attaque F2
    (`bot/runner.py:_asset_class_of`, corrigé indépendamment mais dont cette validation
    structurelle est la seconde ligne de défense, au niveau de la config elle-même)."""
    known = _known_equity_etf_symbols()
    for entry in config.INCUBATING_STRATEGIES:
        if entry.get("asset_class") != "crypto":
            continue
        univers = set(entry.get("univers", []) or [])
        overlap = univers & known
        assert not overlap, (
            f"entrée d'incubation {entry.get('id', '?')!r} : asset_class='crypto' mais univers "
            f"chevauche des symboles actions/ETF CONNUS {sorted(overlap)} -- interdit (vecteur "
            "d'attaque F2, cf. bot/runner.py:_asset_class_of)"
        )


# ------------------------------------------------------------------------------------------
# Garde-fou de non-régression : le schéma validé ci-dessus est bien exigeant (test négatif que
# ces validations attraperaient réellement une violation, sans dépendre de l'état actuel vide
# de INCUBATING_STRATEGIES).
# ------------------------------------------------------------------------------------------


def test_schema_validation_would_actually_catch_a_malformed_entry():
    malformed_missing_field = {
        "id": "x", "module": "bot.strategies.x", "params": {}, "asset_class": "crypto",
        "univers": ["BTC"], "capital_alloc_pct": 0.1, "entered_at": "2026-07-23T00:00:00+00:00",
        # entry_run_id manquant volontairement
    }
    assert REQUIRED_INCUBATION_FIELDS - set(malformed_missing_field.keys()) == {"entry_run_id"}

    malformed_alloc = {"capital_alloc_pct": 1.5}
    assert not (0.0 <= float(malformed_alloc["capital_alloc_pct"]) <= 1.0)

    malformed_asset_class = {"asset_class": "forex"}
    assert malformed_asset_class["asset_class"] not in VALID_ASSET_CLASSES

    malformed_crypto_with_equity = {"asset_class": "crypto", "univers": ["AAPL", "BTC"]}
    known = _known_equity_etf_symbols()
    assert set(malformed_crypto_with_equity["univers"]) & known == {"AAPL"}
