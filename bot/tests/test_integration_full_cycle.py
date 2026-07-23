"""Test d'intégration bout-en-bout (mission d'intégration §4) : un cycle COMPLET de
`bot.runner.main()`, avec prix/historiques MOCKÉS mais RICHES (pas de réseau réel — bloqué dans
ce bac à sable), sur les 3 wallets réels (`bot.config.WALLETS`, poches réelles), et vérifie que
les ordres qui en sortent sont COHÉRENTS avec le SPEC :

  - tailles dans les caps (gross exposure du wallet <= 1 - part cash de ses poches) ;
  - notionnels de chaque fill >= minimum (`bot.config.MIN_NOTIONAL_USD`) ;
  - aucun ordre actions/ETF hors séance régulière NYSE (marché fermé -> NO_TRADE partout,
    positions conservées) ;
  - la poche crypto quasi-passive achète BTC quand son historique mocké le place au-dessus de
    sa SMA200 journalière (filtre de tendance "on") ;
  - CHAQUE poche non-cash d'un wallet dont la stratégie a émis des cibles brutes non nulles ce
    cycle a réellement exécuté au moins un ordre BUY/SELL (pas seulement "un trade quelque
    part" -- assertion ajoutée suite à l'audit qui a trouvé que cette suite ne détectait ni la
    poche actions structurellement gelée par une no-trade band wallet-wide mal calibrée, ni les
    rejets systématiques d'ordres actions/ETF faute de quantités fractionnaires).

Aucun appel réseau réel : `get_prices`, `get_fx_rate`, `get_history` (crypto horaire),
`prefetch_daily_history`/`get_daily_history` (actions/ETF journalier) sont tous substitués par
des doublures déterministes construites ici.
"""

from __future__ import annotations

import json
import math
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import pandas as pd
import pytest

import bot.runner as runner
from bot import config
from bot.feeds.fx import FxRate
from bot.feeds.types import HistoryUnavailableError, Quote
from tools.migrate_to_wallets import migrate

# Mercredi 22 juillet 2026, 14:30 UTC = 10:30 America/New_York (EDT, UTC-4) -> séance NYSE
# régulière ouverte (09:30-16:00 ET), pas de jour férié.
MARKET_OPEN_NOW = datetime(2026, 7, 22, 14, 30, tzinfo=timezone.utc)
# Samedi 25 juillet 2026, 14:30 UTC -> marché fermé (week-end), crypto reste 24/7.
MARKET_CLOSED_NOW = datetime(2026, 7, 25, 14, 30, tzinfo=timezone.utc)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


def _git_ok(repo: Path, *args: str) -> str:
    result = _git(repo, *args)
    assert result.returncode == 0, f"git {args} a échoué : {result.stderr}"
    return result.stdout


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
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


# ------------------------------------------------------------------------------------------
# Générateurs de données synthétiques déterministes (aucun hasard : tests reproductibles)
# ------------------------------------------------------------------------------------------


def _synthetic_hourly_crypto(now: datetime, n_hours: int, start_price: float, drift_per_hour: float) -> pd.DataFrame:
    """Bougies horaires CLÔTURÉES synthétiques, tendance haussière régulière + petite
    oscillation (pour une vol EWMA réaliste, ni nulle ni explosive) — dernière clôture
    largement au-dessus de la SMA200 journalière agrégée (filtre de tendance "on" garanti)."""
    end = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    idx = pd.date_range(end=end, periods=n_hours, freq="h", tz="UTC")
    closes = []
    price = start_price
    for i in range(n_hours):
        price = price * (1 + drift_per_hour) * (1 + 0.0015 * math.sin(i / 5.0))
        closes.append(price)
    df = pd.DataFrame(
        {
            "open": closes, "high": [c * 1.001 for c in closes], "low": [c * 0.999 for c in closes],
            "close": closes, "volume": [1000.0] * n_hours,
        },
        index=idx,
    )
    df.index.name = "ts"
    return df


def _synthetic_daily(now: datetime, n_days: int, start_price: float, drift_per_day: float, wobble: float = 0.004) -> pd.DataFrame:
    """Bougies JOURNALIÈRES CLÔTURÉES synthétiques (jours ouvrés, week-ends exclus), tendance
    régulière + petite oscillation déterministe. `_last_confirmed_month_end`/`_decision_date`
    (bot/strategies/{dual_momentum_etf,xs_momentum_sp100}.py) n'exigent pas une fidélité totale
    au calendrier NYSE réel (ils recherchent la date la plus récente qui EST, au sens du vrai
    calendrier, une fin de mois confirmée) : une plage de jours ouvrés standard suffit."""
    end_date = (now - timedelta(days=1)).date()
    idx = pd.bdate_range(end=end_date, periods=n_days, tz="UTC")
    closes = []
    price = start_price
    for i in range(n_days):
        price = price * (1 + drift_per_day) * (1 + wobble * math.sin(i / 7.0))
        closes.append(price)
    df = pd.DataFrame(
        {
            "open": closes, "high": [c * 1.005 for c in closes], "low": [c * 0.995 for c in closes],
            "close": closes, "volume": [10_000.0] * n_days,
        },
        index=idx,
    )
    df.index.name = "ts"
    return df


