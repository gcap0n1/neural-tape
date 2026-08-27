#!/usr/bin/env bash
# run-cron-v3.sh — NeuralTape v3 cron entry point
#
# Called by systemd timer every 5 minutes.
# Iterates over recent transcripts (not just the latest) so that closed
# sessions left behind by the previous "max(mtime)" logic get backfilled.
# Idempotency is enforced inside run_once via growth-aware markers.

set -euo pipefail

NEURAL_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
export NEURALTAPE_V3=1

if [ -f "$NEURAL_ROOT/.env" ]; then
    set -a
    . "$NEURAL_ROOT/.env"
    set +a
fi

cd "$NEURAL_ROOT"

/usr/bin/python3 << 'PYEOF'
import json, os, subprocess, sys, time
from pathlib import Path

tape_root = Path.cwd()

# Tunables — kept conservative to bound LLM cost per cron tick.
MAX_AGE_DAYS = 7           # only consider transcripts touched in the last week
MIN_BYTES = 4096           # skip only true stubs; short sessions ARE classified.
                           # System-only chats parse to empty text and receive an
                           # "empty" classified marker, so they are not retried.
ACTIVE_THRESHOLD_SEC = 600 # skip transcripts modified in the last 10 min (active session)
MAX_RUNS_PER_TICK = 8      # sessions classified per tick (budget is unlimited)

# Resolve project PER transcript via harvest_sessions heuristic.
# Each transcript may belong to a different project (workspace folder), so we
# cannot reuse a single resolution across all candidates.
plan_by_session: dict[str, dict] = {}
try:
    result = subprocess.run(
        [sys.executable, "tools/harvest_sessions.py", "--limit", "50"],
        capture_output=True, text=True, cwd=tape_root,
    )
    plan = json.loads(result.stdout)
    for entry in plan:
        sid = entry.get("session_id")
        if sid and entry.get("project_id") and entry.get("project_root"):
            plan_by_session[sid] = entry
except (json.JSONDecodeError, IndexError, subprocess.SubprocessError):
    pass  # fall back to tape_root below

# Collect candidate transcripts across VS Code Copilot and Codex stores.
candidates = []
now = time.time()
from neuraltape.v3.transcript_watcher import TranscriptWatcher
from neuraltape.v3.config import load as _load_v3_config

_cfg = _load_v3_config(tape_root)
watcher = TranscriptWatcher(
    extra_sources=_cfg.sources, disabled=_cfg.disabled_sources,
)
for mtime, tp in watcher.find_all_transcripts(max_age_minutes=MAX_AGE_DAYS * 24 * 60):
    try:
        st = tp.stat()
    except OSError:
        continue
    if st.st_size < MIN_BYTES:
        continue
    if (now - st.st_mtime) < ACTIVE_THRESHOLD_SEC:
        continue  # session is live, defer until idle
    candidates.append((mtime, st.st_size, tp))

if not candidates:
    print("[v3-cron] no candidate transcripts (age/size/active filters)")
    sys.exit(0)

# Process newest first. The cap applies to classifications, not candidates:
# already-classified sessions must not starve older unprocessed sessions.
candidates.sort(reverse=True)

print(f"[v3-cron] {len(candidates)} candidate(s)")

sys.path.insert(0, str(tape_root))
os.environ["NEURALTAPE_V3"] = "1"
from neuraltape.v3.run import run_once

total_eps = 0
processed = 0
for mtime, size, tp in candidates:
    if processed >= MAX_RUNS_PER_TICK:
        break
    sid = watcher.session_id_of(tp)
    plan_entry = plan_by_session.get(sid) or {}
    project_root = Path(plan_entry.get("project_root", tape_root))
    project_id_hint = plan_entry.get("project_id", "<unknown>")
    try:
        res = run_once(
            transcript_path=tp,
            project_root=project_root,
            tape_root=tape_root,
        )
    except Exception as exc:  # never let one bad transcript kill the tick
        print(f"[v3-cron] {sid}: ERROR {exc}")
        continue
    total_eps += res.episodes_written
    if not res.skipped:
        processed += 1
    print(
        f"[v3-cron] {sid}: project={project_id_hint} skipped={res.skipped} "
        f"eps={res.episodes_written} ({res.duration_seconds:.1f}s)"
    )

print(f"[v3-cron] tick done: processed={processed} eps_total={total_eps}")
PYEOF
