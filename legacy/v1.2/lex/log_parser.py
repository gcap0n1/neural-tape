#!/usr/bin/env python3
"""
Neural Tape — Log Parser (Watchdog + Pattern Matching)
Watches AI assistant logs and auto-captures insights.
"""

import re
import sys
import json
import time
import argparse
import hashlib
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml")
    sys.exit(1)


# ── Privacy Filter (ported from agentmemory) ───────────────────────────

SECRET_PATTERNS = [
    # Generic key=value secrets (≥20 char values)
    (re.compile(r'(?i)(api[_-]?key|secret|token|password|credential|auth)\s*[=:]\s*["\']?([A-Za-z0-9_\-+/=]{20,})["\']?'), r'\1=[REDACTED_SECRET]'),
    # Bearer tokens
    (re.compile(r'(?i)bearer\s+[A-Za-z0-9_\-\.=]{20,}'), 'Bearer [REDACTED_SECRET]'),
    # OpenAI keys
    (re.compile(r'sk-[a-zA-Z0-9]{20,}'), '[REDACTED_SECRET]'),
    (re.compile(r'sk-ant-[a-zA-Z0-9]{20,}'), '[REDACTED_SECRET]'),
    # GitHub tokens
    (re.compile(r'ghp_[A-Za-z0-9]{36}'), '[REDACTED_SECRET]'),
    (re.compile(r'github_pat_[A-Za-z0-9_]{22,}'), '[REDACTED_SECRET]'),
    # Slack tokens
    (re.compile(r'xox[baprs]-[A-Za-z0-9\-]{10,}'), '[REDACTED_SECRET]'),
    # AWS keys
    (re.compile(r'AKIA[0-9A-Z]{16}'), '[REDACTED_SECRET]'),
    # Google API keys
    (re.compile(r'AIza[0-9A-Za-z_\-]{35}'), '[REDACTED_SECRET]'),
    # JWT tokens
    (re.compile(r'eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}'), '[REDACTED_SECRET]'),
    # npm tokens
    (re.compile(r'npm_[A-Za-z0-9]{36}'), '[REDACTED_SECRET]'),
    # GitLab PATs
    (re.compile(r'glpat-[A-Za-z0-9_\-]{20}'), '[REDACTED_SECRET]'),
    # Doppler secrets
    (re.compile(r'dp\.prod\.[A-Za-z0-9]{20,}'), '[REDACTED_SECRET]'),
]

PRIVATE_TAG = re.compile(r'<private>.*?</private>', re.DOTALL)


def strip_secrets(text: str) -> str:
    """Strip secrets from text. Ported from agentmemory privacy filter."""
    # Strip <private>...</private> blocks
    result = PRIVATE_TAG.sub('[REDACTED]', text)
    # Apply each secret pattern
    for pattern, replacement in SECRET_PATTERNS:
        result = pattern.sub(replacement if not replacement.startswith(r'\1') else replacement, result)
    return result


# ── SHA-256 Dedup (ported from agentmemory) ────────────────────────────

# Volatile tokens stripped before hashing so the same TYPE of event dedupes
# regardless of run IDs, session UUIDs, timestamps, port numbers, etc.
RE_VOLATILE = [
    re.compile(r'run=[0-9a-f]{8}'),                       # opencode run id
    re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'),  # session UUID
    re.compile(r'ses_[0-9a-zA-Z]+'),                      # opencode session token
    re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?'),  # ISO timestamps
    re.compile(r'timestamp=\S+'),
    re.compile(r'pid=\d+'),
    re.compile(r':\d{4,}'),                               # port numbers
]


def normalize_for_dedup(text: str) -> str:
    """Strip volatile tokens so the same event type dedupes across instances."""
    result = text
    for pattern in RE_VOLATILE:
        result = pattern.sub('<VOLATILE>', result)
    return result


class DedupMap:
    """Persistent dedup with TTL. Inspired by agentmemory DedupMap.

    State is persisted to a JSON file so restarts don't re-capture old events.
    """

    def __init__(self, ttl_seconds: int = 86400, state_file: Optional[Path] = None):
        self._map: Dict[str, float] = {}
        self._ttl = ttl_seconds
        self._state_file = state_file
        if state_file:
            self._load()

    def _hash(self, session_id: str, trigger: str, content: str) -> str:
        # Normalize content so volatile run/session IDs don't defeat dedup.
        normalized = normalize_for_dedup(content)
        key = f"{session_id}:{trigger}:{normalized[:500]}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def is_duplicate(self, session_id: str, trigger: str, content: str) -> bool:
        now = time.time()
        self._cleanup(now)
        h = self._hash(session_id, trigger, content)
        if h in self._map:
            return True
        self._map[h] = now
        if self._state_file:
            self._save()
        return False

    def _cleanup(self, now: float):
        expired = [k for k, t in self._map.items() if now - t > self._ttl]
        for k in expired:
            del self._map[k]
        if expired and self._state_file:
            self._save()

    def _load(self):
        if self._state_file and self._state_file.exists():
            try:
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
                # Drop entries already older than TTL on load
                now = time.time()
                self._map = {k: t for k, t in data.items() if now - t <= self._ttl}
            except (json.JSONDecodeError, OSError):
                self._map = {}

    def _save(self):
        if self._state_file:
            try:
                self._state_file.parent.mkdir(parents=True, exist_ok=True)
                self._state_file.write_text(
                    json.dumps(self._map), encoding="utf-8"
                )
            except OSError:
                pass  # Non-fatal: dedup degrades gracefully to in-memory


