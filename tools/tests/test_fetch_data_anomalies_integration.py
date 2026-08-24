"""Tests d'intégration du scan d'anomalies de corporate actions (backlog P0#11) dans
`tools/fetch_data.py:main()` — le scan doit être JOURNALISÉ à chaque régénération des
sections actions/ETF (fichiers `anomalies.json` + `DATA_ANOMALIES.md` dans le staging,
section résumé dans `DATA_REPORT.md`), sans JAMAIS bloquer la publication, même si le
scanner lui-même échoue. Aucun réseau : les sections de téléchargement sont monkeypatchées.
"""

from __future__ import annotations

import gzip
import io
import json
import os

import pandas as pd

import tools.fetch_data as fd


def _write_gz_csv(path: str, rows: list[tuple[str, float, float, float, float, float]]) -> None:
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    buf = io.BytesIO()
    with gzip.open(buf, "wt", encoding="utf-8") as f:
        df.to_csv(f, index=False)
    with open(path, "wb") as f:
        f.write(buf.getvalue())


def _mk_equity_file(staging_dir: str, symbol: str, spike: bool) -> None:
    """Série quotidienne synthétique de 10 jours ouvrés ; si `spike`, insère un saut
    close-to-close de +80% (anomalie certaine au seuil 40%).

    IMPORTANT (finding MAJEUR de l'audit adversarial 2026-08-24) : écrit sous
    `staging_dir/data/equities/` — le VRAI chemin produit par `run_daily_universe`
    (`os.path.join(staging_dir, "data", subdir, ...)`) — jamais `staging_dir/equities/`,
    sous peine de valider un scan pointé sur un répertoire qui n'existe pas en production."""
    os.makedirs(os.path.join(staging_dir, "data", "equities"), exist_ok=True)
    dates = pd.bdate_range("2024-01-02", periods=10)
    rows = []
    price = 100.0
    for i, d in enumerate(dates):
        if spike and i == 5:
            price *= 1.80
        rows.append((d.strftime("%Y-%m-%dT00:00:00+00:00"), price, price * 1.01, price * 0.99, price, 1e6))
        price *= 1.001
    _write_gz_csv(os.path.join(staging_dir, "data", "equities", f"{symbol}.csv.gz"), rows)


def _run_main_equities_only(tmp_path, monkeypatch, prepare_staging, scan_should_fail=False):
    """Exécute `fd.main()` en `--only equities --skip-git`, avec `run_daily_universe`
    monkeypatché pour déposer des fixtures dans le staging au lieu de télécharger."""
    staging = str(tmp_path / "staging")
    os.makedirs(staging, exist_ok=True)

    def fake_run_daily_universe(session, staging_dir, tickers, subdir, max_attempts, workers, min_years):
        prepare_staging(staging_dir)
        return {sym: {"status": "ok", "rows": 10, "source": "fixture"} for sym in ["AAA", "BBB"]}

    monkeypatch.setattr(fd, "run_daily_universe", fake_run_daily_universe)
    if scan_should_fail:
        import tools.check_data_anomalies as cda

        def boom(*args, **kwargs):
            raise RuntimeError("panne simulée du scanner")

        monkeypatch.setattr(cda, "scan_data_dir", boom)

    rc = fd.main(["--only", "equities", "--skip-git", "--staging-dir", staging])
    return rc, staging


def test_scan_journalise_anomalies_dans_le_staging(tmp_path, monkeypatch):
    def prepare(staging_dir):
        _mk_equity_file(staging_dir, "AAA", spike=True)
        _mk_equity_file(staging_dir, "BBB", spike=False)

    rc, staging = _run_main_equities_only(tmp_path, monkeypatch, prepare)
    assert rc == 0

    # anomalies.json : présent, contient le spike de AAA et rien pour BBB.
    with open(os.path.join(staging, "anomalies.json"), encoding="utf-8") as f:
        result = json.load(f)
    assert result["n_files_scanned"] == 2
    spikes = [a for a in result["anomalies"] if a["type"] == "return_spike"]
    assert any(a["symbol"] == "AAA" for a in spikes)
    assert not any(a["symbol"] == "BBB" for a in result["anomalies"])

    # DATA_ANOMALIES.md : présent et non vide.
    with open(os.path.join(staging, "DATA_ANOMALIES.md"), encoding="utf-8") as f:
        md = f.read()
    assert "AAA" in md

    # DATA_REPORT.md : section résumé présente, avec le compte et le renvoi au détail.
    with open(os.path.join(staging, "DATA_REPORT.md"), encoding="utf-8") as f:
        report = f.read()
    assert "## Anomalies de données (actions/ETF)" in report
    assert "DATA_ANOMALIES.md" in report
    assert "aucune donnée n'est corrigée" in report


