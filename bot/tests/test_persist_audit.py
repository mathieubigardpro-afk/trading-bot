"""Tests de bot/persist/audit.py : verify_chain() détecte une falsification du state.json
committé, en remontant l'historique git."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from bot.persist.audit import verify_chain
from bot.persist.journal import append_journal
from bot.persist.state import compute_state_hash, init_state, save_state


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def _git_ok(repo: Path, *args: str) -> str:
    result = _git(repo, *args)
    assert result.returncode == 0, f"git {args} a échoué : {result.stderr}"
    return result.stdout


def _commit_state(repo: Path, state: dict, message: str, trades: list[dict] | None = None) -> str:
    """Committe `state/state.json`, et `state/trades.jsonl` (créé vide s'il n'existe pas
    encore, complété des `trades` fournis pour CE cycle — append-only, cohérent avec
    l'invariant de conservation vérifié par `verify_chain`)."""
    state_path = repo / "state" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    save_state(state, str(state_path))

    trades_path = repo / "state" / "trades.jsonl"
    if not trades_path.exists():
        trades_path.write_text("", encoding="utf-8")
    for record in trades or []:
        append_journal(str(trades_path), record)

    _git_ok(repo, "add", "state/state.json", "state/trades.jsonl")
    _git_ok(repo, "commit", "-m", message)
    return _git_ok(repo, "rev-parse", "HEAD").strip()


def _make_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_ok(repo, "init", "-b", "main")
    return repo


def _build_valid_chain(repo: Path) -> list[tuple[dict, str]]:
    """Construit 3 commits successifs avec une chaîne de hash correcte : chaque état porte
    dans state_hash_prev le sha256 exact de l'état committé juste avant lui, ET chaque
    variation de cash_usd/positions est intégralement justifiée par un fill ajouté à
    trades.jsonl au même cycle (invariant de conservation, cf. audit.py). Retourne
    [(state, commit_hash), ...] dans l'ordre chronologique."""
    s0 = init_state()
    s0["state_hash_prev"] = compute_state_hash(init_state())  # genèse arbitraire cohérente
    c0 = _commit_state(repo, s0, "Run T0")
    hash0 = compute_state_hash(s0)

    # T1 : achat de 0.1 BTC pour 1990$ de notionnel + 10$ de frais -> cash -2000$ pile.
    s1 = init_state()
    s1["last_run_id"] = "2026-07-22T13"
    s1["cash_usd"] = 98000.0
    s1["positions"] = {"BTC": {"qty": 0.1, "prix_moyen": 19900.0}}
    s1["state_hash_prev"] = hash0
    fill_t1 = {
        "run_id": "2026-07-22T13", "ts": "2026-07-22T13:00:00+00:00", "symbol": "BTC",
        "strategy": "ensemble", "side": "BUY", "qty": 0.1, "notional_usd": 1990.0,
        "price_fill": 19900.0, "price_mid_ideal": 19895.0, "fees_usd": 10.0,
        "slippage_usd": 0.5, "quote_source": "binance", "quote_ts": "2026-07-22T13:00:00+00:00",
        "cash_after_usd": 98000.0,
    }
    c1 = _commit_state(repo, s1, "Run T1", trades=[fill_t1])
    hash1 = compute_state_hash(s1)

    # T2 : achat de 0.5 ETH pour 990$ de notionnel + 10$ de frais -> cash -1000$ pile ;
    # la position BTC de T1 reste inchangée (aucun fill BTC ce cycle).
    s2 = init_state()
    s2["last_run_id"] = "2026-07-22T14"
    s2["cash_usd"] = 97000.0
    s2["positions"] = {
        "BTC": {"qty": 0.1, "prix_moyen": 19900.0},
        "ETH": {"qty": 0.5, "prix_moyen": 1980.0},
    }
    s2["state_hash_prev"] = hash1
    fill_t2 = {
        "run_id": "2026-07-22T14", "ts": "2026-07-22T14:00:00+00:00", "symbol": "ETH",
        "strategy": "ensemble", "side": "BUY", "qty": 0.5, "notional_usd": 990.0,
        "price_fill": 1980.0, "price_mid_ideal": 1979.0, "fees_usd": 10.0,
        "slippage_usd": 0.5, "quote_source": "binance", "quote_ts": "2026-07-22T14:00:00+00:00",
        "cash_after_usd": 97000.0,
    }
    c2 = _commit_state(repo, s2, "Run T2", trades=[fill_t2])

    return [(s0, c0), (s1, c1), (s2, c2)]


def _hash_object(repo: Path, content: bytes) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=content, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.decode().strip()


def _tree_of(repo: Path, commit: str) -> str:
    return _git_ok(repo, "rev-parse", f"{commit}^{{tree}}").strip()


def _tree_with_replaced_blob(repo: Path, base_tree: str, path: str, blob_hash: str) -> str:
    """Construit un nouvel arbre = `base_tree` mais avec le blob de `path` remplacé, via un
    index git temporaire (n'affecte jamais l'index/le working tree réels du dépôt)."""
    index_file = repo.parent / f".tmp-index-{base_tree[:8]}-{blob_hash[:8]}"
    env = {**subprocess.os.environ, "GIT_INDEX_FILE": str(index_file)}
    subprocess.run(["git", "-C", str(repo), "read-tree", base_tree], env=env, check=True)
    subprocess.run(
        ["git", "-C", str(repo), "update-index", "--add", "--cacheinfo", "100644", blob_hash, path],
        env=env, check=True,
    )
    result = subprocess.run(
        ["git", "-C", str(repo), "write-tree"], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    index_file.unlink(missing_ok=True)
    return result.stdout.strip()


def _commit_tree(repo: Path, tree: str, parent: str, message: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", tree, "-p", parent, "-m", message],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_verify_chain_ok_on_untampered_history(tmp_path):
    repo = _make_repo(tmp_path)
    _build_valid_chain(repo)

    result = verify_chain(str(repo), path="state/state.json")

    assert result.ok is True
    assert result.n_versions_checked == 3
    assert result.errors == []


def test_verify_chain_detects_tampered_intermediate_commit(tmp_path):
    """Falsifie directement le contenu du commit du MILIEU (sans recalculer la chaîne en
    aval, comme le ferait un éditeur manuel malveillant du fichier/historique) et vérifie que
    verify_chain() détecte l'incohérence sur le commit suivant.

    La falsification est faite via la plomberie git bas niveau (hash-object / read-tree /
    write-tree / commit-tree) plutôt qu'un rebase par ré-application de patchs : on veut
    reproduire fidèlement "le contenu committé d'une version passée a été changé sans que la
    chaîne de hash en aval soit recalculée", pas un scénario de merge avec conflit textuel.
    """
    repo = _make_repo(tmp_path)
    chain = _build_valid_chain(repo)
    (_s0, c0), (_s1, c1), (_s2, c2) = chain

    # Contenu falsifié pour la version du milieu (Run T1) : équity gonflée artificiellement,
    # même state_hash_prev que l'original pour que SEULE cette version soit modifiée (la
    # falsification doit être détectée par la version SUIVANTE, pas par elle-même).
    tampered = init_state()
    tampered["last_run_id"] = "2026-07-22T13"
    tampered["cash_usd"] = 999999.0
    tampered["state_hash_prev"] = compute_state_hash(init_state())
    tampered_bytes = (json.dumps(tampered, sort_keys=True, indent=2) + "\n").encode("utf-8")

    blob_hash = _hash_object(repo, tampered_bytes)
    original_tree_c1 = _tree_of(repo, c1)
    tampered_tree = _tree_with_replaced_blob(repo, original_tree_c1, "state/state.json", blob_hash)
    tampered_c1 = _commit_tree(repo, tampered_tree, c0, "Run T1")

    # Le commit suivant (Run T2) garde EXACTEMENT le même arbre (donc le même
    # state_hash_prev="hash de l'ORIGINAL T1"), mais est maintenant rattaché au parent
    # falsifié — c'est exactement la signature d'une falsification a posteriori.
    original_tree_c2 = _tree_of(repo, c2)
    tampered_c2 = _commit_tree(repo, original_tree_c2, tampered_c1, "Run T2")

    _git_ok(repo, "update-ref", "refs/heads/main", tampered_c2)
    _git_ok(repo, "checkout", "-f", "main")

    result = verify_chain(str(repo), path="state/state.json")

    assert result.ok is False
    assert result.n_versions_checked == 3
    assert any("state_hash_prev" in e for e in result.errors)
    assert any(tampered_c2[:8] in e or tampered_c2 in e for e in result.errors)


def test_verify_chain_respects_max_commits(tmp_path):
    repo = _make_repo(tmp_path)
    _build_valid_chain(repo)

    result = verify_chain(str(repo), path="state/state.json", max_commits=2)
    assert result.n_versions_checked == 2
    assert result.ok is True


def test_verify_chain_reports_error_on_invalid_json_commit(tmp_path):
    repo = _make_repo(tmp_path)
    _build_valid_chain(repo)

    state_path = repo / "state" / "state.json"
    state_path.write_text("{ ceci n'est pas du json valide", encoding="utf-8")
    _git_ok(repo, "add", "state/state.json")
    _git_ok(repo, "commit", "-m", "Run T3 corrompu")

    result = verify_chain(str(repo), path="state/state.json")
    assert result.ok is False
    assert any("invalide" in e or "Run T3" in e or "state/state.json" in e for e in result.errors)


def test_verify_chain_no_history_returns_ok_zero_versions(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "README.md").write_text("rien à voir\n", encoding="utf-8")
    _git_ok(repo, "add", "README.md")
    _git_ok(repo, "commit", "-m", "Init sans state.json")

    result = verify_chain(str(repo), path="state/state.json")
    assert result.n_versions_checked == 0
    assert result.ok is True


# --- Non-régression : finding CRITIQUE de l'audit adversarial ------------------------------
#
# "verify_chain() ne détecte PAS une falsification directe de state.json [...] j'ai pris le
# state.json réel du dépôt [...], gonflé cash_usd [...] sans aucun trade justificatif,
# correctement recopié le state_hash_prev vers le hash du VRAI état précédent [...] : ok=True,
# 0 erreur." Reproduit ici à l'identique (hash recalculé correctement, AUCUNE ligne ajoutée à
# trades.jsonl) : l'invariant de conservation doit désormais lever une erreur explicite, alors
# même que la chaîne de hash seule reste, elle, intacte (c'est précisément ce que l'audit
# reprochait : un attaquant public peut toujours recopier un sha256 correct).


def test_verify_chain_detects_cash_inflation_without_matching_trades(tmp_path):
    repo = _make_repo(tmp_path)

    s0 = init_state()
    _commit_state(repo, s0, "Etat initial")
    hash0 = compute_state_hash(s0)

    forged = init_state()
    forged["last_run_id"] = "2026-07-22T14"
    forged["cash_usd"] = 999000.0  # gonflé de +899 000$ sans le moindre trade
    forged["state_hash_prev"] = hash0  # recalculé correctement : la chaîne de hash est intacte
    _commit_state(repo, forged, "Cycle falsifié")  # aucun fill ajouté à trades.jsonl

    result = verify_chain(str(repo), path="state/state.json")

    assert result.ok is False
    assert any(
        "cash_usd" in e and "incohérente" in e and "899000" in e.replace(",", "").replace("+", "")
        for e in result.errors
    )
    # La chaîne de hash, elle, ne détecte STRICTEMENT rien d'anormal (c'est le cœur du finding
    # critique de l'audit) : la détection vient bien de l'invariant de conservation, pas du hash.
    assert not any("state_hash_prev" in e for e in result.errors)


def test_verify_chain_detects_position_inflation_without_matching_trades(tmp_path):
    """Variante de l'attaque ciblant une position plutôt que le cash : une quantité BTC
    apparaît dans state.json sans qu'aucun fill BUY correspondant n'existe dans trades.jsonl."""
    repo = _make_repo(tmp_path)

    s0 = init_state()
    _commit_state(repo, s0, "Etat initial")
    hash0 = compute_state_hash(s0)

    forged = init_state()
    forged["last_run_id"] = "2026-07-22T14"
    forged["positions"] = {"BTC": {"qty": 5.0, "prix_moyen": 20000.0}}  # BTC apparu du néant
    forged["state_hash_prev"] = hash0
    _commit_state(repo, forged, "Position fantôme")

    result = verify_chain(str(repo), path="state/state.json")

    assert result.ok is False
    assert any("position BTC" in e and "incohérente" in e for e in result.errors)


def test_verify_chain_detects_trades_jsonl_rewrite(tmp_path):
    """Si trades.jsonl lui-même est réécrit (une ligne existante supprimée/modifiée) entre
    deux commits — violation de son invariant append-only — verify_chain() doit le signaler
    explicitement, indépendamment de toute cohérence de cash/positions."""
    repo = _make_repo(tmp_path)
    chain = _build_valid_chain(repo)
    (_s0, c0), (_s1, _c1), (s2, _c2) = chain

    # Cycle T3 : ni nouveau trade, ni changement de cash/positions (chaîne de hash et
    # invariant cash/positions restent valides), mais trades.jsonl est tronqué (perd les 2
    # fills déjà committés à T1/T2) — la seule anomalie possible est la violation append-only.
    s3 = json.loads(json.dumps(s2))  # copie profonde
    s3["last_run_id"] = "2026-07-22T15"
    s3["state_hash_prev"] = compute_state_hash(s2)

    trades_path = repo / "state" / "trades.jsonl"
    trades_path.write_text("", encoding="utf-8")
    state_path = repo / "state" / "state.json"
    save_state(s3, str(state_path))
    _git_ok(repo, "add", "state/state.json", "state/trades.jsonl")
    _git_ok(repo, "commit", "-m", "Run T3 (trades.jsonl tronqué)")

    result = verify_chain(str(repo), path="state/state.json")
    assert result.ok is False
    assert any("append-only" in e for e in result.errors)
