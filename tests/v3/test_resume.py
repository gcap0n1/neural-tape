"""Test per lex/v3/resume.py — Resume Project renderer."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

NT_ROOT = Path(__file__).resolve().parents[2]
if str(NT_ROOT) not in sys.path:
    sys.path.insert(0, str(NT_ROOT))

from lex.v3.resume import ResumeProjectRenderer
from lex.v3.storage import Episode, Storage
from unittest.mock import MagicMock


def _fresh_storage(tmpdir: Path) -> Storage:
    db = tmpdir / "test.db"
    return Storage(db)


def _mock_git_adapter():
    g = MagicMock()
    g.get_current_branch.return_value = "main"
    g.poll_commits.return_value = []
    g.get_recent_files.return_value = ["file1.py", "file2.ts"]
    return g


def test_resume_generates_markdown_without_crash():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        storage = _fresh_storage(tmp)
        git = _mock_git_adapter()

        # Add some episodes
        ep = Episode(
            project_id="testproj", kind="episodic", source_type="transcript",
            title="Test episode for resume", category="tool",
            confidence=0.9, created_at=time.time(),
        )
        storage.put_episode(ep)

        renderer = ResumeProjectRenderer(
            storage=storage, git_adapter=git,
            project_id="testproj", project_root=tmp,
            output_dir=tmp / "v3" / "projects" / "testproj",
        )
        content = renderer.generate()
        assert "# Resume Project: testproj" in content
        assert "Current Focus" in content
        assert "Git State" in content
        assert "testproj" in content


def test_resume_graceful_without_focus_file():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        storage = _fresh_storage(tmp)
        git = _mock_git_adapter()
        renderer = ResumeProjectRenderer(
            storage=storage, git_adapter=git,
            project_id="testproj", project_root=tmp,
            output_dir=tmp / "v3" / "projects" / "testproj",
        )
        content = renderer.generate()
        assert "No focus data available" in content
