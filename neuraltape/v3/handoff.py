"""handoff — Agent Handoff bundle (P5, Fase 2).

Produces `agent-handoff.json` + `agent-handoff.md`: a complete context package
that allows a fresh AI agent to reconstruct the state of work for a given project.

Includes:
- Resume Project summary (from resume.py)
- Recent episodic memory (last 7 days)
- Semantic memory (all patterns + decisions)
- Git state (branch, recent commits, modified files)
- Current focus + working set
- Assistant metadata (which agents have been active)

Output: tape/v3/projects/<id>/agent-handoff.json (machine-readable)
        tape/v3/projects/<id>/agent-handoff.md  (human-readable)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .adapters.git import GitAdapter
    from .storage import Storage

log = logging.getLogger("neural-tape-v3")


class AgentHandoffBundle:
    """Generate agent handoff context package."""

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

    def generate(self) -> dict:
        """Generate the handoff bundle. Returns the full data dict."""
        bundle = {
            "project_id": self.project_id,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "version": "fase-2",
            "focus": self._load_focus(),
            "workset": self._load_workset(),
            "git": self._gather_git(),
            "memory": {
                "recent_episodic": self._gather_episodic(),
                "semantic": self._gather_semantic(),
            },
            "summary": {
                "total_episodes": self._count_episodes(),
                "recent_commits_count": 0,
                "modified_files_count": 0,
            },
        }

        # Fill summary counters
        commits = bundle["git"].get("recent_commits", [])
        bundle["summary"]["recent_commits_count"] = len(commits)
        files = bundle["git"].get("modified_files", [])
        bundle["summary"]["modified_files_count"] = len(files)

        # Write JSON
        json_path = self.output_dir / "agent-handoff.json"
        json_path.write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Write Markdown
        md_path = self.output_dir / "agent-handoff.md"
        md_path.write_text(self._render_markdown(bundle), encoding="utf-8")

        log.info("agent-handoff bundle generated: %s, %s", json_path.name, md_path.name)
        return bundle

    # ---- data gatherers ------------------------------------------------

    def _load_focus(self) -> dict:
        path = self.output_dir / "current-focus.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"goal": "unknown", "confidence": 0.0}

    def _load_workset(self) -> list:
        path = self.output_dir / "working-set.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data.get("files", data.get("entries", []))
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def _gather_git(self) -> dict:
        result: dict = {
            "branch": "unknown",
            "recent_commits": [],
            "modified_files": [],
        }
        try:
            result["branch"] = self.git.get_current_branch()
        except Exception:
            pass
        try:
            commits = self.git.poll_commits(since_epoch=time.time() - 86400 * 3)
            result["recent_commits"] = [
                {
                    "sha": c.sha[:12],
                    "author": c.author,
                    "date": (c.date_iso or "")[:16],
                    "message": c.message_short[:80],
                    "files_changed": len(c.files_changed),
                }
                for c in commits[:10]
            ]
        except Exception:
            pass
        try:
            result["modified_files"] = self.git.get_recent_files(max_files=20)
        except Exception:
            pass
        return result

    def _gather_episodic(self) -> list[dict]:
        recent = self.storage.query_episodes(
            self.project_id,
            kind="episodic",
            since=time.time() - 86400 * 7,
            limit=30,
        )
        return [
            {
                "id": ep.id[:12],
                "category": ep.category,
                "title": ep.title,
                "confidence": ep.confidence,
                "created_at": datetime.fromtimestamp(ep.created_at).isoformat(timespec="minutes"),
            }
            for ep in recent
        ]

    def _gather_semantic(self) -> list[dict]:
        semantic = self.storage.query_episodes(
            self.project_id,
            kind="semantic",
            limit=30,
        )
        return [
            {
                "id": ep.id[:12],
                "category": ep.category,
                "title": ep.title,
                "confidence": ep.confidence,
                "created_at": datetime.fromtimestamp(ep.created_at).isoformat(timespec="minutes"),
            }
            for ep in semantic
        ]

    def _count_episodes(self) -> dict:
        stats = self.storage.stats(self.project_id)
        return {
            "working": stats.get("working", 0),
            "episodic": stats.get("episodic", 0),
            "semantic": stats.get("semantic", 0),
            "total": sum(stats.values()),
        }

    # ---- markdown renderer ---------------------------------------------

    def _render_markdown(self, bundle: dict) -> str:
        lines = [
            f"# Agent Handoff: {self.project_id}",
            f"Generated: {bundle['generated_at']}",
            f"Version: {bundle['version']}",
            "",
            "## Focus",
            f"- **Goal:** {bundle['focus'].get('goal', '?')}",
            f"- **Branch:** `{bundle['git'].get('branch', '?')}`",
            f"- **Confidence:** {bundle['focus'].get('confidence', 0):.2f}",
            "",
            "## Git State",
        ]

        commits = bundle["git"].get("recent_commits", [])
        if commits:
            lines.append(f"- **Recent commits ({len(commits)}):**")
            for c in commits:
                lines.append(f"  - `{c['sha']}` {c.get('date', '')} {c['message']}")
        else:
            lines.append("- No recent commits.")

        files = bundle["git"].get("modified_files", [])
        if files:
            lines.append(f"- **Modified files ({len(files)}):**")
            for f in files[:8]:
                lines.append(f"  - `{f}`")

        lines.extend([
            "",
            f"## Episodic Memory ({len(bundle['memory']['recent_episodic'])})",
        ])
        for ep in bundle["memory"]["recent_episodic"][:15]:
            lines.append(f"- [{ep['created_at'][:10]}] **{ep['category']}** ({ep['confidence']:.2f}) {ep['title']}")

        lines.extend([
            "",
            f"## Semantic Memory ({len(bundle['memory']['semantic'])})",
        ])
        for ep in bundle["memory"]["semantic"][:10]:
            lines.append(f"- [{ep['created_at'][:10]}] **{ep['category']}** ({ep['confidence']:.2f}) {ep['title']}")

        lines.extend([
            "",
            "## Stats",
            f"- Total episodes: {bundle['summary']['total_episodes']}",
            f"- Recent commits: {bundle['summary']['recent_commits_count']}",
            f"- Modified files: {bundle['summary']['modified_files_count']}",
            "",
        ])
        return "\n".join(lines)
