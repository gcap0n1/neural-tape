#!/usr/bin/env python3
"""Backfill archive/*.md frontmatter to the standardized schema (2026-07-18).

Idempotent migration: adds the missing fields the pre_load.py reader expects,
without touching existing fields or body content.

Fields added (only if missing):
    timestamp  — ISO-8601 derived from `date` (midnight local TZ). If the file
                 already has `timestamp`, it is preserved.
    project    — normalized from `workspace` (e.g. 'EterCervo-Workspace.code-workspace'
                 -> 'EterCervo'). If the file already has `project`, preserved.
    confidence — default 'medium' (v2.2 does not score confidence yet).
    assistant  — default 'lex' (v2.2 only classifies Lex transcripts today).

Usage:
    python tools/migrate_archive_schema.py --dry-run
    python tools/migrate_archive_schema.py
    python tools/migrate_archive_schema.py --archive-dir /custom/path
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

# Match YAML frontmatter block delimited by '---' lines.
_FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)

WORKSPACE_SUFFIXES = (
    "-Workspace.code-workspace",
    ".code-workspace",
    ".code.json",
)


def normalize_project(workspace_or_project: str) -> str:
    """Normalize 'EterCervo-Workspace.code-workspace' -> 'EterCervo'."""
    if not workspace_or_project:
        return "default"
    name = str(workspace_or_project)
    for suffix in WORKSPACE_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name or "default"


def parse_frontmatter(text: str) -> Optional[Dict[str, str]]:
    """Parse a flat YAML frontmatter (key: value) into a dict.

    Returns None if no frontmatter is present. Only handles simple scalar keys,
    which is all v2.2 ever writes.
    """
    m = _FM_RE.match(text)
    if not m:
        return None
    fm: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fm[key.strip()] = value.strip()
    return fm


def build_new_frontmatter(fm: Dict[str, str]) -> str:
    """Return the new frontmatter block (between --- lines, no trailing newline)."""
    # Order: stable canonical order.
    canonical = [
        "type",
        "date",
        "timestamp",
        "project",
        "workspace",
        "session",
        "confidence",
        "assistant",
        "status",
        "source",
    ]
    lines = []
    seen = set()
    for key in canonical:
        if key in fm:
            lines.append(f"{key}: {fm[key]}")
            seen.add(key)
    # Preserve any extra keys we didn't list (forward-compat).
    for k, v in fm.items():
        if k not in seen:
            lines.append(f"{k}: {v}")
    return "\n".join(lines)


def derive_timestamp(date_str: str) -> Optional[str]:
    """From 'YYYY-MM-DD' build an ISO-8601 timestamp at local midnight with TZ."""
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None
    return d.astimezone().isoformat(timespec="seconds")


def migrate_file(path: Path, dry_run: bool) -> Dict[str, str]:
    """Return a report dict: status=ok|skipped|error + before/after summary."""
    report: Dict[str, str] = {"path": str(path), "status": "skipped", "changes": ""}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        report["status"] = "error"
        report["changes"] = f"read-error: {e}"
        return report

    fm = parse_frontmatter(text)
    if fm is None:
        report["status"] = "skipped"
        report["changes"] = "no-frontmatter"
        return report

    added = []
    if "timestamp" not in fm:
        ts = derive_timestamp(fm.get("date", ""))
        if ts:
            fm["timestamp"] = ts
            added.append("timestamp")

    if "project" not in fm:
        workspace = fm.get("workspace", "")
        if workspace:
            fm["project"] = normalize_project(workspace)
            added.append("project")

    if "confidence" not in fm:
        fm["confidence"] = "medium"
        added.append("confidence")

    if "assistant" not in fm:
        # Legacy v2.2 archives are all Lex transcripts (cron reads VS Code Copilot).
        fm["assistant"] = "lex"
        added.append("assistant")

    if not added:
        report["status"] = "skipped"
        report["changes"] = "already-conformant"
        return report

    new_fm = build_new_frontmatter(fm)
    m = _FM_RE.match(text)
    assert m is not None  # parse_frontmatter already validated this
    new_text = f"---\n{new_fm}\n---\n" + text[m.end():]

    report["status"] = "ok" if not dry_run else "dry-run"
    report["changes"] = "+".join(added)
    if not dry_run:
        path.write_text(new_text, encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--archive-dir",
        default=str(here / "tape" / "archive"),
        help="Path to tape/archive (default: <repo>/tape/archive)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only, do not write")
    args = parser.parse_args()

    archive_dir = Path(args.archive_dir)
    if not archive_dir.is_dir():
        print(f"[error] archive dir not found: {archive_dir}", file=sys.stderr)
        return 2

    files = sorted(archive_dir.glob("*/*.md"))
    if not files:
        print(f"[info] no .md files under {archive_dir}")
        return 0

    counters = {"ok": 0, "skipped": 0, "dry-run": 0, "error": 0}
    for f in files:
        report = migrate_file(f, args.dry_run)
        counters[report["status"]] = counters.get(report["status"], 0) + 1
        print(f"  [{report['status']:7}] {f.name}  ({report['changes']})")

    label = "DRY-RUN" if args.dry_run else "DONE"
    print(
        f"\n[{label}] {len(files)} files | "
        f"ok={counters.get('ok', 0)} skipped={counters.get('skipped', 0)} "
        f"dry-run={counters.get('dry-run', 0)} errors={counters.get('error', 0)}"
    )
    return 0 if counters.get("error", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
