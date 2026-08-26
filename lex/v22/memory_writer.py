"""MemoryWriter — append classified insights to memory.md + tape/archive/.

memory.md structure (verified):
  - "## How This File Works"   (header/intro)
  - "## Recent Context"        (chronological entries, newest first)
      -> new entries inserted right after this heading
  - "## Tool" / "## Pattern"   (optional category sections further down)

Insertion rule (matches tools/lex-capture.py behavior):
  Insert each new entry immediately after the "## Recent Context" line,
  so the most recent insights appear at the top of Recent Context.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger("neural-tape-v22")

RECENT_CONTEXT_HEADING = "## Recent Context"

CATEGORY_HEADERS = {
    "pattern": "## Pattern",
    "decision": "## Decision",
    "anti-pattern": "## Anti-pattern",
    "antipattern": "## Anti-pattern",
    "preference": "## Preference",
    "tool": "## Tool",
    "warning": "## Warning",
    "fix": "## Fix",
    "bugfix": "## Bugfix",
    "neutral": "## Neutral",
}

# Category → tape/archive subdir mapping (normalize "anti-pattern" → "anti_pattern")
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


class MemoryWriter:
    """Write classified insights to memory.md and tape/archive/."""

    def __init__(self, memory_file: Path, tape_root: Path, *, assistant: str = "lex"):
        self.memory_file = memory_file
        self.tape_root = tape_root
        self.assistant = assistant

    @staticmethod
    def _normalize_project(workspace_label: str) -> str:
        """Normalize a VS Code workspace label into a project name.

        e.g. 'EterCervo-Workspace.code-workspace' -> 'EterCervo'.
        Falls back to the raw label if no known suffix is detected.
        """
        if not workspace_label:
            return "default"
        name = workspace_label
        for suffix in ("-Workspace.code-workspace", ".code-workspace", ".code.json"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        return name or "default"

    def write(self, insights: list[dict], session_label: str, session_id: str) -> int:
        """Write insights. Returns count successfully written.

        Frontmatter schema (v2.2 standardized, 2026-07-18):
          type, date, timestamp, project, workspace, session,
          confidence, assistant, status, source
        """
        count = 0
        now = datetime.now().astimezone()
        today = now.strftime("%Y-%m-%d")
        timestamp = now.isoformat(timespec="seconds")
        project = self._normalize_project(session_label)

        for insight in insights:
            category = insight.get("category", "neutral")
            description = insight.get("description", "").strip()
            context = insight.get("context", "").strip()
            implication = insight.get("implication", "").strip()
            confidence = insight.get("confidence", "medium")
            if not description:
                continue

            try:
                self._append_to_memory(today, category, description, context, implication)
                self._append_to_tape(
                    today,
                    category,
                    description,
                    context,
                    implication,
                    session_label,
                    session_id,
                    timestamp=timestamp,
                    project=project,
                    confidence=confidence,
                    assistant=self.assistant,
                )
                count += 1
            except Exception as e:
                log.error("Failed to write insight '%s': %s", description, e, exc_info=True)

        return count

    # ─── memory.md ──────────────────────────────────────────────────────

    def _append_to_memory(
        self, date: str, category: str, description: str, context: str, implication: str
    ) -> None:
        """Insert entry in Recent Context and in the matching category section."""
        content = self.memory_file.read_text(encoding="utf-8")

        entry = (
            f"\n## [{date}] {category} | {description}\n"
            f"- Context: {context}\n"
            f"- Implication: {implication}\n"
        )

        content = self._insert_after_heading(content, RECENT_CONTEXT_HEADING, entry)

        category_heading = CATEGORY_HEADERS.get(category, f"## {category.title()}")
        content = self._insert_after_heading(content, category_heading, entry)
        content = self._update_updated_marker(content, date)

        self.memory_file.write_text(content, encoding="utf-8")

    def _insert_after_heading(self, content: str, heading: str, entry: str) -> str:
        idx = content.find(heading)
        if idx == -1:
            log.warning("'%s' heading not found, appending section", heading)
            return content.rstrip() + f"\n\n{heading}\n{entry}\n"

        line_end = content.index("\n", idx) + 1
        return content[:line_end] + entry + content[line_end:]

    def _update_updated_marker(self, content: str, date: str) -> str:
        lines = content.splitlines(keepends=True)
        for idx, line in enumerate(lines[:30]):
            if line.lstrip().startswith("> **Updated:**"):
                newline = "\n" if line.endswith("\n") else ""
                lines[idx] = f"> **Updated:** {date}{newline}"
                break
        return "".join(lines)

    # ─── tape/archive/ ──────────────────────────────────────────────────

    def _append_to_tape(
        self,
        date: str,
        category: str,
        description: str,
        context: str,
        implication: str,
        session_label: str,
        session_id: str,
        *,
        timestamp: str = "",
        project: str = "default",
        confidence: str = "medium",
        assistant: str = "lex",
    ) -> None:
        subdir = TAPE_SUBDIR.get(category, category.replace("-", "_"))
        archive_dir = self.tape_root / "tape" / "archive" / subdir
        archive_dir.mkdir(parents=True, exist_ok=True)

        safe_desc = "".join(c if c.isalnum() or c in "-_" else "-" for c in description.lower())[:40]
        filename = f"{date}-{session_id[:8]}-{safe_desc}.md"
        filepath = archive_dir / filename

        # Avoid overwriting if the same insight is captured twice
        if filepath.exists():
            filepath = archive_dir / f"{date}-{session_id[:8]}-{safe_desc}-{datetime.now().strftime('%H%M%S')}.md"

        # Standardized frontmatter (2026-07-18):
        #   timestamp, project, confidence, assistant added for pre_load.py compatibility.
        #   Legacy fields (date, workspace, session, status, source) preserved.
        content = f"""---
type: {category}
date: {date}
timestamp: {timestamp}
project: {project}
workspace: {session_label}
session: {session_id}
confidence: {confidence}
assistant: {assistant}
status: auto-classified
source: neural-tape-v2.2
---

# {description}

## Context
{context}

## Implication
{implication}

## Source
Auto-extracted by Neural Tape v2.2 from VS Code Copilot transcript.
Session: {session_label} ({session_id})
"""
        filepath.write_text(content, encoding="utf-8")
        log.info("Wrote archive file: %s", filepath.relative_to(self.tape_root))
