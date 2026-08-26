#!/usr/bin/env python3
"""NeuralTape v3 — entry point / orchestrator (Fasi 0-2, active pipeline).

v3 is the live pipeline since 2026-07-20: `neural-tape-v3.timer` invokes
`run-cron-v3.sh` -> `run.py run_once` every 5 minutes. This module provides:
    --selfcheck       smoke test: load config, init storage, verify coexistence.
    --bootstrap ...   delegates to bootstrap_projects.py.
    --status          print current v3 status (config + storage + cost).
    --once <session>  classify one session (used by the cron wrapper).

v2.2 (`neural-tape-v22.timer`) is disabled since 2026-07-20; rollback only.

Usage:
    python lex/v3/run.py --selfcheck
    NEURALTAPE_V3=1 python lex/v3/run.py --status
    NEURALTAPE_V3=1 python lex/v3/run.py --once <session> --project-root <path>
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

THIS_DIR = Path(__file__).resolve().parent          # lex/v3/
TAPE_ROOT = THIS_DIR.parent.parent                  # NeuralTape/

# Ensure lex/v3/ is importable for modules loaded via _load_from_path
# (git.py uses `from events import Event` at runtime).
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

log = logging.getLogger("neural-tape-v3")

CLASSIFIED_EVENT = "transcript.classified"
FAILED_EVENT = "transcript.failed"
# Enough for MAX_CHUNKS classifier windows (6 x 30k). Older content is not
# silently dropped: the window carries an explicit omission marker.
RUN_ONCE_MAX_CHARS = 180000

# Failure-claim policy: after MAX_CLASSIFY_FAILURES failures with no successful
# classification in between, the session is deferred for FAILURE_COOLDOWN_SECONDS
# instead of being retried on every tick. A down LLM endpoint must not consume
# the cron budget forever; a success resets the count.
MAX_CLASSIFY_FAILURES = 3
FAILURE_COOLDOWN_SECONDS = 6 * 3600

# Minimum byte growth required before reprocessing an already-classified session.
# Below this threshold the session is considered stable (closed or idle) and
# is not reclassified, avoiding duplicate episodes and wasted LLM calls.
# Legacy markers (pre-fix) lack the `transcript_bytes` field and default to 0,
# which forces one reclassification to populate the size on first run.
GROWTH_THRESHOLD_BYTES = 2048


@dataclass(frozen=True)
class RunOnceResult:
    session_id: str
    project_id: str
    episodes_written: int
    skipped: bool
    focus_path: Path
    workset_path: Path
    parsed_chars: int
    processed_chars: int
    duration_seconds: float


class TranscriptWatcherProtocol(Protocol):
    def find_all_transcripts(
        self,
        max_age_minutes: int,
    ) -> list[tuple[float, Path]]: ...


class ClassifierProtocol(Protocol):
    def classify_and_persist(
        self,
        transcript_text: str,
        session_id: str,
        project_id: str,
    ) -> int: ...


def _load_sibling(name: str):
    """Load a sibling module of this file (lex/v3/<name>.py)."""
    return _load_from_path(f"nt_v3.{name}", THIS_DIR / f"{name}.py")


def _load_from_path(mod_name: str, path: Path):
    """Load a Python module from an arbitrary file path using importlib."""
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module {mod_name!r} from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_env_file(env_path: Path) -> None:
    """Load simple KEY=value entries without overriding the process env."""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _latest_transcript_window(text: str, *, max_chars: int) -> str:
    """Return at most max_chars from the newest end of a transcript."""
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than zero")
    return text if len(text) <= max_chars else text[-max_chars:]


# Event boundary produced by transcript_parser: "[<ts>] [USER]" /
# "[<ts>] [ASSISTANT]" (markers are persona-neutral; LEX kept for legacy reads).
_EVENT_BOUNDARY_RE = re.compile(
    r"^\[[^\]]+\] \[(?:USER|LEX|ASSISTANT)\]", re.MULTILINE
)


def _budgeted_transcript_window(text: str, *, max_chars: int) -> str:
    """Newest-window projection with an explicit omission marker.

    Instead of cutting mid-content, the window starts at the next event
    boundary and the dropped older content is announced, so the classifier
    knows context was omitted rather than silently truncated.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than zero")
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    window = text[omitted:]
    match = _EVENT_BOUNDARY_RE.search(window)
    if match and match.start() > 0:
        omitted += match.start()
        window = window[match.start():]
    return f"[NeuralTape: ~{omitted} chars of older transcript omitted]\n{window}"


