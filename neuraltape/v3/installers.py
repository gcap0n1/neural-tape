"""installers — idempotent hook installers/uninstallers per agent (Fase 3, item 3).

Design constraints (honest engineering):
- Only formats we can verify offline get written. Claude Code
  ``settings.json`` hooks is the one stable, well-documented JSON schema
  (events SessionStart / UserPromptSubmit / ... per the agentmemory study);
  it is the only full writer today.
- Every other agent gets a SNIPPET-ONLY report: exact command + event name
  to paste manually. We refuse to invent config syntax for formats we have
  never seen (Kimi TOML, Grok bundle, opencode plugin, Reasonix settings).
- All writes are guarded by ``apply``; dry-run is the default. Existing
  files are backed up to ``<file>.bak-<epoch>`` before the first
  modification. Idempotency: entries containing the MARKER are never
  duplicated; uninstall removes exactly those entries.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .inject import plan_for

MARKER = "neuraltape hook-inject"

# Agents whose config we know how to merge today.
JSON_HOOKS_AGENTS = {"claude"}

DEFAULT_PATHS = {
    "claude": ".claude/settings.json",
    "reasonix": ".reasonix/settings.json",
    "kimi": ".kimi/config.toml",
}


@dataclass
class InstallReport:
    agent: str
    path: str | None
    action: str            # wrote | merged | would-write | would-merge | snippet-only | removed | would-remove
    backup: str | None = None
    snippet: str | None = None
    notes: str = ""

    def as_dict(self) -> dict:
        return {"agent": self.agent, "path": self.path,
                "action": self.action, "backup": self.backup,
                "snippet": self.snippet, "notes": self.notes}


def _command(agent: str, project: str, db_path: str | None) -> str:
    plan = plan_for(agent, project, db_path or "")
    if plan.command is None:
        raise ValueError(f"agent {agent!r} has no shell command surface")
    return plan.command


def _event_for(agent: str) -> str:
    return plan_for(agent, "", "").event


def _json_hooks_entry(agent: str, project: str, db_path: str | None) -> dict:
    return {"matcher": "",
            "hooks": [{"type": "command",
                       "command": _command(agent, project, db_path)}]}


def _entry_present(root: dict, event: str, cmd: str) -> bool:
    for group in (root.get("hooks", {}) or {}).get(event, []) or []:
        for hook in group.get("hooks", []) or []:
            if MARKER in (hook.get("command") or ""):
                return True
    return False


def _remove_entries(root: dict, event: str) -> int:
    removed = 0
    groups = (root.get("hooks", {}) or {}).get(event, []) or []
    kept = []
    for group in groups:
        hooks = group.get("hooks", []) or []
        matched = [h for h in hooks if MARKER in (h.get("command") or "")]
        remaining = [h for h in hooks if h not in matched]
        removed += len(matched)
        if remaining:
            group = dict(group)
            group["hooks"] = remaining
            kept.append(group)
    if groups:
        root.setdefault("hooks", {})[event] = kept
    return removed


def _backup(path: Path) -> str | None:
    if not path.exists():
        return None
    dst = path.with_name(path.name + f".bak-{int(time.time())}")
    shutil_copy(path, dst)
    return str(dst)


def shutil_copy(src: Path, dst: Path) -> None:
    import shutil
    shutil.copyfile(src, dst)


def install(agent: str, project: str, db_path: str | None = None,
            *, home: Path | None = None, apply: bool = False) -> InstallReport:
    """Install the hook entry for one agent. Dry-run unless apply=True."""
    home = Path(home) if home else Path.home()
    plan = plan_for(agent, project, db_path or "")

    if agent not in JSON_HOOKS_AGENTS or plan.command is None:
        snippet = plan.command or plan.notes
        return InstallReport(
            agent=agent, path=None, action="snippet-only", snippet=snippet,
            notes=(f"formato non verificabile offline ({plan.event}): incolla "
                   f"lo snippet nel config dell'agente. Event: {plan.event}."),
        )

    path = home / DEFAULT_PATHS[agent]
    root: dict = {}
    existed = path.exists()
    if existed:
        try:
            root = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(root, dict):
                raise ValueError("root is not a JSON object")
        except (ValueError, OSError) as exc:
            return InstallReport(
                agent=agent, path=str(path), action="snippet-only",
                snippet=json.dumps(_json_hooks_entry(agent, project, db_path)),
                notes=f"config esistente non leggibile/mergiabile: {exc}",
            )

    event = _event_for(agent)
    cmd = _command(agent, project, db_path)
    if _entry_present(root, event, cmd):
        return InstallReport(agent=agent, path=str(path), action="merged",
                             notes="entry già presente (idempotente)")

    root.setdefault("hooks", {}).setdefault(event, []).append(
        _json_hooks_entry(agent, project, db_path))

    if not apply:
        return InstallReport(agent=agent, path=str(path),
                             action="would-merge",
                             notes="dry-run: rilancia con --apply")

    backup = _backup(path) if existed else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(root, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return InstallReport(agent=agent, path=str(path),
                         action="wrote" if not existed else "merged",
                         backup=backup,
                         notes="hook installato con marker idempotente")


def uninstall(agent: str, *, home: Path | None = None,
              apply: bool = False) -> InstallReport:
    """Remove NeuralTape entries (matched by MARKER). Dry-run by default."""
    home = Path(home) if home else Path.home()
    if agent not in JSON_HOOKS_AGENTS:
        return InstallReport(
            agent=agent, path=None, action="snippet-only",
            notes=("rimozione manuale: l'installer di questo agente è "
                   "snippet-only e non scrive config."))

    path = home / DEFAULT_PATHS[agent]
    if not path.exists():
        return InstallReport(agent=agent, path=str(path),
                             action="would-remove",
                             notes="config assente: nulla da rimuovere")
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return InstallReport(agent=agent, path=str(path),
                             action="snippet-only",
                             notes=f"config illeggibile: {exc}")

    event = _event_for(agent)
    removed = _remove_entries(root, event)
    if removed == 0:
        return InstallReport(agent=agent, path=str(path), action="merged",
                             notes="nessuna entry NeuralTape presente")

    if not apply:
        return InstallReport(agent=agent, path=str(path),
                             action="would-remove",
                             notes=f"dry-run: {removed} entry da rimuovere")

    backup = _backup(path)
    path.write_text(json.dumps(root, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return InstallReport(agent=agent, path=str(path), action="removed",
                         backup=backup,
                         notes=f"{removed} entry rimossa/e")
