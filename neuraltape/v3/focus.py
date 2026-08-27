"""focus — Current Focus generator (D1.4).

Produces `current-focus.json` per project: what is the developer working on,
what's the next step, are there blockers.

Confidence calculation per Q1=D:
    0.5 * git_coherence + 0.3 * working_set_overlap + 0.2 * llm_judge
    If no recent commit (<24h): confidence *= 0.85, confidence_note set.

Trigger per Q5=D:
    idle-trigger + invalidation on branch switch. Not eagerly regenerated.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .adapters.git import GitAdapter
    from .project import Project
    from .storage import Storage

log = logging.getLogger("neural-tape-v3")

FOCUS_DIRNAME = "focus"  # under tape/v3/


@dataclass
class CurrentFocus:
    project_id: str
    project_display: str | None
    branch: str
    goal: str
    next_step: str
    blocked: bool
    blocked_reason: str | None = None
    confidence: float = 0.0
    confidence_note: str | None = None
    captured_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "project_display": self.project_display,
            "branch": self.branch,
            "goal": self.goal,
            "next_step": self.next_step,
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "confidence": self.confidence,
            "confidence_note": self.confidence_note,
            "captured_at": self.captured_at,
        }


class FocusGenerator:
    """Generates current-focus.json by combining storage episodes + git state."""

    def __init__(self, storage: Storage, git_adapter: GitAdapter, project: Project,
                 output_dir: Path | None = None):
        self.storage = storage
        self.git = git_adapter
        self.project = project
        self.output_dir = (output_dir or project.root / "tape" / "v3" / FOCUS_DIRNAME)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Config (with defaults)
        self.commit_stale_hours = 24
        self.weights = {"git": 0.5, "overlap": 0.3, "llm": 0.2}

    def generate(self) -> CurrentFocus:
        """Generate CurrentFocus from storage + git state.

        Steps:
        1. Get current branch
        2. Query recent episodic & semantic episodes for goal extraction
        3. Check git coherence (does latest commit match the goal?)
        4. Calculate confidence
        5. Write to file
        """
        branch = self.git.get_current_branch()

        # 1. Query recent episodes
        recent_eps = self.storage.query_episodes(
            self.project.project_id,
            kind="episodic",
            since=time.time() - 86400 * 7,  # last 7 days
            limit=20,
        )
        semantic_eps = self.storage.query_episodes(
            self.project.project_id,
            kind="semantic",
            limit=10,
        )

        # 2. Extract goal from latest episodic/semantic episodes
        goal = self._extract_goal(recent_eps, semantic_eps)

        # 3. Next step: from latest working episode or git state
        next_step = self._extract_next_step(branch)

        # 4. Blocked detection
        blocked, blocked_reason = self._detect_blocked()

        # 5. Confidence calculation (Q1=D)
        git_coherence = self._git_coherence(goal)
        overlap = self._working_set_overlap(goal)
        llm_judge = self._llm_judge(recent_eps)
        confidence, confidence_note = self._calculate_confidence(
            git_coherence, overlap, llm_judge,
        )

        focus = CurrentFocus(
            project_id=self.project.project_id,
            project_display=self.project.display_name,
            branch=branch,
            goal=goal,
            next_step=next_step,
            blocked=blocked,
            blocked_reason=blocked_reason,
            confidence=confidence,
            confidence_note=confidence_note,
        )

        # Write output
        self._write(focus)
        log.info("current-focus generated: project=%s branch=%s goal=%s conf=%.2f",
                 self.project.project_id, branch, goal[:50], confidence)
        return focus

    # ---- internals ------------------------------------------------------

    def _extract_goal(self, recent_eps: list, semantic_eps: list) -> str:
        """Extract the current goal from recent episodes or git commits.

        Priority:
          1. Latest commit message (if <24h) — strongest signal of what's happening now
          2. Most recent high-confidence episodic/semantic episode title
          3. Fallback: project name
        """
        # Priority 1: latest commit message (fresh signal)
        try:
            recent_commits = self.git.poll_commits(since_epoch=time.time() - self.commit_stale_hours * 3600)
            if recent_commits:
                return recent_commits[0].message_short
        except Exception:
            pass

        # Priority 2: recent episodes
        all_eps = sorted(
            recent_eps + semantic_eps,
            key=lambda e: e.created_at, reverse=True,
        )
        for ep in all_eps[:5]:
            if ep.confidence >= 0.6 and ep.title:
                return ep.title

        # Fallback
        return f"working on {self.project.project_id}"

    def _extract_next_step(self, branch: str) -> str:
        """Derive next step from branch name and recent changes."""
        # Simple heuristic: branch name often contains the next step.
        branch_lower = branch.lower()
        if branch_lower.startswith("feature/"):
            return f"implement {branch_lower.replace('feature/', '').replace('-', ' ')}"
        if branch_lower.startswith("fix/"):
            return f"fix {branch_lower.replace('fix/', '').replace('-', ' ')}"
        # Check for uncommitted changes
        try:
            files = self.git.get_recent_files(max_files=5)
            if files:
                return f"continue working on {', '.join(files[:3])}"
        except Exception:
            pass
        return "undefined — run a session to generate context"

    def _detect_blocked(self) -> tuple[bool, str | None]:
        """Detect if the project is blocked (no recent activity, errors in episodes)."""
        return (False, None)  # Fase 1: simple stub. Fase 2+ will read actual blockers.

    def _git_coherence(self, goal: str) -> float:
        """Check if the latest commit message matches the current goal.

        1.0 if match, 0.0 if no match, 0.5 if partial.
        """
        try:
            recent = self.git.poll_commits(since_epoch=time.time() - 86400)  # last 24h
            if not recent:
                return 0.0  # No recent commits → no git coherence
            latest_msg = recent[0].message_short.lower()
            goal_words = set(goal.lower().split())
            msg_words = set(latest_msg.split())
            if not goal_words or not msg_words:
                return 0.0
            overlap = len(goal_words & msg_words) / max(len(goal_words), len(msg_words))
            return min(1.0, overlap)
        except Exception:
            return 0.0

    def _working_set_overlap(self, goal: str) -> float:
        """Check how much of the working set matches the goal.

        Simplified: use git recent files as a proxy for working set.
        """
        try:
            files = self.git.get_recent_files(max_files=10)
            if not files:
                return 0.0
            # Count files whose paths contain goal-related words.
            goal_words = set(w.lower() for w in goal.split() if len(w) > 2)
            if not goal_words:
                return 0.3
            matching = sum(1 for f in files if any(g in f.lower() for g in goal_words))
            return min(1.0, matching / max(len(files), 1))
        except Exception:
            return 0.3

    def _llm_judge(self, recent_eps: list) -> float:
        """Use the average confidence of recent episodic episodes as LLM judgment.

        In Fase 2+, this could be a separate LLM call. For Fase 1, we use
        the classifier's own confidence as a proxy.
        """
        if not recent_eps:
            return 0.5  # Neutral default
        avg_conf = sum(e.confidence for e in recent_eps) / len(recent_eps)
        return min(1.0, avg_conf)

    def _calculate_confidence(self, git_coherence: float, overlap: float,
                              llm_judge: float) -> tuple[float, str | None]:
        """Q1=D: weighted combination + staleness penalty."""
        w = self.weights
        raw = w["git"] * git_coherence + w["overlap"] * overlap + w["llm"] * llm_judge
        note = None

        # Check if there's been a recent commit
        try:
            recent = self.git.poll_commits(since_epoch=time.time() - self.commit_stale_hours * 3600)
            if not recent:
                raw *= 0.85
                note = f"inferred, no recent commit (<{self.commit_stale_hours}h)"
        except Exception:
            raw *= 0.85
            note = "inferred, no recent commit"

        return (min(1.0, max(0.0, raw)), note)

    def _write(self, focus: CurrentFocus) -> None:
        output_path = self.output_dir / "current-focus.json"
        tmp = output_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(focus.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(output_path)
