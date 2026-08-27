"""NeuralTape v3 — classifier (D1.1).

Replaces v22/classifier.py. Key improvements:
- Extended prompt that asks for layer (working/episodic/semantic) + confidence.
- Output validated against ClassifierInsight dataclass.
- Integrates Redactor (Fase 0) before LLM call.
- Integrates CostPolicy (Fase 0) for budget checks.
- Persists classified insights directly to Storage as episodes.

v3 is the active pipeline since 2026-07-20: it persists classified insights to
Storage (SQLite `tape/v3/neuraltape.db`) and mirrors each episode to
`tape/archive/<category>/` via markdown_export. v3 does NOT write the
profile's manual memory file — that stays curated by the owner through
their own tooling. v2.2 is disabled since 2026-07-20.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# Allow both relative and direct imports (for tests).
_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

from config import V3Config
from cost import CostPolicy
from project import Project
from redaction import Redactor, RedactionEvent
from storage import Episode, Storage, episode_dedup_key

# Optional bridge to v2.2-style markdown archive. Imported lazily inside
# classify_and_persist so the classifier still works in SQLite-only mode
# (tests, isolation). The module lives next to this file as a sibling.
import markdown_export  # noqa: E402

log = logging.getLogger("neural-tape-v3")

# Max characters for the transcript sent to LLM (matches v2.2).
MAX_TRANSCRIPT_CHARS = 30000
# Newest-first bound so a 1MB session cannot fan out into dozens of LLM calls.
MAX_CHUNKS = 6

# The prompt template. {redacted_summary} is filled with the redaction summary
# (or empty if clean). {transcript} is the actual content.
CLASSIFIER_PROMPT = """You are {assistant}, {user}'s senior developer AI agent. You have just completed a work session.

{redacted_summary}

Your task is to extract structured insights for NeuralTape v3's layered memory system.

Categories:
- "pattern": recurring workflows, habits, or operating methods
- "decision": architectural or strategic decisions, including their rationale
- "anti-pattern": approaches that failed, were rejected, or proved incorrect
- "preference": {user}'s explicit preferences about language, tools, or approach
- "tool": observed API/framework/library quirks, real constraints, signatures, or limits
- "warning": critical errors, traps, or situations to avoid

Layers:
- "working": immediately useful references and session details; lifetime minutes to hours
- "episodic": important events, non-trivial fixes, and API discoveries; lifetime weeks
- "semantic": recurring patterns, stable preferences, and architectural decisions with rationale; lifetime months or permanent

For each worthy insight return:
- "category": one of the six categories above
- "title": a concise 5-12 word title
- "context": one line explaining when and why it emerged
- "implication": one line explaining what changes for future recommendations
- "layer": "working" | "episodic" | "semantic"
- "confidence": float 0.0-1.0 representing certainty that the insight is true and useful
- "evidence": an EXACT 5-25 word quote copied from the transcript
- "entities": zero to five canonical technical or product names that appear
    VERBATIM in the insight fields below (feature names, module/class names,
    tool commands, components; CamelCase or TitleCase preferred). They power
    name-based retrieval later. Omit the key when there are none.

RULES:
- Return at most 8 insights; prefer a few correct insights.
- Empty results are valid. If the session is routine or you cannot quote exact
    textual evidence, return {{"insights": []}}.
- Do not invent dates, paths, event names, JSON types, errors, or configuration.
    Every fact in title/context must be supported by the "evidence" field.
- "evidence" must occur literally in the transcript. Paraphrases,
    reconstructions, and deductions are not evidence.
- Every string in "entities" must occur literally inside "title", "context"
    or "evidence". Never invent product, library, or project names.
- Use "preference" only for an explicit user preference. An implementation or
    architectural choice is a "decision".
- Do not turn an `[ASSISTANT]` proposal, recommendation, or inference into a confirmed
    `[USER]` decision. User intent and scheduling require evidence from `[USER]`.
- Use "semantic" only when the transcript demonstrates recurrence or stability.
    A one-time event or decision is "episodic".
- For failures and exceptions, preserve the exact observed error name and do not reinterpret it.
- Do not include `<think>` tags or reasoning outside the JSON object.
- Return valid JSON only, without Markdown or commentary.

Output format:
{{"insights": [{{"category":"...","title":"...","context":"...","implication":"...","layer":"...","confidence":0.0,"evidence":"exact quote","entities":["Name One","TwoWord"]}}]}}

