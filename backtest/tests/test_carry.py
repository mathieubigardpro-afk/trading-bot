"""backtest/tests/test_carry.py — preuves exigées par `backtest/CARRY-EXTENSION-SPEC.md` §6
pour le portage optionnel de la position entre fenêtres OOS contiguës (`simulate_segment(...,
carry_in=...)` / `SegmentResult.carry_out`). Six garanties couvertes :

  1. Rétro-compatibilité bit-à-bit (`carry_in=None`, défaut) -- comparaison à un hash capturé
     AVANT toute modification d'`engine.py` (§1.1a/§1.1b/§6.1).
  2. Reconstruction de segment (§3) : formule exacte, aucun coût d'entrée, `ValueError` sur prix
     manquant/contiguïté rompue, poids nul omis (sauf PnL substantiel associé -- cf. correctif
     MINEUR ci-dessous), `ValueError` symétrique au PACKAGING sur `close` final NaN (correctif
     F3).
  3. Zéro fuite IS->OOS (§6.2) : `carry_out` ne dépend d'aucune donnée postérieure à sa borne,
     ni des poids décidés HORS de la fenêtre simulée.
  4. Conservation économique (§6.3) : la concaténation avec portage reproduit l'équity d'une
     simulation continue unique, aux renormalisations de capital près (écart borné documenté).
  5. Anti-gaming (§6.4/§4.1) : une position traversant 3 fenêtres ne compte qu'UN SEUL trade
     clos, avec `carried_windows`/`entry_ts` corrects -- ÉTENDU (audit adversarial 2026-09-07,
     finding F1 CRITIQUE) par une comparaison `profit_factor` ET somme des PnL des
     `realized_events` entre concaténation portée et simulation continue, sur des scénarios de
     marche aléatoire de poids avec renforcement + réduction à cheval sur des frontières.
  6. Homothétie de la bande de non-négociation "par poche" (§5) -- STATUT NON RÉSOLU (audit
     2026-09-07, finding F2 CRITIQUE) : prouvée UNIQUEMENT pour une poche à rendement ~nul,
     RÉFUTÉE en général sous compounding (test dédié ci-dessous). Clause d'arrêt de la spec §5
     appliquée : aucun changement de comportement de la bande, cf.
     `backtest/CARRY-EXTENSION-SPEC.md` §7 et `backtest/README.md`.

Amendements audit adversarial du 2026-09-07 (isSound:false, 4 findings) -- cf.
`backtest/CARRY-EXTENSION-SPEC.md` §7 pour le détail complet de chaque correctif :
  - F1 (CRITIQUE) : normalisation de `CarryLine.pnl_accum`/`cost_accum` par l'équity de clôture
    du segment (`equity.iloc[-1]`), pas `initial_capital` -- élimine le biais SYSTÉMATIQUEMENT
    favorable de `profit_factor` en mode portage.
  - F3 (MAJEUR) : `ValueError` immédiate au PACKAGING si `close` final NaN sur une position
    encore ouverte (symétrique du garde de reconstruction).
  - MINEUR : un poids porté sous 1e-12 avec PnL accumulé substantiel est CLOS explicitement à la
    frontière plutôt que silencieusement omis.
  - F2 (CRITIQUE, pas de correctif de code) : homothétie de bande réfutée sous compounding,
    documenté, clause d'arrêt appliquée.
"""

from __future__ import annotations

import hashlib
import math

import numpy as np
import pandas as pd
import pytest

from backtest import engine
from backtest import metrics as bt_metrics


# ============================================================================================
# 1. Rétro-compatibilité bit-à-bit (spec §1.1, §6.1)
# ============================================================================================
#
# Les deux scénarios ci-dessous et leurs hash attendus ont été capturés en exécutant
# EXACTEMENT ce code contre `backtest/engine.py` AVANT toute modification de ce fichier pour
# l'extension de portage (HEAD précédant ce commit) -- reproduit ici le principe du "HEAD~1 vs
# HEAD" demandé par la spec sans dépendre d'un état git particulier au moment du test. Toute
# régression de rétro-compatibilité (bit-à-bit, `carry_in` non fourni) ferait diverger ce hash.


def _retro_compat_scenario_spot_only():
    rng = np.random.default_rng(4242)
    n = 250
    cal = pd.bdate_range("2019-05-06", periods=n)
    rets_a = rng.normal(0.0004, 0.015, n)
    rets_b = rng.normal(0.0002, 0.02, n)
    close_a = 80.0 * np.cumprod(1.0 + rets_a)
    close_b = 30.0 * np.cumprod(1.0 + rets_b)
    closes = pd.DataFrame({"A": close_a, "B": close_b}, index=cal)
    opens = closes.shift(1).bfill()
    w = pd.DataFrame({"A": rng.random(n) * 0.5, "B": rng.random(n) * 0.5}, index=cal)
    return engine.simulate_segment(cal, w, opens, closes, 1, n - 1, cost_bps=12.0)


def _retro_compat_scenario_perp_hedged():
    rng = np.random.default_rng(777)
    n = 200
    cal = pd.date_range("2023-02-01", periods=n, freq="h", tz="UTC")
    price = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.008, n))
    opens = pd.DataFrame({"X": price, "X-PERP": price}, index=cal)
    closes = pd.DataFrame({"X": price, "X-PERP": price}, index=cal)
    highs = pd.DataFrame({"X-PERP": price * 1.6}, index=cal)
    lows = pd.DataFrame({"X-PERP": price * 0.4}, index=cal)
    funding = pd.DataFrame({"X-PERP": rng.normal(0.0, 0.0004, n)}, index=cal)
    w = pd.DataFrame({"X": 0.35, "X-PERP": -0.35}, index=cal)
    return engine.simulate_segment(
        cal, w, opens, closes, 1, n - 1, cost_bps=8.0,
        no_trade_band=0.02, apply_vol_targeting=True,
        vol_ewma_halflife_days=engine.risk_overlay.HOURLY_VOL_EWMA_HALFLIFE_PERIODS,
        vol_periods_per_year=engine.risk_overlay.HOURLY_VOL_PERIODS_PER_YEAR,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows, perp_cost_bps=8.0,
    )


def _digest_segment(seg) -> str:
    h = hashlib.sha256()
    h.update(seg.equity.to_numpy().tobytes())
    h.update(seg.returns.to_numpy().tobytes())
    h.update(seg.gross_exposure.to_numpy().tobytes())
    h.update(repr(seg.trades_closed).encode())
    h.update(repr(seg.realized_events).encode())
    h.update(repr(seg.liquidations).encode())
    h.update(repr(sorted(seg.pnl_breakdown.items())).encode())
    return h.hexdigest()


# Hash capturés AVANT toute modification d'engine.py pour cette extension (cf. docstring ci-dessus
# et le rapport d'implémentation de la session) -- NE JAMAIS mettre à jour ces constantes pour
# faire passer un test qui échouerait : un échec ici signifie une régression bit-à-bit réelle.
_EXPECTED_HASH_SPOT_ONLY = "5cfda89543113311df4632c7c658a2957d9066af061a69c9febad5e6968e14ed"
_EXPECTED_HASH_PERP_HEDGED = "0d17829421659b7084fd28880693e6472dd7dced18a5cdb125635025c06d601a"


def test_retro_compat_bit_exact_spot_only_vs_pre_extension_baseline():
    seg = _retro_compat_scenario_spot_only()
    assert _digest_segment(seg) == _EXPECTED_HASH_SPOT_ONLY
    # `carry_out` est un champ NOUVEAU -- présent (TOUJOURS calculé, spec §2) mais un appelant
    # historique qui l'ignore ne voit STRICTEMENT rien changer (vérifié ci-dessus par le hash,
    # qui ne porte que sur les champs préexistants).
    assert seg.carry_out is not None  # position encore ouverte en fin de segment (probable ici)


def test_retro_compat_bit_exact_perp_hedged_vs_pre_extension_baseline():
    seg = _retro_compat_scenario_perp_hedged()
    assert _digest_segment(seg) == _EXPECTED_HASH_PERP_HEDGED