def _failure_backoff_active(
    storage,
    project_id: str,
    session_id: str,
    *,
    now: float,
    max_failures: int = MAX_CLASSIFY_FAILURES,
    cooldown_seconds: float = FAILURE_COOLDOWN_SECONDS,
) -> bool:
    """True when a session failed too many times recently and must wait.

    Failures are counted only after the most recent successful classification,
    so a success resets the claim. A deferred (budget-exhausted) run is not a
    failure and is never recorded as one.
    """
    classified = storage.query_events(
        project_id,
        source_type=CLASSIFIED_EVENT,
        source_ref=session_id,
        limit=1,
    )
    since = classified[0]["captured_at"] if classified else None
    failures = storage.query_events(
        project_id,
        source_type=FAILED_EVENT,
        source_ref=session_id,
        since=since,
        limit=max_failures,
    )
    if len(failures) < max_failures:
        return False
    newest_failure_at = failures[0]["captured_at"]
    return (now - newest_failure_at) < cooldown_seconds


def resolve_transcript(
    session_ref: str,
    *,
    watcher: TranscriptWatcherProtocol | None = None,
    max_age_minutes: int = 10080,
) -> Path:
    """Resolve an exact session id or a unique id prefix to a transcript."""
    direct_path = Path(session_ref).expanduser()
    if direct_path.is_file():
        return direct_path.resolve()

    if watcher is None:
        watcher_mod = _load_sibling("transcript_watcher")
        watcher = watcher_mod.TranscriptWatcher()

    assert watcher is not None
    watcher_mod = _load_sibling("transcript_watcher")
    session_id_of = getattr(
        watcher, "session_id_of", watcher_mod.TranscriptWatcher.get_session_id,
    )
    candidates = watcher.find_all_transcripts(max_age_minutes=max_age_minutes)
    paths = [Path(path) for _, path in candidates]
    exact = [path for path in paths if session_id_of(path) == session_ref]
    if len(exact) == 1:
        return exact[0]

    matches = [path for path in paths if session_id_of(path).startswith(session_ref)]
    if not matches:
        raise FileNotFoundError(
            f"no transcript matches session {session_ref!r} in the last "
            f"{max_age_minutes} minutes"
        )
    if len(matches) > 1:
        choices = ", ".join(session_id_of(path) for path in matches[:5])
        raise ValueError(
            f"ambiguous session prefix {session_ref!r}; matches: {choices}"
        )
    return matches[0]


