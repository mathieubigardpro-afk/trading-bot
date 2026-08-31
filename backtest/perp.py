"""backtest/perp.py — chargeurs de données PERPÉTUELS + FUNDING pour l'extension short du
moteur commun (`backtest/engine.py`), cf. `backtest/PERP-EXTENSION-SPEC.md` (§1 — cette spec
prime sur ce module en cas de divergence).

Ce module réutilise TEL QUEL les conventions de `backtest/data_hourly.py` (chargement d'un CSV
gz par symbole, index = timestamp UTC tz-naive à la précision de l'heure, `ffill` borné jamais
un backfill) pour les klines perpétuelles — seule différence : les colonnes produites dans les
matrices sont renommées `<SYM>-PERP` (jamais `<SYM>` seul) pour ne JAMAIS pouvoir être confondues
avec le spot du même symbole dans `weights_decided`/`opens`/`closes` (cf. spec §1 dernier point).

--------------------------------------------------------------------------------------------
Funding : format des fichiers sources et parsing (spec §1)
--------------------------------------------------------------------------------------------
`<data_dir>/funding/<SYM>.csv.gz`, colonnes `timestamp,funding_rate[,funding_interval_hours]` —
`timestamp` est l'INSTANT DE RÈGLEMENT (pas une ouverture de bougie), au format **ISO8601 mixte**
constaté empiriquement sur les données réelles (branche `market-data`, régénérée 2026-08-24) :
la plupart des lignes portent exactement l'heure ronde (`...T08:00:00+00:00`), certaines portent
un jitter de collecte de quelques millisecondes autour de l'heure ronde (`...T08:00:00.009+00:00`
observé ; la spec anticipe aussi un jitter NÉGATIF, ex. `07:59:59.999`, non rencontré sur ce jeu
de données mais géré identiquement par le `round`). Parsing obligatoire :
`pd.to_datetime(..., utc=True, format="ISO8601")` (gère les deux variantes avec/sans
millisecondes dans la même colonne, ce que `format="%Y-%m-%dT%H:%M:%S%z"` seul ne ferait pas)
PUIS `.dt.round("h")` (jamais `.floor`, qui décalerait tout jitter négatif d'une heure entière).
Vérifié empiriquement sur les 30 symboles de `_data/funding/` : après arrondi, 100% des minutes
valent 0 (aucune ambiguïté résiduelle, aucune collision de deux règlements distincts sur la même
heure ronde après arrondi).

--------------------------------------------------------------------------------------------
Alignement H -> H-1h et orphelins (spec §1, décision de mise en œuvre documentée ici)
--------------------------------------------------------------------------------------------
Un règlement à l'heure ronde H rémunère la position détenue PENDANT la bougie qui SE CLÔT à H,
donc la bougie d'index `H - 1h` dans un calendrier dont l'index est l'OUVERTURE de chaque bougie
horaire. `align_funding_to_calendar` affecte donc chaque règlement à la bougie `H - 1h`.

La spec (§1) prescrit, pour un règlement dont `H - 1h` est ABSENT du calendrier : "perdu s'il est
favorable, compté (sur la bougie précédente disponible) s'il est défavorable" — une règle
pessimiste qui suppose connu le SIGNE de la position détenue. Ce module (le loader) ne connaît
justement PAS ce signe : la même ligne de funding est favorable à un short et défavorable à un
long, ou l'inverse selon le signe de `rate`. Décider ici "favorable/défavorable" obligerait à
dupliquer la logique de position dans le loader, ou à la faire dépendre d'une hypothèse arbitraire
sur le sens du trade -- fragile et invisible à l'audit.

**Décision de mise en œuvre (documentée, PAS une invention de ce module — cf. mission
d'implémentation qui la prescrit explicitement en l'absence d'information de signe au
chargement)** : un règlement orphelin (sa bougie `H-1h` absente du calendrier) est simplement
EXCLU de la matrice alignée (aucune bougie du calendrier ne le reçoit, quel que soit son signe)
et reporté séparément dans `funding_orphans` (retourné par `align_funding_to_calendar`). Le
moteur (`engine.simulate_segment`) ignore purement et simplement `funding_orphans` : il ne lit
que la matrice alignée. **Ceci peut être favorable au bot** (un règlement défavorable orphelin
n'est jamais reporté sur la bougie précédente comme le ferait la lecture littérale de la spec) —
c'est un écart CONNU et documenté par rapport à la lettre de la spec §1, assumé pour ne pas
introduire une dépendance cachée au signe de position dans la couche de chargement des données.
`funding_alignment_report()` compte et journalise ces orphelins par symbole pour qu'aucun audit
ne les découvre en aval sans les avoir vus documentés ici.

**Mesure empirique sur les données réelles** (`_data/funding` + `_data/perp`, les 30 symboles,
calendrier = propre historique de chaque symbole) : **274 orphelins sur 146 568 règlements au
total (0,19 %)**. La quasi-totalité provient d'un unique orphelin structurel par symbole (le tout
premier règlement de l'historique, dont `H-1h` précède la toute première bougie perp disponible
— rien à faire d'autre, aucune bougie n'existe avant le début de la série) ; quelques symboles
(SOL, ainsi que FIL/HBAR/LTC/MANA/NEAR/SAND/TRX/VET/XLM/XRP à 16 chacun, et ICP à 78) présentent
en plus de courts trous RÉELS de couverture dans les klines perp extraites (quelques jours,
jamais corrigés ici — cf. consigne "ne corrige aucune donnée"). Sur la fenêtre spécifiquement
utilisée par le test d'intégration de ce module (BTC, 2024-01 -> 2024-03) : **0 orphelin sur 273
règlements** — la matrice alignée y est complète, cf. `backtest/tests/test_perp.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import pandas as pd

from backtest import data_hourly

# Colonnes obligatoires des klines perp -- identiques au spot horaire (data_hourly).
REQUIRED_PERP_COLUMNS = ["open", "high", "low", "close", "volume"]
REQUIRED_FUNDING_COLUMNS = ["funding_rate"]

PERP_COLUMN_SUFFIX = "-PERP"


def perp_column_name(symbol: str) -> str:
    """Nom de colonne perp dans les matrices du moteur -- convention unique de ce module,
    réutilisée partout (jamais recomposée en dur ailleurs)."""
    return f"{symbol}{PERP_COLUMN_SUFFIX}"


def _perp_path(data_dir: Path, symbol: str) -> Path:
    return Path(data_dir) / "perp" / f"{symbol}.csv.gz"


def _funding_path(data_dir: Path, symbol: str) -> Path:
    return Path(data_dir) / "funding" / f"{symbol}.csv.gz"


def load_perp_klines(data_dir: str | Path, symbols: Iterable[str]) -> Dict[str, pd.DataFrame]:
    """Charge les klines perpétuelles 1h de chaque symbole (`<data_dir>/perp/<SYM>.csv.gz`,
    mêmes colonnes OHLCV que `backtest/data_hourly.py`, timestamp = OUVERTURE de la bougie).

    Retourne un dict clé par le nom de colonne PERP final (`<SYM>-PERP`, cf. `perp_column_name`)
    -- pas par le symbole nu -- pour que toute construction de matrice en aval (`opens_panel`
    maison, cf. `build_aligned_perp_matrices`) produise directement des colonnes `<SYM>-PERP`
    sans étape de renommage séparée qui pourrait être oubliée par un appelant.

    Lève `FileNotFoundError` si un symbole attendu est absent (même convention stricte que
    `backtest/data_hourly.py:load_universe_raw` -- un univers partiellement chargé serait un
    biais silencieux)."""
    data_dir = Path(data_dir)
    out: Dict[str, pd.DataFrame] = {}
    missing: List[str] = []
    for sym in symbols:
        path = _perp_path(data_dir, sym)
        if not path.exists():
            missing.append(sym)
            continue
        df = pd.read_csv(path, compression="gzip")
        if "timestamp" not in df.columns:
            raise ValueError(f"{path}: colonne 'timestamp' manquante")
        missing_cols = [c for c in REQUIRED_PERP_COLUMNS if c not in df.columns]
        if missing_cols:
            raise ValueError(f"{path}: colonnes manquantes {missing_cols}")
        # Klines perp observées propres sur ce jeu de données (pas de jitter constaté, contrairement
        # au funding) mais `format="ISO8601"` reste le choix robuste et sans risque ici aussi --
        # même parseur que `data_hourly.load_raw_series` pour rester tz-naive UTC en sortie.
        ts = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601").dt.tz_convert(None)
        clean = df[REQUIRED_PERP_COLUMNS].astype(float)
        clean.index = ts
        clean.index.name = "timestamp"
        clean = clean.sort_index()
        clean = clean[~clean.index.duplicated(keep="last")]
        out[perp_column_name(sym)] = clean
    if missing:
        raise FileNotFoundError(f"klines perp manquantes sous {data_dir / 'perp'}: {missing}")
    return out


def load_funding(data_dir: str | Path, symbols: Iterable[str]) -> Dict[str, pd.Series]:
    """Charge le funding BRUT (non aligné) de chaque symbole (`<data_dir>/funding/<SYM>.csv.gz`),
    une `pd.Series` par symbole indexée par l'INSTANT DE RÈGLEMENT (arrondi à l'heure la plus
    proche, cf. docstring module -- jamais l'heure brute avec jitter, qui ne matcherait aucune
    bougie du calendrier). Clé du dict = nom de colonne perp final (`<SYM>-PERP`), même convention
    que `load_perp_klines`. `funding_interval_hours` (présent dans les fichiers sources) n'est
    PAS conservé ici : la spec (§1) est explicite -- "on ne ré-échantillonne jamais", chaque ligne
    brute est un règlement à part entière, quel que soit l'intervalle réel (2h/4h/8h) ; cette
    colonne ne sert donc à rien pour l'alignement (qui se contente de la position temporelle de
    chaque règlement)."""
    data_dir = Path(data_dir)
    out: Dict[str, pd.Series] = {}
    missing: List[str] = []
    for sym in symbols:
        path = _funding_path(data_dir, sym)
        if not path.exists():
            missing.append(sym)
            continue
        df = pd.read_csv(path, compression="gzip")
        if "timestamp" not in df.columns:
            raise ValueError(f"{path}: colonne 'timestamp' manquante")
        missing_cols = [c for c in REQUIRED_FUNDING_COLUMNS if c not in df.columns]
        if missing_cols:
            raise ValueError(f"{path}: colonnes manquantes {missing_cols}")
        # Formats ISO8601 mixtes (avec/sans millisecondes) -- cf. docstring module.
        ts = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601").dt.tz_convert(None)
        ts = ts.dt.round("h")  # JAMAIS .floor : un jitter négatif (ex. 07:59:59.999) doit
        # remonter à l'heure ronde SUIVANTE, pas rester accroché à l'heure précédente.
        rate = df["funding_rate"].astype(float)
        series = pd.Series(rate.to_numpy(), index=pd.DatetimeIndex(ts.to_numpy()))
        series = series.sort_index()
        # Deux règlements distincts arrondis sur la MÊME heure ronde (non observé sur les
        # données réelles, cf. docstring -- garde-fou théorique) : sommés plutôt que le dernier
        # écrasant silencieusement le premier, un règlement de funding ne doit jamais disparaître.
        series = series.groupby(level=0).sum()
        out[perp_column_name(sym)] = series
    if missing:
        raise FileNotFoundError(f"funding manquant sous {data_dir / 'funding'}: {missing}")
    return out


def align_funding_to_calendar(
    funding_raw: Dict[str, pd.Series], calendar: pd.DatetimeIndex
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Aligne le funding brut (`load_funding`) sur `calendar` (index = OUVERTURE de bougie).

    Retourne `(funding_aligned, funding_orphans)` :
      - `funding_aligned` : DataFrame index=`calendar`, colonnes=`funding_raw.keys()`
        (`<SYM>-PERP`), valeur = taux réglé À LA CLÔTURE de cette bougie (0.0 si aucun règlement
        ne clôture sur cette bougie -- JAMAIS `NaN`, cf. spec §1 : "une bougie sans règlement
        porte 0.0"). Un règlement à l'heure H est placé sur la bougie `H - 1h` (celle qui se
        clôt à H, cf. docstring module).
      - `funding_orphans` : DataFrame (index = timestamp `H-1h` orphelin, colonnes = symboles
        concernés, `NaN` ailleurs) des règlements dont la bougie `H-1h` est ABSENTE de
        `calendar` -- non appliqués nulle part dans `funding_aligned` (cf. docstring module
        pour la justification de ce choix et son caractère potentiellement favorable au bot).
        Vide (0 ligne) si aucun orphelin. Utiliser `funding_alignment_report()` pour un
        décompte exploitable plutôt que de parcourir ce DataFrame à la main."""
    calendar = pd.DatetimeIndex(calendar)
    cols = list(funding_raw.keys())
    aligned = pd.DataFrame(0.0, index=calendar, columns=cols)
    orphan_frames: List[pd.DataFrame] = []
    for sym, series in funding_raw.items():
        candle_idx = series.index - pd.Timedelta(hours=1)
        shifted = pd.Series(series.to_numpy(), index=candle_idx)
        shifted = shifted.groupby(level=0).sum()
        in_cal = shifted.index.isin(calendar)
        matched = shifted[in_cal]
        if len(matched) > 0:
            aligned.loc[matched.index, sym] = matched.to_numpy()
        if bool((~in_cal).any()):
            orphan_series = shifted[~in_cal]
            orphan_frames.append(orphan_series.to_frame(name=sym))
    if orphan_frames:
        orphans = pd.concat(orphan_frames, axis=1).sort_index()
    else:
        orphans = pd.DataFrame(index=pd.DatetimeIndex([]), columns=cols, dtype=float)
    return aligned, orphans