def test_retro_compat_explicit_carry_in_none_identical_to_omitted():
    """`carry_in=None` explicite doit être IDENTIQUE à l'omettre (défaut Python) -- garantie
    triviale mais couverte explicitement (cf. précédent `test_perp_symbols_none_reproduces_
    historical_path_bit_for_bit` de `test_perp.py`, même esprit)."""
    seg_implicit = _retro_compat_scenario_spot_only()
    rng = np.random.default_rng(4242)
    n = 250
    cal = pd.bdate_range("2019-05-06", periods=n)
    rets_a = rng.normal(0.0004, 0.015, n)
    rets_b = rng.normal(0.0002, 0.02, n)
    close_a = 80.0 * np.cumprod(1.0 + rets_a)
    close_b = 30.0 * np.cumprod(1.0 + rets_b)
    closes = pd.DataFrame({"A": close_a, "B": close_b}, index=cal)
    opens = closes.shift(1).bfill()
    w = pd.DataFrame({"A": rng.random(n) * 0.5, "B": rng.random(n) * 0.5}, index=cal)
    seg_explicit = engine.simulate_segment(cal, w, opens, closes, 1, n - 1, cost_bps=12.0, carry_in=None)
    assert np.array_equal(seg_implicit.equity.to_numpy(), seg_explicit.equity.to_numpy())


def test_segment_result_carry_out_defaults_to_none_for_manual_construction():
    """`SegmentResult.carry_out` a un default `None` -- un appelant historique qui construit le
    dataclass À LA MAIN (ex. un test antérieur à cette extension) sans connaître ce champ ne
    lève jamais de `TypeError` (spec §1.1 : "ne modifie aucun champ existant")."""
    seg = engine.SegmentResult(
        dates=pd.DatetimeIndex([]),
        equity=pd.Series(dtype=float),
        returns=pd.Series(dtype=float),
        gross_exposure=pd.Series(dtype=float),
    )
    assert seg.carry_out is None


_DATA_DIR_VOL_BREAKOUT = None
try:
    from pathlib import Path as _Path

    _DATA_DIR_VOL_BREAKOUT = _Path(__file__).resolve().parents[2] / "_data"
except Exception:  # pragma: no cover -- défensif, jamais attendu
    pass


@pytest.mark.skipif(
    _DATA_DIR_VOL_BREAKOUT is None or not _DATA_DIR_VOL_BREAKOUT.exists(),
    reason="_data/ absent -- reproduction bit-exacte de vol_breakout_6majors/results.json ignorée "
    "(même convention que test_perp.py pour les tests d'intégration données réelles)",
)
def test_reproduces_vol_breakout_results_json_bit_exact_extension_disabled(monkeypatch, tmp_path):
    """Preuve §1.1a : re-jouer intégralement `run_vol_breakout.main()` (extension de portage
    JAMAIS invoquée par ce runner) doit reproduire, fenêtre par fenêtre, `results.json` existant
    (l'archive COMMITTÉE) -- si ce test s'exécute (données réelles présentes) et échoue, c'est une
    régression de rétro-compatibilité du moteur commun, pas de cette extension spécifiquement
    (`run_vol_breakout.py` n'a reçu AUCUNE modification par cette extension).

    Correctif (défaut signalé par le coordinateur, 2026-09-07) sur une version antérieure de ce
    test qui était TAUTOLOGIQUE ET DESTRUCTRICE : `main()` écrit `results.json` dans `OUTPUT_DIR`
    (l'archive du dépôt) AVANT de retourner -- l'ancienne version lisait ensuite CE MÊME fichier
    fraîchement écrit et le comparait au résultat en mémoire (comparaison d'une chose avec
    elle-même, toujours vraie) tout en ÉCRASANT l'archive committée. Corrigé ainsi :
      1. L'archive EXISTANTE est chargée AVANT tout appel à `main()`.
      2. `rvb.OUTPUT_DIR` est monkeypatché vers `tmp_path` -- repris par `build_parser()` comme
         défaut de `--output-dir` à CHAQUE appel de `main()` (lu à jour, pas figé à l'import) --
         ce test n'écrit donc JAMAIS dans `backtest/results/`.
      3. Les données `_data/` actuelles s'étendent au-delà de celles du run original ayant produit
         l'archive (14 fenêtres) -- `main()` en génère désormais 15. Comparaison fenêtre par
         fenêtre des 14 fenêtres COMMUNES (mêmes bornes IS/OOS, vérifiées explicitement) en
         égalité EXACTE (`==`, NaN traité comme égal à NaN) sur `sharpe`/`profit_factor`/
         `max_drawdown`/`n_trades_closed` -- précédent : l'audit de la session #5 a reproduit ces
         fenêtres bit-à-bit sur ces mêmes données. Le CONCATÉNÉ n'est PAS comparé (période
         différente entre les deux runs). La 15e fenêtre (nouvelle, strictement postérieure) est
         vérifiée séparément et n'entre dans aucune comparaison avec l'archive.
      4. Garde défensive anti-régression : échoue si `OUTPUT_DIR` pointe encore vers l'archive
         committée au moment du run, et re-vérifie après coup que le fichier archive sur disque
         est resté BYTE POUR BYTE identique."""
    import hashlib
    import json
    import math

    from backtest import run_vol_breakout as rvb

    archive_path = rvb.OUTPUT_DIR / "results.json"
    original_output_dir = rvb.OUTPUT_DIR
    # 1. Archive EXISTANTE chargée AVANT tout appel à `main()`.
    with open(archive_path, "rb") as f:
        archive_bytes_before = f.read()
    existing = json.loads(archive_bytes_before)
    existing_pw = existing["candidate_vol_breakout"]["per_window"]
    assert len(existing_pw) == 14  # archive committée connue -- sanity check sur ce précondition

    # 2. Redirection OBLIGATOIRE vers tmp_path -- jamais vers l'archive réelle.
    monkeypatch.setattr(rvb, "OUTPUT_DIR", tmp_path)
    # `main()` lit `sys.argv` via `argparse` (aucun paramètre explicite) -- neutralise les
    # arguments propres à pytest pour retomber sur les défauts du script (`--output-dir` défaut
    # = `rvb.OUTPUT_DIR` au moment de l'appel à `build_parser()`, donc `tmp_path` ci-dessus).
    monkeypatch.setattr("sys.argv", ["run_vol_breakout.py"])
    # 4a. Garde défensive AVANT le run : `OUTPUT_DIR` ne doit PLUS pointer vers l'archive.
    assert rvb.OUTPUT_DIR == tmp_path
    assert rvb.OUTPUT_DIR != original_output_dir, (
        "OUTPUT_DIR pointe encore vers l'archive committée au moment du run -- risque "
        "d'écrasement destructeur (défaut corrigé, ne JAMAIS régresser)."
    )

    results, output_dir = rvb.main()

    # 4b. Garde défensive APRÈS le run : le run a bien écrit dans tmp_path, jamais dans l'archive.
    assert output_dir == tmp_path
    assert output_dir != original_output_dir
    assert (tmp_path / "results.json").exists()
    with open(archive_path, "rb") as f:
        archive_bytes_after = f.read()
    assert hashlib.sha256(archive_bytes_after).hexdigest() == hashlib.sha256(archive_bytes_before).hexdigest(), (
        "l'archive backtest/results/vol_breakout_6majors/results.json a été MODIFIÉE par ce "
        "test -- régression du défaut destructeur signalé par le coordinateur."
    )

    new_pw = results["candidate_vol_breakout"]["per_window"]
    assert len(new_pw) == 15  # données _data/ actuelles plus longues -> 1 fenêtre de plus (point 3)

    def _eq(a, b):
        if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
            return True
        return a == b

    fields = ["sharpe", "profit_factor", "max_drawdown", "n_trades_closed"]
    for i in range(14):
        old_w = existing_pw[i]
        new_w = new_pw[i]
        # Mêmes bornes IS/OOS -- condition nécessaire pour que la comparaison fenêtre par
        # fenêtre soit valide (sinon on comparerait deux fenêtres économiquement différentes).
        for bound in ("is_start", "is_end", "oos_start", "oos_end"):
            assert old_w[bound] == new_w[bound], f"fenêtre {i} : bornes {bound} divergentes"
        for field in fields:
            assert _eq(old_w[field], new_w[field]), (
                f"fenêtre {i} : {field} divergent -- archive={old_w[field]!r} "
                f"nouveau_run={new_w[field]!r}"
            )

    # La 15e fenêtre est bien NOUVELLE (strictement postérieure, données étendues) et n'a modifié
    # aucune des 14 premières (déjà vérifié ci-dessus, boucle bornée à `range(14)`).
    assert new_pw[14]["window_index"] == 14
    # OOS de la 15e fenêtre strictement postérieure à celui de la 14e (dernière commune) --
    # valeur exacte confirmée par un run réel sur les données `_data/` au 2026-09-07
    # (`calendar_end` 2026-07-31 23:00 -> pas assez de calendrier pour une 16e fenêtre complète).
    assert new_pw[14]["oos_start"] == "2026-04-01 00:00:00"
    assert new_pw[14]["oos_start"] > existing_pw[13]["oos_start"]


