"""backtest/tests/test_run_funding_carry_carry_option.py — couverture dédiée de l'option
`--carry-across-windows` de `backtest/run_funding_carry.py` (CARRY-EXTENSION-SPEC.md §7) :
threading `carry_in`/`carry_out` entre fenêtres OOS contiguës au niveau du RUNNER, et journal
d'évènement quand une frontière n'est PAS contiguë. Fixture synthétique rapide et déterministe
(jamais de données réelles ici, même esprit que `backtest/tests/test_funding_carry.py`) --
`fcarry.PARAM_GRID` est monkeypatché à UNE seule combinaison pour que `select_params_via_is` ne
simule PAS la fenêtre IS (`len(grid) <= 1` -> pas de sélection, cf. `engine.select_params_via_
is`), le test portant uniquement sur l'orchestration OOS/portage, pas sur la sélection IS."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from backtest import engine
from backtest import run_funding_carry as rfc
from backtest.strategies import funding_carry as fcarry


class _FakeWeightsCache:
    """Remplace `run_funding_carry.WeightsCache` : renvoie toujours la MÊME matrice de poids
    quels que soient les `params` (indifférent ici, `PARAM_GRID` monkeypatché à 1 combinaison
    -- seule l'orchestration walk-forward/portage est testée, jamais le signal funding_carry
    lui-même, déjà couvert par `test_funding_carry.py`)."""

    def __init__(self, weights: pd.DataFrame):
        self._weights = weights

    def get(self, params):
        return self._weights

    def provider(self):
        return lambda params: self._weights


@pytest.fixture()
def _tiny_perp_fixture(monkeypatch):
    monkeypatch.setattr(fcarry, "PARAM_GRID", [{"window_days": 7, "theta_in": 0.05}])
    n = 50
    cal = pd.date_range("2022-01-01", periods=n, freq="h", tz="UTC")
    rng = np.random.default_rng(1)
    price = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.002, n))
    opens = pd.DataFrame({"X": price, "X-PERP": price}, index=cal)
    closes = pd.DataFrame({"X": price, "X-PERP": price}, index=cal)
    highs = pd.DataFrame({"X-PERP": price * 1.5}, index=cal)
    lows = pd.DataFrame({"X-PERP": price * 0.5}, index=cal)
    funding = pd.DataFrame({"X-PERP": rng.normal(0.0002, 0.0001, n)}, index=cal)
    w = pd.DataFrame({"X": 0.3, "X-PERP": -0.3}, index=cal)
    sim_kwargs = dict(
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows, perp_cost_bps=5.0,
        apply_vol_targeting=False, no_trade_band=0.0,
    )
    return cal, opens, closes, w, sim_kwargs


def _windows_contiguous(cal):
    W = engine.WalkForwardWindow
    return [
        W(index=0, is_start=cal[0], is_end=cal[9], oos_start=cal[10], oos_end=cal[19],
          is_start_idx=1, is_end_idx=9, oos_start_idx=10, oos_end_idx=19),
        W(index=1, is_start=cal[0], is_end=cal[9], oos_start=cal[20], oos_end=cal[29],
          is_start_idx=1, is_end_idx=9, oos_start_idx=20, oos_end_idx=29),
        W(index=2, is_start=cal[0], is_end=cal[9], oos_start=cal[30], oos_end=cal[39],
          is_start_idx=1, is_end_idx=9, oos_start_idx=30, oos_end_idx=39),
    ]


def test_carry_across_windows_default_off_never_threads_state(_tiny_perp_fixture):
    """Spec §1.1/§7 : `carry_across_windows=False` (défaut) -- comportement STRICTEMENT
    identique à avant l'option (jamais de `carry_in` transmis, `carry_boundary_events` vide,
    aucune clé `carry_in_used`/`carry_out_has_open_position` dans `per_window`)."""
    cal, opens, closes, w, sim_kwargs = _tiny_perp_fixture
    windows = _windows_contiguous(cal)
    result = rfc.run_walkforward(windows, cal, opens, closes, _FakeWeightsCache(w), 5.0, sim_kwargs)
    assert result["carry_boundary_events"] == []
    for pw in result["per_window"]:
        assert "carry_in_used" not in pw
        assert "carry_out_has_open_position" not in pw


def test_carry_across_windows_threads_contiguous_boundaries(_tiny_perp_fixture):
    """Spec §7 : fenêtres OOS calendaires-adjacentes -> `carry_in` de la fenêtre k+1 EST
    `carry_out` de la fenêtre k (jamais pour la toute première fenêtre, qui n'a pas de
    précédent)."""
    cal, opens, closes, w, sim_kwargs = _tiny_perp_fixture
    windows = _windows_contiguous(cal)
    result = rfc.run_walkforward(
        windows, cal, opens, closes, _FakeWeightsCache(w), 5.0, sim_kwargs, carry_across_windows=True
    )
    assert result["carry_boundary_events"] == []
    per_window = result["per_window"]
    assert per_window[0]["carry_in_used"] is False  # 1ère fenêtre : rien à porter
    assert per_window[1]["carry_in_used"] is True
    assert per_window[2]["carry_in_used"] is True
    # La position (short perp + long spot constant) reste ouverte tout du long -- portée en
    # continu, jamais forcée à se fermer par le découpage en fenêtres (spec §4.1).
    assert all(pw["carry_out_has_open_position"] for pw in per_window)
    assert result["concatenated"]["n_trades_closed"] == 0


def test_carry_across_windows_journals_non_contiguous_boundary(_tiny_perp_fixture):
    """Spec §1.3/§7 : une frontière OOS NON calendaire-adjacente -> `carry_in=None` pour cette
    fenêtre (démarrage à plat) + évènement journalisé dans `carry_boundary_events` (jamais
    silencieux)."""
    cal, opens, closes, w, sim_kwargs = _tiny_perp_fixture
    windows = _windows_contiguous(cal)
    W = engine.WalkForwardWindow
    windows_gap = windows[:2] + [
        W(index=2, is_start=cal[0], is_end=cal[9], oos_start=cal[35], oos_end=cal[44],
          is_start_idx=1, is_end_idx=9, oos_start_idx=35, oos_end_idx=44)
    ]
    result = rfc.run_walkforward(
        windows_gap, cal, opens, closes, _FakeWeightsCache(w), 5.0, sim_kwargs, carry_across_windows=True
    )
    assert len(result["carry_boundary_events"]) == 1
    ev = result["carry_boundary_events"][0]
    assert ev["window_index"] == 2
    assert ev["prev_carry_last_idx"] == 29
    assert ev["oos_start_idx"] == 35
    # La 3e fenêtre démarre bien à PLAT malgré l'option active (pas de carry_in transmis).
    assert result["per_window"][2]["carry_in_used"] is False


def _fake_full_data(seed: int, universe, perp_cols, n: int):
    """Univers/calendrier synthétiques (spot+perp) assez longs pour que `run_funding_carry.
    main()` génère AU MOINS 1 fenêtre walk-forward avec des mois IS/OOS/pas RÉDUITS
    (monkeypatchés à 1 mois chacun) -- univers réduit à 2 symboles (monkeypatché, cf.
    `_patch_main_for_fast_smoke_run`) pour que ce smoke-test de bout en bout de `main()` reste
    rapide (le moteur est un vrai simulateur par bougie -- cf. docstring de ce script,
    "~6.6ms/bougie sur 12 colonnes")."""
    # NOTE : calendrier tz-NAIF délibérément (contrairement aux données réelles horaires, cf.
    # `backtest/data_hourly.py`, tz-UTC) -- `run_funding_carry.subperiod_sharpe` compare
    # `returns.index < SUBPERIOD_SPLIT_DATE` (Timestamp tz-naïf) et lève `TypeError` sur un
    # index tz-aware avec les versions récentes de pandas ; bug PRÉEXISTANT, sans rapport avec
    # cette extension (aucune ligne de `subperiod_sharpe` n'est touchée par CARRY-EXTENSION-
    # SPEC.md) -- contourné ICI côté fixture de test uniquement, jamais dans le script lui-même
    # (hors périmètre de cette mission), pour permettre un smoke-test rapide de bout en bout de
    # `main()`. Documenté dans le rapport d'implémentation.
    cal = pd.date_range("2022-04-03", periods=n, freq="h")
    rng = np.random.default_rng(seed)
    price_by_sym = {s: 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.002, n)) for s in universe}
    spot_opens = pd.DataFrame(price_by_sym, index=cal)
    spot_closes = spot_opens.copy()
    perp_price = {c: price_by_sym[s] for c, s in zip(perp_cols, universe)}
    perp_opens = pd.DataFrame(perp_price, index=cal)
    perp_closes = perp_opens.copy()
    opens = pd.concat([spot_opens, perp_opens], axis=1)
    closes = pd.concat([spot_closes, perp_closes], axis=1)
    highs = pd.concat([spot_opens, perp_opens * 1.5], axis=1)
    lows = pd.concat([spot_opens, perp_opens * 0.5], axis=1)
    funding = pd.DataFrame({c: 0.0001 for c in perp_cols}, index=cal)
    return {
        "calendar": cal, "spot_opens": spot_opens, "spot_closes": spot_closes, "perp_closes": perp_closes,
        "opens": opens, "closes": closes, "highs": highs, "lows": lows, "funding": funding,
        "real_gaps_spot": {s: 0 for s in universe}, "n_nan_spot": 0, "n_nan_perp": 0,
        "funding_orphans_report": {"total": 0},
    }


def _patch_main_for_fast_smoke_run(monkeypatch, seed: int):
    # Univers réduit à 2 symboles (au lieu des 6 de SPEC.md) + une seule fenêtre walk-forward
    # (62 jours, IS/OOS/pas = 1 mois chacun) : ce test couvre le CÂBLAGE `main()` <->
    # `run_walkforward(carry_across_windows=...)` <-> `results.json`, pas l'exactitude
    # économique du moteur (déjà couverte exhaustivement par `test_carry.py`) -- un univers/
    # historique réduits ne changent rien à la garantie testée ici.
    tiny_universe = ["BTC", "ETH"]
    tiny_perp_cols = [f"{s}-PERP" for s in tiny_universe]
    monkeypatch.setattr(fcarry, "PARAM_GRID", [{"window_days": 7, "theta_in": 0.05}])
    monkeypatch.setattr(rfc, "UNIVERSE", tiny_universe)
    monkeypatch.setattr(rfc, "PERP_COLS", tiny_perp_cols)
    monkeypatch.setattr(rfc, "IS_MONTHS", 1)
    monkeypatch.setattr(rfc, "OOS_MONTHS", 1)
    monkeypatch.setattr(rfc, "STEP_MONTHS", 1)
    monkeypatch.setattr(rfc, "EXPECTED_N_WINDOWS", -1)  # désactive l'alerte "nombre de fenêtres inattendu"
    n = 24 * 62  # -> exactement 1 fenêtre IS=1m/OOS=1m/pas=1m (vérifié empiriquement)
    monkeypatch.setattr(rfc, "load_all_data", lambda data_dir: _fake_full_data(seed, tiny_universe, tiny_perp_cols, n))


def test_main_results_json_omits_carry_block_by_default(monkeypatch, tmp_path):
    """Spec §7 dernier point : sans `--carry-across-windows`, `results.json` ne contient PAS la
    clé `carry_across_windows_informative` -- format existant strictement inchangé. Données
    chargées via un `load_all_data` monkeypatché (aucune dépendance à `_data/`, rapide)."""
    _patch_main_for_fast_smoke_run(monkeypatch, seed=2)
    monkeypatch.setattr("sys.argv", ["run_funding_carry.py", "--output-dir", str(tmp_path)])

    results, output_dir = rfc.main()
    assert "carry_across_windows_informative" not in results
    import json

    with open(output_dir / "results.json") as f:
        on_disk = json.load(f)
    assert "carry_across_windows_informative" not in on_disk


def test_main_results_json_includes_carry_block_when_enabled(monkeypatch, tmp_path):
    """Spec §7 : `--carry-across-windows` ajoute le bloc informatif. L'indépendance du verdict
    `promotion_rules_1_2_thresholds_verdict` vis-à-vis de ce bloc est garantie PAR CONSTRUCTION
    (le verdict est calculé, dans le CODE SOURCE de `main()`, à partir de `cand_concat`/
    `candidate_result` -- produits par le run NOMINAL, TOUJOURS à plat entre fenêtres -- avant
    même que le bloc `carry_across_windows_informative` ne soit assemblé plus bas) ; couvert
    empiriquement par `test_carry_across_windows_default_off_never_threads_state` ci-dessus, qui
    prouve que `carry_across_windows=False` ne modifie STRICTEMENT rien à `run_walkforward`."""
    _patch_main_for_fast_smoke_run(monkeypatch, seed=2)
    monkeypatch.setattr("sys.argv", ["run_funding_carry.py", "--output-dir", str(tmp_path), "--carry-across-windows"])

    results, output_dir = rfc.main()
    assert "carry_across_windows_informative" in results
    block = results["carry_across_windows_informative"]
    assert block["carry_across_windows"] is True
    assert "per_window" in block and "concatenated" in block
    assert "n_carry_boundary_events" in block
    assert "promotion_rules_1_2_thresholds_verdict" in results
    assert "carry" not in json.dumps(results["promotion_rules_1_2_thresholds_verdict"]).lower()
