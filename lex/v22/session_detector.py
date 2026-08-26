"""SessionDetector — decide WHEN to classify via idle detection.

Tracks per-transcript byte offsets and modification times in a JSON state file.
A session is classified only when:
  1. New content exists beyond the last known offset, AND
  2. The transcript has not been modified for `idle_threshold_min` minutes.

This avoids burning LLM tokens on partial/active sessions: the classifier runs
once when a session goes cold, on the full delta.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("neural-tape-v22")


class SessionDetector:
    """Track transcript offsets and detect cold sessions ready to classify."""

    def __init__(self, state_file: Path, idle_threshold_min: int = 10):
        self.state_file = state_file
        self.idle_threshold_min = idle_threshold_min

    # ─── State persistence ──────────────────────────────────────────────

    def load_state(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except (json.JSONDecodeError, OSError):
                log.warning("State file corrupt, resetting: %s", self.state_file)
        return {}

    def save_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=2))

    # ─── Core detection ─────────────────────────────────────────────────

    def evaluate(self, transcript: Path) -> dict:
        """
        Inspect a transcript and return a verdict dict:
          {
            classify: bool,       # True → run classifier now
            new_bytes: int,       # bytes beyond last offset
            new_lines: int,       # lines beyond last offset (0 if no offset)
            offset: int,          # last known offset (where to start reading)
            idle_min: float,      # minutes since last modification
            reason: str,          # human-readable reason
          }
        """
        state = self.load_state()
        key = str(transcript)
        entry = state.get(key, {})
        last_offset = entry.get("offset", 0)

        try:
            stat = transcript.stat()
        except OSError as e:
            return self._verdict(False, reason=f"stat failed: {e}")

        current_size = stat.st_size
        current_mtime = stat.st_mtime
        idle_min = (time.time() - current_mtime) / 60

        # Log rotation / truncation → reset offset to read from start
        if current_size < last_offset:
            log.info("Transcript shrunk (rotation?), resetting offset to 0")
            last_offset = 0

        new_bytes = current_size - last_offset

        # No new content since last check
        if new_bytes == 0:
            if last_offset == 0:
                return self._verdict(False, idle_min=idle_min, reason="no content yet")
            if idle_min >= self.idle_threshold_min:
                return self._verdict(
                    False, idle_min=idle_min, reason="already classified / idle, nothing new"
                )
            return self._verdict(False, idle_min=idle_min, reason="session active, no new lines")

        # New content exists
        new_lines = self._count_lines_after(transcript, last_offset)

        # Classify only if session has gone cold
        if idle_min >= self.idle_threshold_min:
            return self._verdict(
                True,
                new_bytes=new_bytes,
                new_lines=new_lines,
                offset=last_offset,
                idle_min=idle_min,
                reason=f"session cold ({idle_min:.0f}min idle, +{new_lines} lines)",
            )

        return self._verdict(
            False,
            new_bytes=new_bytes,
            new_lines=new_lines,
            offset=last_offset,
            idle_min=idle_min,
            reason=f"session active (+{new_lines} lines, {idle_min:.0f}min idle), waiting",
        )

    def mark_classified(self, transcript: Path) -> None:
        """Advance offset to current file size after a successful classification."""
        state = self.load_state()
        key = str(transcript)
        try:
            size = transcript.stat().st_size
            mtime = transcript.stat().st_mtime
        except OSError as e:
            log.error("Cannot stat transcript to mark classified: %s", e)
            return
        state[key] = {
            "offset": size,
            "mtime": mtime,
            "classified_at": time.time(),
            "session_id": transcript.stem,
        }
        self.save_state(state)

    # ─── Helpers ────────────────────────────────────────────────────────

    def _count_lines_after(self, transcript: Path, offset: int) -> int:
        """Count lines after byte offset (cheap, no JSON parse)."""
        count = 0
        try:
            with open(transcript, "rb") as f:
                f.seek(offset)
                for _ in f:
                    count += 1
        except OSError:
            pass
        return count

    def _verdict(
        self,
        classify: bool,
        new_bytes: int = 0,
        new_lines: int = 0,
        offset: int = 0,
        idle_min: float = 0.0,
        reason: str = "",
    ) -> dict:
        return {
            "classify": classify,
            "new_bytes": new_bytes,
            "new_lines": new_lines,
            "offset": offset,
            "idle_min": idle_min,
            "reason": reason,
        }