def run_once(
    transcript_path: Path,
    project_root: Path,
    *,
    tape_root: Path = TAPE_ROOT,
    config_path: Path | None = None,
    classifier_factory: Callable[..., ClassifierProtocol] | None = None,
    max_transcript_chars: int = RUN_ONCE_MAX_CHARS,
) -> RunOnceResult:
    """Classify one transcript and refresh project context idempotently."""
    started_at = time.monotonic()
    transcript_path = Path(transcript_path).resolve()
    project_root = Path(project_root).resolve()
    tape_root = Path(tape_root).resolve()
    if not transcript_path.is_file():
        raise FileNotFoundError(f"transcript not found: {transcript_path}")
    if not project_root.is_dir():
        raise NotADirectoryError(f"project root not found: {project_root}")

    config_mod = _load_sibling("config")
    storage_mod = _load_sibling("storage")
    project_mod = _load_sibling("project")
    redaction_mod = _load_sibling("redaction")
    cost_mod = _load_sibling("cost")
    classifier_mod = _load_sibling("classifier")
    memory_mod = _load_sibling("memory")
    events_mod = _load_sibling("events")
    focus_mod = _load_sibling("focus")
    workset_mod = _load_sibling("workset")
    git_mod = _load_from_path("nt_v3.git_adapter", THIS_DIR / "adapters" / "git.py")
    parser_mod = _load_sibling("transcript_parser")

    cfg = config_mod.load(tape_root, config_path=config_path)
    if not cfg.enabled:
        raise RuntimeError(
            "NeuralTape v3 is disabled. Set NEURALTAPE_V3=1 or v3.enabled=true."
        )

    project = project_mod.ProjectResolver().resolve(project_root)
    storage = storage_mod.Storage(cfg.storage.db_path)
    watcher_mod = _load_sibling("transcript_watcher")
    watcher = watcher_mod.TranscriptWatcher(
        extra_sources=cfg.sources, disabled=cfg.disabled_sources,
    )
    session_id = watcher.session_id_of(transcript_path)

    output_dir = cfg.storage.db_path.parent / "projects" / project.project_id

    # Failure-claim gate: a session that repeatedly fails classification is
    # deferred instead of consuming every tick (see MAX_CLASSIFY_FAILURES).
    if _failure_backoff_active(
        storage, project.project_id, session_id, now=time.time(),
    ):
        log.warning(
            "session %s is in failure backoff; deferring", session_id,
        )
        return RunOnceResult(
            session_id=session_id,
            project_id=project.project_id,
            episodes_written=0,
            skipped=True,
            focus_path=output_dir / "current-focus.json",
            workset_path=output_dir / "working-set.json",
            parsed_chars=0,
            processed_chars=0,
            duration_seconds=time.monotonic() - started_at,
        )

    # Growth-aware idempotency: a session is "already classified" only when an
    # existing marker is present AND the transcript has not grown beyond the
    # threshold since the last classification. This prevents sessions that were
    # classified too early (eps=0 on a short/active snapshot) from being stuck
    # forever, while keeping closed sessions stable.
    try:
        current_size = transcript_path.stat().st_size
    except OSError:
        current_size = 0
    classified_events = storage.query_events(
        project.project_id,
        source_type=CLASSIFIED_EVENT,
        source_ref=session_id,
        limit=5,
    )
    already_classified = False
    if classified_events:
        latest = classified_events[0]
        stored_size = int(latest["payload"].get("transcript_bytes", 0))
        growth = current_size - stored_size
        if growth < 0:
            # Transcript shrank (agent rotated/truncated the session file).
            # Re-process the surviving content; cross-run dedup at persist
            # time prevents duplicate episodes.
            log.warning(
                "session %s shrank by %d bytes since last classification; reprocessing",
                session_id, -growth,
            )
        elif growth < GROWTH_THRESHOLD_BYTES:
            already_classified = True
        else:
            log.info(
                "session %s grew by %d bytes since last classification; reprocessing",
                session_id, growth,
            )
    episodes_written = 0
    parsed_chars = 0
    processed_chars = 0

    if not already_classified:
        transcript_text = parser_mod.TranscriptParser().parse_delta(transcript_path, 0)
        if not transcript_text.strip():
            # No classifiable events (e.g. system-only Grok chats). Record an
            # "empty" classified marker so the session is not retried every
            # tick; growth beyond the threshold re-opens it like any session.
            log.info(
                "session %s has no classifiable events; marking empty", session_id,
            )
            storage.append_event(
                project_id=project.project_id,
                source_type=CLASSIFIED_EVENT,
                source_ref=session_id,
                captured_at=time.time(),
                payload={
                    "episodes_written": 0,
                    "empty": True,
                    "transcript_bytes": current_size,
                },
            )
            return RunOnceResult(
                session_id=session_id,
                project_id=project.project_id,
                episodes_written=0,
                skipped=True,
                focus_path=output_dir / "current-focus.json",
                workset_path=output_dir / "working-set.json",
                parsed_chars=0,
                processed_chars=0,
                duration_seconds=time.monotonic() - started_at,
            )
        parsed_chars = len(transcript_text)
        transcript_text = _budgeted_transcript_window(
            transcript_text,
            max_chars=max_transcript_chars,
        )
        processed_chars = len(transcript_text)

        env_path = os.environ.get("NEURAL_TAPE_ENV")
        if env_path:
            _load_env_file(Path(env_path).expanduser())
        _load_env_file(tape_root / ".env")

        redactor = redaction_mod.Redactor(
            extra_patterns=cfg.redaction.extra_patterns,
            allowlist=cfg.redaction.allowlist,
        )
        policy = cost_mod.CostPolicy(
            budget=cost_mod.CostBudget(
                daily_limit_calls=cfg.cost.daily_limit_calls,
                daily_limit_tokens=cfg.cost.daily_limit_tokens,
            ),
            state_dir=cfg.storage.db_path.parent / ".state",
            fallback_notify_interval_hours=cfg.cost.fallback_notify_interval_hours,
        )
        factory = classifier_factory or classifier_mod.ClassifierV3
        classifier_kwargs: dict = {}
        # When using the real ClassifierV3 (no test factory), mirror every
        # persisted episode to the v2.2-style markdown archive so that
        # pre_load.py / session-context.md see v3 output without changes.
        if classifier_factory is None:
            classifier_kwargs["archive_root"] = tape_root / "tape" / "archive"
        classifier = factory(
            config=cfg,
            project=project,
            storage=storage,
            redactor=redactor,
            cost_policy=policy,
            **classifier_kwargs,
        )
        try:
            episodes_written = classifier.classify_and_persist(
                transcript_text,
                session_id,
                project.project_id,
            )
        except classifier_mod.ClassificationDeferred:
            # Budget exhausted: an intentional defer, not a failure. No marker
            # is written; the session is retried when the budget resets.
            raise
        except Exception as error:
            # Record the failure claim (never a classified marker) so repeated
            # failures eventually put the session in backoff instead of burning
            # every tick while e.g. the LLM endpoint is down.
            storage.append_event(
                project_id=project.project_id,
                source_type=FAILED_EVENT,
                source_ref=session_id,
                captured_at=time.time(),
                payload={"error": str(error)[:500]},
            )
            raise
        # Re-stat after classification in case the transcript grew during the
        # LLM call (active session). We record the post-classification size so
        # the next run only reprocesses if NEW content arrived after this point.
        try:
            final_size = transcript_path.stat().st_size
        except OSError:
            final_size = current_size
        storage.append_event(
            project_id=project.project_id,
            source_type=CLASSIFIED_EVENT,
            source_ref=session_id,
            captured_at=time.time(),
            payload={
                "episodes_written": episodes_written,
                "transcript_bytes": final_size,
            },
        )

    memory_mod.MemoryPromoter(storage=storage, config=cfg).tick(
        project_id=project.project_id,
    )
    event_bus = events_mod.EventBus(
        storage,
        allowed_sources=set(cfg.events.enabled_sources),
    )
    git_adapter = git_mod.GitAdapter(
        project_root=project.root,
        event_bus=event_bus,
        project_id=project.project_id,
    )
    workset_mod.WorkingSetGenerator(
        storage=storage,
        project_root=project.root,
        project_id=project.project_id,
        output_dir=output_dir,
    ).generate()
    focus_mod.FocusGenerator(
        storage=storage,
        git_adapter=git_adapter,
        project=project,
        output_dir=output_dir,
    ).generate()

    # Publish recent git commits to EventBus
    try:
        published_commits = git_adapter.publish_recent_commits()
        if published_commits:
            log.info("published %d git commit events for %s", published_commits, project.project_id)
    except Exception as e:
        log.warning("git commit publishing failed (non-fatal): %s", e)

    # Fase 2: Resume Project renderer + Agent Handoff bundle
    try:
        resume_mod = _load_sibling("resume")
        resume_mod.ResumeProjectRenderer(
            storage=storage,
            git_adapter=git_adapter,
            project_id=project.project_id,
            project_root=project.root,
            output_dir=output_dir,
        ).generate()
    except Exception as e:
        log.warning("resume-project renderer failed (non-fatal): %s", e)

    try:
        handoff_mod = _load_sibling("handoff")
        handoff_mod.AgentHandoffBundle(
            storage=storage,
            git_adapter=git_adapter,
            project_id=project.project_id,
            project_root=project.root,
            output_dir=output_dir,
        ).generate()
    except Exception as e:
        log.warning("agent-handoff bundle failed (non-fatal): %s", e)

    return RunOnceResult(
        session_id=session_id,
        project_id=project.project_id,
        episodes_written=episodes_written,
        skipped=already_classified,
        focus_path=output_dir / "current-focus.json",
        workset_path=output_dir / "working-set.json",
        parsed_chars=parsed_chars,
        processed_chars=processed_chars,
        duration_seconds=time.monotonic() - started_at,
    )


