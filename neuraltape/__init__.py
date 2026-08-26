"""Neural Tape — agent-agnostic layered memory for AI coding agents.

Public API surface. The pipeline modules live under ``lex.v3``; this
package exposes the stable entry points and object model.
"""

from __future__ import annotations

import sys
from pathlib import Path

_V3 = Path(__file__).resolve().parent.parent / "lex" / "v3"
if str(_V3) not in sys.path:
    sys.path.insert(0, str(_V3))

from lex.v3 import __version__  # noqa: E402

from lex.v3.transcript_parser import TranscriptParser  # noqa: E402
from lex.v3.transcript_watcher import TranscriptWatcher  # noqa: E402
from lex.v3.transcript_sources import (  # noqa: E402
    SourceManifest,
    SourceRegistry,
    builtin_sources,
)

__all__ = [
    "TranscriptParser",
    "TranscriptWatcher",
    "SourceManifest",
    "SourceRegistry",
    "builtin_sources",
    "__version__",
]