# ============================================================================================
# 2. Reconstruction de segment (spec §3)
# ============================================================================================


def _flat_two_symbol_fixture(n=20):
    cal = pd.bdate_range("2020-01-06", periods=n)
    closes = pd.DataFrame({"X": 100.0, "Y": 50.0}, index=cal)
    opens = closes.copy()
    return cal, opens, closes


def test_reconstruction_formula_shares_and_cash_no_entry_cost():
    """Spec §3.1-3.2 : `shares_0 = w_last * initial_capital / last_close` (PAS l'open),
    `cash_0 = initial_capital - Σ shares_0*last_close`, ÉQUITY DE DÉPART EXACTEMENT
    `initial_capital`, et AUCUN coût prélevé sur cette reconstruction (le coût ne s'applique
    qu'aux ordres réellement émis ensuite)."""
    n = 20
    cal, opens, closes = _flat_two_symbol_fixture(n)
    # Clôture de la toute première bougie de la fenêtre B qui DÉRIVE par rapport à `last_close`
    # (l'ouverture, elle, reste inchangée -- la décision de HOLD/TRADE au premier tour est prise
    # au prix d'OUVERTURE, cf. `risk_overlay`, mais `SegmentResult.equity` est TOUJOURS marquée
    # à la clôture de chaque bougie, comme pour tout le reste du moteur historique) : ce choix
    # isole la vérification EXACTE de la formule de reconstruction `shares_0 = w_last *
    # initial_capital / last_close` (spec §3.1) sans dépendre du prix d'exécution intra-bougie.
    closes.iloc[10] = {"X": 110.0, "Y": 55.0}
    w = pd.DataFrame({"X": 0.3, "Y": 0.0}, index=cal)

    segA = engine.simulate_segment(cal, w, opens, closes, 1, 9, cost_bps=25.0, no_trade_band=0.0, apply_vol_targeting=False)
    carry = segA.carry_out
    assert carry is not None
    assert set(carry.weights_at_last_close.index) == {"X"}  # Y (poids 0) omis, spec §3.4

    last_close_x = float(closes["X"].iloc[9])
    w_last_x = float(carry.weights_at_last_close["X"])
    initial_capital = 1.0
    expected_shares0 = w_last_x * initial_capital / last_close_x

    # Fenêtre B : poids DÉCIDÉ identique au poids porté (dans la bande, prix d'OUVERTURE
    # inchangé) -- AUCUN ordre ne doit s'exécuter au premier tour, la position dérive donc
    # EXACTEMENT selon `expected_shares0`, marquée à la NOUVELLE clôture perturbée ci-dessus.
    wB = pd.DataFrame({"X": w_last_x, "Y": 0.0}, index=cal)
    segB = engine.simulate_segment(
        cal, wB, opens, closes, 10, 19, cost_bps=25.0, no_trade_band=0.05, apply_vol_targeting=False,
        carry_in=carry,
    )
    # equity[0] de la fenêtre B = cash_0 + shares_0 * close[10] = initial_capital + shares_0 *
    # (close[10] - last_close) : la dérive clôture(fenêtre A)->clôture(bougie 0 de B) est vécue
    # intégralement par la position PORTÉE, comme en continu (spec §3.1 dernier point).
    expected_equity0 = initial_capital + expected_shares0 * (float(closes["X"].iloc[10]) - last_close_x)
    assert segB.equity.iloc[0] == pytest.approx(expected_equity0, rel=1e-12)
    # Aucun coût d'entrée : si un coût avait été prélevé, `equity[0]` serait strictement inférieur
    # à `expected_equity0` (un coût ne peut jamais être positif) -- l'égalité exacte ci-dessus le
    # prouve déjà, assertion supplémentaire pour un message d'échec explicite en cas de régression.
    assert len(segB.realized_events) == 0, "aucun ordre ne doit s'exécuter au premier tour (poids porté = poids décidé)"


def test_reconstruction_zero_weight_symbol_omitted_no_nan_check():
    """Spec §3.4 : un symbole porté à poids EXACTEMENT 0 est omis -- même si son `last_close`
    est NaN, aucune erreur ne doit être levée (il n'est jamais touché)."""
    n = 20
    cal, opens, closes = _flat_two_symbol_fixture(n)
    carry = engine.CarryState(
        last_idx=9,
        weights_at_last_close=pd.Series({"X": 0.3, "Y": 0.0}),
        last_close=pd.Series({"X": 100.0, "Y": float("nan")}),
        open_lines=[],
    )
    w = pd.DataFrame({"X": 0.3, "Y": 0.0}, index=cal)
    seg = engine.simulate_segment(
        cal, w, opens, closes, 10, 19, cost_bps=0.0, no_trade_band=0.05, apply_vol_targeting=False,
        carry_in=carry,
    )
    assert seg is not None  # ne lève pas malgré le NaN sur Y (poids 0 -> jamais lu)


def test_reconstruction_near_zero_weight_with_accumulated_pnl_closes_line_not_dropped():
    """Correctif audit MINEUR (2026-09-07) : un poids porté sous 1e-12 (donc omis de la
    reconstruction, spec §3.4) mais dont la ligne `open_lines` correspondante porte un PnL/coût
    accumulé NON NUL doit être CLOS explicitement à la frontière (`trades_closed`/
    `realized_events` du segment qui reconstruit), jamais silencieusement perdu."""
    n = 20
    cal, opens, closes = _flat_two_symbol_fixture(n)
    carry = engine.CarryState(
        last_idx=9,
        weights_at_last_close=pd.Series({"X": 1e-15}),  # sous le seuil d'omission
        last_close=pd.Series({"X": 100.0}),
        open_lines=[
            engine.CarryLine(
                symbol="X", leg="spot", line_id="X:spot", pnl_accum=0.05, cost_accum=0.001,
                entry_ts=cal[0], carried_windows=2,
            )
        ],
    )
    w = pd.DataFrame({"X": 0.0, "Y": 0.0}, index=cal)
    seg = engine.simulate_segment(
        cal, w, opens, closes, 10, 19, cost_bps=0.0, no_trade_band=0.05, apply_vol_targeting=False,
        carry_in=carry,
    )
    assert len(seg.trades_closed) == 1
    closed = seg.trades_closed[0]
    assert closed["symbol"] == "X"
    assert closed["pnl"] == pytest.approx(0.05)
    assert closed["carried_windows"] == 2
    assert closed["entry_ts"] == cal[0]
    assert closed["close_date"] == cal[9]  # frontière = dernière bougie du segment précédent
    assert len(seg.realized_events) == 1
    assert seg.realized_events[0]["pnl"] == pytest.approx(0.05)
    assert seg.realized_events[0]["closes_line"] is True
    assert seg.carry_out is None  # plus aucune position ouverte pour X (ni Y, jamais entré)