def selfcheck() -> int:
    """Smoke test all v3 components (Fase 0 + Fase 1)."""
    print("NeuralTape v3 - self-check (Fase 0 + Fase 1)")
    print("=" * 55)

    # -- Fase 0 --
    config_mod = _load_sibling("config")
    storage_mod = _load_sibling("storage")
    project_mod = _load_sibling("project")
    redaction_mod = _load_sibling("redaction")
    events_mod = _load_sibling("events")
    cost_mod = _load_sibling("cost")

    # 1. Config load
    cfg = config_mod.load(TAPE_ROOT)
    print(f"[1/10] config loaded - enabled={cfg.enabled}")
    print(f"       storage.db_path = {cfg.storage.db_path}")
    calls = cfg.cost.daily_limit_calls or "unlimited"
    tokens = cfg.cost.daily_limit_tokens or "unlimited"
    print(f"       cost limits     = {calls} calls / {tokens} tokens")

    # 2. Storage init
    storage = storage_mod.Storage(cfg.storage.db_path)
    stats = storage.stats()
    print(f"[2/10] storage OK - db={cfg.storage.db_path.name}, episodes by kind: {stats or 'empty'}")

    # 3. Project resolver
    resolver = project_mod.ProjectResolver()
    self_proj = resolver.resolve(TAPE_ROOT)
    print(f"[3/10] project resolver OK - self={self_proj.project_id} ({self_proj.source})")

    # 4. Redactor
    redactor = redaction_mod.Redactor(
        extra_patterns=cfg.redaction.extra_patterns,
        allowlist=cfg.redaction.allowlist,
    )
    sample = "token=AKIAIOSFODNN7EXAMPLE and password=thisisalongsecret12345"
    events = redactor.redact(sample)[1]
    print(f"[4/10] redactor OK - {len(events)} redaction(s) in sample")

    # 5. EventBus
    bus = events_mod.EventBus(storage, allowed_sources=set(cfg.events.enabled_sources))
    print(f"[5/10] event bus OK - active sources: {sorted(bus.allowed)}")

    # 6. Cost policy
    state_dir = cfg.storage.db_path.parent / ".state"
    policy = cost_mod.CostPolicy(
        budget=cost_mod.CostBudget(
            daily_limit_calls=cfg.cost.daily_limit_calls,
            daily_limit_tokens=cfg.cost.daily_limit_tokens,
        ),
        state_dir=state_dir,
        fallback_notify_interval_hours=cfg.cost.fallback_notify_interval_hours,
    )
    status = policy.status()
    print(f"[6/10] cost policy OK - {status['calls_today']}/{status['calls_limit']} calls today")

    # -- Fase 1 --
    classifier_mod = _load_sibling("classifier")
    memory_mod = _load_sibling("memory")
    focus_mod = _load_sibling("focus")
    workset_mod = _load_sibling("workset")
    # Load git adapter from subdirectory
    git_mod = _load_from_path("nt_v3.git_adapter", THIS_DIR / "adapters" / "git.py")
    GitAdapter = git_mod.GitAdapter

    # 7. ClassifierV3 - instantiation test (no actual LLM call)
    clf = classifier_mod.ClassifierV3(
        config=cfg, project=self_proj, storage=storage,
        redactor=redactor, cost_policy=policy,
        base_url="http://localhost:9999/noop",
        api_key="test-key-0000",
        model="test-model",
    )
    print(f"[7/10] classifier instantiation OK - model={clf.model}")

    # 8. MemoryPromoter - tick (sweeps existing working episodes)
    promoter = memory_mod.MemoryPromoter(storage=storage, config=cfg)
    stats_p = promoter.tick(project_id="neuraltape")
    print(f"[8/10] memory promoter OK - examined={stats_p.examined}")

    # 9. GitAdapter - verify NeuralTape root is a git repo
    git_adapter = GitAdapter(
        project_root=self_proj.root,
        event_bus=bus,
        project_id=self_proj.project_id,
    )
    branch = git_adapter.get_current_branch()
    files = git_adapter.get_recent_files(max_files=5)
    print(f"[9/10] git adapter OK - branch={branch}, recent_files={len(files)}")

    # 10. FocusGenerator + WorkingSetGenerator - instantiation
    focus_dir = cfg.storage.db_path.parent / "focus"
    focus_mod.FocusGenerator(
        storage=storage, git_adapter=git_adapter,
        project=self_proj, output_dir=focus_dir,
    )
    workset_mod.WorkingSetGenerator(
        storage=storage, project_root=self_proj.root,
        project_id=self_proj.project_id, output_dir=focus_dir,
    )
    print(f"[10/10] focus + workset generators instantiated OK")

    # Coexistence check
    v22_state = TAPE_ROOT / "tape" / ".state" / "v22-session-state.json"
    print()
    print(f"v2.2 state file: {v22_state}")
    print(f"  exists: {v22_state.exists()} (v3 does NOT touch this)")
    print(f"v3 feature flag: NEURALTAPE_V3 env = {os.environ.get('NEURALTAPE_V3', '<unset>')}")
    print(f"  -> v3 currently {'ACTIVE' if cfg.enabled else 'DORMANT'}")

    print()
    print("OK self-check passed (Fase 0 + Fase 1).")
    return 0


