"""C1 tests: heuristic extractor, RRF fusion, schema v4 migration golden."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import neuraltape.v3.storage as st_mod
from neuraltape.v3.storage import Episode, Storage, heuristic_entities, rrf_fuse


# ------------------------------------------------------------ heuristic seed

def test_heuristic_catches_camel_acronym_titlecase():
    ents = heuristic_entities("GapEngine bind AWS Deploy Produzione", "")
    lowered = {e.casefold() for e in ents}
    assert {"gapengine", "aws", "deploy produzione"} <= lowered


def test_heuristic_dedupes_and_caps():
    text = ("Nel repo Zigzag Photonics si tocca Zigzag photonics e "
            "ZIGZAG Photonics ancora; ZeroConfig Alpha Beta Gamma Delta Epsilon Zeta")
    ents = heuristic_entities(text, "")
    assert len(ents) <= 5
    keys = [e.casefold() for e in ents]
    assert len(keys) == len(set(keys))


def test_heuristic_ignores_noise_words():
    assert heuristic_entities("The Session Transcript When Why How", "") == []


# ------------------------------------------------------------------- RRF fuse

def test_rrf_two_streams_fuse_and_tiebreak():
    fused = rrf_fuse({"bm25": ["n1", "n2", "n3"], "entities": ["n2"]})
    ids = [eid for eid, _ in fused]
    # n2 gains 1/61 (bm25 #2) + 1/60 (entity #1) -> beats n1's pure 1/60.
    assert ids == ["n2", "n1", "n3"]
    scores = dict(fused)
    assert scores["n2"] > scores["n1"] > scores["n3"]


def test_rrf_single_stream_is_passthrough():
    fused = rrf_fuse({"bm25": ["a", "b"]})
    assert [eid for eid, _ in fused] == ["a", "b"]


# ------------------------------------------------------- v4 migration + recall

def _mk_ep(title="", body=None, project="p1", kind="semantic",
           category="tool", confidence=0.9):
    return Episode(project_id=project, kind=kind, source_type="transcript",
                   title=title, body=body, category=category,
                   confidence=confidence)


def test_migration_v3_to_v4_backfills_entities_with_snapshot():
    tmp = Path(tempfile.mkdtemp(prefix="nt-v4mig-"))
    try:
        db_path = tmp / "m.db"
        old = st_mod.SCHEMA_VERSION
        st_mod.SCHEMA_VERSION = 3
        try:
            s3 = Storage(db_path)
            s3.put_episode(_mk_ep(title="Bootstrap GapEngine bind reale",
                               body="nessun termine distattivo qui dentro"))
        finally:
            st_mod.SCHEMA_VERSION = old
        assert st_mod.SCHEMA_VERSION == 4

        s4 = Storage(db_path)

        con = sqlite3.connect(db_path)
        ver = con.execute("SELECT version FROM schema_version").fetchone()[0]
        cols = {r[1] for r in con.execute("PRAGMA table_info(episodes)")}
        ee_rows = con.execute(
            "SELECT entity_lower FROM episode_entities"
        ).fetchall()
        raw_ent = con.execute("SELECT entities FROM episodes").fetchone()[0]
        con.close()

        assert ver == 4
        assert {"entities", "pinned", "access_count", "last_accessed_at",
                "expires_at"} <= cols
        assert "gapengine" in [r[0] for r in ee_rows]
        assert "GapEngine" in __import__("json").loads(raw_ent or "[]")
        bak4 = Path(str(db_path) + ".bak-v4")
        assert bak4.exists(), "expected .bak-v4 snapshot on populated upgrade"

        get = s4.get_episode(s4.query_episodes("p1")[0].id)
        assert get.entities == ["GapEngine"]

        # Second open must be a no-op: no duplicate backfill, no clobber.
        mtime = bak4.stat().st_mtime_ns
        Storage(db_path)
        assert bak4.stat().st_mtime_ns == mtime
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_golden_named_retrieval_via_entity_stream_only():
    """Body/title hold NO query token at all; only the entity does."""
    tmp = Path(tempfile.mkdtemp(prefix="nt-golden-"))
    try:
        s = Storage(tmp / "g.db")
        needle = s.put_episode(_mk_ep(title="Deploy motori concluso",
                                   body="riscrittura del motore completata"))
        # entities set explicitly post-hoc through put round-trip
        ep = s.get_episode(needle)
        ep.entities = ["GapEngine"]
        s.put_episode(ep)
        for i in range(6):
            filler_body = "gap engine bridge target racconto differenze"
            e = _mk_ep(kind="episodic", category="warning",
                    title=f"report {i}", body=filler_body,
                    confidence=0.8 - i * 0.02)
            e.created_at = e.updated_at = time.time() - i * 10
            s.put_episode(e)

        hits = s.search("gap engine")
        hit_ids = [h.episode.id for h in hits]
        assert needle in hit_ids, "named thing must surface via entity stream"

        # And with ONLY the camel token the needle is essentially alone.
        solo = s.search("GapEngine")
        assert [h.episode.id for h in solo] == [needle]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_put_roundtrip_keeps_entity_index_coherent():
    tmp = Path(tempfile.mkdtemp(prefix="nt-eeco-"))
    try:
        s = Storage(tmp / "c.db")
        eid = s.put_episode(_mk_ep(title="primo", body=None))
        ep = s.get_episode(eid)
        ep.entities = ["AlphaTool", "betaWidget"]
        s.put_episode(ep)
        con = sqlite3.connect(tmp / "c.db")
        pairs = sorted(r[0] for r in con.execute("SELECT entity_lower FROM episode_entities WHERE episode_id=?", (eid,)))
        con.close()
        assert pairs == ["alphatool", "betawidget"]

        ep.entities = ["SoloQuest"]
        s.put_episode(ep)
        con = sqlite3.connect(tmp / "c.db")
        pairs = sorted(r[0] for r in con.execute("SELECT entity_lower FROM episode_entities WHERE episode_id=?", (eid,)))
        con.close()
        assert pairs == ["soloquest"], "old pairs must vanish on overwrite"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
