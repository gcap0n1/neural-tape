"""workset — Working Set generator (D1.5).

Produces `working-set.json` per project: which files are currently active.

Algorithm (from spec):
1. Episodi recenti che menzionano file (da Storage)
2. Git diff (modifiche non committate)
3. mtime dei file nelle ultime 24h

Each file entry includes a `reason` field explaining why it was included.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .storage import Storage

log = logging.getLogger("neural-tape-v3")


@dataclass
class FileEntry:
    path: str               # relative path from project root
    reason: str             # why included: "git-changed" | "recent-commit" | "episode-ref" | "recent-mtime"
    last_modified: float | None = None
    lines_changed: int = 0


@dataclass
class WorkingSet:
    project_id: str
    files: list[dict]       # serialized FileEntry
    captured_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "files": self.files,
            "captured_at": self.captured_at,
        }


class WorkingSetGenerator:
    """Generates working-set.json by combining git state + storage episodes."""

    def __init__(self, storage: Storage, project_root: Path, project_id: str,
                 output_dir: Path | None = None):
        self.storage = storage
        self.project_root = project_root.resolve()
        self.project_id = project_id
        self.output_dir = (output_dir or project_root / "tape" / "v3" / "focus")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, max_files: int = 20) -> WorkingSet:
        """Generate the working set. Returns WorkingSet and writes to file.

        Priority order:
        1. Files from git uncommitted changes
        2. Files from recent commits (last 10)
        3. Files mentioned in recent storage episodes
        4. Files from recent mtime (last 24h)
        """
        files: list[FileEntry] = []
        seen: set[str] = set()

        # 1. Git uncommitted changes (highest priority)
        self._add_git_uncommitted(files, seen)

        # 2. Recent commits
        self._add_git_recent_commits(files, seen)

        # 3. Storage episodes
        self._add_episode_references(files, seen)

        # 4. Recent mtime
        self._add_recent_mtime(files, seen)

        # Limit and serialize
        result = files[:max_files]
        ws = WorkingSet(
            project_id=self.project_id,
            files=[f.__dict__ for f in result],
        )

        self._write(ws)
        log.info("working-set generated: project=%s files=%d", self.project_id, len(result))
        return ws

    # ---- internals ------------------------------------------------------

    def _run_git(self, *args: str) -> str:
        import subprocess
        try:
            r = subprocess.run(
                ["git", *args],
                capture_output=True, text=True, cwd=self.project_root,
                timeout=10,
            )
            if r.returncode == 0:
                return r.stdout.strip()
        except Exception:
            pass
        return ""

    def _add_git_uncommitted(self, files: list[FileEntry], seen: set[str]) -> None:
        raw = self._run_git("diff", "--name-only", "--diff-filter=AM")
        for line in raw.splitlines():
            p = line.strip()
            if p and p not in seen:
                seen.add(p)
                files.append(FileEntry(path=p, reason="git-changed"))
        untracked = self._run_git("ls-files", "--others", "--exclude-standard")
        for line in untracked.splitlines():
            p = line.strip()
            if p and p not in seen:
                seen.add(p)
                files.append(FileEntry(path=p, reason="git-changed"))

    def _add_git_recent_commits(self, files: list[FileEntry], seen: set[str]) -> None:
        raw = self._run_git(
            "log", "--oneline", "--name-only",
            "--diff-filter=AM", "-10", "--format=",
        )
        for line in raw.splitlines():
            p = line.strip()
            if p and p not in seen:
                seen.add(p)
                files.append(FileEntry(path=p, reason="recent-commit"))

    def _add_episode_references(self, files: list[FileEntry], seen: set[str]) -> None:
        """Parse episode bodies for file path references (simple heuristic)."""
        episodes = self.storage.query_episodes(self.project_id, kind="working", limit=50)
        episodes += self.storage.query_episodes(self.project_id, kind="episodic", limit=20)

        for ep in episodes:
            if not ep.body:
                continue
            # Look for lines that look like file paths (end with .py, .ts, .js, .html, etc.)
            for line in ep.body.splitlines():
                line = line.strip()
                if not line:
                    continue
                for ext in (".py", ".ts", ".js", ".html", ".css", ".json", ".yaml",
                            ".yml", ".toml", ".md", ".sql", ".txt", ".conf", ".cfg",
                            ".sh", ".env"):
                    if ext in line and "/" in line:
                        # Extract the potential file path
                        for word in line.split():
                            if word.endswith(ext):
                                if word not in seen:
                                    seen.add(word)
                                    files.append(FileEntry(path=word, reason="episode-ref"))
                                    break

    def _add_recent_mtime(self, files: list[FileEntry], seen: set[str]) -> None:
        """Scan project root for files modified in the last 24h."""
        cutoff = time.time() - 86400
        # Only scan a few common directories to avoid deep scans of node_modules, .git etc.
        scan_dirs = ["app", "apps", "src", "lib", "utils", "core", "models", "views",
                     "controllers", "api", "config", "templates", "static", "components",
                     "pages", "routes", "services", "helpers", "tests"]
        for sd in scan_dirs:
            scan_path = self.project_root / sd
            if not scan_path.exists():
                continue
            try:
                for f in scan_path.rglob("*"):
                    if not f.is_file():
                        continue
                    # Skip common noise dirs
                    skip_parts = {"node_modules", ".git", "__pycache__", ".venv", "venv",
                                  ".terraform", "dist", "build", ".next"}
                    if any(part in f.parts for part in skip_parts):
                        continue
                    # Only source-like extensions
                    if f.suffix not in {".py", ".ts", ".js", ".jsx", ".tsx", ".html",
                                        ".css", ".scss", ".json", ".yaml", ".yml",
                                        ".toml", ".md", ".sql", ".sh", ".env",
                                        ".conf", ".cfg", ".txt", ".vue", ".svelte",
                                        ".go", ".rs", ".java", ".rb", ".php"}:
                        continue
                    mtime = f.stat().st_mtime
                    if mtime >= cutoff:
                        rel = str(f.relative_to(self.project_root))
                        if rel not in seen:
                            seen.add(rel)
                            files.append(FileEntry(path=rel, reason="recent-mtime",
                                                   last_modified=mtime))
            except Exception:
                continue

    def _write(self, ws: WorkingSet) -> None:
        output_path = self.output_dir / "working-set.json"
        tmp = output_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(ws.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(output_path)