def test_reconstruction_near_zero_weight_without_accumulated_pnl_still_omitted_silently():
    """Comportement historique inchangé (spec §3.4) : un poids porté sous 1e-12 dont la ligne
    `open_lines` correspondante n'a NI PnL NI coût accumulé (ou n'a pas de ligne du tout, ex.
    `CarryState` construit à la main sans `open_lines`) reste simplement omis, sans évènement
    synthétique -- il n'y a rien de substantiel à perdre."""
    n = 20
    cal, opens, closes = _flat_two_symbol_fixture(n)
    carry = engine.CarryState(
        last_idx=9,
        weights_at_last_close=pd.Series({"X": 1e-15}),
        last_close=pd.Series({"X": 100.0}),
        open_lines=[],  # aucune ligne connue -- rien à publier
    )
    w = pd.DataFrame({"X": 0.0, "Y": 0.0}, index=cal)
    seg = engine.simulate_segment(
        cal, w, opens, closes, 10, 19, cost_bps=0.0, no_trade_band=0.05, apply_vol_targeting=False,
        carry_in=carry,
    )
    assert seg.trades_closed == []
    assert seg.realized_events == []


def test_packaging_nan_final_close_on_open_position_raises_value_error():
    """Correctif audit F3 (MAJEUR, 2026-09-07) : `close` final NaN (mark-to-zero historique) sur
    un symbole ENCORE en position à la fin du segment doit lever `ValueError` IMMÉDIATEMENT au
    PACKAGING de `carry_out`, jamais empoisonner silencieusement `CarryState` (poids/`last_close`
    NaN) détecté seulement une fenêtre plus tard -- symétrique du garde existant à la
    reconstruction (`carry_in.last_close manquant/NaN`)."""
    n = 10
    cal = pd.bdate_range("2020-01-06", periods=n)
    closes = pd.DataFrame({"X": 100.0}, index=cal)
    closes.iloc[-1] = float("nan")  # trou sur la TOUTE DERNIÈRE bougie du segment
    opens = closes.shift(1).bfill()
    opens.iloc[-1] = 100.0  # open normal -- seul le CLOSE final manque
    w = pd.DataFrame({"X": 0.3}, index=cal)
    with pytest.raises(ValueError, match="close final NaN"):
        engine.simulate_segment(
            cal, w, opens, closes, 1, n - 1, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        )


def test_packaging_nan_final_close_on_flat_symbol_does_not_raise():
    """Symétrique du garde précédent : un `close` final NaN sur un symbole qui N'EST PAS en
    position (poids/shares nuls) en fin de segment ne doit JAMAIS lever -- seul un symbole
    RÉELLEMENT porté est concerné (même logique que le garde perp existant sur les données
    manquantes, ARCHITECTURE.md §0.2 : refus bruyant seulement quand ça change le résultat)."""
    n = 10
    cal = pd.bdate_range("2020-01-06", periods=n)
    closes = pd.DataFrame({"X": 100.0, "Y": 50.0}, index=cal)
    closes.loc[closes.index[-1], "Y"] = float("nan")  # Y : jamais en position (poids toujours 0)
    opens = closes.shift(1).bfill()
    opens.loc[opens.index[-1], "Y"] = 50.0
    w = pd.DataFrame({"X": 0.3, "Y": 0.0}, index=cal)
    seg = engine.simulate_segment(
        cal, w, opens, closes, 1, n - 1, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
    )
    assert seg is not None
    assert seg.carry_out is not None
    assert set(seg.carry_out.weights_at_last_close.index) == {"X"}  # Y jamais entré, jamais porté


def test_reconstruction_nan_price_on_nonzero_weight_raises_value_error():
    """Spec §3.4 : `NaN` sur `last_close` d'un symbole porté à poids NON NUL -> `ValueError`
    immédiat (jamais un prix 0 silencieux, alignée politique perp F1 du 2026-08-31)."""
    n = 20
    cal, opens, closes = _flat_two_symbol_fixture(n)
    carry = engine.CarryState(
        last_idx=9,
        weights_at_last_close=pd.Series({"X": 0.3}),
        last_close=pd.Series({"X": float("nan")}),
        open_lines=[],
    )
    w = pd.DataFrame({"X": 0.3, "Y": 0.0}, index=cal)
    with pytest.raises(ValueError, match="manquant/NaN"):
        engine.simulate_segment(
            cal, w, opens, closes, 10, 19, cost_bps=0.0, no_trade_band=0.05, apply_vol_targeting=False,
            carry_in=carry,
        )


def test_reconstruction_contiguity_violation_raises_value_error():
    """Spec §1.3/§3 : `carry_in.last_idx + 1 != start_idx` -> `ValueError` immédiat -- le moteur
    ne suppose JAMAIS un invariant fourni par l'appelant sans le revérifier."""
    n = 20
    cal, opens, closes = _flat_two_symbol_fixture(n)
    carry = engine.CarryState(
        last_idx=9,
        weights_at_last_close=pd.Series({"X": 0.3}),
        last_close=pd.Series({"X": 100.0}),
        open_lines=[],
    )
    w = pd.DataFrame({"X": 0.3, "Y": 0.0}, index=cal)
    with pytest.raises(ValueError, match="non contigu"):
        engine.simulate_segment(
            cal, w, opens, closes, 11, 19, cost_bps=0.0, no_trade_band=0.05, apply_vol_targeting=False,
            carry_in=carry,
        )


def test_reconstruction_negative_weight_on_non_perp_symbol_raises_value_error():
    """Spec §2 : un `carry_in` qui porte un poids négatif sur un symbole non déclaré perp DANS
    CE segment est une incohérence de configuration -- refus bruyant plutôt qu'un short spot
    silencieux."""
    n = 20
    cal, opens, closes = _flat_two_symbol_fixture(n)
    carry = engine.CarryState(
        last_idx=9,
        weights_at_last_close=pd.Series({"X": -0.3}),
        last_close=pd.Series({"X": 100.0}),
        open_lines=[],
    )
    w = pd.DataFrame({"X": 0.3, "Y": 0.0}, index=cal)
    with pytest.raises(ValueError, match="négatif"):
        engine.simulate_segment(
            cal, w, opens, closes, 10, 19, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
            carry_in=carry,
        )


def test_perp_carry_liquidation_applies_from_first_bar_no_immunity():
    """Spec §3.5 : "aucune immunité de première bougie" -- une position perp reconstruite doit
    pouvoir être liquidée DÈS la première bougie du nouveau segment si le pire prix intra-bougie
    franchit le seuil de maintenance, exactement comme n'importe quelle bougie normale."""
    cal = pd.date_range("2024-01-01", periods=6, freq="h", tz="UTC")
    price = pd.Series([100.0] * 6, index=cal)
    opens = pd.DataFrame({"X-PERP": price}, index=cal)
    closes = pd.DataFrame({"X-PERP": price}, index=cal)
    highs = pd.DataFrame({"X-PERP": price}, index=cal)
    lows = pd.DataFrame({"X-PERP": price}, index=cal)
    funding = pd.DataFrame({"X-PERP": 0.0}, index=cal)
    highs.iloc[3] = 300.0  # spike UNIQUEMENT sur la toute première bougie du segment B (idx 3)

    carry = engine.CarryState(
        last_idx=2,
        weights_at_last_close=pd.Series({"X-PERP": -1.9}),
        last_close=pd.Series({"X-PERP": 100.0}),
        open_lines=[
            engine.CarryLine(
                symbol="X-PERP", leg="perp", line_id="X-PERP:perp",
                pnl_accum=0.0, cost_accum=0.0, entry_ts=cal[0], carried_windows=1,
            )
        ],
    )
    w = pd.DataFrame({"X-PERP": -1.9}, index=cal)
    seg = engine.simulate_segment(
        cal, w, opens, closes, 3, 5, cost_bps=0.0, no_trade_band=0.0, apply_vol_targeting=False,
        perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows,
        perp_initial_margin_frac=0.5, perp_maintenance_margin_frac=0.025, perp_liquidation_fee_bps=100.0,
        carry_in=carry,
    )
    # `seg.liquidations` peut aussi contenir un évènement "ruin" séparé (équity tombée à 0,
    # cf. `test_liquidation_loss_capped_at_cash_bankruptcy_and_ruin` de `test_perp.py`, même
    # convention de filtrage) -- on isole ici la liquidation de la jambe perp elle-même.
    perp_liqs = [l for l in seg.liquidations if l["symbol"] == "X-PERP"]
    assert len(perp_liqs) == 1
    assert perp_liqs[0]["date"] == cal[3]  # la toute PREMIÈRE bougie du segment, aucune immunité


