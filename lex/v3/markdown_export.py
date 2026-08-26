"""markdown_export — Write v3 episodes as standardized markdown archive files.

Bridge module: lets the v3 SQLite pipeline write to the same ``tape/archive/*.md``
layout consumed by ``lex/pre_load.py``. When v3 replaces v2.2 as the active
pipeline, calling ``export_episode_to_markdown`` after each classification keeps
the existing pre-load / session-context.md flow working unchanged.

Schema (standardized 2026-07-18, identical to v2.2 ``memory_writer.py``):

    ---
    type: <category>
    date: YYYY-MM-DD
    timestamp: ISO-8601 with TZ
    project: <normalized>
    workspace: <raw or empty>
    session: <UUID or empty>
    confidence: high | medium | low
    assistant: <name>
    status: auto-classified
    source: neural-tape-v3
    kind: working | episodic | semantic     # v3-only metadata
    ---

Idempotent: re-exporting the same episode overwrites the same file path
(deterministic filename derived from date + short id + slugified title).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .storage import Episode

log = logging.getLogger("neural-tape-v3")


# Category → archive subdir (mirrors v2.2 memory_writer.TAPE_SUBDIR).
TAPE_SUBDIR = {
    "pattern": "pattern",
    "decision": "decision",
    "anti-pattern": "anti_pattern",
    "antipattern": "anti_pattern",
    "preference": "preference",
    "tool": "tool",
    "warning": "warning",
    "fix": "fix",
    "bugfix": "fix",
    "feat": "feat",
    "neutral": "neutral",
}


def _slugify(text: str, max_len: int = 40) -> str:
    """Slugify a title for use in a filename. Lowercase, alnum + dash only."""
    out = []
    for c in (text or "").lower():
        if c.isalnum() or c == "-":
            out.append(c)
        elif c.isspace() or c in "_":
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:max_len] or "untitled"


def _confidence_label(value: float) -> str:
    """Convert numeric confidence in [0,1] to a label."""
    if value >= 0.8:
        return "high"
    if value >= 0.4:
        return "medium"
    return "low"


def _archive_subdir(category: str | None) -> str:
    if not category:
        return "neutral"
    return TAPE_SUBDIR.get(category, category.replace("-", "_"))


def export_episode_to_markdown(
    episode: "Episode",
    archive_root: Path,
    *,
    workspace: str = "",
    session_id: str = "",
    assistant: str = "lex",
) -> Path:
    """Write a single v3 episode as a standardized markdown archive file.

    Args:
        episode: the v3 ``Episode`` dataclass to export.
        archive_root: path to ``tape/archive`` (parent of category subdirs).
        workspace: optional raw workspace label for ``workspace:`` field.
        session_id: optional session UUID for ``session:`` field.
        assistant: source assistant name (default ``lex``).

    Returns:
        The absolute ``Path`` to the written file.
    """
    from datetime import datetime, timezone

    # Build a timezone-aware timestamp from epoch seconds.
    ts_dt = datetime.fromtimestamp(episode.created_at, tz=timezone.utc).astimezone()
    date_str = ts_dt.strftime("%Y-%m-%d")
    timestamp_str = ts_dt.isoformat(timespec="seconds")

    category = episode.category or "neutral"
    subdir = _archive_subdir(category)
    target_dir = Path(archive_root) / subdir
    target_dir.mkdir(parents=True, exist_ok=True)

    slug = _slugify(episode.title)
    short_id = episode.id[:8]
    filename = f"{date_str}-{short_id}-{slug}.md"
    filepath = target_dir / filename

    title = episode.title or slug
    body = (episode.body or "").rstrip()
    confidence = _confidence_label(episode.confidence)

    # Optional workspace/session fields: only emit when present.
    workspace_line = f"workspace: {workspace}\n" if workspace else ""
    session_line = f"session: {session_id}\n" if session_id else ""

    content = f"""---
type: {category}
date: {date_str}
timestamp: {timestamp_str}
project: {episode.project_id}
{workspace_line}{session_line}confidence: {confidence}
assistant: {assistant}
status: auto-classified
source: neural-tape-v3
kind: {episode.kind}
---

# {title}

{body}

## Source
Auto-extracted by Neural Tape v3 from {episode.source_type}.
Project: {episode.project_id} | Episode: {episode.id} | Kind: {episode.kind}
"""
    filepath.write_text(content, encoding="utf-8")
    log.info("Exported v3 episode to markdown: %s", filepath.relative_to(archive_root.parent))
    return filepath


def export_episodes_bulk(
    episodes: list["Episode"],
    archive_root: Path,
    *,
    workspace: str = "",
    assistant: str = "lex",
) -> int:
    """Export multiple episodes. Returns count of files written.

    ``session_id`` cannot be propagated in bulk mode; pass per-episode if needed.
    """
    count = 0
    for ep in episodes:
        session_id = ""
        if ep.raw_payload and isinstance(ep.raw_payload, dict):
            session_id = str(ep.raw_payload.get("session_id", "") or "")
        export_episode_to_markdown(
            ep, archive_root,
            workspace=workspace, session_id=session_id, assistant=assistant,
        )
        count += 1
    return count
