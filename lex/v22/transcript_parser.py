"""TranscriptParser — convert Copilot transcript JSONL → structured text for LLM.

Event types verified on real data (session 9d8a10fd, 177 lines):
  - session.start     : metadata (model, vscode version)
  - user.message      : {content, attachments}              ← Guglielmo's prompt
  - assistant.message : {content, toolRequests, reasoningText}  ← Lex reply + thinking
  - tool.execution_start   : {toolName, arguments}
  - tool.execution_complete: {toolName, arguments, ...}

reasoningText is HIGH VALUE: it exposes Lex's decision process, which makes
classification far more accurate than reading only the final responses.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("neural-tape-v22")

# Truncation limits per field (keep transcript cheap for the classifier)
MAX_REASONING = 2000
MAX_CONTENT = 3000
MAX_TOOL_ARGS = 200


class TranscriptParser:
    """Parse a transcript delta (from byte offset) into LLM-readable text."""

    def parse_delta(self, transcript: Path, offset: int = 0) -> str:
        """Read transcript from byte offset, return structured text."""
        lines: list[str] = []
        with open(transcript, "rb") as f:
            f.seek(offset)
            for raw in f:
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
        """Return structured counts for logging (no formatting)."""
        counts = {
            "user": 0,
            "assistant": 0,
            "reasoning": 0,
            "tool_calls": 0,
            "total_events": 0,
        }
        with open(transcript, "rb") as f:
            f.seek(offset)
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                counts["total_events"] += 1
                etype = event.get("type")
                if etype == "user.message":
                    counts["user"] += 1
                elif etype == "assistant.message":
                    counts["assistant"] += 1
                    data = event.get("data", {})
                    if isinstance(data, dict) and data.get("reasoningText"):
                        counts["reasoning"] += 1
                elif etype in ("tool.execution_start", "tool.execution_complete"):
                    counts["tool_calls"] += 1
        return counts

    # ─── Event formatters ───────────────────────────────────────────────

    def _format_event(self, event: dict) -> str | None:
        etype = event.get("type")
        data = event.get("data", {})
        ts = event.get("timestamp", "")[:19]

        if etype == "session.start":
            return self._fmt_session_start(data, ts)

        if etype == "user.message":
            return self._fmt_user(data, ts)

        if etype == "assistant.message":
            return self._fmt_assistant(data, ts)

        if etype in ("tool.execution_start", "tool.execution_complete"):
            return self._fmt_tool(event, data, ts)

        return None

    def _fmt_session_start(self, data: dict, ts: str) -> str:
        if not isinstance(data, dict):
            return ""
        model = data.get("model", "")
        producer = data.get("producer", "")
        parts = [f"[{ts}] [SESSION START]"]
        if model:
            parts.append(f"model: {model}")
        if producer:
            parts.append(f"producer: {producer}")
        return " | ".join(parts)

    def _fmt_user(self, data, ts: str) -> str:
        if isinstance(data, dict):
            content = data.get("content", "")
        else:
            content = str(data)
        if not content:
            return ""
        return f"[{ts}] [USER]\n{content}"

    def _fmt_assistant(self, data: dict, ts: str) -> str:
        if not isinstance(data, dict):
            return ""
        parts: list[str] = []
        reasoning = data.get("reasoningText", "")
        content = data.get("content", "")
        if reasoning:
            parts.append(
                f"[{ts}] [LEX reasoning]\n{reasoning[:MAX_REASONING]}"
            )
        if content:
            parts.append(f"[{ts}] [LEX]\n{content[:MAX_CONTENT]}")
        return "\n".join(parts) if parts else ""

    def _fmt_tool(self, event: dict, data: dict, ts: str) -> str:
        if not isinstance(data, dict):
            return ""
        name = data.get("toolName") or data.get("name", "?")
        args = data.get("arguments", {})
        args_str = self._compact_args(args)
        is_complete = event.get("type", "").endswith("complete")
        marker = "✓" if is_complete else "→"
        return f"[{ts}] [TOOL {marker} {name}] {args_str}"

    def _compact_args(self, args) -> str:
        """Truncate large tool arguments to keep transcript compact."""
        try:
            s = json.dumps(args, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            s = str(args)
        if len(s) > MAX_TOOL_ARGS:
            s = s[:MAX_TOOL_ARGS] + "…"
        return s