# ============================================================================================
# 3. Zéro fuite IS->OOS (spec §6.2)
# ============================================================================================


def _causality_fixture(n=30, seed=3):
    rng = np.random.default_rng(seed)
    cal = pd.bdate_range("2021-01-04", periods=n)
    close = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, n))
    closes = pd.DataFrame({"X": close}, index=cal)
    opens = closes.shift(1).bfill()
    w = pd.DataFrame({"X": rng.random(n) * 0.6}, index=cal)
    return cal, opens, closes, w


def test_carry_out_does_not_depend_on_data_strictly_after_end_idx():
    """Attaque par perturbation des données FUTURES (spec §6.2, second point) : `carry_out`
    d'un segment [start_idx, end_idx] ne doit JAMAIS changer si on perturbe `opens`/`closes`
    STRICTEMENT APRÈS `end_idx` -- `carry_out` est un résumé causal de ce qui s'est PASSÉ, pas
    de ce qui va se passer."""
    cal, opens, closes, w = _causality_fixture()
    end_idx = 14
    seg_orig = engine.simulate_segment(cal, w, opens, closes, 1, end_idx, cost_bps=10.0, no_trade_band=0.02)

    rng = np.random.default_rng(99)
    perturbed_closes = closes.copy()
    perturbed_opens = opens.copy()
    perturbed_closes.iloc[end_idx + 1 :] *= 1.0 + rng.normal(0.0, 0.5, size=perturbed_closes.iloc[end_idx + 1 :].shape)
    perturbed_opens.iloc[end_idx + 1 :] *= 1.0 + rng.normal(0.0, 0.5, size=perturbed_opens.iloc[end_idx + 1 :].shape)
    seg_perturbed = engine.simulate_segment(
        cal, w, perturbed_opens, perturbed_closes, 1, end_idx, cost_bps=10.0, no_trade_band=0.02
    )

    assert np.array_equal(seg_orig.equity.to_numpy(), seg_perturbed.equity.to_numpy())
    c1, c2 = seg_orig.carry_out, seg_perturbed.carry_out
    assert c1.last_idx == c2.last_idx
    pd.testing.assert_series_equal(c1.weights_at_last_close, c2.weights_at_last_close)
    pd.testing.assert_series_equal(c1.last_close, c2.last_close)
    assert [(l.symbol, l.leg, l.pnl_accum, l.cost_accum) for l in c1.open_lines] == [
        (l.symbol, l.leg, l.pnl_accum, l.cost_accum) for l in c2.open_lines
    ]


def test_carry_out_does_not_depend_on_weights_decided_outside_window():
    """Zéro fuite IS->OOS (spec §6.2, premier point) : `carry_out` d'un segment OOS ne doit
    dépendre QUE des lignes de `weights_decided` réellement consommées par ce segment
    (`[start_idx-1, end_idx-1]`, cf. convention temporelle du moteur) -- modifier les poids
    décidés HORS de cette plage (par ex. une fenêtre IS antérieure, ou une IS différente issue
    d'une autre sélection de grille dans la MÊME grosse matrice) ne doit rien changer."""
    cal, opens, closes, w = _causality_fixture()
    start_idx, end_idx = 10, 24
    seg_orig = engine.simulate_segment(cal, w, opens, closes, start_idx, end_idx, cost_bps=10.0, no_trade_band=0.02)

    rng = np.random.default_rng(123)
    w_perturbed_is = w.copy()
    # Perturbe UNIQUEMENT les lignes hors de [start_idx-1, end_idx-1] (jamais lues par ce segment).
    w_perturbed_is.iloc[: start_idx - 1] = rng.random((start_idx - 1, w.shape[1])) * 0.6
    if end_idx < len(cal) - 1:
        w_perturbed_is.iloc[end_idx:] = rng.random((len(cal) - end_idx, w.shape[1])) * 0.6
    seg_perturbed = engine.simulate_segment(
        cal, w_perturbed_is, opens, closes, start_idx, end_idx, cost_bps=10.0, no_trade_band=0.02
    )

    assert np.array_equal(seg_orig.equity.to_numpy(), seg_perturbed.equity.to_numpy())
    pd.testing.assert_series_equal(seg_orig.carry_out.weights_at_last_close, seg_perturbed.carry_out.weights_at_last_close)


# ============================================================================================
# 4. Conservation économique vs simulation continue (spec §6.3)
# ============================================================================================


def test_economic_conservation_two_windows_spot_matches_continuous_simulation():
    """La concaténation AVEC portage de 2 fenêtres contiguës doit reproduire l'équity d'une
    simulation continue UNIQUE de la même période, aux renormalisations de capital près (spec
    §3 préambule) -- écart attendu proche de la précision machine (aucune perte économique
    cachée par le découpage en fenêtres, tolérance documentée `rel=1e-8`)."""
    n = 20
    rng = np.random.default_rng(1)
    cal = pd.bdate_range("2020-01-06", periods=n)
    close = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, n))
    closes = pd.DataFrame({"X": close}, index=cal)
    opens = closes.shift(1).bfill()
    w = pd.DataFrame({"X": 0.4}, index=cal)

    seg_cont = engine.simulate_segment(cal, w, opens, closes, 1, 19, cost_bps=10.0, no_trade_band=0.0, apply_vol_targeting=False)

    segA = engine.simulate_segment(cal, w, opens, closes, 1, 9, cost_bps=10.0, no_trade_band=0.0, apply_vol_targeting=False)
    segB = engine.simulate_segment(
        cal, w, opens, closes, 10, 19, cost_bps=10.0, no_trade_band=0.0, apply_vol_targeting=False,
        carry_in=segA.carry_out,
    )
    concat_returns = pd.concat([segA.returns, segB.returns])
    equity_carried_rebased = float((1.0 + concat_returns).cumprod().iloc[-1])

    assert equity_carried_rebased == pytest.approx(float(seg_cont.equity.iloc[-1]), rel=1e-8)


def test_economic_conservation_two_windows_perp_hedged_matches_continuous_simulation():
    """Même preuve que ci-dessus, jambe PERP incluse (funding + variation margin traversant la
    frontière) -- la mécanique de portage réutilise `perp_ref`/`perp_pnl_accum` existants, donc
    le gap clôture->ouverture ET le funding de la première bougie du segment B doivent être
    vécus IDENTIQUEMENT à la simulation continue."""
    n = 20
    rng = np.random.default_rng(9)
    cal = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
    price = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, n))
    opens = pd.DataFrame({"X-PERP": price}, index=cal)
    closes = pd.DataFrame({"X-PERP": price}, index=cal)
    highs = pd.DataFrame({"X-PERP": price * 1.6}, index=cal)
    lows = pd.DataFrame({"X-PERP": price * 0.4}, index=cal)
    funding = pd.DataFrame({"X-PERP": rng.normal(0.0, 0.0005, n)}, index=cal)
    w = pd.DataFrame({"X-PERP": -0.3}, index=cal)
    common = dict(
        no_trade_band=0.0, apply_vol_targeting=False, perp_symbols={"X-PERP"},
        funding=funding, highs=highs, lows=lows, perp_cost_bps=8.0,
    )

    seg_cont = engine.simulate_segment(cal, w, opens, closes, 1, 19, cost_bps=8.0, **common)
    segA = engine.simulate_segment(cal, w, opens, closes, 1, 9, cost_bps=8.0, **common)
    segB = engine.simulate_segment(cal, w, opens, closes, 10, 19, cost_bps=8.0, carry_in=segA.carry_out, **common)

    concat_returns = pd.concat([segA.returns, segB.returns])
    equity_carried_rebased = float((1.0 + concat_returns).cumprod().iloc[-1])
    assert equity_carried_rebased == pytest.approx(float(seg_cont.equity.iloc[-1]), rel=1e-8)