def _fake_quote(price: float, now: datetime, source: str = "fake") -> Quote:
    return Quote(bid=price * 0.999, ask=price * 1.001, mid=price, ts=now.isoformat(), source=source)


def _build_fixtures(now: datetime):
    """Construit prix + historiques riches pour TOUS les symboles réellement suivis par
    `bot.config.WALLETS` (crypto union + S&P100 + SPY + univers ETF risqué + IEF)."""
    crypto_symbols = sorted({sym for w in config.WALLETS for sym in w["univers_crypto"]})
    equities_symbols = list(runner.EQUITIES_TRADABLE_SYMBOLS)
    etf_symbols = list(runner.ETF_TRADABLE_SYMBOLS)

    prices: Dict[str, Quote] = {}
    hourly_history: Dict[str, pd.DataFrame] = {}
    for i, sym in enumerate(crypto_symbols):
        start = 100.0 + i * 37.0  # prix de départ variés mais déterministes
        hist = _synthetic_hourly_crypto(now, config.HISTORY_N_HOURS, start_price=start, drift_per_hour=0.0006)
        hourly_history[sym] = hist
        prices[sym] = _fake_quote(float(hist["close"].iloc[-1]), now, source="binance")

    daily_history: Dict[str, pd.DataFrame] = {}
    # SPY : nette tendance haussière -> filtre de régime xs_momentum_sp100 "on".
    spy_hist = _synthetic_daily(now, runner.DAILY_MIN_WARMUP_DAYS, start_price=400.0, drift_per_day=0.0012)
    daily_history["SPY"] = spy_hist
    prices["SPY"] = _fake_quote(float(spy_hist["close"].iloc[-1]), now, source="yahoo")

    for i, sym in enumerate(equities_symbols):
        # Les 12 premiers du classement alphabétique (> top_k=10) ont un fort momentum positif
        # pour garantir au moins top_k=10 candidats éligibles à un momentum strictement positif ;
        # le reste a une tendance neutre/légèrement négative (réaliste, pas de "tout gagnant").
        drift = 0.0015 if i < 15 else -0.0002
        start = 50.0 + (i % 20) * 8.0
        hist = _synthetic_daily(now, runner.DAILY_MIN_WARMUP_DAYS, start_price=start, drift_per_day=drift)
        daily_history[sym] = hist
        prices[sym] = _fake_quote(float(hist["close"].iloc[-1]), now, source="yahoo")

    # IEF (bogey obligataire) : quasiment plat -> les ETF risqués en tendance haussière nette le
    # battent au test de momentum absolu (slots réellement investis, pas 100% IEF par défaut).
    ief_hist = _synthetic_daily(now, runner.DAILY_MIN_WARMUP_DAYS, start_price=100.0, drift_per_day=0.00005)
    daily_history["IEF"] = ief_hist
    prices["IEF"] = _fake_quote(float(ief_hist["close"].iloc[-1]), now, source="yahoo")

    for sym in etf_symbols:
        if sym in daily_history:
            continue  # SPY déjà généré ci-dessus (présent dans les deux univers)
        hist = _synthetic_daily(now, runner.DAILY_MIN_WARMUP_DAYS, start_price=80.0, drift_per_day=0.001)
        daily_history[sym] = hist
        prices[sym] = _fake_quote(float(hist["close"].iloc[-1]), now, source="yahoo")

    return prices, hourly_history, daily_history


def _install_fixtures(monkeypatch, prices: Dict[str, Quote], hourly_history, daily_history, now: datetime):
    def _fake_get_prices(symbols: List[str]) -> Dict[str, Quote]:
        return {sym: prices.get(sym) for sym in symbols}

    def _fake_get_history(symbol: str, n_hours: int) -> pd.DataFrame:
        if symbol not in hourly_history:
            raise HistoryUnavailableError(f"pas de fixture pour {symbol}")
        return hourly_history[symbol].tail(n_hours)

    def _fake_prefetch_daily_history(symbols, asset_class, n_days=None):
        return {sym: ("ok" if sym in daily_history else "indisponible") for sym in symbols}

    def _fake_get_daily_history(symbol: str, n_days: int, asset_class: str) -> pd.DataFrame:
        if symbol not in daily_history:
            raise HistoryUnavailableError(f"pas de fixture pour {symbol}")
        return daily_history[symbol].tail(n_days)

    monkeypatch.setattr(runner, "get_prices", _fake_get_prices)
    monkeypatch.setattr(
        runner, "get_fx_rate",
        lambda pair, last_known=None: FxRate(rate=1.08, ts=now.isoformat(), source="frankfurter", stale=False),
    )
    monkeypatch.setattr(runner, "get_history", _fake_get_history)
    monkeypatch.setattr(runner, "prefetch_daily_history", _fake_prefetch_daily_history)
    monkeypatch.setattr(runner, "get_daily_history", _fake_get_daily_history)


