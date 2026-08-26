"""Test per lex/v3/handoff.py — Agent Handoff bundle."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

NT_ROOT = Path(__file__).resolve().parents[2]
if str(NT_ROOT) not in sys.path:
    sys.path.insert(0, str(NT_ROOT))

from lex.v3.handoff import AgentHandoffBundle
from lex.v3.storage import Episode, Storage
from unittest.mock import MagicMock


def _fresh_storage(tmpdir: Path) -> Storage:
    return Storage(tmpdir / "test.db")


def _mock_git_adapter():
    g = MagicMock()
    g.get_current_branch.return_value = "feature/test"
    g.poll_commits.return_value = []
    g.get_recent_files.return_value = ["src/main.py"]
    return g


def test_handoff_bundle_generates_json_and_md():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        storage = _fresh_storage(tmp)
        git = _mock_git_adapter()

        ep = Episode(
            project_id="testproj", kind="semantic", source_type="transcript",
            title="Semantic pattern for test", category="pattern",
            confidence=0.95, created_at=time.time(),
        )
        storage.put_episode(ep)

        bundle = AgentHandoffBundle(
            storage=storage, git_adapter=git,
            project_id="testproj", project_root=tmp,
            output_dir=tmp / "out",
        )
        data = bundle.generate()

        assert data["project_id"] == "testproj"
        assert "focus" in data
        assert "git" in data
        assert "memory" in data
        assert data["memory"]["semantic"][0]["title"] == "Semantic pattern for test"

        # Check files written
        assert (tmp / "out" / "agent-handoff.json").exists()
        assert (tmp / "out" / "agent-handoff.md").exists()

        # JSON should parse cleanly
        parsed = json.loads((tmp / "out" / "agent-handoff.json").read_text())
        assert parsed["project_id"] == "testproj"
