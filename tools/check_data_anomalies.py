#!/usr/bin/env python3
"""tools/check_data_anomalies.py — détecteur d'anomalies de corporate actions dans les données
OHLCV quotidiennes actions/ETF (`docs/RESEARCH-BACKLOG.md` idée P0#11, ajoutée session
hebdomadaire #1 du 2026-07-27).

--------------------------------------------------------------------------------------------
Le cas DHR/Fortive qui motive ce script
--------------------------------------------------------------------------------------------
L'audit adversarial du backtest inverse-vol (2026-07-27) a trouvé une anomalie concrète dans
les données `market-data` : le spin-off DHR/Fortive (juillet 2016) mal ajusté dans la série
« ajustée » yfinance, produisant un rendement close-to-close de +62% en une seule séance. Ce
saut artificiel a fait entrer DHR dans le top-10 momentum pendant 6 mois, alors que le
mouvement ne reflète aucune performance réelle du titre — un simple artefact d'ajustement de
corporate action mal calculé côté fournisseur. Sans impact favorable sur CE backtest précis
(l'exclure améliorait même le résultat), mais rien ne garantit qu'une future anomalie du même
type ne gonfle pas artificiellement une future candidate — d'où ce garde-fou permanent.

--------------------------------------------------------------------------------------------
Principe directeur : JOURNALISER, jamais corriger silencieusement
--------------------------------------------------------------------------------------------
Ce script ne modifie AUCUNE donnée. Il ne fait que détecter des motifs statistiquement
suspects (rendement extrême, incohérence OHLC, trou de calendrier) et les consigner pour
REVUE HUMAINE. Corriger automatiquement reviendrait à remplacer une erreur silencieuse du
fournisseur par une erreur silencieuse du bot — au moins aussi dangereux, et sans les moyens
(calendrier de corporate actions fiable, ratio de split/spin-off exact) de le faire
correctement ici. Un faux positif est le résultat ATTENDU et ACCEPTÉ sur un vrai krach
idiosyncratique : un titre qui perd réellement 45% en une séance sur un profit warning DOIT
apparaître dans ce rapport — ce n'est pas une erreur de détection, c'est un signal légitime
qu'un humain doit trancher (rien ne distingue a priori, sans calendrier de corporate actions
externe, un -45% "vrai" d'un -45% d'ajustement erroné). Le seuil par défaut (40%, cf.
`DEFAULT_THRESHOLD`) est calibré large-cap S&P100/ETF sectoriels — volontairement large pour
ne pas noyer le signal DHR (+62%) dans du bruit, mais assez bas pour ne rater aucun spin-off
mal ajusté de cette ampleur. Aucun seuil n'est un gate de production : ce script s'exécute en
dehors du pipeline de trading (`bot/`), jamais dans son chemin critique, et son code de retour
NE REFLÈTE PAS le nombre d'anomalies trouvées (voir `main()`).

--------------------------------------------------------------------------------------------
Ce qui est détecté (trois familles indépendantes, cf. `scan_frame`)
--------------------------------------------------------------------------------------------
  (a) `return_spike` : rendement close-to-close absolu > `threshold` entre deux barres
      consécutives de la série BRUTE du titre (même contrat que `backtest/data.py:
      load_raw_series` — pas de réindexation calendaire, un trou de données n'est donc jamais
      confondu avec deux séances consécutives).
  (b) `ohlc_inconsistency` : `low > high`, `close` hors de `[low, high]`, `open` hors de
      `[low, high]`, ou un prix `<= 0` — signes de données corrompues indépendamment de toute
      ampleur de mouvement. Les comparaisons de bornes (pas le `<= 0`) tolèrent un écart
      relatif infime (`_OHLC_REL_TOL`, cf. plus bas) pour ignorer le bruit de flottant réel
      constaté sur les prix ajustés multi-décennies (ex. AAPL 1981 : close et low identiques
      « en vrai » mais différents à 1e-16 près) — sans quoi ce bruit purement numérique noierait
      le signal utile sous des milliers de faux positifs sans rapport avec une vraie
      incohérence de donnée.
  (c) `calendar_gap` : plus de 10 jours CALENDAIRES entre deux barres consécutives (hors tout
      premier point de la série, qui n'a pas de barre précédente et ne peut donc pas être un
      "trou" — cf. `backtest/data.py` : la période avant IPO reste `NaN`/absente par
      construction, ce n'est pas une anomalie de CETTE série). Le seuil de 10 jours reprend
      celui documenté (et vérifié empiriquement comme non déclenché en pratique une fois coté)
      dans `backtest/data.py:align_to_calendar` — un week-end + un jour férié isolé fait au
      plus ~4 jours calendaires, 10 jours laisse une marge large pour les ponts fériés
      multi-jours sans faire remonter du bruit.

Contrat d'entrée IDENTIQUE à `backtest/data.py:load_raw_series` (index = date, colonnes
`open, high, low, close, volume`, trié croissant, dédoublonné) — ce module ne charge rien
lui-même par souci de découplage : `scan_data_dir` fait le lien via `backtest.data` pour rester
la source de vérité unique du format de fichier.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backtest.data import load_raw_series  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tools.check_data_anomalies")

# Seuil de rendement quotidien par défaut — cf. bandeau module. Calibré large-cap : un ETF
# sectoriel ou une action S&P100 qui bouge de >40% en une séance sans corporate action est
# quasi toujours soit une anomalie de donnée, soit un événement suffisamment extrême pour
# mériter une revue humaine de toute façon.
DEFAULT_THRESHOLD = 0.40

# Trou de calendrier (jours CALENDAIRES, pas jours de bourse) au-delà duquel on journalise —
# même valeur que celle documentée/vérifiée dans `backtest/data.py:align_to_calendar`.
DEFAULT_MAX_GAP_DAYS = 10

# Tolérance RELATIVE utilisée uniquement pour les comparaisons de bornes OHLC (low<=high,
# open/close dans [low, high]) — PAS pour le check "prix <= 0" (celui-ci reste une comparaison
# stricte, un prix nul ou négatif est sans ambiguïté anormal). Constat empirique en écrivant ce
# script sur les données réelles `market-data` : les prix ajustés (dividendes/splits cumulés
# sur 40+ ans côté yfinance) portent un bruit de flottant de l'ordre de 1e-15 à 1e-16 EN
# RELATIF (ex. AAPL 1981-09-21 : close=0.06105915457010269 vs low=0.0610591545701027 — la
# même valeur "réelle", deux arrondis de calcul différents) — sans cette tolérance, ce bruit
# purement numérique noie le signal réel (return_spike) sous des milliers de faux
# "close hors de [low, high]" sans rapport avec une VRAIE incohérence de donnée. Ceci ne
# "corrige" aucune donnée (principe directeur du module) : c'est une tolérance de COMPARAISON
# dans le détecteur lui-même, appliquée symétriquement, jamais pour masquer un écart réel.
_OHLC_REL_TOL = 1e-8


def _ohlc_tol(*prices: float) -> float:
    return max((abs(p) for p in prices), default=0.0) * _OHLC_REL_TOL

DEFAULT_SUBDIRS: Tuple[str, ...] = ("equities", "etf")


def _anomaly(symbol: str, date: pd.Timestamp, type_: str, valeur: float, detail: str) -> dict:
    return {
        "symbol": symbol,
        "date": pd.Timestamp(date).date().isoformat(),
        "type": type_,
        "valeur": valeur,
        "detail": detail,
    }


def scan_frame(
    df: pd.DataFrame,
    symbol: str,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS,
) -> List[dict]:
    """Scanne une série OHLCV quotidienne (index = date, colonnes `open, high, low, close,
    volume`, MÊME contrat que `backtest/data.py:load_raw_series` — série brute, propre index du
    titre, triée croissant) pour un unique symbole, et retourne la liste des anomalies
    détectées (dicts `{symbol, date, type, valeur, detail}`, triés par ordre de rencontre =
    ordre chronologique de l'index).

    Ne modifie jamais `df`. Ne lève pas d'exception sur des données suspectes — c'est
    précisément leur détection qui est l'objet de cette fonction ; seule une entrée
    structurellement inexploitable (colonnes manquantes) lève `ValueError`, au même titre que
    `load_raw_series`."""
    missing_cols = [c for c in ("open", "high", "low", "close") if c not in df.columns]
    if missing_cols:
        raise ValueError(f"{symbol}: colonnes manquantes {missing_cols}")

    anomalies: List[dict] = []
    if df.empty:
        return anomalies

    # --- (b) incohérences OHLC : indépendant de toute barre voisine, vérifié barre par barre.
    for ts, row in df.iterrows():
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        if pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c):
            # Une barre partiellement NaN n'est pas de notre ressort ici (le pipeline amont —
            # `backtest/data.py` — décide de son traitement) ; on ne la fait pas planter le scan.
            continue
        tol = _ohlc_tol(o, h, l, c)
        if l > h + tol:
            anomalies.append(_anomaly(
                symbol, ts, "ohlc_inconsistency", float(l - h),
                f"low ({l:.4f}) > high ({h:.4f})",
            ))
        for price_name, price in (("open", o), ("high", h), ("low", l), ("close", c)):
            if price <= 0:
                anomalies.append(_anomaly(
                    symbol, ts, "ohlc_inconsistency", float(price),
                    f"{price_name} <= 0 ({price:.4f})",
                ))
        # Bornes [low, high] vérifiées seulement si low<=high (sinon déjà signalé ci-dessus,
        # pas la peine de doubler le signal avec une comparaison qui n'a plus de sens).
        if l <= h + tol:
            if not (l - tol <= c <= h + tol):
                anomalies.append(_anomaly(
                    symbol, ts, "ohlc_inconsistency", float(c),
                    f"close ({c:.4f}) hors de [low={l:.4f}, high={h:.4f}]",
                ))
            if not (l - tol <= o <= h + tol):
                anomalies.append(_anomaly(
                    symbol, ts, "ohlc_inconsistency", float(o),
                    f"open ({o:.4f}) hors de [low={l:.4f}, high={h:.4f}]",
                ))

    # --- (a) rendement close-to-close extrême, et (c) trou de calendrier — comparent chaque
    # barre à la PRÉCÉDENTE (barre par POSITION dans l'index, comme le reste du pipeline pour
    # les séries brutes, cf. docstring `backtest/data.py`) ; aucune des deux ne s'applique à la
    # toute première barre (pas de précédente = pas de "saut" ni de "trou" mesurable).
    closes = df["close"]
    dates = df.index
    for i in range(1, len(df)):
        prev_close = closes.iloc[i - 1]
        cur_close = closes.iloc[i]
        prev_date = dates[i - 1]
        cur_date = dates[i]

        if pd.notna(prev_close) and pd.notna(cur_close) and prev_close != 0 and not pd.isna(prev_close):
            ret = (cur_close - prev_close) / prev_close
            if abs(ret) > threshold:
                anomalies.append(_anomaly(
                    symbol, cur_date, "return_spike", float(ret),
                    f"rendement close-to-close {ret:+.1%} ({prev_close:.4f} -> {cur_close:.4f}, "
                    f"veille {prev_date.date().isoformat()})",
                ))

        gap_days = (pd.Timestamp(cur_date) - pd.Timestamp(prev_date)).days
        if gap_days > max_gap_days:
            anomalies.append(_anomaly(
                symbol, cur_date, "calendar_gap", float(gap_days),
                f"{gap_days} jours calendaires depuis la barre précédente "
                f"({prev_date.date().isoformat()} -> {cur_date.date().isoformat()})",
            ))

    return anomalies


def scan_data_dir(
    data_dir: str | Path,
    subdirs: Tuple[str, ...] = DEFAULT_SUBDIRS,
    threshold: float = DEFAULT_THRESHOLD,
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS,
) -> dict:
    """Scanne tous les `*.csv.gz` (format `backtest/data.py:load_raw_series`) des sous-dossiers
    `subdirs` de `data_dir` (typiquement `equities/` et `etf/`, cf. contrat de
    `backtest/data.py`) et agrège les anomalies détectées.

    Un fichier illisible/corrompu (échec de `load_raw_series`) est journalisé en erreur et
    SAUTÉ (jamais fatal pour l'ensemble du scan — cohérent avec la posture "un incident isolé
    ne doit jamais faire planter tout le run", cf. `tools/fetch_data.py`), mais reste compté
    dans `n_files_scanned` pour que le rapport ne masque pas silencieusement son existence."""
    data_dir = Path(data_dir)
    anomalies: List[dict] = []
    n_files_scanned = 0

    for subdir in subdirs:
        subdir_path = data_dir / subdir
        if not subdir_path.is_dir():
            logger.warning("sous-dossier introuvable, ignoré : %s", subdir_path)
            continue
        for csv_path in sorted(subdir_path.glob("*.csv.gz")):
            symbol = csv_path.name[: -len(".csv.gz")]
            n_files_scanned += 1
            try:
                df = load_raw_series(csv_path)
            except Exception as exc:  # noqa: BLE001 — un fichier corrompu ne doit pas arrêter le scan.
                logger.error("échec de lecture %s (%s) : %s", symbol, csv_path, exc)
                anomalies.append(_anomaly(
                    symbol, pd.Timestamp.now(tz="UTC").normalize(), "read_error", 0.0,
                    f"échec de lecture de {csv_path.name} : {exc}",
                ))
                continue
            anomalies.extend(
                scan_frame(df, symbol, threshold=threshold, max_gap_days=max_gap_days)
            )

    return {
        "anomalies": anomalies,
        "n_files_scanned": n_files_scanned,
        "params": {
            "threshold": threshold,
            "max_gap_days": max_gap_days,
            "subdirs": list(subdirs),
            "data_dir": str(data_dir),
        },
    }


_TYPE_LABELS = {
    "return_spike": "Sauts de rendement (|close-to-close| > seuil)",
    "ohlc_inconsistency": "Incohérences OHLC",
    "calendar_gap": "Trous de calendrier (> max_gap_days)",
    "read_error": "Fichiers illisibles",
}

_TYPE_ORDER = ["return_spike", "ohlc_inconsistency", "calendar_gap", "read_error"]


def format_report_md(result: dict) -> str:
    """Rend `result` (sortie de `scan_data_dir`, ou structure équivalente construite à la main
    — ex. {"anomalies": [...], "n_files_scanned": N, "params": {...}}) en un rapport markdown
    lisible : un tableau par type d'anomalie, trié par symbole puis date."""
    anomalies = result.get("anomalies", [])
    n_files = result.get("n_files_scanned", "?")
    params = result.get("params", {})

    lines: List[str] = []
    lines.append("# Rapport d'anomalies de données (tools/check_data_anomalies.py)")
    lines.append("")
    lines.append(
        "Détecteur d'anomalies de corporate actions — `docs/RESEARCH-BACKLOG.md` idée P0#11 "
        "(cas DHR/Fortive, audit 2026-07-27). Ce rapport est un JOURNAL pour revue humaine : "
        "aucune correction automatique n'a été appliquée aux données. Un faux positif sur un "
        "vrai krach idiosyncratique est attendu et normal — l'humain tranche."
    )
    lines.append("")
    lines.append(f"- Fichiers scannés : **{n_files}**")
    lines.append(f"- Anomalies détectées : **{len(anomalies)}**")
    if params:
        lines.append(
            f"- Paramètres : seuil de rendement = {params.get('threshold')}, "
            f"trou de calendrier max = {params.get('max_gap_days')} jours, "
            f"sous-dossiers = {params.get('subdirs')}"
        )
    lines.append("")

    if not anomalies:
        lines.append("Aucune anomalie détectée.")
        return "\n".join(lines) + "\n"

    by_type: Dict[str, List[dict]] = {}
    for a in anomalies:
        by_type.setdefault(a.get("type", "?"), []).append(a)

    ordered_types = [t for t in _TYPE_ORDER if t in by_type]
    ordered_types += sorted(t for t in by_type if t not in _TYPE_ORDER)

    for type_ in ordered_types:
        rows = sorted(by_type[type_], key=lambda a: (a.get("symbol", ""), a.get("date", "")))
        label = _TYPE_LABELS.get(type_, type_)
        lines.append(f"## {label} ({len(rows)})")
        lines.append("")
        lines.append("| Symbole | Date | Valeur | Détail |")
        lines.append("|---|---|---|---|")
        for a in rows:
            valeur = a.get("valeur", "")
            valeur_str = f"{valeur:.4f}" if isinstance(valeur, float) else str(valeur)
            detail = str(a.get("detail", "")).replace("|", "\\|")
            lines.append(f"| {a.get('symbol', '')} | {a.get('date', '')} | {valeur_str} | {detail} |")
        lines.append("")

    return "\n".join(lines) + "\n"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", required=True, help="répertoire contenant equities/*.csv.gz et etf/*.csv.gz (contrat backtest/data.py)")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help=f"seuil de rendement close-to-close absolu (défaut {DEFAULT_THRESHOLD})")
    parser.add_argument("--max-gap-days", type=int, default=DEFAULT_MAX_GAP_DAYS, help=f"trou de calendrier max en jours avant signalement (défaut {DEFAULT_MAX_GAP_DAYS})")
    parser.add_argument("--subdirs", nargs="+", default=list(DEFAULT_SUBDIRS), help=f"sous-dossiers à scanner (défaut {list(DEFAULT_SUBDIRS)})")
    parser.add_argument("--out-json", default=None, help="chemin de sortie JSON (optionnel)")
    parser.add_argument("--out-md", default=None, help="chemin de sortie markdown (optionnel)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    """Point d'entrée CLI. Choix DÉLIBÉRÉ : le code de retour reflète UNIQUEMENT une erreur
    d'exécution (0 = run terminé normalement, 1 = exception), JAMAIS le nombre d'anomalies
    trouvées — ce script est un JOURNAL pour revue humaine, pas un gate qui bloquerait un
    pipeline en amont (cf. bandeau module). Une CI qui voudrait bloquer sur ce script devrait
    lire explicitement `n_anomalies` dans le JSON produit, pas le code de retour."""
    args = parse_args(argv)
    try:
        result = scan_data_dir(
            args.data_dir,
            subdirs=tuple(args.subdirs),
            threshold=args.threshold,
            max_gap_days=args.max_gap_days,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("échec d'exécution du scan : %s", exc)
        return 1

    n_anomalies = len(result["anomalies"])
    logger.info(
        "scan terminé : %d fichier(s) scanné(s), %d anomalie(s) détectée(s) (journal — "
        "revue humaine requise, aucune correction appliquée)",
        result["n_files_scanned"], n_anomalies,
    )

    if args.out_json:
        out_json_path = Path(args.out_json)
        out_json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False, sort_keys=True)
        logger.info("JSON écrit : %s", out_json_path)

    if args.out_md:
        out_md_path = Path(args.out_md)
        out_md_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_md_path, "w", encoding="utf-8") as f:
            f.write(format_report_md(result))
        logger.info("markdown écrit : %s", out_md_path)

    if not args.out_json and not args.out_md:
        print(format_report_md(result))

    return 0


if __name__ == "__main__":
    sys.exit(main())