# ============================================================================================
# 5. Anti-gaming : position traversant 3 fenêtres -> exactement 1 trade clos (spec §6.4/§4.1)
# ============================================================================================


def test_anti_gaming_position_crossing_three_windows_counts_as_exactly_one_closed_trade():
    n = 30
    rng = np.random.default_rng(2)
    cal = pd.bdate_range("2020-01-06", periods=n)
    close = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, n))
    closes = pd.DataFrame({"X": close}, index=cal)
    opens = closes.shift(1).bfill()
    raw_w = np.full(n, 0.4)
    raw_w[25:] = 0.0  # sortie réelle vers la fin de la 3e fenêtre
    weights = pd.DataFrame({"X": raw_w}, index=cal)

    bounds = [(1, 9), (10, 19), (20, 29)]
    segments = []
    carry = None
    for s, e in bounds:
        seg = engine.simulate_segment(
            cal, weights, opens, closes, s, e, cost_bps=10.0, no_trade_band=0.0, apply_vol_targeting=False,
            carry_in=carry,
        )
        segments.append(seg)
        carry = seg.carry_out

    total_trades_closed = sum(len(s.trades_closed) for s in segments)
    assert total_trades_closed == 1, (
        "le découpage en 3 fenêtres NE DOIT JAMAIS gonfler le compte de trades clos "
        f"(spec §1.4/§4.1) -- obtenu {total_trades_closed}"
    )
    # Les deux premières fenêtres n'ont RIEN à fermer (position tenue en continu).
    assert segments[0].trades_closed == []
    assert segments[1].trades_closed == []
    final_trade = segments[2].trades_closed[0]
    assert final_trade["carried_windows"] == 2  # a traversé exactement 2 frontières
    assert final_trade["entry_ts"] == cal[1]  # entrée D'ORIGINE (première exécution, jamais réécrite)
    assert segments[2].carry_out is None  # plus aucune position ouverte -- rien à porter davantage


def test_anti_gaming_perp_position_crossing_three_windows_counts_as_exactly_one_closed_trade():
    """Même preuve que ci-dessus, jambe PERP (spec §4.1 s'applique identiquement aux deux
    jambes -- `perp_pnl_accum` pré-amorcé porte déjà tout le mécanisme)."""
    n = 30
    rng = np.random.default_rng(4)
    cal = pd.date_range("2023-05-01", periods=n, freq="h", tz="UTC")
    price = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.005, n))
    opens = pd.DataFrame({"X-PERP": price}, index=cal)
    closes = pd.DataFrame({"X-PERP": price}, index=cal)
    highs = pd.DataFrame({"X-PERP": price * 1.8}, index=cal)
    lows = pd.DataFrame({"X-PERP": price * 0.3}, index=cal)
    funding = pd.DataFrame({"X-PERP": rng.normal(0.0, 0.0003, n)}, index=cal)
    raw_w = np.full(n, -0.3)
    raw_w[25:] = 0.0
    weights = pd.DataFrame({"X-PERP": raw_w}, index=cal)
    common = dict(
        no_trade_band=0.0, apply_vol_targeting=False, perp_symbols={"X-PERP"},
        funding=funding, highs=highs, lows=lows, perp_cost_bps=6.0,
    )

    bounds = [(1, 9), (10, 19), (20, 29)]
    segments = []
    carry = None
    for s, e in bounds:
        seg = engine.simulate_segment(cal, weights, opens, closes, s, e, cost_bps=6.0, carry_in=carry, **common)
        segments.append(seg)
        carry = seg.carry_out

    total_trades_closed = sum(len(s.trades_closed) for s in segments)
    assert total_trades_closed == 1
    final_trade = segments[2].trades_closed[0]
    assert final_trade["leg"] == "perp"
    assert final_trade["carried_windows"] == 2
    assert final_trade["entry_ts"] == cal[1]


# ============================================================================================
# 5bis. Anti-gaming ÉTENDU (audit adversarial 2026-09-07, finding F1 CRITIQUE) : le profit
# factor ne doit JAMAIS être SYSTÉMATIQUEMENT plus favorable en mode portage qu'en simulation
# continue -- comparaison sur `profit_factor` ET la somme des PnL des `realized_events`, sur des
# scénarios de marche aléatoire de poids (prix variables, bande active, coûts non nuls) avec
# renforcement PUIS réduction à cheval sur la frontière de fenêtre (exactement le pattern F1).
# ============================================================================================


def _f1_spot_weight_random_walk_scenario(seed, n=80):
    """Poids = marche aléatoire bornée [0, 0.95] avec une bande active (0.015) et des coûts non
    nuls (20 bps) -- produit naturellement des cycles renforcement/réduction, y compris à cheval
    sur la frontière `boundary` (milieu du calendrier), sans les forcer explicitement : c'est le
    scénario le plus honnête pour détecter un biais systématique (par opposition à un unique
    scénario construit à la main qui pourrait accidentellement esquiver le bug)."""
    rng = np.random.default_rng(seed)
    cal = pd.bdate_range("2020-01-06", periods=n)
    close = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.03, n))
    closes = pd.DataFrame({"X": close}, index=cal)
    opens = closes.shift(1).bfill()
    w = np.clip(0.5 + rng.normal(0, 1.0, n).cumsum() * 0.03, 0.0, 0.95)
    weights = pd.DataFrame({"X": w}, index=cal)
    return cal, opens, closes, weights