Transcript:
---
{transcript}
---"""


class ClassificationError(RuntimeError):
    """The classifier could not complete a trustworthy classification."""


class ClassificationDeferred(ClassificationError):
    """Classification was intentionally deferred by the cost policy."""


@dataclass
class ClassifierInsight:
    category: str
    title: str
    context: str
    implication: str
    layer: str          # "working" | "episodic" | "semantic"
    confidence: float
    evidence: str = ""
    entities: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> ClassifierInsight:
        raw_entities = d.get("entities") or []
        entities: list[str] = []
        seen: set[str] = set()
        for item in raw_entities:
            name = str(item).strip()
            if not name or len(name) < 2 or name.casefold() in seen:
                continue
            seen.add(name.casefold())
            entities.append(name)
            if len(entities) >= 5:
                break
        return cls(
            category=str(d.get("category", "")),
            title=str(d.get("title", "")),
            context=str(d.get("context", "")),
            implication=str(d.get("implication", "")),
            layer=str(d.get("layer", "working")),
            confidence=float(d.get("confidence", 0.0)),
            evidence=str(d.get("evidence", "")),
            entities=entities,
        )

    def validate(self, evidence_source: str | None = None) -> list[str]:
        errors = []
        if self.category not in {"pattern", "decision", "anti-pattern", "preference", "tool", "warning"}:
            errors.append(f"invalid category: {self.category!r}")
        if not self.title:
            errors.append("title is empty")
        if self.layer not in {"working", "episodic", "semantic"}:
            errors.append(f"invalid layer: {self.layer!r}")
        if not (0.0 <= self.confidence <= 1.0):
            errors.append(f"confidence out of range: {self.confidence}")
        if evidence_source is not None:
            normalized_evidence = " ".join(self.evidence.casefold().split())
            normalized_source = " ".join(evidence_source.casefold().split())
            if not normalized_evidence:
                errors.append("evidence is empty")
            elif not 5 <= len(self.evidence.split()) <= 25:
                errors.append("evidence must contain 5-25 words")
            elif normalized_evidence not in normalized_source:
                errors.append("evidence is not an exact transcript excerpt")
        return errors


class ClassifierV3:
    """Classify a transcript into layered insights via OpenAI-compatible LLM.

    Integrates:
    - Redactor (Fase 0) — redacts secrets before LLM
    - CostPolicy (Fase 0) — respects daily budget
    - Storage (Fase 0) — persists classified episodes
    """

    def __init__(
        self,
        config: V3Config,
        project: Project,
        storage: Storage,
        redactor: Redactor,
        cost_policy: CostPolicy,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        archive_root: Path | None = None,
    ):
        self.config = config
        self.project = project
        self.storage = storage
        self.redactor = redactor
        self.cost_policy = cost_policy
        # When set, every persisted episode is also mirrored to the v2.2-style
        # markdown archive so pre_load.py / session-context.md keep working
        # without changes. None = SQLite-only mode (tests).
        self.archive_root = archive_root

        # LLM endpoint config (static .env loading, like v2.2)
        if api_key:
            self.api_key = api_key
        else:
            self.api_key = (
                os.environ.get("LLM_API_KEY")
                or os.environ.get("DEEPSEEK_API_KEY")
                or ""
            )
        if not self.api_key:
            raise RuntimeError(
                "No LLM_API_KEY. Set in .env (LLM_API_KEY=...) or pass directly."
            )
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL", "deepseek-v4-flash")

    # ---- public API -----------------------------------------------------

    def classify(self, transcript_text: str, session_id: str) -> list[ClassifierInsight]:
        """Redact → check budget → LLM → parse → validate. Returns valid insights.

        This does NOT write to storage. Use classify_and_persist for that.
        """
        # 1. Redact
        redacted, redaction_events = self.redactor.redact(transcript_text)
        redaction_summary = self.redactor.summary(redaction_events)
        if redaction_events:
            log.info("redaction: %s", redaction_summary)

        # 2. Check budget BEFORE LLM call
        allowed, reason = self.cost_policy.can_call()
        if not allowed:
            log.warning("LLM call skipped: %s", reason)
            raise ClassificationDeferred(f"LLM call skipped: {reason}")

        # 3. Classify
        chunks = list(reversed(self._split(redacted)))[:MAX_CHUNKS]
        valid: list[ClassifierInsight] = []
        seen_titles: set[str] = set()

        for i, chunk in enumerate(chunks):
            if i > 0:
                log.info("classifying chunk %d/%d (newest first)", i + 1, len(chunks))

            prompt = CLASSIFIER_PROMPT.format(
                transcript=chunk,
                redacted_summary=self._redaction_comment(redaction_summary),
                assistant=self.config.persona.assistant,
                user=self.config.persona.user,
            )

            try:
                response = self._chat_completion(prompt)
                tokens_used = self._estimate_tokens(prompt, response)
                self.cost_policy.record_call(tokens_used)
            except Exception as error:
                log.error("LLM call failed: %s", error, exc_info=True)
                raise ClassificationError(f"LLM call failed: {error}") from error

            content = response.get("choices", [{}])[0].get("message", {}).get("content") or ""
            try:
                data = json.loads(content)
            except json.JSONDecodeError as error:
                log.warning("LLM returned non-JSON. First 300 chars: %s", content[:300])
                raise ClassificationError("LLM returned non-JSON") from error

            insights = data.get("insights", [])
            if not isinstance(insights, list):
                log.warning("LLM 'insights' is not a list")
                raise ClassificationError("LLM 'insights' is not a list")

            for d in insights:
                if not isinstance(d, dict):
                    continue
                try:
                    ins = ClassifierInsight.from_dict(d)
                except (ValueError, TypeError) as e:
                    log.warning("skipping malformed insight: %s", e)
                    continue

                errs = ins.validate(evidence_source=redacted)
                if errs:
                    log.warning("skipping invalid insight: %s", "; ".join(errs))
                    continue

                # Dedup by normalized title
                norm = " ".join(ins.title.lower().split())
                if norm in seen_titles:
                    continue
                seen_titles.add(norm)

                valid.append(ins)
                if len(valid) >= 8:
                    return valid

        return valid

    def classify_and_persist(self, transcript_text: str, session_id: str,
                             project_id: str) -> int:
        """Classify and immediately write insights to Storage as episodes.

        Returns: number of episodes written.
        """
        insights = self.classify(transcript_text, session_id)
        if not insights:
            return 0

        written = 0
        skipped_dupes = 0
        for ins in insights:
            body = f"{ins.context}\n\n{ins.implication}"
            dedup_key = episode_dedup_key(ins.title, body)
            # Cross-run dedup: the same insight already persisted (from another
            # session or a re-classification after transcript rotation) is not
            # written again. See storage.has_episode_by_dedup.
            if self.storage.has_episode_by_dedup(project_id, dedup_key):
                skipped_dupes += 1
                log.debug("dedup skip: %s (already persisted)", ins.title)
                continue
            ep = Episode(
                project_id=project_id,
                kind=ins.layer,
                source_type="transcript",
                source_ref=session_id,
                category=ins.category,
                title=ins.title,
                body=body,
                confidence=ins.confidence,
                raw_payload={"session_id": session_id, "evidence": ins.evidence,
                             "entities": ins.entities},
                entities=ins.entities,
                dedup_key=dedup_key,
            )
            self.storage.put_episode(ep)
            written += 1
            log.debug("episode written: %s [%s] (conf=%.2f)", ins.title, ins.layer, ins.confidence)

            # Mirror to markdown archive for pre_load.py / session-context.md.
            # Failures here are non-fatal: the SQLite episode is the source of truth.
            if self.archive_root is not None:
                try:
                    markdown_export.export_episode_to_markdown(
                        ep,
                        self.archive_root,
                        session_id=session_id,
                        assistant=self.config.persona.assistant,
                    )
                except Exception as exc:
                    log.warning(
                        "markdown export failed for episode %s (non-fatal): %s",
                        ep.id, exc,
                    )

        log.info(
            "classified & persisted %d episode(s) from session %s (%d duplicate(s) skipped)",
            written, session_id, skipped_dupes,
        )
        return written

    # ---- internals ------------------------------------------------------

    def _split(self, text: str, max_chars: int = MAX_TRANSCRIPT_CHARS) -> list[str]:
        if len(text) <= max_chars:
            return [text]
        chunks: list[str] = []
        current: list[str] = []
        cur_size = 0
        for line in text.splitlines(keepends=True):
            if current and cur_size + len(line) > max_chars:
                chunks.append("".join(current))
                current = []
                cur_size = 0
            current.append(line)
            cur_size += len(line)
        if current:
            chunks.append("".join(current))
        return chunks

    def _redaction_comment(self, summary: str) -> str:
        if summary.startswith("redaction: clean"):
            return ""
        return (
            "(Note: secrets in the transcript were replaced with [REDACTED:...]. "
            f"Treat these replacements only as security artifacts. Summary: {summary})"
        )

    @staticmethod
    def _estimate_tokens(prompt: str, response: dict) -> int:
        """Rough token estimation: 1 token ≈ 4 chars. Used for cost tracking."""
        usage = response.get("usage", {})
        if isinstance(usage, dict) and usage.get("total_tokens"):
            return int(usage["total_tokens"])
        # Fallback: estimate from prompt + response content length.
        content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
        return max(1, (len(prompt) + len(content)) // 4)

    def _chat_completion(self, prompt: str) -> dict:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        }
        url = f"{self.base_url}/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body[:500]}") from e
