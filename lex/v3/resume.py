"""resume — Resume Project renderer (P4, Fase 2).

Compose Current Focus + Working Set + recent episodic context into a structured
markdown summary that allows an AI agent (or a human) to resume work where they
left off.

Design:
- Reads current-focus.json and working-set.json from tape/v3/projects/<id>/
- Queries Storage for recent episodic + semantic episodes
- Resolves git state (branch, recent commits, uncommitted files)
- Produces a single markdown document

Output: tape/v3/projects/<id>/resume-project.md
Also returns the content as a string for potential MCP consumption.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .adapters.git import GitAdapter
    from .storage import Storage

log = logging.getLogger("neural-tape-v3")


class ResumeProjectRenderer:
    """Generate a structured 'resume project' markdown summary."""

    def __init__(
        self,
        storage: Storage,
        git_adapter: GitAdapter,
        project_id: str,
        project_root: Path,
        output_dir: Path,
    ):
        self.storage = storage
        self.git = git_adapter
        self.project_id = project_id
        self.project_root = project_root
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self) -> str:
        """Generate the resume-project markdown. Returns the content string."""
        sections: list[str] = [
            f"# Resume Project: {self.project_id}",
            f"Generated: {datetime.now().astimezone().isoformat(timespec='minutes')}",
            "",
        ]

        # 1. Current Focus
        sections.append("## Current Focus")
        sections.append(self._render_focus())
        sections.append("")

        # 2. Working Set (recent files)
        sections.append("## Working Set (recent files)")
        sections.append(self._render_workset())
        sections.append("")

        # 3. Git State
        sections.append("## Git State")
        sections.append(self._render_git())
        sections.append("")

        # 4. Recent Episodic Episodes (last 7 days)
        sections.append("## Recent Episodic Memory (7 days)")
        sections.append(self._render_episodic())
        sections.append("")

        # 5. Semantic Memory Snapshot
        sections.append("## Semantic Memory Snapshot")
        sections.append(self._render_semantic())
        sections.append("")

        content = "\n".join(sections)
        output_path = self.output_dir / "resume-project.md"
        output_path.write_text(content, encoding="utf-8")
        log.info("resume-project generated: %s", output_path)
        return content

    # ---- renderers ------------------------------------------------------

    def _render_focus(self) -> str:
        focus_path = self.output_dir / "current-focus.json"
        if not focus_path.exists():
            return "_No focus data available._"
        try:
            data = json.loads(focus_path.read_text(encoding="utf-8"))
            lines = [
                f"- **Goal:** {data.get('goal', '?')}",
                f"- **Branch:** `{data.get('branch', '?')}`",
                f"- **Blocked:** {'Yes' if data.get('blocked') else 'No'}",
                f"- **Next step:** {data.get('next_step', '?')}",
                f"- **Confidence:** {data.get('confidence', 0):.2f}",
            ]
            if data.get("confidence_note"):
                lines.append(f"- **Note:** {data['confidence_note']}")
            return "\n".join(lines)
        except (json.JSONDecodeError, OSError) as e:
            return f"_Error reading focus: {e}_"

    def _render_workset(self) -> str:
        ws_path = self.output_dir / "working-set.json"
        if not ws_path.exists():
            return "_No working set data available._"
        try:
            data = json.loads(ws_path.read_text(encoding="utf-8"))
            files = data.get("files", data.get("entries", []))
            if not files:
                return "_Working set empty._"
            lines = []
            for f in files[:20]:
                name = f.get("path", f.get("file", str(f)))
                status = f.get("status", "")
                tag = f" `[{status}]`" if status else ""
                lines.append(f"- `{name}`{tag}")
            return "\n".join(lines)
        except (json.JSONDecodeError, OSError) as e:
            return f"_Error reading working set: {e}_"

    def _render_git(self) -> str:
        import time
        lines = []
        try:
            branch = self.git.get_current_branch()
            lines.append(f"- **Branch:** `{branch}`")
        except Exception:
            lines.append("- **Branch:** unknown")

        try:
            commits = self.git.poll_commits(since_epoch=time.time() - 86400 * 3)
            if commits:
                lines.append(f"- **Recent commits ({len(commits)}):**")
                for c in commits[:5]:
                    sha = c.sha[:8]
                    date = (c.date_iso or "?")[:16]
                    msg = c.message_short[:60]
                    lines.append(f"  - `{sha}` {date} {msg}")
            else:
                lines.append("- **Recent commits:** none in last 3 days")
        except Exception:
            lines.append("- **Recent commits:** unavailable")

        try:
            files = self.git.get_recent_files(max_files=10)
            if files:
                lines.append(f"- **Modified files ({len(files)}):**")
                for f in files[:5]:
                    lines.append(f"  - `{f}`")
        except Exception:
            pass

        return "\n".join(lines)

    def _render_episodic(self) -> str:
        import time
        recent = self.storage.query_episodes(
            self.project_id,
            kind="episodic",
            since=time.time() - 86400 * 7,
            limit=20,
        )
        if not recent:
            return "_No recent episodic episodes._"
        lines = []
        for ep in recent[:15]:
            ts = datetime.fromtimestamp(ep.created_at).strftime("%m-%d %H:%M")
            cat = ep.category or "?"
            conf = f"{ep.confidence:.2f}"
            title = ep.title[:70]
            lines.append(f"- [{ts}] **{cat}** ({conf}) {title}")
        return "\n".join(lines)

    def _render_semantic(self) -> str:
        semantic = self.storage.query_episodes(
            self.project_id,
            kind="semantic",
            limit=15,
        )
        if not semantic:
            return "_No semantic memory yet._"
        lines = []
        for ep in semantic[:10]:
            ts = datetime.fromtimestamp(ep.created_at).strftime("%m-%d")
            cat = ep.category or "?"
            title = ep.title[:70]
            lines.append(f"- [{ts}] **{cat}** {title}")
        return "\n".join(lines)
