"""Test per lex/v3/focus.py (D1.4)."""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lex" / "v3"))

logging.disable(logging.CRITICAL)

from focus import CurrentFocus, FocusGenerator  # type: ignore[import-not-found]
from storage import Episode, Storage  # type: ignore[import-not-found]


def _stub_git_adapter():
    """Create a minimal git adapter stub for test."""

    class StubGitAdapter:
        def get_current_branch(self):
            return "feature/test"

        def poll_commits(self, since_epoch=None):
            return []

        def get_recent_files(self, max_files=20):
            return ["dashboard.py", "urls.py", "views.py"]

        def get_diff_stat(self):
            return {"dashboard.py": 5}

    return StubGitAdapter()


def _stub_project(project_id="zeus"):
    from dataclasses import dataclass
    @dataclass
    class Project:
        project_id: str
        root: Path
        source: str = "test"
        config_path: Path | None = None
        display_name: str | None = "Zeus"
        kind: str | None = "django"
    return Project(project_id=project_id, root=Path(tempfile.mkdtemp(prefix="nt-v3-focus-")))


def _storage_with_episodes() -> Storage:
    d = Path(tempfile.mkdtemp(prefix="nt-v3-focus-"))
    s = Storage(d / "test.db")
    # Add some episodes
    s.put_episode(Episode(project_id="zeus", kind="episodic", source_type="transcript",
                          title="HTMX shell migration for Zeus", source_ref="sess1",
                          category="decision", confidence=0.85, body="migrating the shell to HTMX"))
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="transcript",
                          title="dashboard template refactor", source_ref="sess1",
                          category="pattern", confidence=0.7, body="working on dashboard"))
    return s


def test_current_focus_dataclass():
    fc = CurrentFocus(
        project_id="zeus", project_display="Zeus",
        branch="main", goal="HTMX migration",
        next_step="continue dashboard", blocked=False,
        confidence=0.85,
    )
    d = fc.to_dict()
    assert d["project_id"] == "zeus"
    assert d["branch"] == "main"
    assert d["confidence"] == 0.85
    assert "captured_at" in d


def test_focus_generator_generate():
    """Verify the generator produces output without errors."""
    storage = _storage_with_episodes()
    project = _stub_project()
    git = _stub_git_adapter()
    out_dir = Path(tempfile.mkdtemp(prefix="nt-v3-focus-out-"))

    gen = FocusGenerator(storage=storage, git_adapter=git, project=project,
                         output_dir=out_dir)
    focus = gen.generate()

    assert focus.project_id == "zeus"
    assert focus.project_display == "Zeus"
    assert focus.branch == "feature/test"
    assert focus.goal  # non-empty
    assert focus.next_step  # non-empty
    assert 0.0 <= focus.confidence <= 1.0

    # Verify file was written
    out_file = out_dir / "current-focus.json"
    assert out_file.exists()
    data = json.loads(out_file.read_text())
    assert data["project_id"] == "zeus"

    # Confidence must not be zero (there's git coherence from branch)
    assert focus.confidence > 0.0, f"confidence too low: {focus.confidence}"


def test_focus_confidence_calculation():
    """Verify Q1=D: confidence formula is applied."""
    storage = _storage_with_episodes()
    project = _stub_project()

    # Git adapter with no recent commits
    class NoCommitGit:
        def get_current_branch(self): return "feature/x"
        def poll_commits(self, since_epoch=None): return []
        def get_recent_files(self, max_files=20): return ["a.py"]
        def get_diff_stat(self): return {}

    gen = FocusGenerator(storage=storage, git_adapter=NoCommitGit(), project=project,
                         output_dir=Path(tempfile.mkdtemp(prefix="nt-v3-focus-nc-")))
    focus = gen.generate()

    # Without recent commits, confidence should be penalized (< 0.85 of raw max)
    # and a confidence_note should exist
    assert focus.confidence > 0.0
    # Since git_coherence = 0 (no commits), the max raw is 0.3*overlap + 0.2*llm
    # which should be < 0.5, then * 0.85 penalty makes it < 0.43
    assert focus.confidence <= 0.5
