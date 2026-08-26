#!/usr/bin/env python3
"""validate_sessions — run v3 classifier on N historical sessions, collect metrics.

Reads a harvest plan (from harvest_sessions.py) and runs lex/v3/run.py:run_once
on each entry, then aggregates:

    - episodes_written per session / per project
    - layer distribution (working/episodic/semantic)
    - category distribution
    - mean confidence
    - duration per session
    - failures (deferred / errors)

This is the harness for v3 exit-criteria #1 (validate 10 historical sessions on
>=2 projects). It does NOT touch v2.2 cron.

Usage:
    NEURALTAPE_V3=1 python tools/validate_sessions.py /tmp/validation-plan-final.json
    NEURALTAPE_V3=1 python tools/validate_sessions.py plan.json --reset-db
    NEURALTAPE_V3=1 python tools/validate_sessions.py plan.json --only zeus
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

NT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(NT_ROOT))

from lex.v3.run import run_once  # noqa: E402
from lex.v3 import storage as storage_mod  # noqa: E402
from lex.v3 import config as config_mod  # noqa: E402


log = logging.getLogger("nt-v3-validate")


def reset_db(db_path: Path) -> None:
    """Drop episodes/event_log/focus history for a clean run."""
    if not db_path.exists():
        return
    con = sqlite3.connect(db_path)
    con.executescript(
        "DELETE FROM episodes;"
        "DELETE FROM event_log;"
        "DELETE FROM focus_history;"
    )
    con.commit()
    con.close()
    print(f"[reset] wiped episodes/event_log/focusHistory in {db_path}")


def run_plan(plan_path: Path, *, only_project: str | None = None) -> list[dict]:
    """Execute the plan. Returns a list of result dicts (one per session)."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    print(f"[plan] {len(plan)} sessions")

    cfg = config_mod.load(NT_ROOT)
    if not cfg.enabled:
        print("[error] v3 disabled. Set NEURALTAPE_V3=1", file=sys.stderr)
        sys.exit(2)

    results: list[dict] = []
    for i, entry in enumerate(plan, 1):
        sid = entry["session_id"]
        pid = entry.get("project_id")
        if not pid:
            print(f"[{i:>2}/{len(plan)}] {sid[:8]} SKIP (no project_id)")
            results.append({"session_id": sid, "status": "skipped-no-project", **entry})
            continue
        if only_project and pid != only_project:
            continue

        transcript = Path(entry["transcript_path"])
        project_root = Path(entry["project_root"])
        kb = entry["bytes"] // 1024
        print(f"[{i:>2}/{len(plan)}] {sid[:8]} → {pid:12} ({kb}KB) ", end="", flush=True)

        t0 = time.monotonic()
        try:
            res = run_once(
                transcript_path=transcript,
                project_root=project_root,
                tape_root=NT_ROOT,
                config_path=NT_ROOT / "config.yaml",
            )
        except Exception as e:
            print(f"ERROR ({type(e).__name__}: {e})")
            log.exception("run_once failed for %s", sid)
            results.append({
                "session_id": sid, "project_id": pid, "status": "error",
                "error": f"{type(e).__name__}: {e}", "duration": time.monotonic() - t0,
                **entry,
            })
            continue

        status = "skipped-already" if res.skipped else "classified"
        print(f"{status} eps={res.episodes_written} focus+ws written "
              f"({res.duration_seconds:.1f}s)")
        results.append({
            "session_id": sid, "project_id": pid, "status": status,
            "episodes_written": res.episodes_written,
            "skipped": res.skipped,
            "focus_path": str(res.focus_path),
            "workset_path": str(res.workset_path),
            "parsed_chars": res.parsed_chars,
            "processed_chars": res.processed_chars,
            "duration": res.duration_seconds,
            **entry,
        })
    return results


def summarize(results: list[dict], db_path: Path) -> dict:
    """Aggregate metrics from results + DB."""
    summary = {
        "total": len(results),
        "by_status": dict(Counter(r["status"] for r in results)),
        "by_project": dict(Counter(r.get("project_id") or "?" for r in results)),
        "episodes_written_total": sum(r.get("episodes_written", 0) for r in results),
        "episodes_written_by_project": dict(
            Counter({}.get("project_id", "?") for r in results if r.get("episodes_written"))
        ),
        "mean_duration_seconds": (
            sum(r.get("duration", 0) for r in results) /
            max(1, sum(1 for r in results if "duration" in r))
        ),
    }

    # Pull episode details from DB.
    con = sqlite3.connect(db_path)
    by_kind: Counter = Counter()
    by_category: Counter = Counter()
    by_project_kind: dict[str, Counter] = defaultdict(Counter)
    confidences: list[float] = []
    for row in con.execute(
        "SELECT project_id, kind, category, confidence FROM episodes"
    ):
        proj, kind, cat, conf = row
        by_kind[kind] += 1
        if cat:
            by_category[cat] += 1
        by_project_kind[proj][kind] += 1
        confidences.append(conf)
    con.close()

    summary["episodes_by_kind"] = dict(by_kind)
    summary["episodes_by_category"] = dict(by_category)
    summary["episodes_by_project_kind"] = {
        p: dict(c) for p, c in by_project_kind.items()
    }
    summary["mean_confidence"] = (
        sum(confidences) / len(confidences) if confidences else 0.0
    )
    summary["median_confidence"] = (
        sorted(confidences)[len(confidences) // 2] if confidences else 0.0
    )
    return summary


def print_report(results: list[dict], summary: dict) -> None:
    print()
    print("=" * 70)
    print("VALIDATION REPORT")
    print("=" * 70)
    print(f"Sessions:        {summary['total']}")
    print(f"Status:          {summary['by_status']}")
    print(f"Projects:        {summary['by_project']}")
    print(f"Episodes total:  {summary['episodes_written_total']}")
    print(f"Mean duration:   {summary['mean_duration_seconds']:.2f}s")
    print(f"Mean confidence: {summary['mean_confidence']:.2f} "
          f"(median {summary['median_confidence']:.2f})")
    print()
    print("Episodes by kind:")
    for k, n in sorted(summary["episodes_by_kind"].items()):
        print(f"  {k:12} {n}")
    print()
    print("Episodes by category:")
    for k, n in sorted(summary["episodes_by_category"].items(), key=lambda kv: -kv[1]):
        print(f"  {k:14} {n}")
    print()
    print("Episodes by project x kind:")
    for p in sorted(summary["episodes_by_project_kind"]):
        kinds = summary["episodes_by_project_kind"][p]
        flat = ", ".join(f"{k}={n}" for k, n in sorted(kinds.items()))
        print(f"  {p:14} {flat}")
    print()
    print("Per-session detail:")
    for r in results:
        sid = r["session_id"][:8]
        pid = r.get("project_id") or "?"
        status = r["status"]
        eps = r.get("episodes_written", 0)
        dur = r.get("duration", 0)
        print(f"  {sid} {pid:14} {status:18} eps={eps:<3} {dur:.1f}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("plan", help="JSON plan from harvest_sessions.py")
    ap.add_argument("--reset-db", action="store_true",
                    help="wipe episodes/event_log/focusHistory before running")
    ap.add_argument("--only", help="filter to a single project_id")
    ap.add_argument("--output", help="write JSON report to this path")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cfg = config_mod.load(NT_ROOT)
    if args.reset_db:
        reset_db(cfg.storage.db_path)

    results = run_plan(Path(args.plan), only_project=args.only)
    summary = summarize(results, cfg.storage.db_path)
    print_report(results, summary)

    if args.output:
        Path(args.output).write_text(
            json.dumps({"results": results, "summary": summary},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\n[written] {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
