"""inject — per-agent handoff injection plans (Fase 3, slice 2).

The handoff bundle is single-use (see handoff.py). Each coding agent has a
different injection surface; the contracts here come from the v4 roadmap
(decision Guglielmo 2026-08-21) and are kept intentionally thin — the actual
config-file installers are Fase 3 item 3.

Contracts:
- Kimi  : hook on UserPromptSubmit (Kimi IGNORES SessionStart stdout).
- Grok  : no shell hook — consumes via the MCP `handoff` tool.
- Codex : finalize-session surface; the adapter prints the pending bundle
          with consumer=codex so the next-session handoff gets written.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class InjectionPlan:
    agent: str
    event: str
    command: str | None
    notes: str


def _quote(value: str) -> str:
    return shlex.quote(str(value))


def kimi_plan(project: str, db_path: str) -> InjectionPlan:
    cmd = (f"neuraltape hook-inject --agent kimi --project "
           f"{_quote(project)} --db {_quote(db_path)}")
    return InjectionPlan(
        agent="kimi", event="UserPromptSubmit", command=cmd,
        notes="stdout viene anteposto al prompt (SessionStart ignorato); "
              "consumo automatico del bundle pending.",
    )


def grok_plan(project: str, db_path: str) -> InjectionPlan:
    return InjectionPlan(
        agent="grok", event="MCP tools/call", command=None,
        notes=("niente shell: chiama il tool MCP 'handoff' con "
               f"project={project!r} e consume=true; il server giusto è "
               f"'neuraltape serve --db {_quote(db_path)}'."),
    )


def codex_plan(project: str, db_path: str) -> InjectionPlan:
    cmd = (f"neuraltape hook-inject --agent codex --project "
           f"{_quote(project)} --db {_quote(db_path)}")
    return InjectionPlan(
        agent="codex", event="finalize-session", command=cmd,
        notes="l'output diventa l'handoff per la sessione successiva; "
              "consumo con consumer=codex.",
    )


_AGENTS = {"kimi": kimi_plan, "grok": grok_plan, "codex": codex_plan}


def plan_for(agent: str, project: str, db_path: str) -> InjectionPlan:
    try:
        return _AGENTS[agent](project, db_path)
    except KeyError:
        raise ValueError(f"unknown agent {agent!r}; expected one of "
                         f"{sorted(_AGENTS)}") from None