def test_echec_du_scanner_ne_bloque_pas_la_publication(tmp_path, monkeypatch):
    def prepare(staging_dir):
        _mk_equity_file(staging_dir, "AAA", spike=False)

    rc, staging = _run_main_equities_only(tmp_path, monkeypatch, prepare, scan_should_fail=True)
    # Non bloquant : le run réussit quand même…
    assert rc == 0
    # …mais l'échec est signalé dans le rapport (jamais silencieux).
    with open(os.path.join(staging, "DATA_REPORT.md"), encoding="utf-8") as f:
        report = f.read()
    assert "ÉCHEC du scan automatique" in report
    # Et aucun journal d'anomalies trompeur n'est écrit.
    assert not os.path.exists(os.path.join(staging, "anomalies.json"))


def test_journaux_anomalies_publies_sur_la_branche(tmp_path, monkeypatch):
    """Finding MAJEUR de l'audit adversarial 2026-08-24 : les journaux d'anomalies doivent
    faire partie des entrées PUBLIÉES sur la branche orpheline (sinon DATA_REPORT.md pointe
    vers des fichiers absents de la branche — lien mort). Vérifie la constante ET une
    publication réelle dans un dépôt git jetable."""
    assert "anomalies.json" in fd._DEFAULT_PUBLISH_ENTRIES
    assert "DATA_ANOMALIES.md" in fd._DEFAULT_PUBLISH_ENTRIES

    import subprocess

    repo_dir = str(tmp_path / "repo")
    os.makedirs(repo_dir)
    subprocess.run(["git", "init", "-q", "-b", "main", repo_dir], check=True)
    subprocess.run(["git", "-C", repo_dir, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", repo_dir, "config", "user.name", "t"], check=True)
    with open(os.path.join(repo_dir, "seed.txt"), "w") as f:
        f.write("seed\n")
    subprocess.run(["git", "-C", repo_dir, "add", "-A"], check=True)
    subprocess.run(["git", "-C", repo_dir, "commit", "-q", "-m", "seed"], check=True)

    staging = str(tmp_path / "staging")
    os.makedirs(os.path.join(staging, "data", "equities"), exist_ok=True)
    for name, content in [
        ("MANIFEST.json", "{}\n"), ("DATA_REPORT.md", "# r\n"),
        ("anomalies.json", "{}\n"), ("DATA_ANOMALIES.md", "# a\n"),
    ]:
        with open(os.path.join(staging, name), "w") as f:
            f.write(content)

    # NB : la fonction restaure le dépôt et SUPPRIME la branche locale en sortie (finally) —
    # on inspecte donc le commit retourné (objet toujours présent), pas le nom de branche.
    sha = fd.publish_to_orphan_branch(repo_dir, staging, "test-branch", push=False)
    assert sha, "publish_to_orphan_branch doit retourner le sha du commit créé"
    listed = subprocess.run(
        ["git", "-C", repo_dir, "ls-tree", "-r", "--name-only", sha],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    assert "anomalies.json" in listed
    assert "DATA_ANOMALIES.md" in listed


def test_pas_de_scan_si_sections_actions_etf_sautees(tmp_path, monkeypatch):
    staging = str(tmp_path / "staging")
    os.makedirs(staging, exist_ok=True)

    def fake_run_crypto(session, staging_dir, max_attempts, workers):
        return {"archive_from": None, "archive_to": None, "full_coverage_required_from": None,
                "included": {}, "excluded": {}}

    monkeypatch.setattr(fd, "run_crypto", fake_run_crypto)
    rc = fd.main(["--only", "crypto", "--skip-git", "--staging-dir", staging])
    assert rc == 0
    assert not os.path.exists(os.path.join(staging, "anomalies.json"))
    assert not os.path.exists(os.path.join(staging, "DATA_ANOMALIES.md"))
    with open(os.path.join(staging, "DATA_REPORT.md"), encoding="utf-8") as f:
        report = f.read()
    assert "## Anomalies de données (actions/ETF)" not in report
