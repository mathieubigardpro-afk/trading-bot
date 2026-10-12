"""Tests du correctif backlog #18 (robustesse début-de-mois du rafraîchissement crypto) de
`tools/fetch_data.py` : complément API étendu aux mois requis manquants EN QUEUE d'historique
(archive mensuelle Binance Vision du mois clos pas encore publiée), jamais aux trous
anciens/du milieu (trace du biais du survivant).

Intégralement OFFLINE : `download_binance_month` et `fetch_binance_current_month_completion`
sont monkeypatchés (aucun appel réseau), l'horloge de `fd.datetime` est figée.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

import tools.fetch_data as fd

FROZEN_NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

# Fenêtre requise réduite pour les tests (le texte de la raison utilise tout de même
# `CRYPTO_FULL_COVERAGE_REQUIRED_FROM`, sans importance ici).
ARCHIVE_MONTHS = [(2026, 5), (2026, 6), (2026, 7), (2026, 8), (2026, 9)]
REQUIRED_MONTHS = [(2026, 6), (2026, 7), (2026, 8), (2026, 9)]


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW if tz is None else FROZEN_NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    monkeypatch.setattr(fd, "datetime", _FrozenDatetime)


def _candles(start_iso: str, end_iso: str, drop_last: int = 0, drop_first: int = 0) -> pd.DataFrame:
    """Bougies 1h (open_time) de `start_iso` inclus à `end_iso` EXCLU, avec éventuellement
    les `drop_first` premières / `drop_last` dernières supprimées."""
    ts = pd.date_range(start_iso, end_iso, freq="1h", tz="UTC", inclusive="left")
    ts = ts[drop_first: len(ts) - drop_last if drop_last else len(ts)]
    return pd.DataFrame({
        "timestamp": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
    })


class _Net:
    """Faux réseau : archives présentes pour `present` (liste de (y, m)), le reste NOT_FOUND ;
    complément API = `completion_df` ; enregistre tous les appels."""

    def __init__(self, monkeypatch, present, completion_df=None):
        self.completion_calls = []
        self.download_calls = []
        self.completion_df = completion_df if completion_df is not None else _candles(
            "2026-10-01", "2026-10-05 11:00")
        present = set(present)

        def fake_download(session, pair, y, m, max_attempts):
            self.download_calls.append((y, m))
            if (y, m) in present:
                ts = pd.date_range(datetime(y, m, 1, tzinfo=timezone.utc), periods=2, freq="1h")
                return "OK", pd.DataFrame({
                    "timestamp": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
                })
            return "NOT_FOUND", None

        def fake_completion(session, pair, month_start, max_attempts):
            self.completion_calls.append(month_start)
            return self.completion_df.copy()

        monkeypatch.setattr(fd, "download_binance_month", fake_download)
        monkeypatch.setattr(fd, "fetch_binance_current_month_completion", fake_completion)


def _run():
    return fd.process_crypto_symbol(object(), "TST", ARCHIVE_MONTHS, REQUIRED_MONTHS, max_attempts=1)


# ----------------------------------------------------------------------------------------
# (a) queue d'1 mois entièrement couverte par l'API -> inclus
# ----------------------------------------------------------------------------------------


def test_queue_un_mois_couverte_par_api_est_incluse(monkeypatch):
    api = _candles("2026-09-01", "2026-10-05 11:00")
    net = _Net(monkeypatch, present=[(2026, 5), (2026, 6), (2026, 7), (2026, 8)], completion_df=api)

    res = _run()

    assert res.included is True
    assert res.tail_months_completed_via_api == ["2026-09"]
    assert "2026-09" in res.reason and "via API" in res.reason and "non encore publiée" in res.reason
    # L'API est interrogée depuis le DÉBUT du premier mois de la queue (pas du mois courant).
    assert net.completion_calls == [datetime(2026, 9, 1, tzinfo=timezone.utc)]
    # Les bougies de septembre sont bien dans le résultat.
    assert (res.df["timestamp"] == pd.Timestamp("2026-09-30 23:00", tz="UTC")).any()
    assert res.df["timestamp"].is_monotonic_increasing and res.df["timestamp"].is_unique


def test_manifeste_run_crypto_porte_le_champ_tail_months(monkeypatch, tmp_path):
    """Le champ `tail_months_completed_via_api` est écrit pour les inclus ET les exclus."""
    api = _candles("2026-09-01", "2026-10-05 11:00")
    _Net(monkeypatch, present=[(2026, 5), (2026, 6), (2026, 7), (2026, 8)], completion_df=api)
    monkeypatch.setattr(fd, "CRYPTO_SYMBOLS", ["TST"])
    monkeypatch.setattr(fd, "CRYPTO_ARCHIVE_START", (2026, 5))
    monkeypatch.setattr(fd, "CRYPTO_FULL_COVERAGE_REQUIRED_FROM", (2026, 6))
    monkeypatch.setattr(fd, "last_complete_month", lambda now=None: (2026, 9))

    report = fd.run_crypto(object(), str(tmp_path), max_attempts=1, workers=1)

    assert report["included"]["TST"]["tail_months_completed_via_api"] == ["2026-09"]
    assert "via API" in report["included"]["TST"]["reason"]
    # Schéma existant inchangé.
    for key in ("pair", "months_ok", "months_missing", "months_error", "rows", "first_ts", "last_ts", "reason"):
        assert key in report["included"]["TST"]


def test_manifeste_run_crypto_champ_vide_sans_mois_de_queue(monkeypatch, tmp_path):
    _Net(monkeypatch, present=[(2026, 5), (2026, 6), (2026, 7), (2026, 8), (2026, 9)])
    monkeypatch.setattr(fd, "CRYPTO_SYMBOLS", ["TST"])
    monkeypatch.setattr(fd, "CRYPTO_ARCHIVE_START", (2026, 5))
    monkeypatch.setattr(fd, "CRYPTO_FULL_COVERAGE_REQUIRED_FROM", (2026, 6))
    monkeypatch.setattr(fd, "last_complete_month", lambda now=None: (2026, 9))

    report = fd.run_crypto(object(), str(tmp_path), max_attempts=1, workers=1)

    assert report["included"]["TST"]["tail_months_completed_via_api"] == []
    assert report["included"]["TST"]["reason"] == "historique complet depuis la fenêtre requise"


# ----------------------------------------------------------------------------------------
# (b) queue couverte partiellement -> exclu, raison enrichie
# ----------------------------------------------------------------------------------------


def test_queue_partiellement_couverte_derniere_bougie_absente_est_exclue(monkeypatch):
    # Les 3 dernières bougies de septembre manquent (dernière = 20:00 < fin - 1h).
    api = pd.concat([
        _candles("2026-09-01", "2026-10-01", drop_last=3),
        _candles("2026-10-01", "2026-10-05 11:00"),
    ], ignore_index=True)
    # NB : on retire aussi toute bougie d'octobre située avant la fin de septembre ci-dessus ;
    # les bougies d'octobre n'ont pas le droit de « boucher » la fin de septembre.
    _Net(monkeypatch, present=[(2026, 5), (2026, 6), (2026, 7), (2026, 8)], completion_df=api)

    res = _run()

    assert res.included is False
    assert res.tail_months_completed_via_api == []
    assert "complément API en queue tenté" in res.reason
    assert "mois 2026-09 non couvert" in res.reason
    assert f"{len(api)} bougies" in res.reason
    assert res.df is None


def test_queue_debut_de_mois_absent_au_dela_de_la_tolerance_est_exclue(monkeypatch):
    # Les 2 premières bougies de septembre manquent (première = 02:00 > début + 1h).
    api = _candles("2026-09-01", "2026-10-05 11:00", drop_first=2)
    _Net(monkeypatch, present=[(2026, 5), (2026, 6), (2026, 7), (2026, 8)], completion_df=api)

    res = _run()

    assert res.included is False
    assert "mois 2026-09 non couvert" in res.reason


def test_api_vide_queue_exclue_avec_raison_enrichie(monkeypatch):
    empty = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    _Net(monkeypatch, present=[(2026, 5), (2026, 6), (2026, 7), (2026, 8)], completion_df=empty)

    res = _run()

    assert res.included is False
    assert "0 bougies" in res.reason and "mois 2026-09 non couvert" in res.reason


def test_semantique_mois_couvert_tolerance_exacte():
    base = _candles("2026-09-01", "2026-10-01")
    assert fd._month_covered_by_candles(base, 2026, 9) is True
    # Première bougie à début + 1h : toléré ; à début + 2h : non couvert.
    assert fd._month_covered_by_candles(_candles("2026-09-01", "2026-10-01", drop_first=1), 2026, 9) is True
    assert fd._month_covered_by_candles(_candles("2026-09-01", "2026-10-01", drop_first=2), 2026, 9) is False
    # Dernière bougie = 23:00 (fin - 1h) : couvert ; 22:00 : non couvert (pessimiste).
    assert fd._month_covered_by_candles(_candles("2026-09-01", "2026-10-01", drop_last=1), 2026, 9) is False
    # Aucune bougie dans le mois : non couvert, même avec des bougies du mois suivant.
    assert fd._month_covered_by_candles(_candles("2026-10-01", "2026-10-03"), 2026, 9) is False
    assert fd._month_covered_by_candles(pd.DataFrame(columns=["timestamp"]), 2026, 9) is False


# ----------------------------------------------------------------------------------------
# (c) trou au MILIEU / ancien -> exclusion inchangée, API jamais appelée
# ----------------------------------------------------------------------------------------


def test_trou_au_milieu_exclu_comme_avant_et_api_non_appelee(monkeypatch):
    # 2026-07 manque (milieu), septembre présent. L'API « pourrait » tout couvrir.
    api = _candles("2026-06-01", "2026-10-05 11:00")
    net = _Net(monkeypatch, present=[(2026, 5), (2026, 6), (2026, 8), (2026, 9)], completion_df=api)

    res = _run()

    assert res.included is False
    assert net.completion_calls == []
    assert res.tail_months_completed_via_api == []
    assert res.reason == (
        "historique incomplet sur la fenêtre de complétude requise "
        f"[{fd._yyyymm(*fd.CRYPTO_FULL_COVERAGE_REQUIRED_FROM)} .. 2026-09] : 1 mois manquant(s) "
        "(2026-07) — exclu (trace du biais du survivant)"
    )


def test_trou_du_milieu_plus_queue_manquante_exclu_sans_appel_api(monkeypatch):
    # 2026-07 (milieu) ET 2026-09 (queue) manquent : le trou ancien l'emporte, pas d'API.
    net = _Net(monkeypatch, present=[(2026, 5), (2026, 6), (2026, 8)])

    res = _run()

    assert res.included is False
    assert net.completion_calls == []
    assert "2 mois manquant(s) (2026-07, 2026-09)" in res.reason
    assert res.reason.endswith("— exclu (trace du biais du survivant)")
    assert "complément API" not in res.reason


def test_mois_manquant_au_debut_de_la_fenetre_est_un_trou_ancien(monkeypatch):
    net = _Net(monkeypatch, present=[(2026, 5), (2026, 7), (2026, 8), (2026, 9)])

    res = _run()

    assert res.included is False
    assert net.completion_calls == []


def test_queue_trop_longue_exclue_sans_appel_api(monkeypatch):
    """Au-delà de `CRYPTO_TAIL_COMPLETION_MAX_MONTHS`, ce n'est plus un retard de publication
    (symbole récent/délisté) : exclusion historique, API non sollicitée."""
    net = _Net(monkeypatch, present=[(2026, 5)])  # juin..septembre (4 mois) manquants

    res = _run()

    assert len(REQUIRED_MONTHS) > fd.CRYPTO_TAIL_COMPLETION_MAX_MONTHS
    assert res.included is False
    assert net.completion_calls == []
    assert res.reason.endswith("— exclu (trace du biais du survivant)")


def test_split_required_gaps():
    req = [(2026, 6), (2026, 7), (2026, 8), (2026, 9)]
    assert fd._split_required_gaps(req, []) == ([], [])
    assert fd._split_required_gaps(req, ["2026-09"]) == (["2026-09"], [])
    assert fd._split_required_gaps(req, ["2026-08", "2026-09"]) == (["2026-08", "2026-09"], [])
    assert fd._split_required_gaps(req, ["2026-07"]) == ([], ["2026-07"])
    assert fd._split_required_gaps(req, ["2026-07", "2026-09"]) == (["2026-09"], ["2026-07"])
    assert fd._split_required_gaps(req, ["2026-06", "2026-07", "2026-08"]) == ([], ["2026-06", "2026-07", "2026-08"])


# ----------------------------------------------------------------------------------------
# (d) aucun mois manquant -> chemin historique inchangé
# ----------------------------------------------------------------------------------------


def test_aucun_mois_manquant_complement_mois_courant_inchange(monkeypatch):
    net = _Net(monkeypatch, present=ARCHIVE_MONTHS)

    res = _run()

    assert res.included is True
    assert res.reason == "historique complet depuis la fenêtre requise"
    assert res.tail_months_completed_via_api == []
    # Complément appelé exactement une fois, depuis le début du MOIS COURANT (octobre).
    assert net.completion_calls == [datetime(2026, 10, 1, tzinfo=timezone.utc)]
    assert (res.df["timestamp"] == pd.Timestamp("2026-10-05 10:00", tz="UTC")).any()


# ----------------------------------------------------------------------------------------
# (e) queue de 2 mois contigus couverte -> inclus
# ----------------------------------------------------------------------------------------


def test_queue_deux_mois_contigus_couverte_est_incluse(monkeypatch):
    api = _candles("2026-08-01", "2026-10-05 11:00")
    net = _Net(monkeypatch, present=[(2026, 5), (2026, 6), (2026, 7)], completion_df=api)

    res = _run()

    assert res.included is True
    assert res.tail_months_completed_via_api == ["2026-08", "2026-09"]
    assert "2026-08, 2026-09" in res.reason and "via API" in res.reason
    assert net.completion_calls == [datetime(2026, 8, 1, tzinfo=timezone.utc)]


def test_queue_deux_mois_dont_un_seul_couvert_est_exclue(monkeypatch):
    # Août couvert, septembre absent de l'API : un seul mois non couvert suffit à exclure.
    api = pd.concat([
        _candles("2026-08-01", "2026-09-01"),
        _candles("2026-10-01", "2026-10-05 11:00"),
    ], ignore_index=True)
    _Net(monkeypatch, present=[(2026, 5), (2026, 6), (2026, 7)], completion_df=api)

    res = _run()

    assert res.included is False
    assert "mois 2026-09 non couvert" in res.reason
    assert "2026-08 non" not in res.reason


# ----------------------------------------------------------------------------------------
# Pagination réelle de la fonction généralisée (fetch_with_retries monkeypatché)
# ----------------------------------------------------------------------------------------


def test_fetch_completion_pagine_depuis_un_depart_anterieur_au_mois_courant(monkeypatch):
    """Départ = début du mois de queue (2026-09-01), `now` figé au 2026-10-20 12:00 : ~ 1 140
    bougies closes, donc 2 pages (1000 + le reste) -- vérifie que la pagination existante
    accepte un départ antérieur au mois courant et que le curseur avance d'une page à l'autre."""
    later = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

    class _Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return later

    monkeypatch.setattr(fd, "datetime", _Later)
    starts = []

    class _Resp:
        def __init__(self, rows):
            self._rows = rows

        def json(self):
            return self._rows

    class _Result:
        def __init__(self, rows):
            self.status = "OK"
            self.response = _Resp(rows)

    def fake_fetch(session, url, params=None, max_attempts=1, context=""):
        start = params["startTime"]
        starts.append(start)
        rows = []
        t = start
        hour = 3_600_000
        last_closed_open = int(later.timestamp() * 1000) // hour * hour - hour
        while len(rows) < params["limit"] and t <= last_closed_open + hour:
            rows.append([t, "1", "1", "1", "1", "1", t + hour - 1])
            t += hour
        return _Result(rows)

    monkeypatch.setattr(fd, "fetch_with_retries", fake_fetch)

    df = fd.fetch_binance_current_month_completion(
        object(), "TSTUSDT", datetime(2026, 9, 1, tzinfo=timezone.utc), max_attempts=1)

    assert len(starts) == 2  # 2 pages : 1000 + le reste (~ 1 150 bougies closes au total)
    assert df["timestamp"].min() == pd.Timestamp("2026-09-01", tz="UTC")
    assert df["timestamp"].is_unique
    assert fd._month_covered_by_candles(df, 2026, 9) is True
