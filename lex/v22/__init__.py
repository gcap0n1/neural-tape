"""Neural Tape v2.2 — Automatic Copilot transcript classifier.

Reads VS Code Copilot transcripts from disk, classifies insights via LLM,
and writes to _Lex/memory.md + tape/archive/. Zero user action required.

Usage:
    python -m neural_tape.lex.v22.run             # one-shot cron entry
    python -m neural_tape.lex.v22.run --dry-run   # parse only, no LLM/write
    python -m neural_tape.lex.v22.run --watch     # continuous (dev)
"""

__version__ = "2.2.0"
