"""Tests for the DeepSeek Reasonix adapter (registry manifest + parser)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from nt_v3.transcript_sources import builtin_sources
from nt_v3.transcript_parser import TranscriptParser
from nt_v3.transcript_watcher import TranscriptWatcher


def _reasonix_events() -> list[dict]:
    return [
        {"role": "system", "content": "You are Reasonix, a coding agent."},
        {
            "role": "user",
            "content": "<response-language>\nFinal language: English\n</response-language>\n\nSpeak italian",
            "raw_content": "Speak italian",
            "createdAt": 1787580727194,
        },
        {
            "role": "assistant",
            "content": "Va bene, parlo italiano.",
            "reasoning_content": "L'utente chiede di parlare italiano.",
            "workDurationMs": 2500,
        },
        {
            "role": "tool",
            "name": "Bash",
            "content": "echo ciao\nexit 0",
            "tool_call_id": "call_1",
        },
    ]


def _write(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
        encoding="utf-8",
    )


def test_parser_reads_reasonix_schema():
    with tempfile.TemporaryDirectory(prefix="nt-reasonix-") as tmp:
        transcript = Path(tmp) / "sessions" / "20260824-141110.434747236-agente.jsonl"
        _write(transcript, _reasonix_events())
        parsed = TranscriptParser().parse_delta(transcript)

        # system instructions excluded
        assert "You are Reasonix" not in parsed
        # user: clean raw_content wins over the noisy content wrapper
        assert "[USER]\nSpeak italian" in parsed
        assert "<response-language>" not in parsed
        # assistant: reasoning + content
        assert "[ASSISTANT reasoning]\nL'utente chiede di parlare italiano." in parsed
        assert "[ASSISTANT]\nVa bene, parlo italiano." in parsed
        # tool call with bounded content
        assert "[TOOL → Bash]" in parsed
        assert "echo ciao" in parsed
        assert "[LEX" not in parsed


def test_watcher_discovers_reasonix_and_skips_sidecars():
    with tempfile.TemporaryDirectory(prefix="nt-reasonix-") as tmp:
        home = Path(tmp)
        session_dir = (
            home / ".reasonix" / "projects" / "-home-gcap0n1" / "sessions"
        )
        main = session_dir / "20260824-141110.434747236-agent.jsonl"
        events = session_dir / "20260824-141110.434747236-agent.events.jsonl"
        conflicts = session_dir / "20260824-141110.434747236-agent.conflicts.jsonl"
        _write(main, _reasonix_events())
        _write(events, [{"schema_version": 1, "type": "append"}])
        _write(conflicts, [{"conflict": "x"}])

        watcher = TranscriptWatcher(home=home)
        found = {path for _, path in watcher.find_all_transcripts(max_age_minutes=60)}

        assert main.resolve() in found
        assert events.resolve() not in found
        assert conflicts.resolve() not in found
        assert watcher.get_workspace_label(main) == "gcap0n1"
        assert (
            watcher.session_id_of(main)
            == "20260824-141110.434747236-agent"
        )


def test_reasonix_home_env_override():
    with tempfile.TemporaryDirectory(prefix="nt-reasonix-") as tmp:
        home = Path(tmp)
        custom = home / "reasonix-alt"
        sources = builtin_sources(home, env={"REASONIX_HOME": str(custom)})
        reasonix = next(s for s in sources if s.id == "reasonix")
        assert reasonix.base == custom

    with tempfile.TemporaryDirectory(prefix="nt-reasonix-") as tmp:
        home = Path(tmp)
        # NEURALTAPE_<ID>_HOME wins over the native env var
        a = home / "a"
        b = home / "b"
        sources = builtin_sources(
            home, env={"REASONIX_HOME": str(a), "NEURALTAPE_REASONIX_HOME": str(b)}
        )
        reasonix = next(s for s in sources if s.id == "reasonix")
        assert reasonix.base == b


def test_reasonix_sessions_unambitious_filter():
    """A .jsonl in a different layout must not be flagged as Reasonix."""
    with tempfile.TemporaryDirectory(prefix="nt-reasonix-") as tmp:
        other = Path(tmp) / "x.jsonl"
        other.write_text(
            json.dumps(
                {"role": "assistant", "content": "plain openai-style, no reasonix fields"}
            )
            + "\n",
            encoding="utf-8",
        )
        parsed = TranscriptParser().parse_delta(other)
        # no reasonix markers; role-based formatting requires reasonix fields
        assert "[ASSISTANT]" not in parsed