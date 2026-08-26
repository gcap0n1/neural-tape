"""Parse supported assistant transcripts into classifier-friendly text.

Neural Tape v3 accepts four JSONL schemas: the legacy VS Code GitHub Copilot
schema, the Codex rollout schema stored under ``~/.codex/sessions``, the
Kimi Code wire schema (protocol 1.5) stored under
``~/.kimi-code/sessions/*/session_*/agents/main/wire.jsonl``, and the
Grok Build schema stored under
``~/.grok/sessions/<urlencoded-cwd>/<uuid>/chat_history.jsonl``.  Only user,
assistant, reasoning-summary, and tool-call data is retained; system/developer
instructions, harness reminders and tool outputs are intentionally excluded.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


MAX_REASONING = 2000
MAX_CONTENT = 3000
MAX_TOOL_ARGS = 200


class TranscriptParser:
    """Parse a transcript delta from any supported JSONL schema."""

    def parse_delta(self, transcript: Path, offset: int = 0) -> str:
        lines: list[str] = []
        with Path(transcript).open("rb") as stream:
            stream.seek(offset)
            for raw in stream:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                formatted = self._format_event(event)
                if formatted:
                    lines.append(formatted)
        return "\n".join(lines)

    def parse_delta_structured(self, transcript: Path, offset: int = 0) -> dict:
        counts = {
            "user": 0,
            "assistant": 0,
            "reasoning": 0,
            "tool_calls": 0,
            "total_events": 0,
        }
        with Path(transcript).open("rb") as stream:
            stream.seek(offset)
            for raw in stream:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                counts["total_events"] += 1
                kind = self._event_kind(event)
                if kind in counts:
                    counts[kind] += 1
        return counts

    def _event_kind(self, event: dict) -> str | None:
        etype = event.get("type")
        if etype == "user.message":
            return "user"
        if etype == "assistant.message":
            return "assistant"
        if etype in ("tool.execution_start", "tool.execution_complete"):
            return "tool_calls"

        if etype == "user" and not event.get("synthetic_reason"):
            return "user" if self._grok_text(event) else None
        if etype == "assistant" and self._grok_text(event):
            return "assistant"
        if etype == "reasoning" and self._grok_reasoning(event):
            return "reasoning"
        if etype == "assistant" and event.get("tool_calls"):
            return "tool_calls"

        # Reasonix (DeepSeek): OpenAI-chat-format events keyed by role.
        if self._is_reasonix_event(event):
            role = event.get("role")
            if role == "user":
                return "user"
            if role == "assistant":
                return "assistant"
            if role == "tool":
                return "tool_calls"
            return None

        if etype == "turn.prompt":
            return "user" if self._kimi_prompt_text(event) else None
        if etype == "context.append_loop_event":
            sub = event.get("event") or {}
            stype = sub.get("type")
            if stype == "content.part":
                ptype = (sub.get("part") or {}).get("type")
                if ptype == "think":
                    return "reasoning"
                if ptype == "text":
                    return "assistant"
                return None
            if stype == "tool.call":
                return "tool_calls"
            return None

        payload = event.get("payload") or {}
        if etype != "response_item" or not isinstance(payload, dict):
            return None
        ptype = payload.get("type")
        if ptype == "message" and payload.get("role") == "user":
            return "user"
        if ptype == "message" and payload.get("role") == "assistant":
            return "assistant"
        if ptype == "reasoning" and self._codex_reasoning(payload):
            return "reasoning"
        if ptype in ("function_call", "custom_tool_call"):
            return "tool_calls"
        return None

    def _format_event(self, event: dict) -> str | None:
        etype = event.get("type")
        ts = self._event_ts(event)

        if etype == "session.start":
            return self._legacy_session(event.get("data", {}), ts)
        if etype == "user.message":
            return self._legacy_user(event.get("data", {}), ts)
        if etype == "assistant.message":
            return self._legacy_assistant(event.get("data", {}), ts)
        if etype in ("tool.execution_start", "tool.execution_complete"):
            return self._legacy_tool(event, event.get("data", {}), ts)

        # Kimi Code wire schema (protocol 1.5). ``context.append_message``
        # user entries carry harness-injected reminders, not the user voice:
        # they are excluded on purpose; real prompts arrive via ``turn.prompt``.
        if etype == "system" and self._is_grok_event(event):
            return self._grok_session(event, ts)
        if etype == "user" and self._is_grok_event(event):
            if event.get("synthetic_reason"):
                return None
            text = self._grok_text(event)
            return f"[{ts}] [USER]\n{text[:MAX_CONTENT]}" if text else None
        if etype == "reasoning" and self._is_grok_event(event):
            reasoning = self._grok_reasoning(event)
            return f"[{ts}] [ASSISTANT reasoning]\n{reasoning[:MAX_REASONING]}" if reasoning else None
        if etype == "assistant" and self._is_grok_event(event):
            return self._grok_assistant(event, ts)

        # Reasonix (DeepSeek): OpenAI-chat-format events keyed by role.
        if self._is_reasonix_event(event):
            role = event.get("role")
            if role == "user":
                return self._reasonix_user(event, ts)
            if role == "assistant":
                return self._reasonix_assistant(event, ts)
            if role == "tool":
                return self._reasonix_tool(event, ts)
            return None

        if etype == "metadata":
            return self._kimi_session(event, ts)
        if etype == "turn.prompt":
            text = self._kimi_prompt_text(event)
            return f"[{ts}] [USER]\n{text[:MAX_CONTENT]}" if text else None
        if etype == "context.append_loop_event":
            return self._kimi_loop_event(event, ts)

        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            return None
        if etype == "session_meta":
            return self._codex_session(payload, ts)
        if etype != "response_item":
            return None

        ptype = payload.get("type")
        if ptype == "message":
            return self._codex_message(payload, ts)
        if ptype == "reasoning":
            reasoning = self._codex_reasoning(payload)
            return f"[{ts}] [ASSISTANT reasoning]\n{reasoning[:MAX_REASONING]}" if reasoning else None
        if ptype in ("function_call", "custom_tool_call"):
            return self._codex_tool(payload, ts)
        return None

    @staticmethod
    def _event_ts(event: dict) -> str:
        ts = event.get("timestamp")
        if ts:
            return str(ts)[:19]
        epoch_ms = event.get("time", event.get("created_at", event.get("createdAt")))
        if isinstance(epoch_ms, (int, float)):
            return datetime.fromtimestamp(epoch_ms / 1000).isoformat(timespec="seconds")
        return ""

    @staticmethod
    def _legacy_session(data: dict, ts: str) -> str:
        if not isinstance(data, dict):
            return ""
        parts = [f"[{ts}] [SESSION START]"]
        if data.get("model"):
            parts.append(f"model: {data['model']}")
        if data.get("producer"):
            parts.append(f"producer: {data['producer']}")
        return " | ".join(parts)

    @staticmethod
    def _legacy_user(data, ts: str) -> str:
        content = data.get("content", "") if isinstance(data, dict) else str(data)
        return f"[{ts}] [USER]\n{content[:MAX_CONTENT]}" if content else ""

    @staticmethod
    def _legacy_assistant(data: dict, ts: str) -> str:
        if not isinstance(data, dict):
            return ""
        parts: list[str] = []
        reasoning = data.get("reasoningText", "")
        content = data.get("content", "")
        if reasoning:
            parts.append(f"[{ts}] [ASSISTANT reasoning]\n{reasoning[:MAX_REASONING]}")
        if content:
            parts.append(f"[{ts}] [ASSISTANT]\n{content[:MAX_CONTENT]}")
        return "\n".join(parts)

    def _legacy_tool(self, event: dict, data: dict, ts: str) -> str:
        if not isinstance(data, dict):
            return ""
        name = data.get("toolName") or data.get("name", "?")
        marker = "✓" if str(event.get("type", "")).endswith("complete") else "→"
        return f"[{ts}] [TOOL {marker} {name}] {self._compact_args(data.get('arguments', {}))}"

    @staticmethod
    def _codex_session(payload: dict, ts: str) -> str:
        parts = [f"[{ts}] [SESSION START]"]
        if payload.get("source"):
            parts.append(f"source: {payload['source']}")
        if payload.get("cwd"):
            parts.append(f"cwd: {payload['cwd']}")
        return " | ".join(parts)

    @staticmethod
    def _content_text(payload: dict) -> str:
        parts: list[str] = []
        for item in payload.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in ("input_text", "output_text", "text"):
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts)

    def _codex_message(self, payload: dict, ts: str) -> str | None:
        role = payload.get("role")
        if role not in ("user", "assistant"):
            return None
        content = self._content_text(payload)
        if not content:
            return None
        marker = "USER" if role == "user" else "ASSISTANT"
        return f"[{ts}] [{marker}]\n{content[:MAX_CONTENT]}"

    @staticmethod
    def _codex_reasoning(payload: dict) -> str:
        parts: list[str] = []
        for item in payload.get("summary") or []:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif isinstance(item, str) and item:
                parts.append(item)
        return "\n".join(parts)

    def _codex_tool(self, payload: dict, ts: str) -> str:
        name = payload.get("name", "?")
        args = payload.get("arguments")
        if args is None:
            args = payload.get("input", {})
        return f"[{ts}] [TOOL → {name}] {self._compact_args(args)}"

    # ---- Kimi Code (wire protocol 1.5) ----------------------------------

    @staticmethod
    def _kimi_session(event: dict, ts: str) -> str:
        parts = [f"[{ts}] [SESSION START]", "source: kimi-code"]
        if event.get("protocol_version"):
            parts.append(f"protocol: {event['protocol_version']}")
        return " | ".join(parts)

    @staticmethod
    def _kimi_prompt_text(event: dict) -> str:
        inp = event.get("input")
        if isinstance(inp, str):
            return inp
        parts: list[str] = []
        for item in inp or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts)

    def _kimi_loop_event(self, event: dict, ts: str) -> str | None:
        sub = event.get("event") or {}
        stype = sub.get("type")
        if stype == "content.part":
            part = sub.get("part") or {}
            ptype = part.get("type")
            if ptype == "think":
                think = part.get("think")
                if isinstance(think, str) and think:
                    return f"[{ts}] [ASSISTANT reasoning]\n{think[:MAX_REASONING]}"
                return None
            if ptype == "text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    return f"[{ts}] [ASSISTANT]\n{text[:MAX_CONTENT]}"
                return None
            return None
        if stype == "tool.call":
            name = sub.get("name", "?")
            return f"[{ts}] [TOOL → {name}] {self._compact_args(sub.get('args', {}))}"
        # tool.result (tool output), step.begin/step.end (usage noise): excluded.
        return None

    # ---- Grok Build (chat_history.jsonl) --------------------------------

    @staticmethod
    def _is_grok_event(event: dict) -> bool:
        return event.get("type") in ("system", "user", "assistant", "reasoning", "tool_result")

    @staticmethod
    def _grok_session(event: dict, ts: str) -> str:
        parts = [f"[{ts}] [SESSION START]", "source: grok-build"]
        content = event.get("content")
        if isinstance(content, str) and "Grok" in content[:80]:
            parts.append("model: grok")
        return " | ".join(parts)

    @staticmethod
    def _grok_text(event: dict) -> str:
        content = event.get("content")
        if isinstance(content, str):
            return content
        parts: list[str] = []
        for item in content or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in ("text", "input_text", "output_text"):
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _grok_reasoning(event: dict) -> str:
        parts: list[str] = []
        for item in event.get("summary") or []:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif isinstance(item, str) and item:
                parts.append(item)
        return "\n".join(parts)

    def _grok_assistant(self, event: dict, ts: str) -> str | None:
        parts: list[str] = []
        text = self._grok_text(event)
        if text:
            parts.append(f"[{ts}] [ASSISTANT]\n{text[:MAX_CONTENT]}")
        for call in event.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            name = call.get("name", "?")
            parts.append(
                f"[{ts}] [TOOL → {name}] {self._compact_args(call.get('arguments', {}))}"
            )
        return "\n".join(parts) if parts else None

    # ---- Reasonix (DeepSeek) schema helpers ---------------------------------
    #
    # Layout: ~/.reasonix/projects/<encoded-cwd>/sessions/<ts>-<agent>.jsonl
    # Events are OpenAI-chat-format objects keyed by role:
    #   user      → {role, content, raw_content, createdAt}   (content carries a
    #               <response-language> wrapper; raw_content is the clean text)
    #   assistant → {role, content, reasoning_content, workDurationMs}
    #   tool      → {role, name, content, tool_call_id, ...}
    #   system    → instructions (excluded)

    @staticmethod
    def _is_reasonix_event(event: dict) -> bool:
        role = event.get("role")
        if role not in ("user", "assistant", "tool"):
            return False
        return (
            "raw_content" in event
            or "reasoning_content" in event
            or "tool_call_id" in event
        )

    def _reasonix_user(self, event: dict, ts: str) -> str | None:
        text = event.get("raw_content") or event.get("content")
        if not isinstance(text, str) or not text.strip():
            return None
        return f"[{ts}] [USER]\n{text[:MAX_CONTENT]}"

    def _reasonix_assistant(self, event: dict, ts: str) -> str | None:
        parts: list[str] = []
        reasoning = event.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            parts.append(f"[{ts}] [ASSISTANT reasoning]\n{reasoning[:MAX_REASONING]}")
        content = event.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(f"[{ts}] [ASSISTANT]\n{content[:MAX_CONTENT]}")
        return "\n".join(parts) if parts else None

    def _reasonix_tool(self, event: dict, ts: str) -> str | None:
        name = event.get("name") or "tool"
        content = event.get("content")
        if not isinstance(content, str):
            content = json.dumps(content or {}, ensure_ascii=False)
        text = str(content).replace("\n", " ").strip()
        if not text:
            text = "(empty)"
        return f"[{ts}] [TOOL → {name}] {text[:MAX_TOOL_ARGS]}"

    @staticmethod
    def _compact_args(args) -> str:
        try:
            compact = json.dumps(args, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            compact = str(args)
        return compact if len(compact) <= MAX_TOOL_ARGS else compact[:MAX_TOOL_ARGS] + "…"
