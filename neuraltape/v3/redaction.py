"""redaction — secret redaction layer before LLM payload (D0.1).

Goal: NO secret reaches the cloud classifier (DeepSeek). We replace matches with
``[REDACTED:<kind>]`` so debugging knows what was caught without exposing the value.

Patterns are ordered from most-specific to most-generic to avoid double-redaction
(e.g. a JWT should be caught by the JWT rule, not by the generic assignment rule).

False-positive avoidance: the generic ``api[_-]?key|secret|token|password`` rule
requires an actual assignment-like form (``key = value`` or ``key: value``) with a
value long enough (>=16 chars) to be plausibly a secret. Function names like
``def api_key_handler`` are NOT matched because they lack the ``=`` / ``:`` separator.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("neural-tape-v3")


@dataclass
class RedactionEvent:
    kind: str
    start: int
    length: int
    snippet: str = ""   # first 12 chars of the matched value, for debug only


@dataclass
class RedactionStats:
    total: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)


# (compiled_regex, kind). Order matters: most specific first.
_DEFAULT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # --- Private keys (PEM blocks) ---
    (re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |)PRIVATE KEY-----.*?-----END (?:RSA |EC |DSA |OPENSSH |PGP |)PRIVATE KEY-----",
        re.DOTALL,
    ), "private-key-pem"),

    # --- AWS: long-lived (AKIA) and STS temporary (ASIA) access-key IDs ---
    # Exact published format (4-char prefix + 16) with word boundaries: an
    # open tail would destroy ordinary uppercase text ("ASIAEAST1CLUSTER").
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "aws-access-key-id"),
    (re.compile(
        r"(?i)aws(_|-)?(secret|access(_|-)?key|session(_|-)?token)\b[\"\s'=:]*\b([A-Za-z0-9/+=]{40})\b"
    ), "aws-secret"),

    # --- GitHub (all prefixes + fine-grained PATs) ---
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"), "github-token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "github-token"),

    # --- Slack (bot/user/admin/app-level + refresh) ---
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "slack-token"),
    (re.compile(r"\bxapp-[A-Za-z0-9-]{10,}\b"), "slack-token"),

    # --- Stripe secret + restricted keys (port from ai-memory sanitize.rs) ---
    (re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9_\-]{16,}\b"), "stripe-key"),

    # --- Google: bare API keys + OAuth refresh tokens ---
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}\b"), "google-api-key"),
    (re.compile(r"\b1//[0-9A-Za-z_\-]{20,}\b"), "google-oauth-refresh"),

    # --- Meta / Facebook Graph API access tokens ---
    (re.compile(r"\bEAA[A-Za-z0-9]{20,}\b"), "meta-graph-token"),

    # --- Telegram bot tokens (<bot-id>:<secret>). Two branches: the AA shape
    # every issued token takes, and the length-anchored documented example
    # shape (a bare \d+:word rule would eat timestamps and host:port pairs).
    (re.compile(r"\b\d{6,10}:(?:AA[A-Za-z0-9_\-]{30,}|[A-Za-z0-9_\-]{34,35})\b"),
     "telegram-bot-token"),

    # --- GoHighLevel Private Integration Tokens (UUID-anchored: `pit-` is an
    # English fragment, so a permissive tail would redact "pit-stop-strategy").
    (re.compile(
        r"\bpit-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    ), "gohighlevel-token"),

    # --- Google / GCP service account JSON ---
    (re.compile(
        r'"type"\s*:\s*"service_account"[^}]{0,2000}"private_key"\s*:',
        re.DOTALL,
    ), "gcp-service-account"),

    # --- JWT (3 base64url segments separated by dots, starts with ey) ---
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
     "jwt"),

    # --- Bearer tokens in headers ---
    (re.compile(r"\b[Bb]earer\s+[A-Za-z0-9_\-\.=]{20,}"), "bearer-token"),

    # --- Credentials embedded in URLs: scheme://user:pass@host ---
    (re.compile(r"(?i)\b(https?|ftp|postgres(?:ql)?|redis|mongodb)://[^/\s:@]{1,64}:[^/@\s]{1,64}@"),
     "url-credentials"),

    # --- DeepSeek / OpenAI / Anthropic key prefixes ---
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "llm-api-key"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"), "anthropic-api-key"),

    # --- Generic assignment: key = value OR key: value, value length >= 16 ---
    # Must have a separator and a long opaque value. Avoids `def api_key_handler`.
    (re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|access[_-]?token|auth[_-]?token|client[_-]?secret)"
        r"\s*[:=]\s*[\"\']?([A-Za-z0-9_\-+/=]{16,})[\"\']?"
    ), "generic-assignment"),

    # --- Provider env-var assignments (port from ai-memory sanitize.rs):
    # explicit so `OPENAI_API_KEY=anything` triggers even without sk- shape.
    (re.compile(
        r"(?i)\b(ANTHROPIC_API_KEY|OPENAI_API_KEY|OPENROUTER_API_KEY|DEEPSEEK_API_KEY|"
        r"VOYAGE_API_KEY|MISTRAL_API_KEY|GROQ_API_KEY|HF_TOKEN|HUGGINGFACE_TOKEN|"
        r"AWS_(SECRET_)?ACCESS_KEY[A-Z_]*|GITHUB_TOKEN|GH_TOKEN|GITLAB_TOKEN|"
        r"GOOGLE_API_KEY|GEMINI_API_KEY|OLLAMA_API_KEY|LLM_API_KEY)\s*[=:]\s*\S+"
    ), "provider-env"),

    # --- Generic env-var catch-all: UPPER_CASE *_KEY/_TOKEN/_SECRET/... = value
    (re.compile(
        r"\b[A-Z][A-Z0-9_]*_(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|CREDENTIALS|PRIVATE_KEY)"
        r"\s*[=:]\s*\S+"
    ), "generic-env"),

    # --- Filesystem paths that commonly contain credentials ---
    (re.compile(r"(?:/[^/\s]+)*/\.(ssh|aws|kube|gnupg)(?:/[^\s]*)?"), "credential-path"),
    (re.compile(r"(?:/[^/\s]+)*/\.config/gcloud(?:/[^\s]*)?"), "credential-path"),
]


class Redactor:
    """Redact known secret patterns from text. Replaces with [REDACTED:<kind>].

    ``allowlist`` holds substrings that are never redacted, checked per match:
    a pattern still runs, but a matched span containing an allowlisted entry
    survives unchanged (e.g. a project codename colliding with the generic
    env-var catch-all). Port of ai-memory's `[sanitize].allowlist` semantics.
    """

    def __init__(
        self,
        extra_patterns: list[tuple[str, str]] | None = None,
        allowlist: list[str] | None = None,
    ):
        self._patterns: list[tuple[re.Pattern, str]] = list(_DEFAULT_PATTERNS)
        for regex, kind in (extra_patterns or []):
            try:
                self._patterns.append((re.compile(regex), kind))
            except re.error as e:
                log.warning("Skipping invalid redaction regex %r (%s)", regex, e)
        self._allowlist = [entry for entry in (allowlist or []) if entry]

    def redact(self, text: str) -> tuple[str, list[RedactionEvent]]:
        """Return (redacted_text, events). Non-destructive: input is unchanged."""
        events: list[RedactionEvent] = []
        # Apply patterns sequentially; each operates on the current working string.
        # Because earlier (specific) patterns replace matches with [REDACTED:...],
        # later (generic) patterns won't re-match the redacted placeholder.
        working = text
        for regex, kind in self._patterns:
            working = regex.sub(
                lambda m: self._emit(m, kind, events),
                working,
            )
        return working, events

    def stats(self, events: list[RedactionEvent]) -> RedactionStats:
        s = RedactionStats(total=len(events))
        for ev in events:
            s.by_kind[ev.kind] = s.by_kind.get(ev.kind, 0) + 1
        return s

    def summary(self, events: list[RedactionEvent]) -> str:
        if not events:
            return "redaction: clean (0 secrets)"
        s = self.stats(events)
        parts = [f"{k}={v}" for k, v in sorted(s.by_kind.items())]
        return f"redaction: {s.total} secret(s) redacted — " + ", ".join(parts)

    # ---- internals ------------------------------------------------------

    def _emit(self, match: re.Match, kind: str, sink: list[RedactionEvent]) -> str:
        matched_text = match.group(0)
        # Per-match allowlist: a span containing an allowlisted entry survives.
        if any(entry in matched_text for entry in self._allowlist):
            return matched_text
        start, end = match.span()
        # Snippet for debug: first 12 chars of the matched text (not the value per se,
        # but of the whole match, which often includes the label too — we cap length).
        snippet = matched_text[:12]
        sink.append(RedactionEvent(
            kind=kind, start=start, length=end - start, snippet=snippet,
        ))
        return f"[REDACTED:{kind}]"