def _read_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _wallet_alloc_ex_cash(wallet_cfg: dict) -> float:
    return sum(
        float(p["capital_alloc_pct"]) for p in wallet_cfg.get("pockets", []) if p.get("strategy_ref")
    )


# ------------------------------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------------------------------


def test_no_trade_band_scale_by_symbol_matches_pocket_alloc_for_all_wallets():
    """Unitaire, sans réseau ni cycle complet : `runner._no_trade_band_scale_by_symbol()`
    (correctif audit critique #1) doit associer à chaque symbole tradable d'un wallet le
    `capital_alloc_pct` EXACT de la poche qui le porte, pour les 3 wallets réels de
    `bot.config.WALLETS` (pas seulement un cas synthétique)."""
    for wallet_cfg in config.WALLETS:
        scale = runner._no_trade_band_scale_by_symbol(wallet_cfg)
        for pocket in wallet_cfg.get("pockets", []) or []:
            if not pocket.get("strategy_ref"):
                continue
            alloc = float(pocket["capital_alloc_pct"])
            if pocket["asset_class"] == "crypto":
                symbols = wallet_cfg["univers_crypto"]
            else:
                symbols = runner.POCKET_STRATEGY_TRADABLE_SYMBOLS[pocket["strategy_ref"]]
            for sym in symbols:
                assert scale[sym] == pytest.approx(alloc), (
                    f"wallet {wallet_cfg['id']}: {sym} attendu alloc={alloc}, obtenu {scale.get(sym)}"
                )
        # poche cash : aucun symbole n'y est rattaché (pas de strategy_ref -> ignorée)
        cash_pockets = [p for p in wallet_cfg["pockets"] if p["asset_class"] == "cash"]
        assert all(p.get("strategy_ref") is None for p in cash_pockets)


