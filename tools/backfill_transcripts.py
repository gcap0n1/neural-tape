#!/usr/bin/env python3
"""backfill_transcripts — one-shot catchup for transcripts outside the cron window.

The 5-minute cron only considers transcripts touched in the last 7 days
(run-cron-v3.sh MAX_AGE_DAYS). A session that sat idle longer (machine off,
timer disabled) would otherwise never be classified. This tool widens the
window on demand; growth-aware markers in run_once keep it idempotent, so it
is safe to re-run.

Usage:
    python tools/backfill_transcripts.py --days 30 --dry-run   # preview only
    python tools/backfill_transcripts.py --days 30 --limit 20  # classify up to 20

Project attribution reuses tools/harvest_sessions.py (workspace hint first,
then content scoring), exactly like the cron wrapper.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Same live-session defer window as run-cron-v3.sh.
ACTIVE_THRESHOLD_SEC = 600
# Same stub filter as run-cron-v3.sh (short sessions ARE classified; empty
# ones receive an "empty" marker and are not retried).
MIN_BYTES = 4096


def _load_harvest():
    spec = importlib.util.spec_from_file_location(
        "harvest_sessions", ROOT / "tools" / "harvest_sessions.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="look back this many days (default: %(default)s)")
    ap.add_argument("--limit", type=int, default=None,
                    help="classify at most N sessions in this run")
    ap.add_argument("--dry-run", action="store_true",
                    help="list candidates without calling the classifier")
    args = ap.parse_args()

    harvest_mod = _load_harvest()
    projects = harvest_mod.discover_projects(harvest_mod.DEFAULT_BASE)
    if not projects:
        print("[error] no projects found under", harvest_mod.DEFAULT_BASE, file=sys.stderr)
        return 2

    plan = harvest_mod.harvest(None, projects, min_bytes=MIN_BYTES)
    plan_by_session = {
        e["session_id"]: e for e in plan
        if e.get("session_id") and e.get("project_id") and e.get("project_root")
    }

    from lex.v3.transcript_watcher import TranscriptWatcher
    watcher = TranscriptWatcher()
    now = time.time()
    candidates: list[tuple[float, int, Path]] = []
    for mtime, tp in watcher.find_all_transcripts(max_age_minutes=args.days * 24 * 60):
        try:
            st = tp.stat()
        except OSError:
            continue
        if st.st_size < MIN_BYTES:
            continue
        if (now - st.st_mtime) < ACTIVE_THRESHOLD_SEC:
            continue  # live session: leave it to the cron idle path
        candidates.append((mtime, st.st_size, tp))
    candidates.sort(reverse=True)

    print(f"[backfill] window={args.days}d candidates={len(candidates)}"
          f"{' (dry-run)' if args.dry_run else ''}")

    if args.dry_run:
        for mtime, size, tp in candidates[: args.limit or len(candidates)]:
            sid = TranscriptWatcher.get_session_id(tp)
            entry = plan_by_session.get(sid) or {}
            print(f"  {sid}  project={entry.get('project_id', '<unknown>'):15} "
                  f"bytes={size:8}  {tp}")
        return 0

    os.environ["NEURALTAPE_V3"] = "1"
    from lex.v3.run import run_once

    processed = 0
    total_eps = 0
    for mtime, size, tp in candidates:
        if args.limit is not None and processed >= args.limit:
            break
        sid = TranscriptWatcher.get_session_id(tp)
        entry = plan_by_session.get(sid) or {}
        project_root = Path(entry.get("project_root", ROOT))
        try:
            res = run_once(transcript_path=tp, project_root=project_root, tape_root=ROOT)
        except Exception as exc:
            print(f"[backfill] {sid}: ERROR {exc}")
            continue
        if not res.skipped:
            processed += 1
        total_eps += res.episodes_written
        print(f"[backfill] {sid}: project={entry.get('project_id', '<unknown>')} "
              f"skipped={res.skipped} eps={res.episodes_written} "
              f"({res.duration_seconds:.1f}s)")

    print(f"[backfill] done: classified={processed} eps_total={total_eps}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
