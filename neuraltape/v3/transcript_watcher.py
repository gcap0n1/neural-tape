"""Discover recent transcripts produced by any registered assistant source.

Discovery is data-driven: every supported agent is described by a
SourceManifest in neuraltape/v3/transcript_sources.py.  This module keeps the
stable public API used by the cron tick, backfill, harvest and resume
tools, delegating strategy questions to a SourceRegistry.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

from transcript_sources import (  # noqa: E402
    SourceManifest,
    SourceRegistry,
    builtin_sources,
    legacy_workspace_label,
)


class TranscriptWatcher:
    """Find assistant transcripts across every registered source store."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        vscode_user: Path | None = None,
        codex_home: Path | None = None,
        kimi_home: Path | None = None,
        grok_home: Path | None = None,
        extra_sources: list[SourceManifest] | None = None,
        disabled: set[str] | None = None,
    ):
        self.home = (home or Path.home()).expanduser()
        self.vscode_user = vscode_user or self.home / ".config" / "Code" / "User"
        self.codex_home = codex_home or self.home / ".codex"
        self.kimi_home = kimi_home or self.home / ".kimi-code"
        self.grok_home = grok_home or self.home / ".grok"

        sources = builtin_sources(
            self.home,
            vscode_user=vscode_user,
            codex_home=codex_home,
            kimi_home=kimi_home,
            grok_home=grok_home,
        )
        sources.extend(extra_sources or [])
        if disabled:
            sources = [s for s in sources if s.id not in disabled]
        self._registry = SourceRegistry(sources)

    def _paths(self):
        yield from self._registry.all_paths()

    def find_active_transcript(self, max_age_minutes: int = 60) -> Path | None:
        candidates = self.find_all_transcripts(max_age_minutes=max_age_minutes)
        return candidates[0][1] if candidates else None

    def find_all_transcripts(self, max_age_minutes: int = 60) -> list[tuple[float, Path]]:
        now = time.time()
        candidates: dict[Path, float] = {}
        for path in self._paths():
            try:
                resolved = path.resolve()
                mtime = resolved.stat().st_mtime
            except OSError:
                continue
            if (now - mtime) / 60 >= max_age_minutes:
                continue
            if not self._should_index(resolved):
                continue
            candidates[resolved] = mtime
        return sorted(((mtime, path) for path, mtime in candidates.items()), reverse=True)

    def get_workspace_label(self, transcript: Path) -> str:
        transcript = Path(transcript).resolve()
        label = self._registry.workspace_label_of(transcript)
        if label is not None:
            return label
        return legacy_workspace_label(transcript)

    def session_id_of(self, transcript: Path) -> str:
        """Manifest-driven session id with the legacy name-based fallback."""
        sid = self._registry.session_id_of(transcript)
        if sid is not None:
            return sid
        return self.get_session_id(transcript)

    def _should_index(self, transcript: Path) -> bool:
        return self._registry.should_index(transcript)

    @staticmethod
    def get_session_id(transcript: Path) -> str:
        """Name-based session id (legacy: no manifest context needed)."""
        path = Path(transcript)
        if path.name == "wire.jsonl":
            # Kimi Code layout: sessions/wd_*/session_<uuid>/agents/<agent>/wire.jsonl
            for parent in path.parents:
                if parent.name.startswith("session_"):
                    return parent.name
        if path.name == "chat_history.jsonl":
            return path.parent.name
        return path.stem