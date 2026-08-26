"""Test per l'orchestratore one-shot di NeuralTape v3."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from nt_v3.run import (
    _budgeted_transcript_window,
    _latest_transcript_window,
    resolve_transcript,
    run_once,
)
from nt_v3.storage import Episode, Storage


class FakeClassifier:
    calls = 0

    def __init__(self, *, storage: Storage, **_kwargs):
        self.storage = storage

    def classify_and_persist(
        self,
        transcript_text: str,
        session_id: str,
        project_id: str,
    ) -> int:
        type(self).calls += 1
        self.storage.put_episode(
            Episode(
                project_id=project_id,
                kind="episodic",
                source_type="transcript",
                source_ref=session_id,
                category="decision",
                title="Attivare orchestratore Neural Tape v3",
                body="La v3 deve processare sessioni reali in modo idempotente.",
                confidence=0.9,
            )
        )
        return 1


def _init_git_repo(project_root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=project_root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "neural-tape-test@example.invalid"],
        cwd=project_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Neural Tape Test"],
        cwd=project_root,
        check=True,
    )
    (project_root / "README.md").write_text("# Test project\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project_root, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=project_root, check=True)


def test_run_once_persists_context_and_is_idempotent():
    FakeClassifier.calls = 0
    with tempfile.TemporaryDirectory(prefix="nt-v3-run-") as tmp:
        temp_root = Path(tmp)
        tape_root = temp_root / "neural-tape"
        project_root = temp_root / "project"
        transcript = temp_root / "session-123.jsonl"
        tape_root.mkdir()
        project_root.mkdir()
        _init_git_repo(project_root)

        project_config = project_root / ".neuraltape" / "project.yaml"
        project_config.parent.mkdir()
        project_config.write_text(
            "project_id: test-project\ndisplay_name: Test Project\n",
            encoding="utf-8",
        )
        config_path = tape_root / "config.yaml"
        config_path.write_text(
            "v3:\n"
            "  enabled: true\n"
            "  storage:\n"
            "    db_path: tape/v3/neuraltape.db\n",
            encoding="utf-8",
        )
        transcript.write_text(
            json.dumps(
                {
                    "type": "user.message",
                    "timestamp": "2026-07-15T12:00:00Z",
                    "data": {"content": "Attiviamo Neural Tape v3"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        first = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FakeClassifier,
        )
        second = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FakeClassifier,
        )

        storage = Storage(tape_root / "tape" / "v3" / "neuraltape.db")
        episodes = storage.query_episodes("test-project")
        output_dir = tape_root / "tape" / "v3" / "projects" / "test-project"

        assert first.episodes_written == 1
        assert first.skipped is False
        assert second.episodes_written == 0
        assert second.skipped is True
        assert FakeClassifier.calls == 1
        assert len(episodes) == 1
        assert episodes[0].source_ref == "session-123"
        assert (output_dir / "current-focus.json").exists()
        assert (output_dir / "working-set.json").exists()


def test_run_once_reprocesses_when_transcript_grows():
    """Regression: a session classified too early (eps=0 on a short snapshot)
    must be reprocessed when the transcript grows beyond the threshold.

    Previously the `transcript.classified` marker was written unconditionally,
    freezing the session forever. The fix stores `transcript_bytes` and only
    skips when the size has not changed significantly.
    """
    FakeClassifier.calls = 0
    with tempfile.TemporaryDirectory(prefix="nt-v3-run-") as tmp:
        temp_root = Path(tmp)
        tape_root = temp_root / "neural-tape"
        project_root = temp_root / "project"
        transcript = temp_root / "session-grow.jsonl"
        tape_root.mkdir()
        project_root.mkdir()
        _init_git_repo(project_root)

        project_config = project_root / ".neuraltape" / "project.yaml"
        project_config.parent.mkdir()
        project_config.write_text(
            "project_id: test-grow\n display_name: Test Grow\n",
            encoding="utf-8",
        )
        config_path = tape_root / "config.yaml"
        config_path.write_text(
            "v3:\n  enabled: true\n  storage:\n    db_path: tape/v3/neuraltape.db\n",
            encoding="utf-8",
        )

        # Initial short snapshot (no real insights yet)
        transcript.write_text(
            json.dumps(
                {
                    "type": "user.message",
                    "timestamp": "2026-07-15T12:00:00Z",
                    "data": {"content": "Attiviamo Neural Tape v3"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        first = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FakeClassifier,
        )
        assert first.skipped is False
        assert FakeClassifier.calls == 1

        # No growth -> must stay skipped
        unchanged = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FakeClassifier,
        )
        assert unchanged.skipped is True
        assert FakeClassifier.calls == 1

        # Append a large block of new content (> GROWTH_THRESHOLD_BYTES = 2KB)
        with open(transcript, "a", encoding="utf-8") as fh:
            for i in range(200):
                fh.write(
                    json.dumps(
                        {
                            "type": "assistant.message",
                            "timestamp": "2026-07-15T13:00:00Z",
                            "data": {"content": f"Nuova attività session {i:03d} " * 5},
                        }
                    )
                    + "\n"
                )

        grown = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FakeClassifier,
        )
        assert grown.skipped is False, "growing session must be reprocessed"
        assert FakeClassifier.calls == 2, "classifier must run again after growth"


def test_resolve_transcript_rejects_ambiguous_prefix():
    class FakeWatcher:
        def find_all_transcripts(self, max_age_minutes: int):
            assert max_age_minutes == 60
            return [
                (2.0, Path("/tmp/session-abc-one.jsonl")),
                (1.0, Path("/tmp/session-abc-two.jsonl")),
            ]

    try:
        resolve_transcript("session-abc", watcher=FakeWatcher(), max_age_minutes=60)
    except ValueError as error:
        assert "ambiguous" in str(error).lower()
    else:
        raise AssertionError("ambiguous session prefix was accepted")


def test_resolve_transcript_prefers_exact_session_id():
    exact = Path("/tmp/session-abc.jsonl")

    class FakeWatcher:
        def find_all_transcripts(self, max_age_minutes: int):
            assert max_age_minutes == 60
            return [
                (2.0, Path("/tmp/session-abc-extra.jsonl")),
                (1.0, exact),
            ]

    resolved = resolve_transcript(
        "session-abc",
        watcher=FakeWatcher(),
        max_age_minutes=60,
    )

    assert resolved == exact


def test_latest_transcript_window_keeps_recent_context():
    transcript = "OLD-CONTEXT\n" + ("x" * 100) + "\nNEW-CONTEXT"

    window = _latest_transcript_window(transcript, max_chars=40)

    assert len(window) == 40
    assert "OLD-CONTEXT" not in window
    assert window.endswith("NEW-CONTEXT")


# ---------------------------------------------------------------------------
# Fase 0 (2026-08-21): memory-loss hardening
# ---------------------------------------------------------------------------


def _scaffold(temp_root: Path, session_name: str, project_id: str):
    """Shared fixture: tape root + git project + config. Returns paths."""
    tape_root = temp_root / "neural-tape"
    project_root = temp_root / "project"
    transcript = temp_root / session_name
    tape_root.mkdir()
    project_root.mkdir()
    _init_git_repo(project_root)
    project_config = project_root / ".neuraltape" / "project.yaml"
    project_config.parent.mkdir()
    project_config.write_text(
        f"project_id: {project_id}\ndisplay_name: Test\n", encoding="utf-8"
    )
    config_path = tape_root / "config.yaml"
    config_path.write_text(
        "v3:\n  enabled: true\n  storage:\n    db_path: tape/v3/neuraltape.db\n",
        encoding="utf-8",
    )
    return tape_root, project_root, transcript, config_path


def _user_message(content: str) -> str:
    return json.dumps(
        {
            "type": "user.message",
            "timestamp": "2026-07-15T12:00:00Z",
            "data": {"content": content},
        }
    ) + "\n"


def test_budgeted_window_announces_omitted_content():
    old_event = "[2026-07-15T10:00:00Z] [USER]\nvecchio contenuto\n"
    filler = "x" * 500 + "\n"
    new_event = "[2026-07-15T11:00:00Z] [USER]\ncontenuto nuovo"
    transcript = old_event + filler + new_event

    window = _budgeted_transcript_window(transcript, max_chars=200)

    first_line, _, rest = window.partition("\n")
    assert first_line.startswith("[NeuralTape: ~")
    assert "chars of older transcript omitted]" in first_line
    # The window restarts on an event boundary, not mid-content.
    assert rest.startswith("[2026-07-15T11:00:00Z] [USER]")
    assert "contenuto nuovo" in window
    assert "vecchio contenuto" not in window

    # No truncation -> unchanged.
    assert _budgeted_transcript_window("short", max_chars=200) == "short"


def test_empty_transcript_is_marked_and_not_retried():
    FakeClassifier.calls = 0
    with tempfile.TemporaryDirectory(prefix="nt-v3-run-") as tmp:
        temp_root = Path(tmp)
        tape_root, project_root, transcript, config_path = _scaffold(
            temp_root, "session-empty.jsonl", "test-empty"
        )
        # Junk that parses to zero classifiable events.
        transcript.write_text("not a jsonl event\n", encoding="utf-8")

        result = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FakeClassifier,
        )
        assert result.skipped is True
        assert result.episodes_written == 0
        assert FakeClassifier.calls == 0, "no LLM call for empty transcripts"

        storage = Storage(tape_root / "tape" / "v3" / "neuraltape.db")
        markers = storage.query_events(
            "test-empty",
            source_type="transcript.classified",
            source_ref="session-empty",
        )
        assert len(markers) == 1
        assert markers[0]["payload"].get("empty") is True

        # Second run: still skipped, no new marker, no classifier call.
        again = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FakeClassifier,
        )
        assert again.skipped is True
        assert FakeClassifier.calls == 0


def test_shrunk_transcript_is_reprocessed():
    """Rotation/truncation: a transcript that shrank must be re-classified."""
    FakeClassifier.calls = 0
    with tempfile.TemporaryDirectory(prefix="nt-v3-run-") as tmp:
        temp_root = Path(tmp)
        tape_root, project_root, transcript, config_path = _scaffold(
            temp_root, "session-shrink.jsonl", "test-shrink"
        )
        transcript.write_text(_user_message("Prima versione lunga della sessione"), encoding="utf-8")

        first = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FakeClassifier,
        )
        assert first.skipped is False

        # Agent rotates the file: new content, smaller than before.
        transcript.write_text(_user_message("Ruotata"), encoding="utf-8")

        second = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FakeClassifier,
        )
        assert second.skipped is False, "shrunk transcript must be reprocessed"
        assert FakeClassifier.calls == 2


def test_marker_found_among_many_other_sessions():
    """Regression: the classified marker must be found by source_ref, not by
    scanning only the N newest events of the whole project."""
    FakeClassifier.calls = 0
    with tempfile.TemporaryDirectory(prefix="nt-v3-run-") as tmp:
        temp_root = Path(tmp)
        tape_root, project_root, transcript, config_path = _scaffold(
            temp_root, "session-mine.jsonl", "test-many"
        )
        transcript.write_text(_user_message("Sessione da ricordare"), encoding="utf-8")

        first = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FakeClassifier,
        )
        assert first.skipped is False

        # Six newer markers for OTHER sessions bury ours beyond any small limit.
        storage = Storage(tape_root / "tape" / "v3" / "neuraltape.db")
        import time as _time
        for i in range(6):
            storage.append_event(
                project_id="test-many",
                source_type="transcript.classified",
                source_ref=f"other-{i}",
                captured_at=_time.time() + i + 1,
                payload={"episodes_written": 1, "transcript_bytes": 99999},
            )

        second = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FakeClassifier,
        )
        assert second.skipped is True, "marker must be found via source_ref filter"
        assert FakeClassifier.calls == 1


class FailingClassifier:
    calls = 0

    def __init__(self, **_kwargs):
        pass

    def classify_and_persist(self, transcript_text, session_id, project_id):
        type(self).calls += 1
        raise RuntimeError("LLM endpoint down")


def test_failure_backoff_defers_after_repeated_failures():
    FailingClassifier.calls = 0
    with tempfile.TemporaryDirectory(prefix="nt-v3-run-") as tmp:
        temp_root = Path(tmp)
        tape_root, project_root, transcript, config_path = _scaffold(
            temp_root, "session-fail.jsonl", "test-fail"
        )
        transcript.write_text(_user_message("Sessione che fallisce"), encoding="utf-8")

        for attempt in range(3):
            try:
                run_once(
                    transcript,
                    project_root,
                    tape_root=tape_root,
                    config_path=config_path,
                    classifier_factory=FailingClassifier,
                )
            except RuntimeError:
                pass
            else:
                raise AssertionError(f"attempt {attempt} should have raised")
        assert FailingClassifier.calls == 3

        storage = Storage(tape_root / "tape" / "v3" / "neuraltape.db")
        failures = storage.query_events(
            "test-fail", source_type="transcript.failed", source_ref="session-fail"
        )
        assert len(failures) == 3
        # No classified marker may exist for a failed session.
        classified = storage.query_events(
            "test-fail",
            source_type="transcript.classified",
            source_ref="session-fail",
        )
        assert classified == []

        # Fourth run: backoff kicks in, classifier is not invoked.
        deferred = run_once(
            transcript,
            project_root,
            tape_root=tape_root,
            config_path=config_path,
            classifier_factory=FailingClassifier,
        )
        assert deferred.skipped is True
        assert FailingClassifier.calls == 3