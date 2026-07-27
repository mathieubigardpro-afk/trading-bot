"""backtest/data.py — chargement des prix quotidiens pour le moteur commun (backtest/engine.py).

Contrat des fichiers d'entrée (fourni par l'appelant via `--data-dir`, JAMAIS copié dans ce
dépôt — cf. docstring `backtest/__init__.py`) : un CSV gz par ticker, colonnes
`timestamp,open,high,low,close,volume`, clôtures quotidiennes déjà AJUSTÉES (dividendes/splits),
un fichier sous `<data_dir>/equities/<TICKER>.csv.gz` ou `<data_dir>/etf/<TICKER>.csv.gz`.

Deux représentations différentes des mêmes données sont exposées, pour deux usages distincts
qu'il ne faut JAMAIS confondre (cf. `backtest/strategies/xsmom.py`) :

  - `load_raw_series()` / `load_universe_raw()` : la série **brute** d'un ticker, telle que
    présente dans son fichier (SON PROPRE index, sans réindexation). C'est la représentation à
    utiliser pour tout calcul de signal (momentum, volatilité réalisée, SMA) qui compte des
    "jours de bourse" par POSITION dans l'historique réel du titre — exactement le contrat de
    `bot/strategies/xs_momentum_sp100._daily_closes` / `_momentum_as_of` (aucun backfill, aucune
    barre synthétique insérée avant la cotation réelle du titre).
  - `align_to_calendar()` : réindexation d'une série sur un calendrier canonique commun (celui
    de l'ETF de référence, SPY) — nécessaire pour la simulation de PORTEFEUILLE (valoriser au
    prix du jour, exécuter au prochain open) où toutes les lignes doivent partager le même axe
    de dates. Un `ffill` borné (`max_ffill_days`) absorbe un éventuel trou ponctuel de donnée
    SANS jamais masquer une absence prolongée (avant IPO / après retrait), qui doit rester `NaN`
    pour ne jamais être tradée par erreur — vérifié empiriquement sur ce jeu de données : aucun
    titre de `UNIVERSE_SP100` ne présente de trou > 10 jours calendaires une fois coté (calendrier
    strictement identique à celui de SPY dès la date de première cotation du titre), le `ffill`
    ci-dessous est donc un filet de sécurité documenté, pas un mécanisme activement sollicité.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd

REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume"]


def load_raw_series(path: Path) -> pd.DataFrame:
    """Charge un CSV gz de bougies quotidiennes, index = date (tz-naive, normalisée à minuit),
    trié croissant, dédoublonné (dernière valeur conservée) — même convention que
    `bot/strategies/xs_momentum_sp100._daily_closes`, étendue à `open` (nécessaire pour
    l'exécution t+1 du moteur)."""
    df = pd.read_csv(path, compression="gzip")
    if "timestamp" not in df.columns:
        raise ValueError(f"{path}: colonne 'timestamp' manquante")
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        raise ValueError(f"{path}: colonnes manquantes {missing_cols}")
    ts = pd.to_datetime(df["timestamp"], utc=True)
    idx = ts.dt.tz_convert(None).dt.normalize()
    out = df[REQUIRED_COLUMNS].astype(float)
    out.index = idx
    out.index.name = "date"
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


def _resolve_path(data_dir: Path, symbol: str, subdir: str) -> Path:
    return Path(data_dir) / subdir / f"{symbol}.csv.gz"


def load_symbol(data_dir: str | Path, symbol: str, subdir: str) -> pd.DataFrame:
    path = _resolve_path(Path(data_dir), symbol, subdir)
    if not path.exists():
        raise FileNotFoundError(f"donnée introuvable pour {symbol!r}: {path}")
    return load_raw_series(path)


def load_universe_raw(
    data_dir: str | Path, tickers: Iterable[str], subdir: str = "equities"
) -> Dict[str, pd.DataFrame]:
    """Charge la série BRUTE (son propre index, cf. docstring module) de chaque ticker de
    `tickers`. Lève `FileNotFoundError` si un ticker attendu est absent — un univers SPEC
    partiellement chargé serait un biais silencieux, jamais acceptable ici."""
    data_dir = Path(data_dir)
    out: Dict[str, pd.DataFrame] = {}
    missing: List[str] = []
    for t in tickers:
        path = _resolve_path(data_dir, t, subdir)
        if not path.exists():
            missing.append(t)
            continue
        out[t] = load_raw_series(path)
    if missing:
        raise FileNotFoundError(
            f"tickers manquants sous {data_dir / subdir}: {missing}"
        )
    return out


def build_calendar(reference: pd.DataFrame, start=None, end=None) -> pd.DatetimeIndex:
    """Calendrier de trading canonique = l'index (déjà trié, dédoublonné) d'une série de
    référence (SPY en pratique — la plus longue série disponible parmi les actifs REQUIS par
    la stratégie, donc la contrainte la plus stricte sur la période testable, cf.
    `docs/RESEARCH-BACKLOG.md` idée #3 : "identiques à xs_momentum_sp100" -> le filtre de
    régime SPY borne la période testable à partir de janvier 1993)."""
    idx = reference.index
    if start is not None:
        idx = idx[idx >= pd.Timestamp(start)]
    if end is not None:
        idx = idx[idx <= pd.Timestamp(end)]
    return idx


def align_to_calendar(
    df: pd.DataFrame, calendar: pd.DatetimeIndex, max_ffill_days: int = 5
) -> pd.DataFrame:
    """Réindexe `df` (colonnes OHLCV) sur `calendar`, `ffill` borné à `max_ffill_days` lignes
    (PAS un backfill : seule une valeur PASSÉE peut être propagée vers l'avant ; les dates
    antérieures à la première cotation réelle du titre restent `NaN`, jamais remplies). Voir
    docstring module pour la justification et la vérification empirique de non-usage sur ce
    jeu de données."""
    out = df.reindex(calendar)
    out = out.ffill(limit=max_ffill_days)
    return out


def align_universe_to_calendar(
    raw: Dict[str, pd.DataFrame], calendar: pd.DatetimeIndex, max_ffill_days: int = 5
) -> Dict[str, pd.DataFrame]:
    return {sym: align_to_calendar(df, calendar, max_ffill_days) for sym, df in raw.items()}


def opens_panel(aligned: Dict[str, pd.DataFrame], universe: Iterable[str]) -> pd.DataFrame:
    universe = list(universe)
    return pd.DataFrame({sym: aligned[sym]["open"] for sym in universe})


def closes_panel(aligned: Dict[str, pd.DataFrame], universe: Iterable[str]) -> pd.DataFrame:
    universe = list(universe)
    return pd.DataFrame({sym: aligned[sym]["close"] for sym in universe})
