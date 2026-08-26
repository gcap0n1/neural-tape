"""Test per lex/v3/events.py (D0.4)."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lex" / "v3"))

from events import ACTIVE_SOURCES_PHASE0, Event, EventBus  # type: ignore[import-not-found]
from storage import Storage  # type: ignore[import-not-found]


def _bus() -> tuple[EventBus, Storage]:
    d = Path(tempfile.mkdtemp(prefix="nt-v3-evt-"))
    s = Storage(d / "test.db")
    return EventBus(s), s


def test_publish_transcript():
    bus, _ = _bus()
    rid = bus.publish(Event(
        project_id="zeus", source_type="transcript",
        source_ref="abc123.jsonl",
        payload={"transcript_path": "/tmp/x.jsonl", "new_bytes": 100,
                 "new_lines": 5, "session_id": "abc123"},
    ))
    assert rid > 0


def test_publish_git_commit():
    bus, _ = _bus()
    rid = bus.publish(Event(
        project_id="zeus", source_type="git.commit",
        source_ref="deadbeef",
        payload={"sha": "deadbeef", "author": "lex", "message_short": "fix",
                 "files_changed_count": 3, "branch": "main"},
    ))
    assert rid > 0


def test_unknown_source_type_rejected():
    bus, _ = _bus()
    try:
        bus.publish(Event(project_id="zeus", source_type="totally-made-up", payload={}))
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown source_type")


def test_future_source_type_rejected_in_phase0():
    """Fase-2 source types are known but must be rejected now."""
    bus, _ = _bus()
    try:
        bus.publish(Event(project_id="zeus", source_type="test.fail", payload={}))
    except ValueError as e:
        assert "later phase" in str(e) or "not in allowed" in str(e)
        return
    raise AssertionError("expected ValueError for future source_type")


def test_missing_payload_keys_rejected():
    bus, _ = _bus()
    try:
        bus.publish(Event(project_id="zeus", source_type="transcript",
                          payload={"transcript_path": "/x"}))  # missing new_bytes etc.
    except ValueError as e:
        assert "missing" in str(e).lower()
        return
    raise AssertionError("expected ValueError for missing payload keys")


def test_query_orders_by_time_desc():
    """Newest first."""
    bus, _ = _bus()
    t0 = time.time()
    bus.publish(Event(project_id="zeus", source_type="transcript", captured_at=t0,
                      source_ref="s1",
                      payload={"transcript_path": "/1", "new_bytes": 1, "new_lines": 1, "session_id": "1"}))
    bus.publish(Event(project_id="zeus", source_type="git.commit", captured_at=t0 + 10,
                      source_ref="sha2",
                      payload={"sha": "sha2", "author": "a", "message_short": "m",
                               "files_changed_count": 1, "branch": "main"}))
    events = bus.query("zeus")
    assert len(events) == 2
    # newest first → git.commit (t0+10) comes before transcript (t0)
    assert events[0].captured_at >= events[1].captured_at
    assert events[0].source_type == "git.commit"


def test_query_filter_by_source_type():
    bus, _ = _bus()
    bus.publish(Event(project_id="zeus", source_type="transcript", source_ref="s1",
                      payload={"transcript_path": "/1", "new_bytes": 1, "new_lines": 1, "session_id": "1"}))
    bus.publish(Event(project_id="zeus", source_type="git.commit", source_ref="s2",
                      payload={"sha": "s2", "author": "a", "message_short": "m",
                               "files_changed_count": 1, "branch": "main"}))
    only_commits = bus.query("zeus", source_type="git.commit")
    assert len(only_commits) == 1
    assert only_commits[0].source_type == "git.commit"


def test_project_isolation_in_events():
    bus, _ = _bus()
    bus.publish(Event(project_id="zeus", source_type="transcript", source_ref="s1",
                      payload={"transcript_path": "/1", "new_bytes": 1, "new_lines": 1, "session_id": "1"}))
    bus.publish(Event(project_id="cais-lp", source_type="transcript", source_ref="s2",
                      payload={"transcript_path": "/2", "new_bytes": 2, "new_lines": 2, "session_id": "2"}))
    z = bus.query("zeus")
    assert len(z) == 1
    assert z[0].project_id == "zeus"


def test_allowed_sources_restriction():
    """If allowed_sources is restricted, publishing a normally-active source fails."""
    d = Path(tempfile.mkdtemp(prefix="nt-v3-evt2-"))
    s = Storage(d / "test.db")
    # Restrict to git.commit only.
    bus = EventBus(s, allowed_sources={"git.commit"})
    try:
        bus.publish(Event(project_id="zeus", source_type="transcript", source_ref="s1",
                          payload={"transcript_path": "/1", "new_bytes": 1, "new_lines": 1, "session_id": "1"}))
    except ValueError as e:
        assert "allowed" in str(e)
        return
    raise AssertionError("expected ValueError when source not in allowed_sources")


def test_phase0_sources_are_documented():
    assert ACTIVE_SOURCES_PHASE0 == {"transcript", "git.commit"}
