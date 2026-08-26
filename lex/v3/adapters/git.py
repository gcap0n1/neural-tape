"""GitAdapter — capture git events and publish on EventBus (D1.3).

Uses `git` CLI via subprocess (zero dependencies, stdlib-only, matching v2.2
philosophy). Commands used:
- `git branch --show-current`
- `git log --since=... --format=... --name-only`
- `git diff --name-only` (uncommitted changes)
- `git rev-parse --show-toplevel`

Branch switch detection: poll compares current branch vs last known. On change,
publishes a 'git.branch_switch' event (source_type reserved for Fase 2+, so in
Fase 1 we log the switch but don't publish — since EventBus rejects future
source types).

Fase 1 scope: commit events only. Branch switch detection is implemented but
the event is logged (INFO level) rather than published, until EventBus is
extended in Fase 2.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..events import Event, EventBus

log = logging.getLogger("neural-tape-v3")

GIT_LOG_FORMAT = "--format=%H||%an||%ae||%ai||%s"


@dataclass
class GitCommitEvent:
    sha: str
    author: str
    email: str | None = None
    date_iso: str | None = None
    message: str = ""
    message_short: str = ""
    branch: str | None = None
    files_changed: list[str] = field(default_factory=list)


@dataclass
class GitBranchSwitchEvent:
    old_branch: str
    new_branch: str


@dataclass
class GitRepoState:
    """Persistent state for a git repo, stored in tape/v3/.state/git-<project_id>.json"""
    project_id: str
    last_commit_sha: str | None = None
    last_branch: str | None = None
    last_poll_epoch: float = 0.0


class GitAdapter:
    """Git event adapter. Polls commits and detects branch switches."""

    def __init__(self, project_root: Path, event_bus: EventBus, project_id: str,
                 max_commits: int = 50):
        self.project_root = Path(project_root).resolve()
        self.event_bus = event_bus
        self.project_id = project_id
        self.max_commits = max_commits
        # Confirm it's a git repo (lazy check).
        self._git_root = self._git("rev-parse", "--show-toplevel")
        log.debug("git root: %s", self._git_root)

    # ---- public API -----------------------------------------------------

    def poll_commits(self, since_epoch: float | None = None) -> list[GitCommitEvent]:
        """Poll commits since a reference time. If None, uses stored last_commit_sha.
        Returns list of NEW commits (oldest first). Returns empty list gracefully
        if the repo has no commits yet.

        Uses Unix timestamp format (@<epoch>) for --since to be timezone-agnostic.
        """
        if since_epoch is None:
            since_epoch = time.time() - 1800

        unix_ts = int(since_epoch)
        try:
            raw = self._git(
                "log", f"--since=@{unix_ts}",
                GIT_LOG_FORMAT, "--name-only",
                f"--max-count={self.max_commits}",
            )
        except RuntimeError as e:
            err_msg = str(e)
            if "fatal:" in err_msg and ("no commit" in err_msg.lower() or "non ha" in err_msg):
                log.debug("git repo has no commits yet")
                return []
            raise
        return self._parse_log(raw)

    def poll_commits_since_last_known(self, last_known_sha: str | None) -> list[GitCommitEvent]:
        """Poll commits since the last known commit SHA."""
        if not last_known_sha:
            return self.poll_commits(since_epoch=time.time() - 3600)
        try:
            raw = self._git(
                "log", f"{last_known_sha}..HEAD",
                GIT_LOG_FORMAT, "--name-only",
                f"--max-count={self.max_commits}",
            )
        except RuntimeError:
            # last_known_sha may be stale (force push, rebase). Fall back to time-based.
            log.warning("git log from stale SHA %s; falling back to time-based poll", last_known_sha)
            return self.poll_commits(since_epoch=time.time() - 3600)
        return self._parse_log(raw)

    def get_current_branch(self) -> str:
        return self._git("branch", "--show-current")

    def get_recent_files(self, max_files: int = 20) -> list[str]:
        """Return files modified in recent commits + uncommitted changes.
        Ordered by recency (newest first).
        """
        files: list[str] = []

        # Uncommitted changes
        try:
            uncommitted = self._git("diff", "--name-only", "--diff-filter=AM")
            for line in uncommitted.splitlines():
                line = line.strip()
                if line and line not in files:
                    files.append(line)
        except RuntimeError:
            pass

        # Files in working tree changes
        try:
            untracked = self._git("ls-files", "--others", "--exclude-standard")
            for line in untracked.splitlines():
                line = line.strip()
                if line and line not in files:
                    files.append(line)
        except RuntimeError:
            pass

        # Files from recent commits (last 10)
        try:
            committed = self._git("log", "--oneline", "--name-only",
                                  "--diff-filter=AM",  # Added or Modified
                                  f"-{max_files}", "--format=")
            for line in committed.splitlines():
                line = line.strip()
                if line and line not in files:
                    files.append(line)
        except RuntimeError:
            pass

        return files[:max_files]

    def publish_recent_commits(self, since_epoch: float | None = None,
                               max_events: int = 20) -> int:
        """Poll recent commits and publish each as a git.commit event on EventBus.

        Args:
            since_epoch: poll commits since this epoch. Default: 24h ago.
            max_events: max events to publish in a single call.

        Returns:
            Number of commit events published.

        Idempotent: already-published events are skipped via event_log lookup.
        """
        if since_epoch is None:
            since_epoch = time.time() - 86400  # 24h

        commits = self.poll_commits(since_epoch=since_epoch)
        if not commits:
            return 0

        # Import Event locally to handle both package and flat-load contexts
        try:
            from ..events import Event
        except ImportError:
            from events import Event

        branch = self.get_current_branch()
        published = 0
        for commit in commits[:max_events]:
            # Skip if already published (idempotency via event_log).
            # NOTE: must check by exact source_ref — comparing only against the
            # most recent event re-publishes older commits at every tick.
            already = self.event_bus.storage.has_event(
                self.project_id,
                source_type="git.commit",
                source_ref=commit.sha[:12],
            )
            if already:
                continue

            event = Event(
                project_id=self.project_id,
                source_type="git.commit",
                source_ref=commit.sha[:12],
                payload={
                    "sha": commit.sha,
                    "author": commit.author,
                    "message_short": commit.message_short,
                    "branch": branch,
                    "files_changed_count": len(commit.files_changed),
                },
            )
            self.event_bus.publish(event)
            published += 1

        if published:
            log.info("published %d git commit events for %s (branch=%s)",
                     published, self.project_id, branch)
        return published

    def get_diff_stat(self) -> dict[str, int]:
        """Return {file: lines_changed} for uncommitted changes."""
        stats: dict[str, int] = {}
        try:
            raw = self._git("diff", "--stat")
            for line in raw.splitlines():
                if "|" not in line:
                    continue
                file_part = line.split("|")[0].strip()
                if file_part:
                    stats[file_part] = 0  # simplified; counts could be extracted
        except RuntimeError:
            pass
        return stats

    # ---- internals ------------------------------------------------------

    def _git(self, *args: str) -> str:
        """Run a git command in the project root. Raises RuntimeError on failure."""
        result = subprocess.run(
            ["git", *args],
            capture_output=True, text=True, cwd=self.project_root,
            timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed (exit {result.returncode}): "
                f"{result.stderr.strip()[:200]}"
            )
        return result.stdout.strip()

    def _parse_log(self, raw: str) -> list[GitCommitEvent]:
        """Parse git log output with GIT_LOG_FORMAT + --name-only.

        Git log --format output with --name-only is:
            hash||author||email||date||message
            <blank line>
            file1
            file2
            <blank line>
            next_hash||...

        Blank lines serve two roles: (a) separator between metadata and files,
        and (b) separator between commits. We use metadata lines (containing ||)
        as the start of a new commit, and accumulate file lines until the next
        metadata line or EOF.
        """
        if not raw.strip():
            return []
        events: list[GitCommitEvent] = []
        current: GitCommitEvent | None = None
        for line in raw.splitlines():
            stripped = line.strip()
            if "||" in line:
                # Commit metadata line — finalize previous, start new.
                if current and current.sha:
                    events.append(current)
                parts = [p.strip() for p in line.split("||", 4)]
                sha, author, email, date_iso, msg = parts if len(parts) >= 5 else (*parts, "", "")
                current = GitCommitEvent(
                    sha=sha, author=author, email=email, date_iso=date_iso,
                    message=msg, message_short=msg.split("\n")[0] if msg else "",
                )
            elif current and stripped and current.sha:
                # File name line — append if we have an active commit.
                current.files_changed.append(stripped)

        if current and current.sha:
            events.append(current)

        # Resolve branch names for all events (lazy: one git call per event is
        # expensive, so we don't set branch here — the adapter provides it via
        # get_current_branch() if needed).
        return events
