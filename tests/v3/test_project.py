"""Test per lex/v3/project.py (Q4=C)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lex" / "v3"))

from project import (  # type: ignore[import-not-found]
    ID_REGEX,
    ProjectResolver,
    write_project_config,
)


def _tmp_root() -> Path:
    d = Path(tempfile.mkdtemp(prefix="nt-v3-proj-"))
    return d


def test_config_id_used():
    root = _tmp_root()
    write_project_config(root, "zeus", display_name="Zeus", kind="django-app")
    r = ProjectResolver()
    proj = r.resolve(root)
    assert proj.project_id == "zeus"
    assert proj.source == "config"
    assert proj.config_path is not None
    assert proj.config_path.exists()
    assert proj.display_name == "Zeus"
    assert proj.kind == "django-app"


def test_fallback_hash_when_no_config():
    root = _tmp_root()
    r = ProjectResolver()
    proj = r.resolve(root)
    assert proj.source == "fallback-hash"
    assert proj.project_id.startswith("auto-")
    # Hash suffix is 10 hex chars (default).
    suffix = proj.project_id[len("auto-"):]
    assert len(suffix) == 10
    assert all(c in "0123456789abcdef" for c in suffix)


def test_fallback_id_is_stable():
    """Resolving the same root twice returns the SAME id (idempotency)."""
    root = _tmp_root()
    r1 = ProjectResolver()
    p1 = r1.resolve(root)
    r2 = ProjectResolver()
    p2 = r2.resolve(root)
    assert p1.project_id == p2.project_id


def test_invalid_project_id_in_config():
    """An invalid project_id in config triggers fallback to hash."""
    import yaml
    root = _tmp_root()
    cfg_dir = root / ".neuraltape"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "project.yaml").write_text(yaml.safe_dump({"project_id": "UPPER CASE"}))
    r = ProjectResolver()
    proj = r.resolve(root)
    # Should fall back to hash, not raise.
    assert proj.source == "fallback-hash"
    assert proj.project_id.startswith("auto-")


def test_invalid_project_id_in_writer_raises():
    root = _tmp_root()
    try:
        write_project_config(root, "UPPER CASE")
    except ValueError:
        return
    raise AssertionError("expected ValueError for invalid project_id")


def test_collision_detected():
    """Two different roots with the SAME configured project_id must raise."""
    root_a = _tmp_root()
    root_b = _tmp_root()
    write_project_config(root_a, "dupe-id")
    write_project_config(root_b, "dupe-id", force=True)
    r = ProjectResolver()
    r.resolve(root_a)  # first OK
    try:
        r.resolve(root_b)
    except ValueError as e:
        assert "dupe-id" in str(e)
        return
    raise AssertionError("expected ValueError on collision")


def test_id_regex_accepts_valid():
    for ok in ("zeus", "cais-lp", "s4all-bot", "a", "abc123", "x-y-z-1"):
        assert ID_REGEX.match(ok), f"should accept {ok!r}"


def test_id_regex_rejects_invalid():
    for bad in ("UPPER", "1- Starts with dash", "under_score", "", "x" * 33, "with space"):
        assert not ID_REGEX.match(bad), f"should reject {bad!r}"


def test_resolve_is_cached():
    """Same root twice returns the SAME Project instance from cache."""
    root = _tmp_root()
    write_project_config(root, "cais-lp")
    r = ProjectResolver()
    p1 = r.resolve(root)
    p2 = r.resolve(root)
    assert p1 is p2