# ── Synthetic Compression (ported from agentmemory buildSyntheticCompression) ─

# Patterns for rule-based extraction (zero LLM)
RE_FILE_PATH = re.compile(r'([\/\w\-\.]+\.(?:py|js|ts|tsx|jsx|json|yaml|yml|md|txt|sh|sql|go|rs|java|c|cpp|h|hpp|toml|cfg|ini|conf))')
RE_ERROR_MSG = re.compile(r'(?:error|failed|exception|traceback)[:\s]*([^\n]{10,160})', re.IGNORECASE)
RE_TOOL_NAME = re.compile(r'(?:tool|function|command)[:\s]*([a-zA-Z_][\w\-\.]{2,40})', re.IGNORECASE)


def compress_synthetic(content: str, trigger: str, category: str) -> dict:
    """Rule-based compression. Returns dict with title, facts, narrative.
    
    Ported from agentmemory buildSyntheticCompression — zero LLM, pure regex extraction.
    """
    # Extract structured elements
    files = list(set(RE_FILE_PATH.findall(content)))[:5]
    error_match = RE_ERROR_MSG.search(content)
    tool_match = RE_TOOL_NAME.search(content)

    # Build title
    if error_match and category == "bug_found":
        title = f"{trigger} — {error_match.group(1).strip()[:60]}"
    elif tool_match:
        title = f"{trigger} — tool: {tool_match.group(1)}"
    elif files:
        title = f"{trigger} — {files[0]}"
    else:
        # Fallback: first 60 chars of content
        title = f"{trigger} — {content.strip()[:60]}"

    # Build facts
    facts = []
    if error_match:
        facts.append(f"Error: {error_match.group(1).strip()[:120]}")
    if tool_match:
        facts.append(f"Tool/Function: {tool_match.group(1)}")
    if files:
        facts.append(f"Files mentioned: {', '.join(files[:3])}")
    if not facts:
        facts.append(f"Trigger: {trigger}")
        facts.append(f"Content preview: {content.strip()[:100]}")

    # Build narrative
    if category == "bug_found" and error_match:
        narrative = f"Detected error during {trigger}: {error_match.group(1).strip()[:140]}"
    elif category == "warning":
        narrative = f"Warning captured by {trigger}: {content.strip()[:140]}"
    elif category == "code_change" and files:
        narrative = f"Code change involved file(s): {', '.join(files[:3])}"
    else:
        narrative = f"Auto-captured insight via {trigger}."

    return {
        "title": title.strip(),
        "facts": facts,
        "narrative": narrative.strip(),
    }

try:
    from watchdog.observers import Observer  # type: ignore[import-untyped]
    from watchdog.events import FileSystemEventHandler  # type: ignore[import-untyped]
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    print("Warning: watchdog not installed, falling back to polling mode")


@dataclass
class Insight:
    type: str
    session_id: str
    project: str
    timestamp: str
    confidence: str
    trigger: str
    content: str
    raw_log_line: str
    source: str = "log-parser"
    status: str = "staging"
    related: List[str] = field(default_factory=list)
    # Synthetic compression fields (ported from agentmemory)
    title: str = ""
    facts: List[str] = field(default_factory=list)
    narrative: str = ""

    def to_frontmatter(self) -> str:
        # Use compressed title if available, otherwise generic
        title_line = self.title if self.title else f"{self.type.upper()} — Auto-captured"
        lines = [
            "---",
            f"type: {self.type}",
            f"session_id: {self.session_id}",
            f"project: {self.project}",
            f"timestamp: {self.timestamp}",
            f"confidence: {self.confidence}",
            f"trigger: {self.trigger}",
            f"source: {self.source}",
            f"status: {self.status}",
            f"related: {self.related}",
            "---",
            "",
            f"# {title_line}",
            "",
        ]
        # Narrative (synthesis) if available
        if self.narrative:
            lines.append(self.narrative)
            lines.append("")
        # Facts if available
        if self.facts:
            lines.append("## Facts")
            for fact in self.facts:
                lines.append(f"- {fact}")
            lines.append("")
        # Raw content fallback
        lines.extend([
            f"{self.content}",
            "",
            "## Context",
            f"- Session: `{self.session_id}`",
            f"- Project: `{self.project}`",
            f"- Detected: {self.timestamp}",
            f"- Log reference: `{self.raw_log_line[:200]}`",
        ])
        return "\n".join(lines)


