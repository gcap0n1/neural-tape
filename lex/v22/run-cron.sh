#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TAPE_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON="${PYTHON:-/usr/bin/python}"
STATE_DIR="$TAPE_ROOT/tape/.state"
LOG_FILE="$STATE_DIR/v22-cron.log"
LOCK_FILE="$STATE_DIR/v22.lock"

mkdir -p "$STATE_DIR"
cd "$TAPE_ROOT"

/usr/bin/flock -n "$LOCK_FILE" "$PYTHON" lex/v22/run.py >> "$LOG_FILE" 2>&1 || true