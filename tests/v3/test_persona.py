"""Tests for persona configuration and persona-neutral transcript markers."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from nt_v3.transcript_parser import TranscriptParser
from nt_v3.config import load


def test_config_persona_defaults():
    with tempfile.TemporaryDirectory(prefix="nt-persona-") as tmp:
        cfg = load(Path(tmp))
        assert cfg.persona.assistant == "assistant"
        assert cfg.persona.user == "user"


def test_config_persona_override():
    with tempfile.TemporaryDirectory(prefix="nt-persona-") as tmp:
        root = Path(tmp)
        cfg_path = root / "config.yaml"
        cfg_path.write_text(
            """
v3:
  enabled: true
  persona:
    assistant: "atlas"
    user: "Ada"
""",
            encoding="utf-8",
        )
        cfg = load(root, config_path=cfg_path)
        assert cfg.persona.assistant == "atlas"
        assert cfg.persona.user == "Ada"


def test_config_persona_blank_values_fall_back():
    with tempfile.TemporaryDirectory(prefix="nt-persona-") as tmp:
        root = Path(tmp)
        cfg_path = root / "config.yaml"
        cfg_path.write_text(
            """
v3:
  persona:
    assistant: "   "
    user: ""
""",
            encoding="utf-8",
        )
        cfg = load(root, config_path=cfg_path)
        assert cfg.persona.assistant == "assistant"
        assert cfg.persona.user == "user"


def test_parser_emits_persona_neutral_markers():
    """Transcript markers carry no assistant name: [USER] / [ASSISTANT]."""
    with tempfile.TemporaryDirectory(prefix="nt-persona-") as tmp:
        transcript = Path(tmp) / "legacy.jsonl"
        events = [
            {
                "type": "user.message",
                "data": {"content": "Apri MyWorkspace"},
                "ts": 1000,
            },
            {
                "type": "assistant.message",
                "data": {
                    "reasoningText": "Verifico prima",
                    "content": "Fatto",
                },
                "ts": 2000,
            },
        ]
        transcript.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
            encoding="utf-8",
        )
        parsed = TranscriptParser().parse_delta(transcript)

        assert "[USER]\nApri MyWorkspace" in parsed
        assert "[ASSISTANT reasoning]\nVerifico prima" in parsed
        assert "[ASSISTANT]\nFatto" in parsed
        assert "[LEX" not in parsed


def test_classifier_prompt_format_fills_persona():
    from nt_v3.classifier import CLASSIFIER_PROMPT

    prompt = CLASSIFIER_PROMPT.format(
        transcript="t",
        redacted_summary="r",
        assistant="atlas",
        user="Ada",
    )
    assert "You are atlas, Ada's senior developer AI agent." in prompt
    assert "Ada's explicit preferences" in prompt