class Config:
    def __init__(self, path: Path):
        with open(path, "r", encoding="utf-8") as f:
            self.raw = yaml.safe_load(f)
        self.paths = self.raw.get("paths", {})
        self.log_parser = self.raw.get("log_parser", {})
        self.assistants = self.raw.get("assistants", {})

    def get_log_path(self, assistant: str = "kimi") -> Path:
        if assistant == "kimi":
            p = self.paths.get("kimi_logs", "")
        else:
            p = self.assistants.get(assistant, {}).get("log_path", "")
        return Path(p) if p else Path.home() / ".kimi" / "logs"

    def get_staging_dir(self) -> Path:
        root = self.paths.get("neural_tape_root", ".")
        return Path(root) / "tape" / "staging"

    def get_patterns(self, assistant: str = "kimi") -> Dict[str, Any]:
        assistant_patterns = self.assistants.get(assistant, {}).get("patterns")
        if assistant_patterns:
            return assistant_patterns
        return self.log_parser.get("patterns") or {}

    def is_pattern_enabled(self, pattern_name: str, assistant: str = "kimi") -> bool:
        """Check if a pattern is enabled (default: True)."""
        assistant_patterns = self.assistants.get(assistant, {}).get("patterns", {})
        if pattern_name in assistant_patterns:
            return assistant_patterns[pattern_name].get("enabled", True)
        return True


