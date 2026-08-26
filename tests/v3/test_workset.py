"""Test per lex/v3/workset.py (D1.5)."""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lex" / "v3"))

logging.disable(logging.CRITICAL)

from workset import WorkingSet, WorkingSetGenerator  # type: ignore[import-not-found]
from storage import Episode, Storage  # type: ignore[import-not-found]


def _storage_with_episode_refs() -> Storage:
    d = Path(tempfile.mkdtemp(prefix="nt-v3-ws-"))
    s = Storage(d / "test.db")
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="transcript",
                          title="working on dashboard.py", source_ref="s1",
                          body="modified dashboard.py and urls.py", confidence=0.7))
    return s


def test_working_set_dataclass():
    ws = WorkingSet(project_id="zeus", files=[
        {"path": "dashboard.py", "reason": "git-changed", "last_modified": None},
        {"path": "urls.py", "reason": "recent-commit", "last_modified": 1000},
    ])
    d = ws.to_dict()
    assert d["project_id"] == "zeus"
    assert len(d["files"]) == 2
    assert "captured_at" in d


def test_workset_file_entry():
    """Verify FileEntry is created properly."""
    from workset import FileEntry  # type: ignore
    fe = FileEntry(path="src/main.py", reason="git-changed", last_modified=42.0)
    assert fe.path == "src/main.py"
    assert fe.reason == "git-changed"


def test_workset_generator_in_git_repo():
    """In a real git repo, the generator picks up files."""
    import subprocess
    repo = Path(tempfile.mkdtemp(prefix="nt-v3-ws-git-"))
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)

    # Create a file and commit
    (repo / "app.py").write_text("print('hi')", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True)

    # Modify a file (uncommitted)
    (repo / "app.py").write_text("print('hi2')", encoding="utf-8")
    # Create untracked
    (repo / "new.py").write_text("x", encoding="utf-8")

    storage = _storage_with_episode_refs()
    out_dir = Path(tempfile.mkdtemp(prefix="nt-v3-ws-out-"))

    gen = WorkingSetGenerator(storage=storage, project_root=repo,
                              project_id="zeus", output_dir=out_dir)
    ws = gen.generate(max_files=10)

    assert ws.project_id == "zeus"
    assert len(ws.files) >= 1
    assert ws.captured_at > 0
    assert any("app.py" in f["path"] for f in ws.files)
    assert any(
        f["path"] == "new.py" and f["reason"] == "git-changed"
        for f in ws.files
    )

    # File must exist on disk
    out_file = out_dir / "working-set.json"
    assert out_file.exists()
    data = json.loads(out_file.read_text())
    assert data["project_id"] == "zeus"


def test_workset_generator_no_git():
    """Without git, the generator still produces empty working set."""
    proj = Path(tempfile.mkdtemp(prefix="nt-v3-ws-nogit-"))
    # Create a source file to test mtime scanning
    app_dir = proj / "app"
    app_dir.mkdir()
    (app_dir / "main.py").write_text("x", encoding="utf-8")
    # Touch the file so mtime is recent
    time.sleep(0.1)

    storage = _storage_with_episode_refs()
    out_dir = Path(tempfile.mkdtemp(prefix="nt-v3-ws-out-"))

    gen = WorkingSetGenerator(storage=storage, project_root=proj,
                              project_id="zeus", output_dir=out_dir)
    ws = gen.generate(max_files=10)

    # Should still produce output, even if files list is partial
    assert ws.project_id == "zeus"
    out_file = out_dir / "working-set.json"
    assert out_file.exists()
