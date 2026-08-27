#!/usr/bin/env python3
"""harvest_sessions — list assistant transcripts and assign a v3 project_id.

Deterministic path-based heuristic. NO LLM cost. Used for the v3 exit-criteria
validation campaign (10 sessions on >=2 projects).

Strategy (priority order):
    1. Workspace hint: the agent's own workspace label (Kimi `wd_<folder>`,
       Codex session cwd, Grok cwd dir, Copilot workspace.json folder). If it
       equals a known project's folder basename, that project wins outright —
       the session was opened there, which outranks content references.
    2. Otherwise parse each transcript JSONL and count occurrences of known
       project roots in the message content (file paths, commands). The project
       with the most distinct path references wins.
    3. Fallback: 'unknown' if no project root is mentioned.

Project roots come from the .neuraltape/project.yaml files under DEFAULT_BASE.

Output: a JSON plan consumable by validate_sessions.py:
    [
      {"session_id": "...", "transcript_path": "...", "project_id": "myworkspace",
       "project_root": "/path/to/MyWorkspace", "score": {project: count},
       "bytes": 123456, "mtime_iso": "2026-07-18..."},
      ...
    ]

Usage:
    python tools/harvest_sessions.py --min-bytes 50000 > validation-plan.json
    python tools/harvest_sessions.py --limit 10 --pretty
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Direct execution (`python tools/harvest_sessions.py`) puts only tools/ on
# sys.path; add the repository root so the shared v3 parser/watcher import.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from neuraltape.v3.project import default_projects_base  # noqa: E402

# Same default base as bootstrap_projects.py (env / external disk / $HOME).
DEFAULT_BASE = default_projects_base()
DEFAULT_TRANSCRIPTS_GLOB = None

# Roots that are NOT project workspaces (exclude from heuristic).
EXCLUDE_DIRS = {".venv", "node_modules", "__pycache__", ".git"}


def discover_projects(base: Path) -> dict[str, Path]:
    """Find every workspace root that has a .neuraltape/project.yaml.

    Returns {project_id: root_path}.
    """
    projects: dict[str, Path] = {}
    for cfg in sorted(base.glob("*/.neuraltape/project.yaml")):
        try:
            import yaml
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            pid = str(data.get("project_id", "")).strip()
            if pid and re.match(r"^[a-z0-9][a-z0-9-]{0,31}$", pid):
                projects[pid] = cfg.parent.parent.resolve()
        except Exception as e:
            print(f"[warn] cannot read {cfg}: {e}", file=sys.stderr)
    return projects


def extract_text_from_transcript(path: Path, max_bytes: int = 4_000_000) -> str:
    """Read a Copilot or Codex transcript as classifier-friendly text.

    VS Code Copilot transcript schema (verified 2026-07-18):
        {"type": "user.message" | "assistant.message" | ...,
         "data": {"content": "...", "reasoningText": "...", "toolRequests": [...]},
         "id": ..., "timestamp": ..., "parentId": ...}

    Parsing is shared with the live v3 classifier so source support cannot drift.
    The full file is parsed to preserve Codex ``session_meta.cwd`` near the
    beginning; high-volume fields are bounded by the parser itself.
    """
    from neuraltape.v3.transcript_parser import TranscriptParser

    # The parser already truncates each high-volume field. Keeping the whole
    # JSONL preserves Codex session_meta.cwd, which is normally near the start.
    # max_bytes remains in the signature for CLI/API compatibility.
    _ = max_bytes
    return TranscriptParser().parse_delta(path)


def score_projects(text: str, projects: dict[str, Path]) -> dict[str, int]:
    """Count occurrences of each project root (and its basename) in text.

    Counts both the absolute path and the bare basename, but gives higher
    weight to absolute path hits (more reliable signal).
    """
    scores: dict[str, int] = defaultdict(int)
    lowered = text.lower()
    for pid, root in projects.items():
        root_str = str(root)
        name = root.name.lower()
        # Absolute path occurrences (weight 3).
        abs_hits = text.count(root_str)
        # Basename occurrences as a standalone token (weight 1) — only when
        # preceded by a separator to avoid substring false positives.
        name_hits = len(re.findall(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", lowered))
        # Strip leading 'auto-' fallback project_ids from matching (they look
        # like hashes and never appear in transcripts).
        if pid.startswith("auto-"):
            continue
        total = abs_hits * 3 + name_hits
        if total > 0:
            scores[pid] = total
    return dict(scores)


def assign_project(scores: dict[str, int]) -> str | None:
    """Pick the winning project_id from scores. None if no signal."""
    if not scores:
        return None
    # Highest score wins; ties broken by alphabetical order (deterministic).
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def harvest(
    transcripts_glob: str | None,
    projects: dict[str, Path],
    *,
    min_bytes: int = 0,
    limit: int | None = None,
) -> list[dict]:
    """Discover, score, and return the validation plan."""
    # Expand and deduplicate transcripts.
    from neuraltape.v3.transcript_watcher import TranscriptWatcher
    watcher = TranscriptWatcher()
    if transcripts_glob:
        raw_paths = [Path(p) for p in _glob(transcripts_glob)]
    else:
        raw_paths = [
            path for _, path in watcher.find_all_transcripts(
                max_age_minutes=10 * 365 * 24 * 60,
            )
        ]
    paths = sorted({p.expanduser().resolve() for p in raw_paths if p.is_file()})
    if min_bytes > 0:
        paths = [p for p in paths if p.stat().st_size >= min_bytes]
    # Most recent first (validation campaign cares about recent sessions).
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if limit:
        paths = paths[:limit]

    plan: list[dict] = []
    for path in paths:
        hint = workspace_hint_project(path, watcher, projects)
        if hint is not None:
            winner = hint
            scores: dict[str, int] = {}
        else:
            text = extract_text_from_transcript(path)
            scores = score_projects(text, projects)
            winner = assign_project(scores)
        entry = {
            "session_id": TranscriptWatcher.get_session_id(path),
            "transcript_path": str(path),
            "project_id": winner,
            "project_root": str(projects[winner]) if winner else None,
            "hint": f"workspace:{hint}" if hint is not None else None,
            "score": dict(sorted(scores.items(), key=lambda kv: -kv[1])),
            "bytes": path.stat().st_size,
            "mtime_iso": _iso(path.stat().st_mtime),
        }
        plan.append(entry)
    return plan


def workspace_hint_project(
    path: Path,
    watcher,
    projects: dict[str, Path],
) -> str | None:
    """Strong attribution hint from the agent's own workspace label.

    Kimi transcripts live under ``wd_<folder>_<hash>``, Codex/Grok expose the
    session cwd, Copilot records the workspace folder. When that label equals
    a known project's folder basename, the session was opened in that project
    — a stronger signal than content scoring (which can lose on cross-project
    sessions that mostly talk *about* another repo).
    """
    try:
        label = watcher.get_workspace_label(path)
    except Exception:
        return None
    label_norm = label.strip().lower()
    if not label_norm or label_norm == "unknown":
        return None
    for pid, root in projects.items():
        if root.name.lower() == label_norm:
            return pid
    return None


def _glob(pattern: str) -> list[str]:
    """Glob that supports ~ and returns paths."""
    from glob import glob
    return glob(Path(pattern).expanduser().as_posix())


def _iso(epoch: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=str(DEFAULT_BASE),
                    help="base dir containing workspace folders (default: %(default)s)")
    ap.add_argument(
        "--glob",
        default=DEFAULT_TRANSCRIPTS_GLOB,
        help="optional transcript glob; default discovers Copilot, Codex, Kimi and Grok stores",
    )
    ap.add_argument("--min-bytes", type=int, default=50_000,
                    help="skip tiny transcripts (default: 50KB)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap the plan to N most recent sessions")
    ap.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    args = ap.parse_args()

    projects = discover_projects(Path(args.base))
    if not projects:
        print("[error] no projects found under", args.base, file=sys.stderr)
        return 2

    print(f"[info] discovered {len(projects)} projects: {sorted(projects)}", file=sys.stderr)

    plan = harvest(
        args.glob, projects,
        min_bytes=args.min_bytes, limit=args.limit,
    )

    # Summary on stderr.
    counts: Counter = Counter(e["project_id"] or "unknown" for e in plan)
    print(f"[info] {len(plan)} sessions planned. Distribution:", file=sys.stderr)
    for pid, n in counts.most_common():
        print(f"   {pid:20} {n}", file=sys.stderr)

    indent = 2 if args.pretty else None
    print(json.dumps(plan, indent=indent, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