def test_anti_gaming_profit_factor_matches_continuous_simulation_across_seeds_spot():
    """Finding F1 (CRITIQUE, audit adversarial 2026-09-07) : AVANT le correctif (normalisation
    de `CarryLine.pnl_accum` par `initial_capital` au lieu de l'équity de clôture du segment),
    ce test échouait avec `profit_factor` SYSTÉMATIQUEMENT plus favorable en mode portage sur une
    nette majorité des 20 seeds (biais directionnel net, jamais l'inverse). Preuve exigée : sur
    N>=10 seeds (20 ici), le signe de l'écart de `profit_factor` (carry - continu) doit être
    ÉQUILIBRÉ -- ni toujours positif ni toujours négatif -- et jamais MAJORITAIREMENT favorable
    au-delà d'un seuil documenté. Un résidu non nul, non systématique et borné subsiste (cf.
    `backtest/README.md` section "Portage inter-fenêtres" et spec §4.2/§7) : c'est l'artefact
    ACCEPTÉ de la renormalisation du capital par fenêtre (chaque contribution à
    `trades_closed`/`realized_events` d'une ligne portée est exprimée en fraction du capital de
    base DE SA PROPRE fenêtre, sommée -- spec §4.2, assumé dès la pré-inscription) -- PAS le bug
    F1 (qui, lui, cassait la COHÉRENCE d'échelle entre les `shares` reconstruites et le PnL porté
    et produisait un biais DIRECTIONNEL, éliminé par ce correctif). Quantifié ci-dessous plutôt
    que simplement affirmé."""
    n_seeds = 20
    boundary = 40
    n = 80
    cost_bps = 20.0
    band = 0.015

    pf_diffs = []
    rel_residuals = []
    for seed in range(n_seeds):
        cal, opens, closes, weights = _f1_spot_weight_random_walk_scenario(seed, n=n)
        common = dict(cost_bps=cost_bps, no_trade_band=band, apply_vol_targeting=False)

        seg_cont = engine.simulate_segment(cal, weights, opens, closes, 1, n - 1, **common)
        segA = engine.simulate_segment(cal, weights, opens, closes, 1, boundary - 1, **common)
        segB = engine.simulate_segment(
            cal, weights, opens, closes, boundary, n - 1, carry_in=segA.carry_out, **common
        )

        # Équity : conservation économique inchangée par cette extension de test (§6.3, déjà
        # couverte ailleurs) -- réaffirmée ici pour garantir que tout écart de `profit_factor`
        # observé ci-dessous vient bien de l'ATTRIBUTION par ligne, jamais d'une perte/gain réel.
        concat_returns = pd.concat([segA.returns, segB.returns])
        eq_carried = float((1.0 + concat_returns).cumprod().iloc[-1])
        assert eq_carried == pytest.approx(float(seg_cont.equity.iloc[-1]), rel=1e-8), seed

        pnl_cont = [e["pnl"] for e in seg_cont.realized_events]
        pnl_carry = [e["pnl"] for e in segA.realized_events] + [e["pnl"] for e in segB.realized_events]
        assert len(pnl_cont) == len(pnl_carry), (
            "le découpage en fenêtres ne doit JAMAIS changer le NOMBRE d'évènements de "
            f"réalisation (seed={seed})"
        )

        pf_cont = bt_metrics.profit_factor(pnl_cont)
        pf_carry = bt_metrics.profit_factor(pnl_carry)
        if not (math.isnan(pf_cont) or math.isnan(pf_carry) or math.isinf(pf_cont) or math.isinf(pf_carry)):
            pf_diffs.append(pf_carry - pf_cont)

        denom = max(sum(abs(p) for p in pnl_cont), 1e-9)
        rel_residuals.append(abs(sum(pnl_carry) - sum(pnl_cont)) / denom)

    n_favorable = sum(1 for d in pf_diffs if d > 1e-9)
    n_unfavorable = sum(1 for d in pf_diffs if d < -1e-9)
    # Jamais systématiquement favorable (mission F1) : AU MOINS autant de seeds défavorables que
    # favorables n'est pas exigé au sens strict (bruit d'échantillonnage possible), mais une
    # majorité écrasante favorable (le comportement PRÉ-correctif) est explicitement exclue --
    # seuil documenté à 65% (marge au-dessus de l'équilibre 50/50 pour absorber le bruit, très en
    # dessous de la quasi-unanimité favorable observée avant correctif sur ce même scénario).
    assert n_favorable <= 0.65 * len(pf_diffs), (
        f"profit_factor SYSTÉMATIQUEMENT plus favorable en mode portage : {n_favorable}/"
        f"{len(pf_diffs)} seeds favorables (seuil 65%) -- régression du correctif F1"
    )
    assert n_unfavorable > 0, "au moins un seed doit être défavorable -- sinon le biais F1 persiste"

    # Résidu résiduel (approximation ACCEPTÉE de la renormalisation par fenêtre, spec §4.2) :
    # borné à un epsilon documenté, jamais explosif -- 30% relatif est généreux (observé <=17%
    # sur ce scénario) mais garde une marge contre un scénario de seed légèrement différent.
    assert max(rel_residuals) < 0.30, (
        f"résidu relatif de PnL porté anormalement élevé : {max(rel_residuals):.3f} "
        "(epsilon documenté 0.30, cf. backtest/README.md)"
    )


def _f1_perp_weight_random_walk_scenario(seed, n=80):
    rng = np.random.default_rng(seed)
    cal = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
    price = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, n))
    opens = pd.DataFrame({"X-PERP": price}, index=cal)
    closes = pd.DataFrame({"X-PERP": price}, index=cal)
    highs = pd.DataFrame({"X-PERP": price * 1.8}, index=cal)
    lows = pd.DataFrame({"X-PERP": price * 0.3}, index=cal)
    funding = pd.DataFrame({"X-PERP": rng.normal(0.0, 0.0004, n)}, index=cal)
    w = np.clip(-0.5 + rng.normal(0, 1.0, n).cumsum() * 0.02, -0.9, 0.0)
    weights = pd.DataFrame({"X-PERP": w}, index=cal)
    return cal, opens, closes, highs, lows, funding, weights


def test_anti_gaming_profit_factor_matches_continuous_simulation_across_seeds_perp():
    """Même preuve que la version spot ci-dessus, jambe PERP (spec F1 amendement : "implémente
    cette convention pour spot ET perp"). Le résidu observé est nettement plus petit côté perp
    (`perp_pnl_accum` est déjà une comptabilité "un seul bucket fongible", sans le double bucket
    `avg_cost`/`_spot_prior_pnl` du spot qui rendait le bug F1 visible) -- seuils resserrés en
    conséquence, documentés ci-dessous plutôt que réutilisés tels quels du cas spot."""
    n_seeds = 20
    boundary = 40
    n = 80
    cost_bps = 15.0
    band = 0.015

    pf_diffs = []
    rel_residuals = []
    for seed in range(n_seeds):
        cal, opens, closes, highs, lows, funding, weights = _f1_perp_weight_random_walk_scenario(seed, n=n)
        common = dict(
            cost_bps=cost_bps, no_trade_band=band, apply_vol_targeting=False,
            perp_symbols={"X-PERP"}, funding=funding, highs=highs, lows=lows, perp_cost_bps=cost_bps,
        )

        seg_cont = engine.simulate_segment(cal, weights, opens, closes, 1, n - 1, **common)
        segA = engine.simulate_segment(cal, weights, opens, closes, 1, boundary - 1, **common)
        segB = engine.simulate_segment(
            cal, weights, opens, closes, boundary, n - 1, carry_in=segA.carry_out, **common
        )

        concat_returns = pd.concat([segA.returns, segB.returns])
        eq_carried = float((1.0 + concat_returns).cumprod().iloc[-1])
        assert eq_carried == pytest.approx(float(seg_cont.equity.iloc[-1]), rel=1e-8), seed

        pnl_cont = [e["pnl"] for e in seg_cont.realized_events]
        pnl_carry = [e["pnl"] for e in segA.realized_events] + [e["pnl"] for e in segB.realized_events]
        assert len(pnl_cont) == len(pnl_carry), seed

        pf_cont = bt_metrics.profit_factor(pnl_cont)
        pf_carry = bt_metrics.profit_factor(pnl_carry)
        if not (math.isnan(pf_cont) or math.isnan(pf_carry) or math.isinf(pf_cont) or math.isinf(pf_carry)):
            pf_diffs.append(pf_carry - pf_cont)

        denom = max(sum(abs(p) for p in pnl_cont), 1e-9)
        rel_residuals.append(abs(sum(pnl_carry) - sum(pnl_cont)) / denom)

    n_favorable = sum(1 for d in pf_diffs if d > 1e-9)
    assert n_favorable <= 0.65 * len(pf_diffs), (
        f"profit_factor SYSTÉMATIQUEMENT plus favorable en mode portage (perp) : {n_favorable}/"
        f"{len(pf_diffs)} seeds favorables (seuil 65%)"
    )
    assert max(rel_residuals) < 0.10, (
        f"résidu relatif de PnL porté anormalement élevé côté perp : {max(rel_residuals):.3f} "
        "(epsilon documenté 0.10, plus resserré que le spot -- cf. docstring de ce test)"
    )


# ============================================================================================
# 6. Homothétie de la bande "par poche" (spec §5)
# ============================================================================================


