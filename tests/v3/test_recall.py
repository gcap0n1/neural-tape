"""Test recall: FTS5 index, v2->v3 migration, sync triggers, Storage.search."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

logging.disable(logging.CRITICAL)

import neuraltape.v3.storage as st_mod
from neuraltape.v3.storage import Episode, Storage


def _tmp_db() -> tuple[Path, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="nt-recall-"))
    return tmp, tmp / "recall.db"


def _ep(project_id="p1", kind="semantic", title="t", body=None,
        source_type="transcript", confidence=0.8, created_at=None):
    ep = Episode(
        project_id=project_id, kind=kind, title=title, body=body,
        source_type=source_type, confidence=confidence,
    )
    if created_at is not None:
        ep.created_at = created_at
        ep.updated_at = created_at
    return ep


# ------------------------------------------------------------------ migration

def test_migration_v2_to_v3_backfills_index_and_baks():
    tmp, db_path = _tmp_db()
    try:
        old = st_mod.SCHEMA_VERSION
        st_mod.SCHEMA_VERSION = 2
        try:
            s2 = Storage(db_path)
            s2.put_episode(_ep(title="Preflight ZEUS",
                               body="Il preflight usa il bind reale su porta 8002."))
            s2.put_episode(_ep(title="Snapshot Contabo",
                               body="Disco VPS con auto-delete trenta giorni.",
                               project_id="p2"))
        finally:
            st_mod.SCHEMA_VERSION = old
        assert st_mod.SCHEMA_VERSION == old

        # Simulate an index seeded *before* the upgrade, then emptied so the
        # v2->v3 rebuild has real work to do.
        con = sqlite3.connect(db_path)
        ver = con.execute("SELECT version FROM schema_version").fetchone()[0]
        con.execute("INSERT INTO episodes_fts(episodes_fts) VALUES('delete-all')")
        con.commit()
        stale = con.execute(
            "SELECT count(*) FROM episodes_fts WHERE title MATCH 'preflight'"
        ).fetchone()[0]
        con.close()
        assert ver == 2
        assert stale == 0

        s3 = Storage(db_path)  # triggers the 2 -> 3 ladder

        bak = Path(str(db_path) + ".bak-v3")
        assert bak.exists(), "expected .bak-v3 snapshot for populated DB"
        hits = s3.search("preflight bind")
        assert len(hits) == 1 and "8002" in hits[0].episode.body
        assert s3.search("contabo auto")[0].episode.project_id == "p2"

        con = sqlite3.connect(db_path)
        assert con.execute("SELECT version FROM schema_version").fetchone()[0] == st_mod.SCHEMA_VERSION
        con.close()

        # Idempotent reopen must not rewrite the backup nor re-seed.
        mtime_before = bak.stat().st_mtime_ns
        Storage(db_path)
        assert bak.stat().st_mtime_ns == mtime_before
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_schema_from_future_raises():
    tmp, db_path = _tmp_db()
    try:
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        con.execute("INSERT INTO schema_version VALUES (99)")
        con.commit()
        con.close()
        raised = False
        try:
            Storage(db_path)
        except RuntimeError:
            raised = True
        assert raised, "opening a DB newer than the code must fail loudly"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------- triggers

def test_triggers_keep_index_in_sync():
    tmp, db_path = _tmp_db()
    try:
        s = Storage(db_path)
        s.put_episode(_ep(title="gap engine", body="round legale massimo due"))

        hits = s.search("gap engine round")
        assert len(hits) == 1
        eid = hits[0].episode.id

        # UPDATE path: body rewritten -> new terms searchable, old gone.
        ep = s.get_episode(eid)
        ep.body = "completamento al limite senza round extra"
        s.put_episode(ep)
        assert s.search("round extra") != []          # same id under the hood
        assert all(h.episode.id == eid for h in s.search("senza round"))
        assert s.search("legale massimo") == []

        # DELETE path (raw maintenance op): episode vanishes from recall.
        con = sqlite3.connect(db_path)
        con.execute("DELETE FROM episodes WHERE id = ?", (eid,))
        con.commit()
        con.close()
        assert s.search("completamento") == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -------------------------------------------------------------------- ranking

def test_bm25_ranks_needle_first():
    tmp, db_path = _tmp_db()
    try:
        s = Storage(db_path)
        needle = _ep(title="quota soglia",
                     body="soglia custom novanta centimetri sul canale")
        s.put_episode(needle)
        for i in range(12):
            s.put_episode(_ep(kind="episodic",
                              title=f"nota {i}",
                              body=f"routine giornaliera generica {i}"))
        top = s.search("novanta soglia canale", limit=5)
        assert top[0].episode.id == needle.id
        assert top[0].rank >= top[-1].rank            # best fused score first
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------- filtri

def test_filters_kind_confidence_project_since_limit():
    tmp, db_path = _tmp_db()
    try:
        s = Storage(db_path)
        t0 = time.time() - 60
        a_sem = _ep(kind="semantic", confidence=0.95, created_at=t0,
                    body="deploy produzione descrizione comune")
        a_work = _ep(kind="working", confidence=0.40, created_at=t0 + 2,
                     body="deploy produzione descrizione comune")
        b_sem = _ep(project_id="pB", kind="semantic", confidence=0.90,
                    created_at=t0 + 1,
                    body="deploy produzione descrizione comune")
        for ep in (a_sem, a_work, b_sem):
            s.put_episode(ep)

        r = s.search("deploy produzione")
        assert {h.episode.id for h in r} == {a_sem.id, a_work.id, b_sem.id}

        only_p1 = s.search("deploy produzione", project_id="p1")
        assert {h.episode.id for h in only_p1} == {a_sem.id, a_work.id}

        confident = s.search("deploy produzione", min_confidence=0.9)
        assert {h.episode.id for h in confident} == {a_sem.id, b_sem.id}

        working = s.search("deploy produzione", kind="working")
        assert [h.episode.id for h in working] == [a_work.id]

        fresh = s.search("deploy produzione", since=t0 + 1.5)
        assert {h.episode.id for h in fresh} == {a_work.id}

        assert len(s.search("deploy produzione", limit=2)) <= 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------- diacritici

def test_italian_diacritics_fold_both_ways():
    tmp, db_path = _tmp_db()
    try:
        s = Storage(db_path)
        base = _ep(body="configurazione ambiente completata senza errori")
        accented = _ep(title="qualità più", body="velocità misurata ridotta")
        s.put_episode(base)
        s.put_episode(accented)

        assert any("configurazione" in (h.episode.body or "")
                   for h in s.search("configurazióne ambiente"))
        hits = s.search("piu velocita qualita")
        assert any(h.episode.title == "qualità più" for h in hits)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -------------------------------------------------------------------- guards

def test_search_guards():
    tmp, db_path = _tmp_db()
    try:
        s = Storage(db_path)
        s.put_episode(_ep(body="contenuto utile ma irrilevante qui"))

        for bad in ("", "   ", "!?.", "---"):
            raised = False
            try:
                s.search(bad)
            except ValueError:
                raised = True
            assert raised, f"empty/punctuation query must raise: {bad!r}"

        raised = False
        try:
            s.search("contenuto", kind="bogus-kind")
        except ValueError:
            raised = True
        assert raised

        # Pasted snippets must not inject FTS5 syntax.
        hostile = 'colonna" ne " (SELECT) : MATCH'
        s.search(hostile) is not None           # may be empty, never 500-ish
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
