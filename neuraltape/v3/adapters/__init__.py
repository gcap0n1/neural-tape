"""NeuralTape v3 adapters — external system integrations.

Fase 1:
- git.py : git event adapter (commit, branch switch, file list)
"""

from __future__ import annotations

from .git import GitAdapter, GitCommitEvent, GitBranchSwitchEvent

__all__ = ["GitAdapter", "GitCommitEvent", "GitBranchSwitchEvent"]
