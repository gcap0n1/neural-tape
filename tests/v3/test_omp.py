"""Tests for the Oh My Pi adapter (registry manifest + parser)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from nt_v3.transcript_parser import TranscriptParser
from nt_v3.transcript_sources import builtin_sources
from nt_v3.transcript_watcher import TranscriptWatcher


def _write(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
        encoding="utf-8",
    )


def test_parser_reads_omp_schema():
    with tempfile.TemporaryDirectory(prefix="nt-omp-") as tmp:
        transcript = Path(tmp) / "enc" / "session-1.jsonl"
        assistant_repr = repr(
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Verifico la struttura"},
                    {"type": "text", "text": "Fatto, ecco il piano"},
                    {
                        "type": "toolCall",
                        "name": "read",
                        "arguments": {"path": "."},
                        "intent": "Checking workspace",
                    },
                ],
            }
        )
        _write(
            transcript,
            [
                {"type": "session", "version": "3", "id": "s1",
                 "timestamp": "2026-08-26T13:31:59.635Z", "cwd": "/home/user/MyWorkspace"},
                {"type": "title", "v": "1", "title": "sessione", "source": "auto"},
                {"type": "message", "message": json.dumps(
                    {"role": "user", "content": [{"type": "text", "text": "Ciao Lex"}]},
                    ensure_ascii=False,
                )},
                {"type": "message", "message": assistant_repr},
                {"type": "message", "message": json.dumps(
                    {"role": "toolResult", "content": [{"type": "text", "text": "SEGRETO-TOOL-OUTPUT"}]},
                    ensure_ascii=False,
                )},
            ],
        )
        parsed = TranscriptParser().parse_delta(transcript)

        assert "[SESSION START] | source: omp | cwd: /home/user/MyWorkspace" in parsed
        assert "[USER]\nCiao Lex" in parsed
        assert "[ASSISTANT reasoning]\nVerifico la struttura" in parsed
        assert "[ASSISTANT]\nFatto, ecco il piano" in parsed
        assert "[TOOL → read] Checking workspace" in parsed
        assert "SEGRETO-TOOL-OUTPUT" not in parsed
        assert "type: title" not in parsed


def test_watcher_discovers_omp_and_derives_label_from_session_cwd():
    with tempfile.TemporaryDirectory(prefix="nt-omp-") as tmp:
        home = Path(tmp)
        enc = "--run-media-user-Back-Up-EterCervo--"
        f = home / ".omp" / "agent" / "sessions" / enc / "2026-08-26T13-31-59-635Z_sid.jsonl"
        _write(
            f,
            [{"type": "session", "id": "sid", "timestamp": "2026-08-26T13:31:59.635Z",
              "cwd": "/run/media/user/Back-Up/EterCervo"}],
        )

        watcher = TranscriptWatcher(home=home)
        found = {p for _, p in watcher.find_all_transcripts(max_age_minutes=60)}

        assert f.resolve() in found
        assert watcher.get_workspace_label(f) == "EterCervo"
        assert watcher.session_id_of(f) == "2026-08-26T13-31-59-635Z_sid"


def test_omp_home_env_override():
    with tempfile.TemporaryDirectory(prefix="nt-omp-") as tmp:
        home = Path(tmp)
        custom = home / "omp-alt"
        sources = builtin_sources(home, env={"NEURALTAPE_OMP_HOME": str(custom)})
        omp = next(s for s in sources if s.id == "omp")
        assert omp.base == custom
        assert omp.globs == ("*/*.jsonl",)