@pytest.mark.parametrize("alloc", [0.1, 0.35, 1.0])
def test_no_trade_band_pocket_wallet_homothety(alloc):
    """Spec §5 : poids wallet = alloc x poids poche, bande wallet = 0,05 x alloc -- doit produire
    les MÊMES décisions hold/trade (mêmes bougies avec/sans ordre) que poids poche avec bande
    0,05. Isolé sur prix CONSTANTS + coût nul (même convention que les tests de bande existants
    de `test_engine.py`) pour retirer toute rétroaction de compounding sur l'équity qui romprait
    l'homothétie EXACTE (l'équity de base reste `initial_capital` tout du long dans les DEUX
    runs, seule condition sous laquelle la mise à l'échelle du poids et de la bande commute
    EXACTEMENT avec la comparaison `|scaled_w - current_w| < band`, cf. rapport d'implémentation
    pour la dérivation complète) -- vol targeting désactivé pour la même raison (isole
    strictement le mécanisme de bande, comme le fait déjà `test_default_no_trade_band_is_5pct_
    aligned_production`).

    **AVERTISSEMENT (audit adversarial 2026-09-07, finding F2 CRITIQUE, STATUT NON RÉSOLU) : ce
    test NE PROUVE PAS l'équivalence bande poche/bande wallet en GÉNÉRAL.** Il prouve uniquement
    le cas particulier "poche à rendement ~NUL" (prix constants ci-dessous -- l'équity de poche
    reste exactement `initial_capital` tout du long, condition SUFFISANTE mais pas nécessaire en
    général). Dès que du COMPOUNDING existe (équity de poche != `initial_capital`, cas normal sur
    un horizon réel), la relation poche->wallet devient AFFINE (`equity_wallet = 1 + alloc *
    (equity_poche - 1)`), PAS homothétique (`equity_wallet = alloc * equity_poche`) -- l'
    équivalence des décisions hold/trade est alors RÉFUTÉE, cf.
    `test_no_trade_band_pocket_wallet_homothety_refuted_under_compounding` ci-dessous (garde
    contre une re-revendication future de l'équivalence générale). Clause d'arrêt de la spec §5
    appliquée : ce point reste NON RÉSOLU, renvoyé au backlog pour analyse dédiée -- aucune
    candidate ne doit s'appuyer sur l'équivalence poche/wallet hors du cas rendement-poche-nul
    tant qu'il n'est pas instruit séparément (cf. `backtest/README.md` et
    `backtest/CARRY-EXTENSION-SPEC.md` §7)."""
    rng = np.random.default_rng(55)
    n = 200
    cal = pd.bdate_range("2015-01-05", periods=n)
    opens = pd.DataFrame({"X": 100.0}, index=cal)
    closes = pd.DataFrame({"X": 100.0}, index=cal)
    pocket_w = pd.DataFrame({"X": rng.random(n) * 0.9}, index=cal)

    seg_pocket = engine.simulate_segment(
        cal, pocket_w, opens, closes, 1, n - 1, cost_bps=0.0, no_trade_band=0.05, apply_vol_targeting=False
    )
    wallet_w = pocket_w * alloc
    seg_wallet = engine.simulate_segment(
        cal, wallet_w, opens, closes, 1, n - 1, cost_bps=0.0, no_trade_band=0.05 * alloc, apply_vol_targeting=False
    )

    exp_pocket = seg_pocket.gross_exposure.to_numpy()
    exp_wallet = seg_wallet.gross_exposure.to_numpy()
    assert exp_wallet == pytest.approx(alloc * exp_pocket, abs=1e-10)

    # "mêmes bougies avec ordre / sans ordre" (mission) : comparaison des TURNOVERS non nuls
    # (jamais des montants, l'échelle diffère par construction) -- une bougie de trade se
    # traduit ici par un CHANGEMENT de l'exposition affichée (prix constant -> l'exposition ne
    # peut varier QUE par un ordre).
    trade_bars_pocket = np.abs(np.diff(np.concatenate([[0.0], exp_pocket]))) > 1e-9
    trade_bars_wallet = np.abs(np.diff(np.concatenate([[0.0], exp_wallet]))) > 1e-9
    assert np.array_equal(trade_bars_pocket, trade_bars_wallet)
    assert trade_bars_pocket.sum() > 0 and trade_bars_pocket.sum() < n - 1  # non trivial : hold ET trade coexistent


def test_no_trade_band_pocket_wallet_homothety_refuted_under_compounding():
    """Finding F2 (CRITIQUE, audit adversarial 2026-09-07) -- garde contre une re-revendication
    future de l'équivalence bande poche/bande wallet EN GÉNÉRAL (`test_no_trade_band_pocket_
    wallet_homothety` ci-dessus ne prouve que le cas particulier rendement-poche-nul, cf. son
    AVERTISSEMENT). Scénario DÉTERMINISTE (aucun aléa -- reproductible bit-à-bit, pas de risque
    de "chance" sur un seed) construit pour isoler exactement le mécanisme de la réfutation :

      1. Bar 1 : entrée à poids `w1=0.5` (poche) / `w1*alloc` (wallet) -- équity des DEUX = 1.0.
      2. Bar 2 : HOLD des deux côtés (poids décidé inchangé, prix inchangé -- juste pour vérifier
         qu'aucun ordre ne bouge encore les choses).
      3. Saut de prix +200% (100 -> 300) entre les bars 2 et 3, position tenue -- l'équity de
         POCHE grimpe à 2.0 (compounding réel, PAS `initial_capital`), l'équity WALLET grimpe
         SEULEMENT à 1.2 -- exactement la relation AFFINE `1 + alloc*(equity_poche - 1)` = `1 +
         0.2*(2.0 - 1.0)` = `1.2`, prouvée exactement ci-dessous (PAS l'homothétie `alloc *
         equity_poche` = `0.4`, très éloignée).
      4. Bar 3 (poids décidé = `w2=0.75` poche / `w2*alloc` wallet) : le poids COURANT de poche
         (`shares*price/equity_poche` = `0.75`) est à l'INTÉRIEUR de la bande de `w2=0.75` ->
         HOLD poche. Le poids courant WALLET (`0.25`, car son équity a grossi MOINS vite que
         `alloc * equity_poche` ne le suppose) est à `0.25 - 0.75*alloc` = `0.25 - 0.15` = `0.10`
         de la cible wallet -- BIEN AU-DELÀ de la bande wallet (`0.05*alloc = 0.01`) -> TRADE
         wallet. Les décisions hold/trade DIVERGENT : la bande "par poche" n'est PAS équivalente
         à la bande de production dès que du compounding existe."""
    n = 4
    cal = pd.bdate_range("2020-01-06", periods=n)
    close = pd.Series([100.0, 100.0, 300.0, 300.0], index=cal)  # saut de prix entre bar 2 et 3
    opens = close.shift(1).bfill()
    closes = pd.DataFrame({"X": close}, index=cal)
    opens_df = pd.DataFrame({"X": opens}, index=cal)

    alloc = 0.2
    band = 0.05
    w1, w2 = 0.5, 0.75

    pocket_w = pd.DataFrame({"X": [w1, w1, w2, w2]}, index=cal)
    seg_pocket = engine.simulate_segment(
        cal, pocket_w, opens_df, closes, 1, 3, cost_bps=0.0, no_trade_band=band, apply_vol_targeting=False
    )
    wallet_w = pocket_w * alloc
    seg_wallet = engine.simulate_segment(
        cal, wallet_w, opens_df, closes, 1, 3, cost_bps=0.0, no_trade_band=band * alloc, apply_vol_targeting=False
    )

    eq_pocket = seg_pocket.equity.to_numpy()
    eq_wallet = seg_wallet.equity.to_numpy()
    # La relation poche->wallet est AFFINE, pas homothétique -- réfutation quantitative directe.
    assert eq_wallet == pytest.approx(1.0 + alloc * (eq_pocket - 1.0), abs=1e-12)
    assert eq_wallet != pytest.approx(alloc * eq_pocket, abs=1e-3)  # l'homothétie, elle, est FAUSSE

    exp_pocket = seg_pocket.gross_exposure.to_numpy()
    exp_wallet = seg_wallet.gross_exposure.to_numpy()
    trade_bars_pocket = np.abs(np.diff(np.concatenate([[0.0], exp_pocket]))) > 1e-9
    trade_bars_wallet = np.abs(np.diff(np.concatenate([[0.0], exp_wallet]))) > 1e-9
    # Réfutation de l'équivalence des DÉCISIONS (l'objet même de la spec §5) : la poche HOLD au
    # dernier bar (poids courant déjà dans la bande de sa cible) alors que le wallet, lui, TRADE
    # (son équity ayant crû AFFINEMENT, pas homothétiquement, son poids courant est sorti de la
    # bande wallet) -- PAS les mêmes décisions, contrairement à ce que §5 revendiquait.
    assert not trade_bars_pocket[-1]
    assert trade_bars_wallet[-1]
    assert not np.array_equal(trade_bars_pocket, trade_bars_wallet)
