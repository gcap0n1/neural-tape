"""Test per lex/v3/classifier.py (D1.1)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_FAKE_CONFIG = SimpleNamespace(
    persona=SimpleNamespace(assistant="assistant", user="user"),
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lex" / "v3"))

from classifier import (  # type: ignore[import-not-found]
    CLASSIFIER_PROMPT,
    ClassificationError,
    ClassifierInsight,
    ClassifierV3,
)


class CleanRedactor:
    def redact(self, text: str):
        return text, []

    def summary(self, _events):
        return "redaction: clean"


class OpenCostPolicy:
    def can_call(self):
        return True, "ok"

    def record_call(self, _tokens_used: int):
        return None


def _build_classifier() -> ClassifierV3:
    return ClassifierV3(
        config=_FAKE_CONFIG,
        project=object(),
        storage=object(),
        redactor=CleanRedactor(),
        cost_policy=OpenCostPolicy(),
        api_key="test-key",
    )


def test_classifier_insight_valid():
    ins = ClassifierInsight(
        category="pattern",
        title="test insight",
        context="during session X",
        implication="use this approach in future",
        layer="working",
        confidence=0.8,
    )
    errs = ins.validate()
    assert errs == [], f"expected no errors, got {errs}"


def test_classifier_insight_missing_title():
    ins = ClassifierInsight(
        category="pattern", title="", context="", implication="",
        layer="working", confidence=0.5,
    )
    errs = ins.validate()
    assert any("title is empty" in e for e in errs)


def test_classifier_insight_invalid_category():
    ins = ClassifierInsight(
        category="bogus", title="x", context="", implication="",
        layer="working", confidence=0.5,
    )
    errs = ins.validate()
    assert any("invalid category" in e for e in errs)


def test_classifier_insight_invalid_layer():
    ins = ClassifierInsight(
        category="pattern", title="x", context="", implication="",
        layer="quantum", confidence=0.5,
    )
    errs = ins.validate()
    assert any("invalid layer" in e for e in errs)


def test_classifier_insight_out_of_range_confidence():
    ins = ClassifierInsight(
        category="pattern", title="x", context="", implication="",
        layer="working", confidence=1.5,
    )
    errs = ins.validate()
    assert any("confidence out of range" in e for e in errs)


def test_classifier_insight_from_dict():
    data = {
        "category": "decision",
        "title": "choose sqlite over json",
        "context": "during storage design",
        "implication": "better query capabilities",
        "layer": "semantic",
        "confidence": 0.95,
        "evidence": "choose sqlite over json storage",
    }
    ins = ClassifierInsight.from_dict(data)
    assert ins.category == "decision"
    assert ins.title == "choose sqlite over json"
    assert ins.layer == "semantic"
    assert ins.confidence == 0.95
    assert ins.evidence == "choose sqlite over json storage"


def test_classifier_insight_from_dict_missing_fields():
    data = {"category": "pattern", "title": "test"}
    ins = ClassifierInsight.from_dict(data)
    assert ins.context == ""
    assert ins.implication == ""
    assert ins.layer == "working"
    assert ins.confidence == 0.0


def test_classifier_insight_dedup_normalization():
    """Titles that differ by case/whitespace must normalize to the same key."""
    ins1 = ClassifierInsight("pattern", "Use SQLite", "c", "i", "working", 0.7)
    ins2 = ClassifierInsight("pattern", "  use SQLITE  ", "c", "i", "working", 0.7)
    norm1 = " ".join(ins1.title.lower().split())
    norm2 = " ".join(ins2.title.lower().split())
    assert norm1 == norm2


def test_redaction_integration():
    """Verify the redactor works before classifier gets text (integration test)."""
    from redaction import Redactor  # type: ignore
    r = Redactor()
    text = "my token=sk-abc123def456ghi789jkl012mno345pqr"
    redacted, ev = r.redact(text)
    # The generic-assignment pattern should catch "token=..." with long value
    assert "[REDACTED:" in redacted
    assert "sk-abc123" not in redacted


def test_classifier_processes_newest_chunk_first():
    classifier = _build_classifier()
    prompts = []

    def fake_completion(prompt: str):
        prompts.append(prompt)
        return {"choices": [{"message": {"content": '{"insights": []}'}}]}

    classifier._chat_completion = fake_completion
    old_chunk = "OLD-CONTEXT " + ("x" * 16000) + "\n"
    new_chunk = "NEW-CONTEXT " + ("y" * 16000) + "\n"

    classifier.classify(old_chunk + new_chunk, "session-long")

    assert "NEW-CONTEXT" in prompts[0]
    assert "OLD-CONTEXT" in prompts[1]


def test_classifier_rejects_missing_or_fabricated_evidence():
    classifier = _build_classifier()
    responses = iter(
        [
            {
                "choices": [{"message": {"content": json.dumps({"insights": [{
                    "category": "tool",
                    "title": "Invented schema detail",
                    "context": "Codex uses payload.messages",
                    "implication": "Parse that field",
                    "layer": "episodic",
                    "confidence": 1.0,
                    "evidence": "payload.messages is the Codex schema",
                }]})}}]
            },
            {
                "choices": [{"message": {"content": json.dumps({"insights": [{
                    "category": "decision",
                    "title": "Missing evidence",
                    "context": "A decision was made",
                    "implication": "Remember it",
                    "layer": "episodic",
                    "confidence": 0.9,
                    "evidence": "",
                }]})}}]
            },
        ]
    )
    classifier._chat_completion = lambda _prompt: next(responses)

    assert classifier.classify("Actual transcript says only this", "session-false") == []
    assert classifier.classify("Actual transcript says only this", "session-empty") == []


def test_classifier_accepts_exact_whitespace_normalized_evidence():
    classifier = _build_classifier()
    content = json.dumps({"insights": [{
        "category": "decision",
        "title": "Pilot moved to September",
        "context": "The user postponed the pilot",
        "implication": "Resume in September",
        "layer": "episodic",
        "confidence": 1.0,
        "evidence": "Il pilot sara' per settembre",
    }]})
    classifier._chat_completion = lambda _prompt: {
        "choices": [{"message": {"content": content}}]
    }

    insights = classifier.classify(
        "Il pilot   sara' per\nsettembre, non oggi.",
        "session-grounded",
    )

    assert len(insights) == 1
    assert insights[0].title == "Pilot moved to September"


def test_classifier_raises_when_llm_call_fails():
    classifier = _build_classifier()

    def failing_completion(_prompt: str):
        raise OSError("provider unavailable")

    classifier._chat_completion = failing_completion

    try:
        classifier.classify("session content", "session-failed")
    except ClassificationError as error:
        assert "provider unavailable" in str(error)
    else:
        raise AssertionError("ClassificationError was not raised")


def test_classifier_prompt_requires_evidence_for_preferences_and_errors():
    assert "explicit user preference" in CLASSIFIER_PROMPT
    assert "exact observed error name" in CLASSIFIER_PROMPT
    assert "do not reinterpret it" in CLASSIFIER_PROMPT
    assert "an EXACT 5-25 word quote" in CLASSIFIER_PROMPT
    assert "Empty results are valid" in CLASSIFIER_PROMPT
    assert "You are {assistant}, {user}'s senior developer AI agent." in CLASSIFIER_PROMPT
    assert "Do not turn an `[ASSISTANT]` proposal" in CLASSIFIER_PROMPT
    assert "require evidence from `[USER]`" in CLASSIFIER_PROMPT
    assert "<think>" in CLASSIFIER_PROMPT


def test_episode_dedup_key_normalizes_title():
    from classifier import episode_dedup_key

    k1 = episode_dedup_key("Use SQLite", "body")
    k2 = episode_dedup_key("  use sqlite  ", "body")
    k3 = episode_dedup_key("Use SQLite", "different body")

    assert k1 == k2
    assert k1 != k3


def test_persist_dedup_skips_known_insight():
    """Cross-run dedup: the same insight extracted from another session (or a
    rotated transcript) is not persisted twice."""
    import tempfile as _tempfile
    from pathlib import Path as _P

    from storage import Storage

    tmp = _tempfile.mkdtemp(prefix="nt-v3-clf-")
    storage = Storage(_P(tmp) / "test.db")
    classifier = ClassifierV3(
        config=_FAKE_CONFIG,
        project=object(),
        storage=storage,
        redactor=CleanRedactor(),
        cost_policy=OpenCostPolicy(),
        api_key="test-key",
    )
    content = json.dumps({"insights": [{
        "category": "decision",
        "title": "Standardize on Postgres",
        "context": "Picked during storage design",
        "implication": "Use Postgres by default",
        "layer": "semantic",
        "confidence": 0.95,
        "evidence": "Standardize on Postgres from now",
    }]})
    classifier._chat_completion = lambda _prompt: {
        "choices": [{"message": {"content": content}}]
    }
    transcript = "We decided to Standardize on Postgres from now for new services."

    first = classifier.classify_and_persist(transcript, "sess-1", "p")
    second = classifier.classify_and_persist(transcript, "sess-2", "p")

    assert first == 1
    assert second == 0, "identical insight must be skipped on the second run"
    episodes = storage.query_episodes("p")
    assert len(episodes) == 1
    assert episodes[0].source_ref == "sess-1"
