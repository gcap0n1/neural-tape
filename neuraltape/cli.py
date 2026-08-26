"""Console entry point for the ``neuraltape`` command."""

from __future__ import annotations

import sys
from pathlib import Path

_V3 = Path(__file__).resolve().parent.parent / "lex" / "v3"
if str(_V3) not in sys.path:
    sys.path.insert(0, str(_V3))


def main() -> int:
    from lex.v3.run import main as run_main

    return run_main()