class LogParser:
    """Watchdog-based log parser for AI assistant logs."""

    def __init__(self, config: Config, assistant: str = "kimi"):
        self.config = config
        self.assistant = assistant
        self.watch_path = config.get_log_path(assistant)
        self.patterns = self._compile_patterns(config.get_patterns(assistant))
        self.staging_dir = config.get_staging_dir()
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        # Persistent state files (survive restarts so we don't re-read old logs)
        state_dir = self.staging_dir.parent / ".state"
        state_dir.mkdir(parents=True, exist_ok=True)
        self.offsets_file = state_dir / f"offsets-{assistant}.json"
        self.dedup_file = state_dir / f"dedup-{assistant}.json"
        self.file_offsets: Dict[str, int] = self._load_offsets()
        self.current_session: Optional[str] = None
        self.project: str = self._detect_project()
        self.dedup = DedupMap(ttl_seconds=86400, state_file=self.dedup_file)

    def _load_offsets(self) -> Dict[str, int]:
        """Load persisted byte offsets so restarts resume from last position."""
        if self.offsets_file.exists():
            try:
                data = json.loads(self.offsets_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {k: int(v) for k, v in data.items()}
            except (json.JSONDecodeError, OSError, ValueError):
                pass
        return {}

    def _save_offsets(self):
        """Persist current byte offsets to disk."""
        try:
            self.offsets_file.parent.mkdir(parents=True, exist_ok=True)
            self.offsets_file.write_text(
                json.dumps(self.file_offsets), encoding="utf-8"
            )
        except OSError:
            pass  # Non-fatal

    def _detect_project(self) -> str:
        """Auto-detect project from cwd or git."""
        cwd = Path.cwd()
        # Try git repo name
        git_dir = cwd / ".git"
        if git_dir.exists():
            return cwd.name
        return "default"

    def _compile_patterns(self, patterns: Dict) -> Dict[str, Any]:
        compiled = {}
        if not patterns:
            return compiled
        for name, spec in patterns.items():
            # Skip disabled patterns (enabled: false in config.yaml)
            if spec.get("enabled", True) is False:
                print(f"[NeuralTape] Pattern '{name}' disabled, skipping")
                continue
            try:
                compiled[name] = {
                    "regex": re.compile(spec["regex"]),
                    "category": spec.get("category", "meta"),
                    "confidence": spec.get("confidence", "low"),
                    "threshold": spec.get("threshold", None),
                }
            except re.error as e:
                print(f"Invalid regex for pattern '{name}': {e}")
        return compiled

    def _extract_session_id(self, line: str) -> Optional[str]:
        # Try to find session ID in line
        m = re.search(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", line)
        if m:
            return m.group(1)
        return self.current_session

    def process_line(self, line: str) -> Optional[Insight]:
        """Process single log line. Return Insight if matched."""
        session_id = self._extract_session_id(line) or "unknown"
        self.current_session = session_id

        for trigger_name, spec in self.patterns.items():
            m = spec["regex"].search(line)
            if m:
                # Check threshold if present
                if spec["threshold"] is not None:
                    try:
                        val = float(m.group(1))
                        if val < spec["threshold"]:
                            continue
                    except (ValueError, IndexError):
                        pass

                content = m.group(0) if not m.groups() else m.group(1)

                # Privacy filter: strip secrets before storage
                content = strip_secrets(content)
                raw_line_safe = strip_secrets(line.strip())

                # SHA-256 dedup: skip if same content seen recently
                if self.dedup.is_duplicate(session_id, trigger_name, content):
                    continue

                # Synthetic compression (rule-based, zero LLM)
                compressed = compress_synthetic(content, trigger_name, spec["category"])

                insight = Insight(
                    type=spec["category"],
                    session_id=session_id,
                    project=self.project,
                    timestamp=datetime.now().isoformat(),
                    confidence=spec["confidence"],
                    trigger=trigger_name,
                    content=content,
                    raw_log_line=raw_line_safe,
                    title=compressed["title"],
                    facts=compressed["facts"],
                    narrative=compressed["narrative"],
                )
                return insight
        return None

    def write_staging(self, insight: Insight) -> Path:
        """Write insight to staging directory."""
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        sid = insight.session_id[:8] if insight.session_id != "unknown" else "no-session"
        fname = f"{ts}-{sid}-{insight.type}.md"
        fpath = self.staging_dir / fname
        fpath.write_text(insight.to_frontmatter(), encoding="utf-8")
        print(f"[NeuralTape] Captured: {insight.type} ({insight.trigger}) -> {fname}")
        return fpath

    def process_file(self, log_file: Path, once: bool = False) -> List[Insight]:
        """Process a log file incrementally."""
        insights = []
        key = str(log_file)

        if not log_file.exists():
            return insights

        # Handle log rotation/truncation: if file shrank below saved offset,
        # the log was rotated — start from the beginning.
        current_size = log_file.stat().st_size
        saved_offset = self.file_offsets.get(key, 0)
        if saved_offset > current_size:
            saved_offset = 0
        offset = saved_offset

        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(offset)
            for line in f:
                insight = self.process_line(line)
                if insight:
                    self.write_staging(insight)
                    insights.append(insight)
            self.file_offsets[key] = f.tell()

        if self.file_offsets.get(key) != saved_offset:
            self._save_offsets()

        return insights

    def scan_existing(self) -> List[Insight]:
        """Process all existing log files once."""
        all_insights = []
        if not self.watch_path.exists():
            print(f"[NeuralTape] Log path not found: {self.watch_path}")
            return all_insights

        for log_file in self.watch_path.glob("*.log"):
            all_insights.extend(self.process_file(log_file))
        return all_insights

    def start_watchdog(self):
        """Start file system watcher."""
        if not WATCHDOG_AVAILABLE:
            print("Watchdog not available, use --polling")
            return

        class Handler(FileSystemEventHandler):
            def __init__(self, parser):
                self.parser = parser

            def on_modified(self, event):
                src = str(event.src_path)
                if not event.is_directory and src.endswith(".log"):
                    self.parser.process_file(Path(src))

            def on_created(self, event):
                src = str(event.src_path)
                if not event.is_directory and src.endswith(".log"):
                    self.parser.process_file(Path(src))

        observer = Observer()
        observer.schedule(Handler(self), str(self.watch_path), recursive=False)
        observer.start()
        print(f"[NeuralTape] Watching: {self.watch_path}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()

    def start_polling(self, interval: float = 2.0):
        """Start polling mode."""
        print(f"[NeuralTape] Polling: {self.watch_path} (interval={interval}s)")
        try:
            while True:
                self.scan_existing()
                time.sleep(interval)
        except KeyboardInterrupt:
            print("[NeuralTape] Stopped.")


def main():
    parser = argparse.ArgumentParser(description="Neural Tape Log Parser")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--assistant", default="kimi", help="Assistant name")
    parser.add_argument("--once", action="store_true", help="Process existing logs once and exit")
    parser.add_argument("--polling", action="store_true", help="Use polling instead of watchdog")
    parser.add_argument("--interval", type=float, default=2.0, help="Polling interval")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        # Try relative to script
        script_dir = Path(__file__).parent.parent
        config_path = script_dir / args.config

    config = Config(config_path)
    lp = LogParser(config, assistant=args.assistant)

    if args.once:
        insights = lp.scan_existing()
        print(f"[NeuralTape] Processed {len(insights)} insights.")
    elif args.polling:
        lp.start_polling(args.interval)
    else:
        lp.start_watchdog()


if __name__ == "__main__":
    main()
