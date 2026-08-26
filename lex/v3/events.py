"""events — EventBus minimale (D0.4).

In Fase 0 the bus supports exactly 2 source types:
- 'transcript'  : payload {transcript_path, new_bytes, new_lines, session_id}
- 'git.commit'  : payload {sha, author, message_short, branch, files_changed_count}

More source types are *defined* here (for forward compat) but rejected at publish
time until the corresponding adapter is implemented in Fase 2.

The bus ONLY persists raw events in event_log (D0.3). Promoting an event to an
Episode (kind=episodic) is the job of the Fase 1 classifier, not the bus.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .storage import Storage

log = logging.getLogger("neural-tape-v3")

# Source types active in Fase 0.
ACTIVE_SOURCES_PHASE0 = {"transcript", "git.commit"}

# Source types reserved for Fase 2+ — known but not yet publishable.
FUTURE_SOURCES = {
    "git.branch_switch", "git.merge", "git.tag",
    "test.pass", "test.fail",
    "build", "docker.restart", "deploy",
    "todo.completed", "manual",
}

REQUIRED_PAYLOAD_KEYS = {
    "transcript":  {"transcript_path", "new_bytes", "new_lines", "session_id"},
    "git.commit":  {"sha", "author", "message_short", "files_changed_count"},
}


@dataclass
class Event:
    project_id: str
    source_type: str
    payload: dict
    source_ref: str | None = None
    captured_at: float = field(default_factory=time.time)


class EventBus:
    """Minimal event bus. Publishes to Storage.event_log."""

    def __init__(self, storage: "Storage", allowed_sources: set[str] | None = None):
        self.storage = storage
        self.allowed = set(allowed_sources) if allowed_sources is not None else set(ACTIVE_SOURCES_PHASE0)

    def publish(self, event: Event) -> int:
        """Publish an event. Returns the event_log row id.

        Raises:
            ValueError: if source_type is unknown, not allowed, or payload is malformed.
        """
        self._validate(event)
        row_id = self.storage.append_event(
            project_id=event.project_id,
            source_type=event.source_type,
            source_ref=event.source_ref,
            captured_at=event.captured_at,
            payload=event.payload,
        )
        log.debug("event published: project=%s source=%s row=%d",
                  event.project_id, event.source_type, row_id)
        return row_id

    def query(self, project_id: str, *,
              source_type: str | None = None,
              since: float | None = None,
              limit: int = 100) -> list[Event]:
        """Query events newest-first. Returns Event objects (payloads parsed)."""
        rows = self.storage.query_events(
            project_id, source_type=source_type, since=since, limit=limit,
        )
        return [
            Event(
                project_id=r["project_id"],
                source_type=r["source_type"],
                payload=r["payload"],
                source_ref=r["source_ref"],
                captured_at=r["captured_at"],
            )
            for r in rows
        ]

    # ---- internals ------------------------------------------------------

    def _validate(self, event: Event) -> None:
        if not event.project_id or not isinstance(event.project_id, str):
            raise ValueError("event.project_id must be a non-empty string")

        if event.source_type in FUTURE_SOURCES:
            raise ValueError(
                f"source_type '{event.source_type}' is reserved for a later phase; "
                f"active sources in Fase 0: {sorted(self.allowed)}"
            )
        if event.source_type not in ACTIVE_SOURCES_PHASE0:
            raise ValueError(
                f"unknown source_type {event.source_type!r}; "
                f"known: {sorted(ACTIVE_SOURCES_PHASE0 | FUTURE_SOURCES)}"
            )
        if event.source_type not in self.allowed:
            raise ValueError(
                f"source_type {event.source_type!r} is not in allowed_sources {sorted(self.allowed)}"
            )

        required = REQUIRED_PAYLOAD_KEYS.get(event.source_type)
        if required:
            missing = required - set(event.payload or {})
            if missing:
                raise ValueError(
                    f"{event.source_type} payload missing required keys: {sorted(missing)}"
                )

        if not isinstance(event.payload, dict):
            raise ValueError("event.payload must be a dict")
