"""Test per lex/v3/markdown_export.py — forward-compat bridge v3 -> markdown archive.

Verifica che gli episodi v3 vengano scritti con lo stesso schema markdown
consumato da lex/pre_load.py (standardizzato 2026-07-18).

Pattern: funzioni test_*() al top level, scoperte dal runner custom di v3."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

# Il runner inietta NeuralTape/ come nt_v3. Facciamo entrambi gli import path.
NT_ROOT = Path(__file__).resolve().parents[2]
if str(NT_ROOT) not in sys.path:
    sys.path.insert(0, str(NT_ROOT))

from lex.v3.markdown_export import export_episode_to_markdown, export_episodes_bulk
from lex.v3.storage import Episode


def _episode(title: str = "Test insight schema v3 export", conf: float = 0.85) -> Episode:
    return Episode(
        project_id="EterCervo",
        kind="episodic",
        source_type="transcript",
        title=title,
        body="Context line.\n\nImplication line.",
        category="tool",
        confidence=conf,
        created_at=time.time(),
        source_ref="abc-123",
        raw_payload={"session_id": "deadbeef-1234-5678-9012-abcdef000000"},
    )


def test_export_single_episode_writes_standard_schema():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ep = _episode()
        path = export_episode_to_markdown(
            ep, root,
            workspace="EterCervo-Workspace.code-workspace",
            session_id="deadbeef-1234-5678-9012-abcdef000000",
            assistant="lex",
        )
        assert path.exists(), f"missing file: {path}"
        assert path.parent.name == "tool", f"wrong subdir: {path.parent.name}"
        text = path.read_text(encoding="utf-8")
        # Required core fields.
        for needle in [
            "type: tool",
            "timestamp: 20",
            "project: EterCervo",
            "confidence: high",
            "assistant: lex",
            "source: neural-tape-v3",
            "kind: episodic",
            "# Test insight schema v3 export",
        ]:
            assert needle in text, f"missing {needle!r} in:\n{text}"


def test_export_idempotent_same_filename():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ep = _episode()
        p1 = export_episode_to_markdown(ep, root, workspace="ws")
        p2 = export_episode_to_markdown(ep, root, workspace="ws")
        assert p1 == p2, f"non-idempotent: {p1} vs {p2}"


def test_confidence_label_thresholds():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for conf, expected in [(0.9, "high"), (0.5, "medium"), (0.1, "low")]:
            ep = _episode(conf=conf)
            path = export_episode_to_markdown(ep, root)
            text = path.read_text(encoding="utf-8")
            assert f"confidence: {expected}" in text, f"conf={conf} expected {expected}"


def test_bulk_export_count():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        episodes = [_episode(title=f"T{i}") for i in range(3)]
        for i, e in enumerate(episodes):
            e.id = f"{i:08d}11122233344455566677788{int(time.time())%100:02d}"[:32].ljust(32, "0")
        n = export_episodes_bulk(episodes, root, workspace="ws")
        assert n == 3, f"expected 3 exports, got {n}"
        files = list(root.glob("*/*.md"))
        assert len(files) == 3, f"expected 3 files, got {len(files)}"


def test_unknown_category_falls_back_to_neutral_subdir():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ep = _episode()
        ep.category = "unknown_thing"
        path = export_episode_to_markdown(ep, root)
        assert path.parent.name == "unknown_thing", f"got {path.parent.name}"


def test_workspace_session_omitted_when_empty():
    """The optional workspace/session lines should NOT appear when not provided."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ep = _episode()
        path = export_episode_to_markdown(ep, root)  # no workspace/session
        text = path.read_text(encoding="utf-8")
        assert "workspace:" not in text, "empty workspace should be omitted"
        assert "session:" not in text, "empty session should be omitted"