def funding_alignment_report(funding_orphans: pd.DataFrame) -> Dict[str, int]:
    """Décompte des règlements orphelins (`align_funding_to_calendar`) par symbole, plus un
    total -- destiné à être publié tel quel dans un rapport de backtest (`results.json`) pour
    qu'un audit adversarial voie ce nombre sans devoir relire ce module (cf. mission : "sur les
    données réelles ce nombre doit être quasi nul, vérifie-le"). Un total non nul n'est PAS une
    erreur en soi (cf. docstring module -- orphelin de bord de série + trous réels documentés
    dans les données sources), mais doit toujours être visible."""
    counts = {str(col): int(funding_orphans[col].notna().sum()) for col in funding_orphans.columns}
    counts["total"] = int(sum(counts.values()))
    return counts


def _panel(aligned: Dict[str, pd.DataFrame], col: str) -> pd.DataFrame:
    """Assemble une matrice (une colonne OHLC par symbole déjà aligné) -- équivalent local de
    `data_hourly.opens_panel`/`closes_panel` mais générique sur `col` (high/low en plus)."""
    return pd.DataFrame({sym: df[col] for sym, df in aligned.items()})


def build_aligned_perp_matrices(
    data_dir: str | Path,
    symbols: Iterable[str],
    calendar: pd.DatetimeIndex,
    max_ffill_hours: int = data_hourly.DEFAULT_MAX_FFILL_HOURS,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Construit, en une seule fonction, les six matrices perp attendues par
    `engine.simulate_segment` (`opens`, `highs`, `lows`, `closes`, `funding`, `funding_orphans`),
    toutes indexées sur `calendar` (index = ouverture de bougie) et colonnées `<SYM>-PERP`.

    Klines : réindexées sur `calendar` avec `ffill` borné à `max_ffill_hours` LIGNES (jamais de
    backfill), même filet de sécurité documenté que `backtest/data_hourly.py:align_to_calendar`
    -- un trou ponctuel de quelques heures est absorbé, une absence prolongée (avant listing du
    perp, p. ex.) reste `NaN` et ne doit JAMAIS être tradée par erreur.
    Funding : aligné via `align_funding_to_calendar` (pas de `ffill` -- une bougie sans
    règlement porte struturellement 0.0, cf. spec §1, jamais une valeur recopiée d'une bougie
    antérieure)."""
    symbols = list(symbols)
    raw_klines = load_perp_klines(data_dir, symbols)
    aligned_klines = data_hourly.align_universe_to_calendar(raw_klines, calendar, max_ffill_hours)
    opens = _panel(aligned_klines, "open")
    highs = _panel(aligned_klines, "high")
    lows = _panel(aligned_klines, "low")
    closes = _panel(aligned_klines, "close")

    funding_raw = load_funding(data_dir, symbols)
    funding, funding_orphans = align_funding_to_calendar(funding_raw, calendar)

    return opens, highs, lows, closes, funding, funding_orphans
