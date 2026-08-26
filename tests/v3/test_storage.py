"""Test per lex/v3/storage.py (D0.3)."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lex" / "v3"))

from storage import Episode, Storage, episode_dedup_key  # type: ignore[import-not-found]


def _fresh_db() -> Storage:
    d = Path(tempfile.mkdtemp(prefix="nt-v3-stor-"))
    return Storage(d / "test.db")


def test_storage_releases_db_file():
    """Each operation must close SQLite so Windows can unlink the DB (WinError 32)."""
    d = Path(tempfile.mkdtemp(prefix="nt-v3-stor-"))
    db = d / "test.db"
    s = Storage(db)
    s.put_episode(Episode(project_id="p", kind="working", source_type="t", title="x"))
    db.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def test_roundtrip_episode():
    s = _fresh_db()
    ep = Episode(
        project_id="zeus", kind="episodic", source_type="transcript",
        title="Test insight", body="body text", category="decision",
        confidence=0.8, raw_payload={"foo": "bar"},
    )
    eid = s.put_episode(ep)
    got = s.get_episode(eid)
    assert got is not None
    assert got.id == eid
    assert got.project_id == "zeus"
    assert got.kind == "episodic"
    assert got.title == "Test insight"
    assert got.body == "body text"
    assert got.confidence == 0.8
    assert got.raw_payload == {"foo": "bar"}


def test_invalid_kind_rejected():
    s = _fresh_db()
    ep = Episode(project_id="zeus", kind="bogus", source_type="manual", title="x")
    try:
        s.put_episode(ep)
    except ValueError:
        return
    raise AssertionError("expected ValueError for invalid kind")


def test_project_isolation():
    """Episodes from project A must not appear in project B queries."""
    s = _fresh_db()
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="transcript", title="Z1"))
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="transcript", title="Z2"))
    s.put_episode(Episode(project_id="cais-lp", kind="working", source_type="transcript", title="C1"))

    z = s.query_episodes("zeus")
    c = s.query_episodes("cais-lp")
    assert len(z) == 2
    assert len(c) == 1
    assert all(e.project_id == "zeus" for e in z)
    assert all(e.project_id == "cais-lp" for e in c)


def test_query_by_kind_filter():
    s = _fresh_db()
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="t", title="w"))
    s.put_episode(Episode(project_id="zeus", kind="episodic", source_type="t", title="e"))
    s.put_episode(Episode(project_id="zeus", kind="semantic", source_type="t", title="s"))

    working = s.query_episodes("zeus", kind="working")
    episodic = s.query_episodes("zeus", kind="episodic")
    assert len(working) == 1
    assert len(episodic) == 1
    assert working[0].title == "w"


def test_promote_episode():
    s = _fresh_db()
    eid = s.put_episode(Episode(project_id="zeus", kind="working", source_type="t", title="x"))
    ok = s.promote_episode(eid, "episodic")
    assert ok is True
    got = s.get_episode(eid)
    assert got is not None
    assert got.kind == "episodic"
    assert got.updated_at >= got.created_at


def test_promote_unknown_returns_false():
    s = _fresh_db()
    ok = s.promote_episode("nonexistent-id", "episodic")
    assert ok is False


def test_query_since_filter():
    s = _fresh_db()
    t0 = time.time()
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="t", title="old",
                          created_at=t0 - 1000))
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="t", title="new",
                          created_at=t0))
    recent = s.query_episodes("zeus", since=t0 - 10)
    assert len(recent) == 1
    assert recent[0].title == "new"


def test_stats_by_kind():
    s = _fresh_db()
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="t", title="w1"))
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="t", title="w2"))
    s.put_episode(Episode(project_id="zeus", kind="episodic", source_type="t", title="e1"))
    stats = s.stats("zeus")
    assert stats.get("working") == 2
    assert stats.get("episodic") == 1
    assert stats.get("semantic", 0) == 0


def test_schema_version_recorded():
    s = _fresh_db()
    import sqlite3
    with sqlite3.connect(s.db_path) as c:
        row = c.execute("SELECT version FROM schema_version").fetchone()
    assert row is not None
    assert row[0] == 2


def test_v1_database_migrates_to_v2():
    """A schema-v1 DB gains episodes.dedup_key (+ index) in place, keeping rows."""
    import sqlite3
    s = _fresh_db()
    # Downgrade to a realistic v1 state: drop column-level knowledge.
    with sqlite3.connect(s.db_path) as c:
        c.execute("UPDATE schema_version SET version = 1")
        c.execute("DROP INDEX idx_ep_dedup")
        # Recreate the episodes table without dedup_key, preserving one row.
        c.execute("ALTER TABLE episodes RENAME TO episodes_old")
        c.execute(
            """CREATE TABLE episodes (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
                source_type TEXT NOT NULL, source_ref TEXT, category TEXT,
                title TEXT NOT NULL, body TEXT, confidence REAL DEFAULT 0.0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, raw_payload TEXT)"""
        )
        c.execute(
            "INSERT INTO episodes SELECT id, project_id, kind, source_type, source_ref, "
            "category, title, body, confidence, created_at, updated_at, raw_payload "
            "FROM episodes_old"
        )
        c.execute("DROP TABLE episodes_old")
        # Seed a legacy row using the v1 column set.
        c.execute(
            "INSERT INTO episodes (id, project_id, kind, source_type, title, created_at, updated_at) "
            "VALUES ('legacy-1', 'p', 'episodic', 't', 'legacy row', 1.0, 1.0)"
        )
    # Re-open: bootstrap must run the v1->v2 migration without losing rows.
    s2 = Storage(s.db_path)
    with sqlite3.connect(s2.db_path) as c:
        version = c.execute("SELECT version FROM schema_version").fetchone()[0]
        cols = {r[1] for r in c.execute("PRAGMA table_info(episodes)")}
        count = c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    assert version == 2
    assert "dedup_key" in cols
    assert count == 1
    legacy = s2.get_episode("legacy-1")
    assert legacy is not None and legacy.title == "legacy row"


def test_has_episode_by_dedup():
    s = _fresh_db()
    assert s.has_episode_by_dedup("p", "key-1") is False
    s.put_episode(Episode(
        project_id="p", kind="episodic", source_type="t",
        title="t", dedup_key="key-1",
    ))
    assert s.has_episode_by_dedup("p", "key-1") is True
    # Same key in another project does not collide.
    assert s.has_episode_by_dedup("other", "key-1") is False


def test_query_events_filters_by_source_ref():
    s = _fresh_db()
    for i, ref in enumerate(["sess-a", "sess-b", "sess-a"]):
        s.append_event(
            project_id="p", source_type="transcript.classified",
            source_ref=ref, captured_at=1000.0 + i, payload={"i": i},
        )
    hits = s.query_events("p", source_type="transcript.classified", source_ref="sess-a")
    assert [h["payload"]["i"] for h in hits] == [2, 0]  # newest first


def test_legacy_db_without_schema_version_migrates():
    """A pre-versioned v1 DB (no schema_version rows, no dedup_key) must gain
    both on first open — not be stamped as v2 without the column."""
    import sqlite3
    d = Path(tempfile.mkdtemp(prefix="nt-v3-stor-"))
    db = d / "legacy.db"
    with sqlite3.connect(db) as c:
        c.execute(
            """CREATE TABLE episodes (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
                source_type TEXT NOT NULL, source_ref TEXT, category TEXT,
                title TEXT NOT NULL, body TEXT, confidence REAL DEFAULT 0.0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, raw_payload TEXT)"""
        )
        c.execute(
            "INSERT INTO episodes (id, project_id, kind, source_type, title, body, "
            "created_at, updated_at) "
            "VALUES ('legacy-1', 'p', 'episodic', 't', 'Legacy Title', 'legacy body', 1.0, 1.0)"
        )
    s = Storage(db)
    with sqlite3.connect(s.db_path) as c:
        version = c.execute("SELECT version FROM schema_version").fetchone()[0]
        cols = {r[1] for r in c.execute("PRAGMA table_info(episodes)")}
        key = c.execute("SELECT dedup_key FROM episodes WHERE id = 'legacy-1'").fetchone()[0]
    assert version == 2
    assert "dedup_key" in cols
    assert key == episode_dedup_key("Legacy Title", "legacy body")
    assert s.has_episode_by_dedup("p", key)


def test_v2_backfills_null_dedup_keys():
    """Rows inserted before schema v2 (NULL keys) get fingerprinted on reopen."""
    import sqlite3
    s = _fresh_db()
    with sqlite3.connect(s.db_path) as c:
        c.execute(
            "INSERT INTO episodes (id, project_id, kind, source_type, title, body, "
            "created_at, updated_at, dedup_key) "
            "VALUES ('old-1', 'p', 'episodic', 't', 'Old Insight', 'old body', 1.0, 1.0, NULL)"
        )
    s2 = Storage(s.db_path)
    got = s2.get_episode("old-1")
    assert got is not None
    assert got.dedup_key == episode_dedup_key("Old Insight", "old body")
    assert s2.has_episode_by_dedup("p", got.dedup_key)
