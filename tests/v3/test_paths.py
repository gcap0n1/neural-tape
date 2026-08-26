"""Tests for portable path resolution (Fase 1, item 3)."""

from __future__ import annotations

import os
from pathlib import Path

from nt_v3.project import default_projects_base


def test_default_projects_base_env_override():
    os.environ["NEURALTAPE_PROJECTS_ROOT"] = "/tmp/workspaces"
    try:
        assert default_projects_base() == Path("/tmp/workspaces")
    finally:
        del os.environ["NEURALTAPE_PROJECTS_ROOT"]


def test_default_projects_base_env_whitespace_ignored():
    os.environ["NEURALTAPE_PROJECTS_ROOT"] = "   "
    try:
        base = default_projects_base()
    finally:
        del os.environ["NEURALTAPE_PROJECTS_ROOT"]
    assert isinstance(base, Path)
    assert base != Path("   ")


def test_default_projects_base_expands_tilde():
    os.environ["NEURALTAPE_PROJECTS_ROOT"] = "~/work"
    try:
        assert default_projects_base() == Path("~/work").expanduser()
    finally:
        del os.environ["NEURALTAPE_PROJECTS_ROOT"]