def test_full_cycle_market_open_produces_coherent_orders(tmp_path, monkeypatch):
    origin, clone = _make_repo(tmp_path)
    monkeypatch.setattr(runner, "repo_dir", lambda: str(clone))
    monkeypatch.chdir(clone)

    prices, hourly_history, daily_history = _build_fixtures(MARKET_OPEN_NOW)
    _install_fixtures(monkeypatch, prices, hourly_history, daily_history, MARKET_OPEN_NOW)

    exit_code = runner.main(now=MARKET_OPEN_NOW)
    assert exit_code == 0

    any_trade_anywhere = False
    any_btc_buy = False

    for wallet_cfg in config.WALLETS:
        wallet_id = wallet_cfg["id"]
        state_path = clone / config.wallet_state_json(wallet_id)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["fx"]["initial_rate"] == pytest.approx(1.08)

        trades = _read_jsonl(clone / config.wallet_trades_jsonl(wallet_id))
        equity_lines = _read_jsonl(clone / config.wallet_equity_jsonl(wallet_id))
        decisions = _read_jsonl(clone / config.wallet_decisions_jsonl(wallet_id))
        assert len(equity_lines) == 1
        equity_rec = equity_lines[0]

        # --- notionnels >= minimum (contrat ExchangeSim, vérifié explicitement ici) ---
        for t in trades:
            assert t["notional_usd"] >= config.MIN_NOTIONAL_USD
            any_trade_anywhere = True
            if t["symbol"] == "BTC" and t["side"] == "BUY":
                any_btc_buy = True

        # --- exposition brute du wallet <= somme des allocations non-cash de ses poches
        # (+ petite marge flottante) : jamais de dépassement de la structure en poches ---
        max_alloc = _wallet_alloc_ex_cash(wallet_cfg)
        assert equity_rec["gross_exposure_pct"] <= max_alloc + 1e-6, (
            f"wallet {wallet_id}: gross_exposure_pct={equity_rec['gross_exposure_pct']} "
            f"dépasse la somme des poches non-cash ({max_alloc})"
        )

        # --- decisions.jsonl cohérent : marché ouvert -> les décisions actions/ETF portent
        # market_open=True (séance régulière NYSE en cours au moment du test) ---
        for d in decisions:
            if d["asset_class"] in ("equities", "etf"):
                assert d["market_open"] is True
            else:
                assert d["market_open"] is True  # crypto : toujours 24/7

    assert any_trade_anywhere, "aucun trade sur aucun des 3 wallets — fixtures manifestement dégénérées"
    assert any_btc_buy, (
        "BTC est au-dessus de sa SMA200 journalière mockée dans TOUS les wallets qui le "
        "suivent (prudent/équilibré/agressif) : au moins un achat BTC est attendu"
    )

    # --- couverture décisive audit (findings critiques #1/#2, mission d'intégration §4) : ---
    # "au moins un trade quelque part" ne suffit pas -- CHAQUE poche non-cash d'un wallet dont la
    # stratégie a émis des cibles brutes non nulles ce cycle doit avoir RÉELLEMENT déployé une
    # part de son capital (gross_exposure > 0), pas seulement la poche crypto. Sans cette
    # assertion, une poche actions structurellement gelée (no-trade band wallet-wide, finding
    # #1) ou des ordres actions/ETF systématiquement rejetés (pas de quantité fractionnaire,
    # finding #2) passaient inaperçus derrière `any_trade_anywhere=True` porté par la seule
    # poche crypto.
    for wallet_cfg in config.WALLETS:
        wallet_id = wallet_cfg["id"]
        decisions = _read_jsonl(clone / config.wallet_decisions_jsonl(wallet_id))
        executed_by_pocket: Dict[str, set] = {}
        raw_signal_nonzero_by_pocket: Dict[str, bool] = {}
        for d in decisions:
            asset_class = d["asset_class"]
            executed_by_pocket.setdefault(asset_class, set())
            # `decision` in {"BUY", "SELL"} = ordre RÉELLEMENT EXÉCUTÉ par ExchangeSim (pas
            # seulement une cible calculée par le RiskManager, cf. `poids_cible_apres_risk` qui
            # peut être non nul alors même que l'ordre est ensuite rejeté par ExchangeSim -- ce
            # que ce test doit précisément détecter, finding critique #2).
            if d["decision"] in ("BUY", "SELL"):
                executed_by_pocket[asset_class].add(d["symbol"])
            if d.get("poids_cible_brut"):
                raw_signal_nonzero_by_pocket[asset_class] = True

        for pocket in wallet_cfg.get("pockets", []) or []:
            asset_class = pocket.get("asset_class")
            if asset_class == "cash" or not pocket.get("strategy_ref"):
                continue
            if not raw_signal_nonzero_by_pocket.get(asset_class):
                continue  # signal brut nul ce cycle (ex. filtre de régime "off") : rien à exiger
            deployed = executed_by_pocket.get(asset_class) or set()
            assert deployed, (
                f"wallet {wallet_id}: la poche '{asset_class}' (stratégie "
                f"{pocket.get('strategy_ref')!r}) a émis des cibles brutes non nulles ce cycle "
                "mais AUCUN ordre BUY/SELL n'a été réellement exécuté sur cette poche -- poche "
                "structurellement gelée (no-trade band mal calibrée -> poids_cible_apres_risk "
                "resté au poids actuel, et/ou ordres systématiquement rejetés par ExchangeSim "
                "-- cf. findings critiques #1/#2 de l'audit)"
            )


def test_full_cycle_market_closed_blocks_equities_etf_orders_but_not_crypto(tmp_path, monkeypatch):
    origin, clone = _make_repo(tmp_path)
    monkeypatch.setattr(runner, "repo_dir", lambda: str(clone))
    monkeypatch.chdir(clone)

    prices, hourly_history, daily_history = _build_fixtures(MARKET_CLOSED_NOW)
    _install_fixtures(monkeypatch, prices, hourly_history, daily_history, MARKET_CLOSED_NOW)

    exit_code = runner.main(now=MARKET_CLOSED_NOW)
    assert exit_code == 0

    any_crypto_trade = False

    for wallet_cfg in config.WALLETS:
        wallet_id = wallet_cfg["id"]
        trades = _read_jsonl(clone / config.wallet_trades_jsonl(wallet_id))
        decisions = _read_jsonl(clone / config.wallet_decisions_jsonl(wallet_id))

        for t in trades:
            asset_class = runner._asset_class_of(t["symbol"], wallet_cfg["univers_crypto"])
            assert asset_class == "crypto", (
                f"wallet {wallet_id}: ordre {t['side']} {t['symbol']} exécuté marché fermé "
                "(week-end) alors que seule la crypto doit trader 24/7"
            )
            any_crypto_trade = True

        for d in decisions:
            if d["asset_class"] in ("equities", "etf"):
                assert d["market_open"] is False
                assert d["decision"] == "NO_TRADE"
            else:
                assert d["market_open"] is True

    assert any_crypto_trade, "aucun trade crypto le week-end — fixtures dégénérées (crypto doit rester 24/7)"
