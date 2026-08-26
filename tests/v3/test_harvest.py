"""Test per tools/harvest_sessions.py — workspace hint (Fase 0, item 9)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NT_ROOT = HERE.parent.parent


def _load_harvest():
    spec = importlib.util.spec_from_file_location(
        "harvest_sessions", NT_ROOT / "tools" / "harvest_sessions.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["harvest_sessions"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeWatcher:
    def __init__(self, label: str):
        self._label = label

    def get_workspace_label(self, transcript):
        assert transcript is not None
        return self._label


def _projects():
    return {
        "etercervo": Path("/run/media/x/EterCervo"),
        "zeus": Path("/run/media/x/Zeus"),
    }


def test_workspace_hint_matches_project_basename():
    harvest = _load_harvest()
    assert (
        harvest.workspace_hint_project(
            Path("/tmp/x.jsonl"), _FakeWatcher("Zeus"), _projects()
        )
        == "zeus"
    )


def test_workspace_hint_label_is_case_insensitive():
    harvest = _load_harvest()
    assert (
        harvest.workspace_hint_project(
            Path("/tmp/x.jsonl"), _FakeWatcher("etercervo"), _projects()
        )
        == "etercervo"
    )


def test_workspace_hint_unknown_label_returns_none():
    harvest = _load_harvest()
    assert (
        harvest.workspace_hint_project(
            Path("/tmp/x.jsonl"), _FakeWatcher("unknown"), _projects()
        )
        is None
    )
    assert (
        harvest.workspace_hint_project(
            Path("/tmp/x.jsonl"), _FakeWatcher("SomeOtherFolder"), _projects()
        )
        is None
    )
