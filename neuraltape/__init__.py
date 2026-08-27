"""Neural Tape — agent-agnostic layered memory for AI coding agents.

Public API surface. The pipeline lives in ``neuraltape.v3``; this
package exposes the stable entry points and object model.
"""

from __future__ import annotations

from neuraltape.v3 import __version__
from neuraltape.v3.transcript_parser import TranscriptParser
from neuraltape.v3.transcript_sources import (
    SourceManifest,
    SourceRegistry,
    builtin_sources,
)
from neuraltape.v3.transcript_watcher import TranscriptWatcher

__all__ = [
    "TranscriptParser",
    "TranscriptWatcher",
    "SourceManifest",
    "SourceRegistry",
    "builtin_sources",
    "__version__",
]