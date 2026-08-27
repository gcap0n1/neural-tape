"""C2 tests: lifecycle columns — pin, feedback, TTL sweep, reinforcement."""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("nt_preload_lc", ROOT / "lex" / "pre_load.py")
_pl = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("nt_preload_lc", _pl)
_spec.loader.exec_module(_pl)

from neuraltape.v3.storage import Episode, Storage  # noqa: E402


def _ep(title="t", body=None, project="p1", confidence=0.9,
        kind="semantic", category="decision", created_at=None):
    ep = Episode(project_id=project, kind=kind, source_type="transcript",
                 title=title, body=body, category=category,
                 confidence=confidence)
    if created_at is not None:
        ep.created_at = ep.updated_at = created_at
    return ep


def _mk_storage():
    tmp = Path(tempfile.mkdtemp(prefix="nt-life-"))
    return Storage(tmp / "life.db"), tmp


def test_pin_toggle_persists_and_is_idempotent():
    s, tmp = _mk_storage()
    try:
        eid = s.put_episode(_ep())
        assert s.get_episode(eid).pinned is False
        assert s.pin_episode(eid) is True
        assert s.get_episode(eid).pinned is True
        assert s.pin_episode(eid, False) is True   # toggle back: update ok
        assert s.get_episode(eid).pinned is False
        assert s.pin_episode("missing-id") is False  # unknown id -> no row hit
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_feedback_reinforcement_and_kill_switch():
    s, tmp = _mk_storage()
    try:
        eid = s.put_episode(_ep())
        first = s.feedback(eid, "helpful")
        assert first["access_count"] == 1 and first["last_accessed_at"] > 0
        s.feedback(eid, "helpful")                       # second access
        s.feedback(eid, "not_helpful")                   # net zero
        assert s.get_episode(eid).access_count == 1

        assert s.pin_episode(eid) is True
        res = s.feedback(eid, "wrong")
        assert res["pinned"] == 0                        # kill switch unpins
        assert res["expires_at"] is not None             # TTL set ~ now
        assert time.time() - res["expires_at"] < 30

        raised = False
        try:
            s.feedback(eid, "bogus")
        except ValueError:
            raised = True
        assert raised

        raised_key = False
        try:
            s.feedback("unknown", "helpful")
        except KeyError:
            raised_key = True
        assert raised_key
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_search_excludes_expired_but_authority_can_resurrect():
    s, tmp = _mk_storage()
    try:
        live_id = s.put_episode(_ep(title="vivo con term"))
        dead_id = s.put_episode(_ep(title="morto con term"))
        s.pin_episode(dead_id)
        hits_before = {h.episode.id for h in s.search("term")}
        assert {live_id, dead_id} <= hits_before

        s.feedback(dead_id, "wrong")                     # expires + unpin
        hits_after = {h.episode.id for h in s.search("term")}
        assert live_id in hits_after and dead_id not in hits_after

        s.pin_episode(dead_id)                           # authority resurrection
        hits_res = {h.episode.id for h in s.search("term")}
        assert dead_id in hits_res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_touch_access_batch_and_counts():
    s, tmp = _mk_storage()
    try:
        i1 = s.put_episode(_ep(title="uno", body=None))
        i2 = s.put_episode(_ep(title="due", body=None))
        n = s.touch_access([i1, i1, i2])                 # dedup inside batch
        assert n == 2
        assert s.get_episode(i1).access_count == 1
        assert s.get_episode(i2).access_count == 1
        assert s.touch_access([]) == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_preload_pins_sort_first_and_touches():
    tmp = Path(tempfile.mkdtemp(prefix="nt-pre-pin-"))
    try:
        (tmp / "config.yaml").write_text(
            "paths:\n"
            f"  neural_tape_root: {tmp}\n"
            "pre_load:\n"
            "  max_insights: 5\n"
            "  max_patterns: 2\n"
            "  lookback_days: 7\n",
            encoding="utf-8",
        )
        db_path = tmp / "tape" / "v3" / "neuraltape.db"
        s = Storage(db_path)
        t0 = time.time() - 60
        plain = _ep(title="normale successivo", created_at=t0 + 4)
        s.put_episode(plain)
        old_pinned = _ep(title="pinato antico priorita", created_at=t0)
        old_pinned.pinned = True
        s.put_episode(old_pinned)

        config = _pl.Config(tmp / "config.yaml")
        out = _pl.PreLoad(config).generate(project="p1")
        text = out.read_text(encoding="utf-8")

        content_rows = [l for l in text.splitlines() if "| tape/archive/" in l]
        assert len(content_rows) == 2
        assert "pinato antico priorita" in content_rows[0], \
            "pinned must rank ahead of newer unpinned insight"
        assert any("normale successivo" in r for r in content_rows)

        # touch ran inside generate (non-fatal): both surfaced rows grew.
        fresh = Storage(db_path)
        assert fresh.get_episode(old_pinned.id).access_count >= 1
        assert fresh.get_episode(plain.id).access_count >= 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
