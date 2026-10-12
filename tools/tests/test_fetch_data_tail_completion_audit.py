"""Tests d'attaque de l'auditeur adversarial indépendant du correctif #18 (2026-10-12),
intégrés au dépôt après correction des 3 findings MINEURS qu'ils documentaient (trou intérieur
dans un mois de queue, colonne timestamp absente, bougies API antérieures à startTime) -- les
3 tests « rouges » de l'audit sont désormais verts sur le code corrigé. Le test différentiel
ancien/nouveau (A1) exige un instantané du code PRÉ-correctif : exécuté et vert pendant
l'audit (0 divergence sur 201 cas), il se SKIPPE proprement quand l'instantané n'est pas
disponible (CI) -- le résultat d'audit fait foi, cf. RESEARCH-LOG.md 2026-10-12."""
from __future__ import annotations
import importlib.util, itertools, logging, sys
from datetime import datetime, timezone
import pandas as pd, pytest
import tools.fetch_data as fd

FROZEN = datetime(2026, 10, 5, 12, 0, 5, tzinfo=timezone.utc)

class _FD(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN if tz is None else FROZEN.astimezone(tz)

@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(fd, "datetime", _FD)

_OLD_SNAPSHOT = "/tmp/claude-0/aud/fetch_data_OLD.py"  # instantané pré-correctif (audit uniquement)


def _load_old():
    import os
    if not os.path.exists(_OLD_SNAPSHOT):
        pytest.skip("instantané pré-correctif indisponible (test différentiel exécuté lors de l'audit du 2026-10-12)")
    spec = importlib.util.spec_from_file_location("fd_old", _OLD_SNAPSHOT)
    m = importlib.util.module_from_spec(spec); sys.modules["fd_old"] = m; spec.loader.exec_module(m); return m

ARCH = [(2026, 5), (2026, 6), (2026, 7), (2026, 8), (2026, 9)]
REQ = [(2026, 6), (2026, 7), (2026, 8), (2026, 9)]
H = pd.Timedelta(hours=1)

def candles(s, e, val=1.0):
    ts = pd.date_range(s, e, freq="1h", tz="UTC", inclusive="left")
    return pd.DataFrame({"timestamp": ts, "open": val, "high": val, "low": val, "close": val, "volume": val})

def install(mod, monkeypatch, present, errors=(), api=None, boom_on_api=False):
    calls = []
    def dl(s, p, y, m, a):
        if (y, m) in errors: return "ERROR", None
        if (y, m) in present:
            ts = pd.date_range(datetime(y, m, 1, tzinfo=timezone.utc), periods=3, freq="1h")
            return "OK", pd.DataFrame({"timestamp": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})
        return "NOT_FOUND", None
    def comp(s, p, ms, a):
        if boom_on_api: raise AssertionError("API appelée alors qu'elle ne devait pas l'être")
        calls.append(ms)
        return (api if api is not None else candles("2026-10-01", "2026-10-05 12:00", 2.0)).copy()
    monkeypatch.setattr(mod, "download_binance_month", dl)
    monkeypatch.setattr(mod, "fetch_binance_current_month_completion", comp)
    monkeypatch.setattr(mod, "datetime", _FD)
    return calls

# --- A1 : différentiel exhaustif ancien/nouveau hors cas "queue" --------------------------
def test_a1_differentiel_exhaustif(monkeypatch):
    old = _load_old(); logging.disable(logging.CRITICAL)
    ym = lambda t: f"{t[0]}-{t[1]:02d}"
    n_checked = 0
    for mask in itertools.product([0, 1, 2], repeat=len(ARCH)):
        present = {ARCH[i] for i, v in enumerate(mask) if v == 0}
        errs = {ARCH[i] for i, v in enumerate(mask) if v == 2}
        req_missing = [ym(r) for r in REQ if r not in present]
        tail, gaps = fd._split_required_gaps(REQ, req_missing)
        if req_missing and not (gaps or len(tail) > fd.CRYPTO_TAIL_COMPLETION_MAX_MONTHS):
            continue
        co = install(old, monkeypatch, present, errs); ro = old.process_crypto_symbol(object(), "T", ARCH, REQ, 1)
        cn = install(fd, monkeypatch, present, errs); rn = fd.process_crypto_symbol(object(), "T", ARCH, REQ, 1)
        assert (ro.included, ro.reason, ro.months_ok, ro.months_missing, ro.months_error) == \
               (rn.included, rn.reason, rn.months_ok, rn.months_missing, rn.months_error), mask
        assert co == cn, mask
        if ro.df is not None: assert ro.df.equals(rn.df)
        n_checked += 1
    logging.disable(logging.NOTSET)
    assert n_checked > 100

def test_a1_trou_milieu_api_jamais_appelee(monkeypatch):
    install(fd, monkeypatch, {(2026, 5), (2026, 6), (2026, 8)}, boom_on_api=True)  # 07 milieu + 09 queue
    r = fd.process_crypto_symbol(object(), "T", ARCH, REQ, 1)
    assert not r.included and r.reason.endswith("— exclu (trace du biais du survivant)")

def test_a3_fenetre_entiere_manquante_reelle_pas_d_api(monkeypatch):
    arch = fd.month_range(fd.CRYPTO_ARCHIVE_START, (2026, 9)); req = fd.month_range(fd.CRYPTO_FULL_COVERAGE_REQUIRED_FROM, (2026, 9))
    install(fd, monkeypatch, set(), boom_on_api=True)
    r = fd.process_crypto_symbol(object(), "NEW", arch, req, 1)
    assert not r.included and r.reason.endswith("— exclu (trace du biais du survivant)")

def test_a3_mois_en_erreur_en_queue_traite_comme_manquant_et_trace(monkeypatch):
    install(fd, monkeypatch, {(2026, 5), (2026, 6), (2026, 7), (2026, 8)}, errors={(2026, 9)},
            api=candles("2026-09-01", "2026-10-05 12:00"))
    r = fd.process_crypto_symbol(object(), "T", ARCH, REQ, 1)
    assert r.included and r.tail_months_completed_via_api == ["2026-09"]
    assert r.months_error == ["2026-09"]  # l'erreur reste tracée séparément

def test_a3_mois_en_erreur_au_milieu_exclu_sans_api(monkeypatch):
    install(fd, monkeypatch, {(2026, 5), (2026, 6), (2026, 8), (2026, 9)}, errors={(2026, 7)}, boom_on_api=True)
    r = fd.process_crypto_symbol(object(), "T", ARCH, REQ, 1)
    assert not r.included

# --- A4 ------------------------------------------------------------------------------------
def test_a4_bougie_en_formation_exclue_chemin_queue(monkeypatch):
    # vrai fetch (fetch_with_retries mocké) : bougie 12:00 close_time 12:59 > now => exclue
    class R:
        def __init__(s, rows): s.rows = rows
        def json(s): return s.rows
    class Res:
        status = "OK"
        def __init__(s, rows): s.response = R(rows)
    hour = 3_600_000
    def fake(session, url, params=None, **k):
        t = params["startTime"]; rows = []
        while len(rows) < 1000 and t <= int(FROZEN.timestamp() * 1000):
            rows.append([t, "1", "1", "1", "1", "1", t + hour - 1]); t += hour
        return Res(rows)
    monkeypatch.setattr(fd, "fetch_with_retries", fake)
    df = fd.fetch_binance_current_month_completion(object(), "TSTUSDT", datetime(2026, 9, 1, tzinfo=timezone.utc), 1)
    assert df["timestamp"].max() == pd.Timestamp("2026-10-05 11:00", tz="UTC")

def test_a4_api_qui_renvoie_des_bougies_anterieures_ne_doit_pas_ecraser_l_archive(monkeypatch):
    # API "fautive" : renvoie aussi juin-août (valeur 2.0) alors que l'archive a 1.0
    bad_api = candles("2026-06-01", "2026-10-05 12:00", 2.0)
    install(fd, monkeypatch, {(2026, 5), (2026, 6), (2026, 7), (2026, 8)}, api=bad_api)
    r = fd.process_crypto_symbol(object(), "T", ARCH, REQ, 1)
    row = r.df[r.df.timestamp == pd.Timestamp("2026-06-01", tz="UTC")].iloc[0]
    assert row.close == 1.0, "bougie d'archive écrasée silencieusement par l'API (keep='last')"

# --- A2 ------------------------------------------------------------------------------------
def test_a2_trou_interieur_dans_un_mois_de_queue_devrait_exclure(monkeypatch):
    api = pd.concat([candles("2026-09-01", "2026-09-10"), candles("2026-09-13", "2026-10-05 12:00")], ignore_index=True)
    install(fd, monkeypatch, {(2026, 5), (2026, 6), (2026, 7), (2026, 8)}, api=api)
    r = fd.process_crypto_symbol(object(), "T", ARCH, REQ, 1)
    assert not r.included, "mois de queue à trou intérieur de 3 jours inclus comme « historique complet »"

def test_a2_colonne_timestamp_absente_ne_leve_pas():
    assert fd._month_covered_by_candles(pd.DataFrame({"x": [1]}), 2026, 9) is False

def test_a2_doublons_et_hors_mois():
    full = candles("2026-09-01", "2026-10-01")
    assert fd._month_covered_by_candles(pd.concat([full, full]), 2026, 9)
    assert not fd._month_covered_by_candles(candles("2026-10-01", "2026-10-20"), 2026, 9)
    assert not fd._month_covered_by_candles(candles("2026-08-01", "2026-09-01"), 2026, 9)
    assert fd._month_covered_by_candles(full.assign(timestamp=full.timestamp.dt.tz_convert("Asia/Tokyo")), 2026, 9)
    assert fd._month_covered_by_candles(candles("2026-12-01", "2027-01-01"), 2026, 12)

# --- A7 ------------------------------------------------------------------------------------
def _fake_pager(monkeypatch, now, fail_on=None):
    calls = []
    hour = 3_600_000
    class R:
        def __init__(s, rows): s.rows = rows
        def json(s): return s.rows
    class Res:
        def __init__(s, st, rows=None): s.status = st; s.response = R(rows)
    def fake(session, url, params=None, **k):
        calls.append(params["startTime"])
        if fail_on == len(calls): return Res("ERROR")
        t = params["startTime"]; rows = []
        while len(rows) < 1000 and t <= int(now.timestamp() * 1000):
            rows.append([t, "1", "1", "1", "1", "1", t + hour - 1]); t += hour
        return Res("OK", rows)
    monkeypatch.setattr(fd, "fetch_with_retries", fake)
    return calls

@pytest.mark.parametrize("fail_on,tail,expect_incl", [(2, [(2026, 8), (2026, 9)], False), (3, [(2026, 7), (2026, 8), (2026, 9)], False)])
def test_a7_pagination_interrompue_en_queue_exclut(monkeypatch, fail_on, tail, expect_incl):
    present = set(ARCH) - set(tail)
    _fake_pager(monkeypatch, FROZEN, fail_on=fail_on)
    monkeypatch.setattr(fd, "download_binance_month", lambda s, p, y, m, a: (("OK", candles(datetime(y, m, 1), "2026-12-31").head(3)) if (y, m) in present else ("NOT_FOUND", None)))
    r = fd.process_crypto_symbol(object(), "T", ARCH, REQ, 1)
    assert r.included is expect_incl and "non couvert" in r.reason

def test_a7_budget_pages_queue_3_mois(monkeypatch):
    calls = _fake_pager(monkeypatch, FROZEN)
    fd.fetch_binance_current_month_completion(object(), "TUSDT", datetime(2026, 7, 1, tzinfo=timezone.utc), 1)
    assert len(calls) == 3
