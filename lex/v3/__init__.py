"""NeuralTape v3 — active pipeline (Fasi 0-2 complete, live since 2026-07-20).

Modules:
- config    : v3 config loader (extends v2.2 config.yaml)
- project   : project identity resolver (Q4=C, config-first + hash fallback)
- redaction : secret redaction before LLM payload (D0.1)
- storage   : SQLite persistent layer for episodes (D0.3)
- events    : EventBus minimale, 2 source types Fase 0 (D0.4)
- cost      : cost/fallback policy for LLM calls (D0.5)
- classifier: layered insight extraction (working/episodic/semantic)
- memory    : layered memory engine
- focus     : current-focus.json generator per project
- workset   : working-set.json generator per project
- resume    : Resume Project renderer (Fase 2)
- handoff   : Agent Handoff bundle (Fase 2)
- markdown_export: mirrors episodes to tape/archive/<category>/

Active via `neural-tape-v3.timer` (run-cron-v3.sh -> run.py run_once).
Activated manually via feature flag `v3.enabled` in config.yaml
or env NEURALTAPE_V3=1. v2.2 (`lex/v22/`) is disabled since 2026-07-20.
"""

from __future__ import annotations

__version__ = "3.2.0"
