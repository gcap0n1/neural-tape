#!/usr/bin/env python3
"""Neural Tape v2.2 — cron orchestrator (entry point).

Runs as a one-shot (via cron every 5 min):
    python3 -m neural_tape.lex.v22.run

Flags:
    --dry-run   : parse transcript, print what would be classified, NO LLM/write
    --watch     : continuous loop (dev mode, 60s poll)
    --once ID   : classify a specific transcript by session id (testing)

Pipeline:
    1. TranscriptWatcher.find_active_transcript()  (cheap)
    2. SessionDetector.evaluate()                  (cheap — idle check)
    3. TranscriptParser.parse_delta()              (cheap)
    4. LLMClassifier.classify()                    (THE ONLY EXPENSIVE STEP)
    5. MemoryWriter.write()
    6. Notifier.notify()
    7. SessionDetector.mark_classified()
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# Resolve paths relative to this file so the script works from any cwd
# v22/run.py → lex/v22 → lex → neural-tape → EterCervo
THIS_DIR = Path(__file__).resolve().parent
TAPE_ROOT = THIS_DIR.parent.parent  # neural-tape/
ETERCERVO_ROOT = Path(os.environ.get("ETERCERVO_ROOT", TAPE_ROOT.parent)).expanduser().resolve()
MEMORY_FILE = Path(
    os.environ.get("LEX_MEMORY_FILE", ETERCERVO_ROOT / "_Lex" / "memory.md")
).expanduser().resolve()
STATE_FILE = TAPE_ROOT / "tape" / ".state" / "v22-session-state.json"
LOG_FILE = TAPE_ROOT / "tape" / ".state" / "v22.log"

# The folder is named "neural-tape" (with a hyphen) which is not a valid Python
# identifier, so it cannot be imported as a package. Load sibling modules by path.
import importlib.util


def _load_module(name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(name, file_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_V22_DIR = THIS_DIR
watcher_mod = _load_module("nt_v22.watcher", _V22_DIR / "watcher.py")
detector_mod = _load_module("nt_v22.session_detector", _V22_DIR / "session_detector.py")
parser_mod = _load_module("nt_v22.transcript_parser", _V22_DIR / "transcript_parser.py")
writer_mod = _load_module("nt_v22.memory_writer", _V22_DIR / "memory_writer.py")
notifier_mod = _load_module("nt_v22.notifier", _V22_DIR / "notifier.py")

TranscriptWatcher = watcher_mod.TranscriptWatcher
SessionDetector = detector_mod.SessionDetector
TranscriptParser = parser_mod.TranscriptParser
MemoryWriter = writer_mod.MemoryWriter
Notifier = notifier_mod.Notifier


def setup_logging(verbose: bool = False) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def process_transcript(
    transcript: Path,
    detector: SessionDetector,
    parser: TranscriptParser,
    dry_run: bool,
    notifier: Notifier,
    force: bool = False,
    preview: bool = False,
) -> int:
    """Process a single transcript. Returns exit code (0 = ok)."""
    log = logging.getLogger("neural-tape-v22")
    watcher = TranscriptWatcher()
    workspace_label = watcher.get_workspace_label(transcript)
    session_id = watcher.get_session_id(transcript)

    log.info("Transcript: %s (workspace: %s)", transcript.name, workspace_label)

    # 1. Idle detection
    verdict = detector.evaluate(transcript)
    log.info("Verdict: %s", verdict["reason"])

    if force and verdict["new_bytes"] > 0:
        verdict["classify"] = True
        verdict["reason"] = f"forced classification (+{verdict['new_lines']} lines)"
        log.info("Verdict override: %s", verdict["reason"])

    if not verdict["classify"]:
        return 0  # Not ready — session still active or already classified

    # 2. Parse delta
    text = parser.parse_delta(transcript, verdict["offset"])
    if not text.strip():
        log.info("Empty delta — nothing to classify.")
        detector.mark_classified(transcript)
        return 0

    counts = parser.parse_delta_structured(transcript, verdict["offset"])
    log.info(
        "Delta: %d events (%d user, %d assistant, %d reasoning, %d tools), %d chars",
        counts["total_events"],
        counts["user"],
        counts["assistant"],
        counts["reasoning"],
        counts["tool_calls"],
        len(text),
    )

    if dry_run:
        print("\n=== DRY RUN: transcript text (first 3000 chars) ===")
        print(text[:3000])
        print(f"\n[DRY RUN] Would classify {len(text)} chars. Skipping LLM + write.")
        return 0

    # 3. Classify (the only expensive step)
    try:
        classifier_mod = _load_module("nt_v22.classifier", THIS_DIR / "classifier.py")
        classifier = classifier_mod.LLMClassifier(ETERCERVO_ROOT, TAPE_ROOT)
        log.info("Calling LLM classifier (model: %s)...", classifier.model)
        insights = classifier.classify(text)
    except Exception as e:
        log.error("Classifier unavailable: %s", e)
        notifier.notify("Neural Tape", f"⚠️ Classificatore LLM non disponibile: {e}")
        return 1

    log.info("LLM returned %d insight(s).", len(insights))

    if preview:
        print("\n=== PREVIEW: LLM insights (no write) ===")
        print(json.dumps({"insights": insights}, ensure_ascii=False, indent=2))
        return 0

    # 4. Write
    if insights:
        writer = MemoryWriter(MEMORY_FILE, TAPE_ROOT)
        written = writer.write(insights, workspace_label, session_id)
        notifier.notify(
            "Neural Tape 🧠",
            f"{written} insight(s) catturati dalla sessione {workspace_label}.",
        )
    else:
        log.info("No noteworthy insights in this session.")
        notifier.notify("Neural Tape", f"Sessione {workspace_label} analizzata, nessun insight degno.")

    # 5. Mark classified (advance offset)
    detector.mark_classified(transcript)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Neural Tape v2.2 cron entry")
    parser.add_argument("--dry-run", action="store_true", help="parse only, no LLM/write")
    parser.add_argument("--preview", action="store_true", help="call LLM and print insights, no write")
    parser.add_argument("--watch", action="store_true", help="continuous mode (dev, 60s poll)")
    parser.add_argument("--once", metavar="SESSION_ID", help="classify a specific transcript by id")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    log = logging.getLogger("neural-tape-v22")
    log.info("═══ Neural Tape v2.2 run started ═══")

    detector = SessionDetector(STATE_FILE)
    transcript_parser = TranscriptParser()
    notifier = Notifier()

    try:
        watcher = TranscriptWatcher()
    except RuntimeError as e:
        log.error("%s", e)
        return 1

    # Specific session (--once for testing)
    if args.once:
        transcripts = watcher.find_all_transcripts(max_age_minutes=60 * 24 * 7)
        target = next((t for _, t in transcripts if args.once in t.stem), None)
        if not target:
            log.error("Transcript containing '%s' not found.", args.once)
            return 1
        # Force classify: reset its offset to 0 first
        state = detector.load_state()
        key = str(target)
        if key in state:
            log.info("Resetting offset for --once test")
            del state[key]
            detector.save_state(state)
        return process_transcript(
            target,
            detector,
            transcript_parser,
            args.dry_run,
            notifier,
            force=True,
            preview=args.preview,
        )

    # Normal mode: find the active transcript
    if args.watch:
        log.info("Watch mode: polling every 60s. Ctrl+C to stop.")
        try:
            while True:
                transcript = watcher.find_active_transcript(max_age_minutes=60)
                if transcript:
                    process_transcript(
                        transcript,
                        detector,
                        transcript_parser,
                        args.dry_run,
                        notifier,
                        preview=args.preview,
                    )
                else:
                    log.debug("No active transcript.")
                time.sleep(60)
        except KeyboardInterrupt:
            log.info("Watch stopped.")
        return 0

    # One-shot (cron mode)
    transcript = watcher.find_active_transcript(max_age_minutes=60)
    if not transcript:
        log.info("No active transcript in last 60 min. Idle.")
        return 0

    return process_transcript(
        transcript,
        detector,
        transcript_parser,
        args.dry_run,
        notifier,
        preview=args.preview,
    )


if __name__ == "__main__":
    sys.exit(main())
