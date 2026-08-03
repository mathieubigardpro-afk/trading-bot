"""backtest/data_hourly.py — chargeur HORAIRE minimal pour l'univers crypto 6 majors
(`backtest/run_vol_breakout.py`), même esprit que `backtest/data.py` (quotidien, actions) mais
adapté à des bougies horaires : PAS de normalisation à minuit (`load_raw_series` de `data.py`
tronque l'heure, ce qui écraserait 24 bougies/jour sur un seul timestamp -- inutilisable ici).

Contrat des fichiers d'entrée : `_data/crypto/<SYMBOLE>.csv.gz`, colonnes
`timestamp,open,high,low,close,volume`, une ligne = une heure UTC (2022-01-01 -> 2026-06-30).

--------------------------------------------------------------------------------------------
Calendrier commun et alignement (cf. mission -- "alignement quasi trivial, vérifie et
documente le nombre de trous réels")
--------------------------------------------------------------------------------------------
Le calendrier canonique est l'UNION des timestamps de tous les symboles de l'univers (pas la
série d'un seul symbole de référence comme `data.py` le fait avec SPY -- ici les 6 majors ont
tous une couverture quasi identique 2022-2026, aucune raison de privilégier arbitrairement l'un
d'eux). `align_to_calendar` réindexe chaque symbole sur cette union et applique un `ffill` BORNÉ
à `max_ffill_hours=3` heures (documenté : un filet de sécurité pour un trou ponctuel de
quelques heures, jamais un backfill, jamais un remplissage illimité qui masquerait une absence
prolongée de cotation).

**Vérification empirique faite avant d'écrire ce module** (BTC, ETH, SOL, DOGE, LINK, AVAX,
2022-01-01T00:00 -> 2026-06-30T23:00) : les 6 fichiers contiennent EXACTEMENT les mêmes 39 407
timestamps horaires (`union(6 symboles) == intersection(6 symboles)`, écart nul) -- la seule
irrégularité du pas horaire présente dans les données brutes (un pas de 2h au lieu de 1h, le
2023-03-24 12:00 -> 14:00 UTC, probablement un artefact de collecte au changement d'heure d'été)
affecte IDENTIQUEMENT les 6 symboles : l'heure 2023-03-24T13:00 est absente des 6 fichiers à la
fois, donc absente de l'UNION elle-même -- ce n'est donc même pas un "trou" au sens de cette
fonction (rien à `ffill`, l'heure n'existe simplement pas dans le calendrier canonique, comme un
jour férié partagé). **Trous réels rencontrés après réindexation sur le calendrier commun : 0
pour les 6 symboles** (`count_real_gaps` ci-dessous le revérifie programmatiquement et est
appelé par `backtest/run_vol_breakout.py` pour que ce chiffre soit documenté dans les résultats,
pas seulement affirmé ici). Le `ffill` borné reste un filet de sécurité codé par prudence
(cohérence avec `backtest/data.py`), pas un mécanisme activement sollicité sur ce jeu de données.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd

REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume"]

# Filet de sécurité documenté (cf. docstring module) -- borné à 3h, jamais un backfill illimité.
DEFAULT_MAX_FFILL_HOURS = 3


def load_raw_series(path: Path) -> pd.DataFrame:
    """Charge un CSV gz de bougies HORAIRES, index = timestamp UTC (tz-naive, PRÉCISION HEURE
    conservée -- pas de `.normalize()`, contrairement à `backtest/data.py:load_raw_series` qui
    est quotidien), trié croissant, dédoublonné (dernière valeur conservée)."""
    df = pd.read_csv(path, compression="gzip")
    if "timestamp" not in df.columns:
        raise ValueError(f"{path}: colonne 'timestamp' manquante")
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        raise ValueError(f"{path}: colonnes manquantes {missing_cols}")
    ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(None)
    out = df[REQUIRED_COLUMNS].astype(float)
    out.index = ts
    out.index.name = "timestamp"
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


def _resolve_path(data_dir: Path, symbol: str) -> Path:
    return Path(data_dir) / f"{symbol}.csv.gz"


def load_symbol(data_dir: str | Path, symbol: str) -> pd.DataFrame:
    path = _resolve_path(Path(data_dir), symbol)
    if not path.exists():
        raise FileNotFoundError(f"donnée horaire introuvable pour {symbol!r}: {path}")
    return load_raw_series(path)


def load_universe_raw(data_dir: str | Path, symbols: Iterable[str]) -> Dict[str, pd.DataFrame]:
    """Charge la série BRUTE (son propre index horaire) de chaque symbole. Lève
    `FileNotFoundError` si un symbole attendu est absent -- un univers SPEC partiellement chargé
    serait un biais silencieux, jamais acceptable (même convention que `backtest/data.py`)."""
    data_dir = Path(data_dir)
    out: Dict[str, pd.DataFrame] = {}
    missing: List[str] = []
    for sym in symbols:
        path = _resolve_path(data_dir, sym)
        if not path.exists():
            missing.append(sym)
            continue
        out[sym] = load_raw_series(path)
    if missing:
        raise FileNotFoundError(f"symboles manquants sous {data_dir}: {missing}")
    return out


def build_calendar(raw: Dict[str, pd.DataFrame], start=None, end=None) -> pd.DatetimeIndex:
    """Calendrier horaire canonique = UNION des timestamps de tous les symboles de l'univers
    (cf. docstring module -- pas un symbole de référence unique comme `data.py`/SPY, aucun des
    6 majors n'a de raison a priori d'être la contrainte la plus stricte)."""
    idx = pd.DatetimeIndex([])
    for df in raw.values():
        idx = idx.union(df.index)
    idx = idx.sort_values()
    if start is not None:
        idx = idx[idx >= pd.Timestamp(start)]
    if end is not None:
        idx = idx[idx <= pd.Timestamp(end)]
    return idx


def align_to_calendar(
    df: pd.DataFrame, calendar: pd.DatetimeIndex, max_ffill_hours: int = DEFAULT_MAX_FFILL_HOURS
) -> pd.DataFrame:
    """Réindexe `df` (colonnes OHLCV) sur `calendar`, `ffill` borné à `max_ffill_hours` LIGNES
    (le calendrier n'étant pas forcément à pas constant -- cf. le pas de 2h documenté ci-dessus
    -- une limite en "lignes" plutôt qu'en durée reste cohérente avec `backtest/data.py`, et
    reste un filet de sécurité pour un trou ponctuel de quelques heures, jamais un backfill).
    Les dates antérieures à la première cotation réelle (aucun symbole concerné ici, cf.
    docstring module) resteraient `NaN`, jamais remplies."""
    out = df.reindex(calendar)
    out = out.ffill(limit=max_ffill_hours)
    return out


def align_universe_to_calendar(
    raw: Dict[str, pd.DataFrame], calendar: pd.DatetimeIndex, max_ffill_hours: int = DEFAULT_MAX_FFILL_HOURS
) -> Dict[str, pd.DataFrame]:
    return {sym: align_to_calendar(df, calendar, max_ffill_hours) for sym, df in raw.items()}


def opens_panel(aligned: Dict[str, pd.DataFrame], universe: Iterable[str]) -> pd.DataFrame:
    universe = list(universe)
    return pd.DataFrame({sym: aligned[sym]["open"] for sym in universe})


def closes_panel(aligned: Dict[str, pd.DataFrame], universe: Iterable[str]) -> pd.DataFrame:
    universe = list(universe)
    return pd.DataFrame({sym: aligned[sym]["close"] for sym in universe})


def count_real_gaps(raw: Dict[str, pd.DataFrame], calendar: pd.DatetimeIndex) -> Dict[str, int]:
    """Compte, pour chaque symbole, le nombre de lignes du calendrier commun où la clôture BRUTE
    (avant tout `ffill`) est `NaN` -- le nombre de "vrais trous" que l'alignement doit combler.
    Appelé par `backtest/run_vol_breakout.py` pour documenter empiriquement ce chiffre dans
    `results.json` (cf. mission : "vérifie et documente le nombre de trous réels rencontrés"),
    plutôt que de se contenter de l'affirmer dans une docstring."""
    gaps: Dict[str, int] = {}
    for sym, df in raw.items():
        reindexed_close = df["close"].reindex(calendar)
        gaps[sym] = int(reindexed_close.isna().sum())
    return gaps