def status() -> int:
    config_mod = _load_sibling("config")
    cfg = config_mod.load(TAPE_ROOT)
    print(f"v3 enabled          : {cfg.enabled}")
    print(f"v3 db               : {cfg.storage.db_path}")
    calls = cfg.cost.daily_limit_calls or "unlimited"
    tokens = cfg.cost.daily_limit_tokens or "unlimited"
    print(f"cost daily limits   : {calls} calls / {tokens} tokens")
    print(f"events sources      : {cfg.events.enabled_sources}")
    print(f"redaction extras    : {len(cfg.redaction.extra_patterns)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="NeuralTape v3 entry point (Fase 0 + Fase 1)")
    ap.add_argument("--selfcheck", action="store_true", help="smoke test all v3 modules")
    ap.add_argument("--status", action="store_true", help="print current v3 config status")
    ap.add_argument("--once", metavar="SESSION", help="process one transcript id or unique prefix")
    ap.add_argument("--project-root", type=Path, help="explicit project root for --once")
    ap.add_argument("--config", type=Path, help="optional config.yaml path")
    ap.add_argument("--max-age-minutes", type=int, default=10080)
    ap.add_argument("--max-transcript-chars", type=int, default=RUN_ONCE_MAX_CHARS)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if args.selfcheck:
        return selfcheck()
    if args.status:
        return status()
    if args.once:
        if args.project_root is None:
            ap.error("--project-root is required with --once")
        try:
            transcript = resolve_transcript(
                args.once,
                max_age_minutes=args.max_age_minutes,
            )
        except (FileNotFoundError, ValueError) as error:
            log.error("%s", error)
            return 2
        try:
            result = run_once(
                transcript,
                args.project_root,
                config_path=args.config,
                max_transcript_chars=args.max_transcript_chars,
            )
        except Exception as error:
            log.error("v3 once failed: %s", error, exc_info=args.verbose)
            return 1
        state = "skipped" if result.skipped else "classified"
        print(
            f"v3 once: {state} session={result.session_id} "
            f"project={result.project_id} episodes={result.episodes_written} "
            f"chars={result.processed_chars}/{result.parsed_chars} "
            f"duration={result.duration_seconds:.2f}s"
        )
        print(f"focus:   {result.focus_path}")
        print(f"workset: {result.workset_path}")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
