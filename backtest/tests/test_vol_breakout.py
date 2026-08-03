"""backtest/tests/test_vol_breakout.py — tests synthétiques de la COUCHE STRATÉGIE (`backtest/
strategies/vol_breakout.py`), jamais de données réelles ici (cf. `backtest/tests/test_engine.py`
pour la même convention côté moteur). Cinq garanties couvertes, directement issues de
`backtest/results/vol_breakout_6majors/SPEC.md` :

  1. `weights_decided` ne contient JAMAIS de `NaN`, y compris pendant tout le warm-up.
  2. Causalité : perturber les données STRICTEMENT APRÈS `t` ne change AUCUN poids `<= t`.
  3. Sortie : le poids retombe à `0.0` exactement quand `close(t) < middle_band(t)`.
  4. Pas d'entrée sans squeeze (même si cassure haussière ET régime haussier avérés).
  5. Pas d'entrée sous le SMA de régime (même si squeeze ET cassure haussière avérés).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.strategies import vol_breakout as vb


def _quiet_then_breakout_then_drop(n: int = 300, seed: int = 1) -> pd.DataFrame:
    """Fixture à deux symboles : `A` traverse une compression de volatilité ("squeeze"), une
    cassure haussière, puis une chute sous la bande médiane ; `B` reste bruyant tout du long
    (jamais de squeeze, contrôle négatif)."""
    rng = np.random.default_rng(seed)
    close_a = np.concatenate(
        [
            100 + rng.normal(0, 0.01, 150),  # quiet -> bandwidth faible -> squeeze
            np.linspace(100, 130, 30),  # cassure haussière
            np.linspace(130, 80, 20),  # chute sous la bande médiane -> sortie attendue
            100 + rng.normal(0, 0.5, n - 200),
        ]
    )[:n]
    close_b = 100 + rng.normal(0, 1.0, n)  # bruyant : jamais de squeeze
    idx = pd.date_range("2022-01-01", periods=n, freq="h")
    return pd.DataFrame({"A": close_a, "B": close_b}, index=idx)


_SMALL_PARAMS = dict(
    window_hours=10,
    squeeze_percentile=0.35,
    squeeze_lookback_hours=40,
    entry_squeeze_lookback_hours=5,
    regime_sma_hours=20,
)


def test_weights_never_nan_including_warmup():
    closes = _quiet_then_breakout_then_drop()
    params = vb.VolBreakoutParams(**_SMALL_PARAMS)
    weights = vb.generate_weight_decisions(closes, params)
    assert not bool(weights.isna().any().any())
    # warm-up explicite : les tout premiers ticks (avant `window_hours`/`regime_sma_hours`
    # observations réelles) doivent être 0.0, jamais NaN.
    assert (weights.iloc[0] == 0.0).all()
    assert weights.shape == closes.shape


def test_weights_never_nan_on_full_universe_scale():
    """Même garantie, à l'échelle des vrais paramètres SPEC (W=110h, régime 4800h) sur une durée
    comparable au jeu de données réel restreinte (warm-up dominant), pour couvrir le cas
    `window_hours`/`squeeze_lookback_hours`/`regime_sma_hours` de la grille pré-enregistrée."""
    rng = np.random.default_rng(3)
    n = 6000
    idx = pd.date_range("2022-01-01", periods=n, freq="h")
    closes = pd.DataFrame(
        {
            "BTC": 100 + np.cumsum(rng.normal(0, 0.05, n)),
            "ETH": 50 + np.cumsum(rng.normal(0, 0.03, n)),
        },
        index=idx,
    )
    grid_subset = [(55, 0.20), (110, 0.35)]
    for window_hours, squeeze_percentile in grid_subset:
        params = vb.VolBreakoutParams(window_hours=window_hours, squeeze_percentile=squeeze_percentile)
        weights = vb.generate_weight_decisions(closes, params)
        assert not bool(weights.isna().any().any())


def test_causality_future_perturbation_does_not_change_past_weights():
    """Perturber `closes` STRICTEMENT APRÈS l'instant `t_cut` ne doit changer AUCUN poids décidé
    à une date `<= t_cut` -- la garantie de causalité centrale exigée par la mission ('perturber
    les données après t ne change pas les poids <= t')."""
    closes = _quiet_then_breakout_then_drop(n=400, seed=5)
    params = vb.VolBreakoutParams(**_SMALL_PARAMS)

    weights_original = vb.generate_weight_decisions(closes, params)

    t_cut = 250
    perturbed = closes.copy()
    rng = np.random.default_rng(99)
    # Perturbation volontairement énorme et déstabilisante (pas un simple bruit), APRÈS t_cut
    # uniquement -- si un seul poids <= t_cut changeait, ce serait une fuite de look-ahead.
    perturbed.iloc[t_cut + 1 :] = perturbed.iloc[t_cut + 1 :] * (
        1.0 + rng.normal(0, 0.3, size=perturbed.iloc[t_cut + 1 :].shape)
    )
    weights_perturbed = vb.generate_weight_decisions(perturbed, params)

    pd.testing.assert_frame_equal(
        weights_original.iloc[: t_cut + 1], weights_perturbed.iloc[: t_cut + 1]
    )
    # Contrôle de non-trivialité : le test doit vraiment exercer une différence après t_cut
    # (sinon il passerait même avec un moteur look-ahead cassé qui ignorerait le futur partout).
    assert not weights_original.iloc[t_cut + 1 :].equals(weights_perturbed.iloc[t_cut + 1 :])


def test_exit_exactly_when_close_below_middle_band():
    """Chaque transition poids 1/6 -> 0.0 doit coïncider EXACTEMENT avec `close(t) < middle(t)`
    (condition de sortie de la SPEC), jamais un autre déclencheur, et le poids ne doit RIEN
    valoir d'autre que `0.0` ou `1/6`."""
    closes = _quiet_then_breakout_then_drop()
    params = vb.VolBreakoutParams(**_SMALL_PARAMS)
    weights = vb.generate_weight_decisions(closes, params)
    middle = closes.rolling(params.window_hours).mean()

    assert set(np.unique(weights.to_numpy())).issubset({0.0, vb.PORTFOLIO_WEIGHT_PER_SYMBOL})

    for sym in weights.columns:
        w = weights[sym]
        was_in_position = w.shift(1).fillna(0.0) > 0.0
        exited_now = was_in_position & (w == 0.0)
        exit_dates = w.index[exited_now]
        assert len(exit_dates) > 0 or sym == "B"  # A doit avoir au moins une sortie observée
        for date in exit_dates:
            assert bool(closes.loc[date, sym] < middle.loc[date, sym]), (
                f"sortie de {sym} à {date} sans close < middle_band"
            )


def test_no_entry_without_squeeze_even_with_breakout_and_regime_ok():
    """Avec `squeeze_percentile=0.0` (jamais satisfaisable : le rank-percentile minimal possible
    sur une fenêtre glissante est `1/lookback_hours > 0`), le squeeze n'est JAMAIS actif -- donc
    AUCUNE entrée, même sur une série construite pour satisfaire cassure haussière ET régime."""
    n = 300
    idx = pd.date_range("2022-01-01", periods=n, freq="h")
    close = np.concatenate(
        [100 + np.linspace(0, 5, 150), np.linspace(105, 140, 150)]
    )  # tendance haussière franche, aucune compression marquée nécessaire pour ce test
    closes = pd.DataFrame({"A": close}, index=idx)

    params_never_squeeze = vb.VolBreakoutParams(
        window_hours=10,
        squeeze_percentile=0.0,
        squeeze_lookback_hours=40,
        entry_squeeze_lookback_hours=5,
        regime_sma_hours=20,
    )
    weights_never_squeeze = vb.generate_weight_decisions(closes, params_never_squeeze)
    assert (weights_never_squeeze["A"] == 0.0).all()

    # Contrôle de non-trivialité : avec un squeeze_percentile permissif (1.0, toujours actif) sur
    # les MÊMES données, au moins une entrée doit apparaître -- sinon le test précédent serait
    # vide de sens (aucune entrée n'était possible de toute façon).
    params_always_squeeze = vb.VolBreakoutParams(
        window_hours=10,
        squeeze_percentile=1.0,
        squeeze_lookback_hours=40,
        entry_squeeze_lookback_hours=5,
        regime_sma_hours=20,
    )
    weights_always_squeeze = vb.generate_weight_decisions(closes, params_always_squeeze)
    assert (weights_always_squeeze["A"] > 0.0).any()


def test_no_entry_below_regime_sma_even_with_squeeze_and_breakout():
    """Une cassure haussière au-dessus de la bande supérieure PENDANT un squeeze actif ne doit
    déclencher AUCUNE entrée si `close(t) <= SMA(close, regime_sma_hours)(t)` -- construit une
    série qui casse fortement au-dessus de sa bande de Bollinger COURTE tout en restant sous sa
    SMA de régime LONGUE (tendance de fond baissière)."""
    n = 400
    idx = pd.date_range("2022-01-01", periods=n, freq="h")
    rng = np.random.default_rng(11)
    # Tendance de fond baissière longue (garde close < SMA longue tout du long), avec une petite
    # compression + cassure locale au milieu (satisfait squeeze + close > upper_band local).
    trend = np.linspace(200, 100, n)
    close = trend + rng.normal(0, 0.05, n)
    close[150:180] = trend[150:180] + rng.normal(0, 0.01, 30)  # compression locale
    close[180:190] = close[179] + np.linspace(0.5, 3.0, 10)  # petite cassure locale, reste << SMA longue
    closes = pd.DataFrame({"A": close}, index=idx)

    params = vb.VolBreakoutParams(
        window_hours=10,
        squeeze_percentile=0.5,
        squeeze_lookback_hours=40,
        entry_squeeze_lookback_hours=5,
        regime_sma_hours=350,  # SMA longue, dominée par la tendance baissière de fond
    )
    weights = vb.generate_weight_decisions(closes, params)
    regime_ok = vb._regime_filter(closes, params.regime_sma_hours)
    assert not bool(regime_ok["A"].any())  # contrôle : le régime est bien "off" tout du long
    assert (weights["A"] == 0.0).all()
