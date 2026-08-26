"""TranscriptWatcher — find the most recently active Copilot transcript.

VS Code writes one JSONL per chat session at:
    ~/.config/Code/User/workspaceStorage/<HASH>/GitHub.copilot-chat/transcripts/<session-id>.jsonl

This module auto-detects the active transcript across all workspaces,
resolving the workspace hash to a human-readable folder name via workspace.json.
"""

from __future__ import annotations

import glob
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("neural-tape-v22")

# Linux: ~/.config/Code/User ; macOS: ~/Library/Application Support/Code/User
_VSCODE_USER_CANDIDATES = [
    Path.home() / ".config" / "Code" / "User",
    Path.home() / "Library" / "Application Support" / "Code" / "User",
    Path.home() / ".config" / "Code - Insiders" / "User",
]


def _resolve_vscode_user() -> Path | None:
    for candidate in _VSCODE_USER_CANDIDATES:
        if candidate.is_dir():
            return candidate
    return None


class TranscriptWatcher:
    """Find the most recently active Copilot transcript across all workspaces."""

    def __init__(self, vscode_user: Path | None = None):
        self.vscode_user = vscode_user or _resolve_vscode_user()
        if self.vscode_user is None:
            raise RuntimeError(
                "VS Code User directory not found. Looked in: "
                + ", ".join(str(p) for p in _VSCODE_USER_CANDIDATES)
            )
        self.workspace_storage = self.vscode_user / "workspaceStorage"

    def find_active_transcript(self, max_age_minutes: int = 60) -> Path | None:
        """Return the most recently modified transcript touched within max_age_minutes."""
        candidates: list[tuple[float, Path]] = []
        pattern = str(
            self.workspace_storage / "*" / "GitHub.copilot-chat" / "transcripts" / "*.jsonl"
        )
        for path_str in glob.glob(pattern):
            path = Path(path_str)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            age_min = (time.time() - mtime) / 60
            if age_min < max_age_minutes:
                candidates.append((mtime, path))

        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    def find_all_transcripts(self, max_age_minutes: int = 60) -> list[tuple[float, Path]]:
        """Return all transcripts touched within max_age_minutes, newest first."""
        candidates: list[tuple[float, Path]] = []
        pattern = str(
            self.workspace_storage / "*" / "GitHub.copilot-chat" / "transcripts" / "*.jsonl"
        )
        for path_str in glob.glob(pattern):
            path = Path(path_str)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            age_min = (time.time() - mtime) / 60
            if age_min < max_age_minutes:
                candidates.append((mtime, path))
        candidates.sort(reverse=True)
        return candidates

    def get_workspace_label(self, transcript: Path) -> str:
        """Resolve workspace hash → folder name via workspace.json."""
        # transcripts/<session>.jsonl  →  GitHub.copilot-chat/transcripts/...
        # parent.parent.parent = workspaceStorage/<HASH>
        try:
            hash_dir = transcript.parent.parent.parent
        except IndexError:
            return "unknown"
        ws_json = hash_dir / "workspace.json"
        if ws_json.exists():
            try:
                data = json.loads(ws_json.read_text())
                folder = data.get("folder") or data.get("workspace")
                if folder:
                    return Path(folder).name
            except (json.JSONDecodeError, OSError):
                pass
        return hash_dir.name[:8]

    def get_session_id(self, transcript: Path) -> str:
        """Extract the session UUID from the filename."""
        return transcript.stem
