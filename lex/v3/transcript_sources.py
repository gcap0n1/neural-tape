"""Data-driven discovery manifests for assistant transcript stores.

Each supported agent is described by a :class:`SourceManifest`: where its
transcripts live (``base`` + ``globs``), how the session id and workspace
label are derived, and which subagent transcripts must be skipped.  The
:class:`SourceRegistry` consumes manifests and answers the discovery
questions the cron tick, backfill and harvest tools ask the
TranscriptWatcher.  Adding a new agent means adding a manifest — built-in
or via the ``sources`` config section — not editing the watcher.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence
from urllib.parse import unquote

# ── strategy constants ──────────────────────────────────────────────────────

SESSION_FILE_STEM = "file_stem"
SESSION_PARENT_DIR = "parent_dir"
SESSION_KIMI_DIR = "kimi_session_dir"

WS_VSCODE = "vscode"
WS_CODEX_CWD = "codex_cwd"
WS_KIMI_WD = "kimi_wd"
WS_GROK_ENCODED = "grok_urlencoded"
WS_REASONIX_PROJECT = "reasonix_project"
WS_PARENT_DIR = "parent_dir"

FILTER_CODEX = "codex"
FILTER_GROK = "grok"
FILTER_REASONIX = "reasonix"

_SESSION_STRATEGIES = {SESSION_FILE_STEM, SESSION_PARENT_DIR, SESSION_KIMI_DIR}
_WORKSPACE_STRATEGIES = {WS_VSCODE, WS_CODEX_CWD, WS_KIMI_WD, WS_GROK_ENCODED, WS_REASONIX_PROJECT, WS_PARENT_DIR}
_FILTERS = {"", FILTER_CODEX, FILTER_GROK, FILTER_REASONIX}

_KIMI_WD_RE = re.compile(r"^wd_(?P<label>.+)_[0-9a-f]{12}$")


# ── manifest ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SourceManifest:
    """Immutable description of one assistant transcript store."""

    id: str
    label: str
    base: Path
    globs: tuple[str, ...]
    session_id: str = SESSION_FILE_STEM
    workspace: str = WS_PARENT_DIR
    subagent_filter: str = ""


# ── built-in manifests ──────────────────────────────────────────────────────

def builtin_sources(
    home: Path,
    *,
    vscode_user: Path | None = None,
    codex_home: Path | None = None,
    kimi_home: Path | None = None,
    grok_home: Path | None = None,
    env: dict[str, str] | None = None,
) -> list[SourceManifest]:
    """Return the five built-in manifests with env-overridable bases.

    Each source id can be overridden via ``NEURALTAPE_<ID>_HOME`` (e.g.
    ``NEURALTAPE_CODEX_HOME``).  Explicit constructor kwargs take
    precedence over env, which takes precedence over the default.

    The ``vscode_user`` -> ``copilot`` base path is the only one that
    follows the VS Code user-data directory convention rather than a
    single ``~/.agent`` directory.
    """
    home = Path(home).expanduser()
    env = dict(env or os.environ)

    def _base(
        source_id: str,
        default: Path,
        explicit: Path | None,
        native_env: str | None = None,
    ) -> Path:
        if explicit is not None:
            return Path(explicit).expanduser()
        raw = env.get(f"NEURALTAPE_{source_id.upper()}_HOME")
        if raw:
            return Path(raw).expanduser()
        if native_env:
            native = env.get(native_env)
            if native:
                return Path(native).expanduser()
        return default

    vscode_user = vscode_user or home / ".config" / "Code" / "User"

    return [
        SourceManifest(
            id="copilot",
            label="VS Code Copilot",
            base=Path(vscode_user).expanduser(),
            globs=(
                "workspaceStorage/*/GitHub.copilot-chat/transcripts/*.jsonl",
            ),
            session_id=SESSION_FILE_STEM,
            workspace=WS_VSCODE,
        ),
        SourceManifest(
            id="codex",
            label="Codex CLI",
            base=_base("codex", home / ".codex", codex_home),
            globs=("sessions/**/*.jsonl", "archived_sessions/*.jsonl"),
            session_id=SESSION_FILE_STEM,
            workspace=WS_CODEX_CWD,
            subagent_filter=FILTER_CODEX,
        ),
        SourceManifest(
            id="kimi",
            label="Kimi Code",
            base=_base("kimi", home / ".kimi-code", kimi_home),
            globs=("sessions/*/*/agents/main/wire.jsonl",),
            session_id=SESSION_KIMI_DIR,
            workspace=WS_KIMI_WD,
        ),
        SourceManifest(
            id="grok",
            label="Grok Build",
            base=_base("grok", home / ".grok", grok_home),
            globs=("sessions/*/*/chat_history.jsonl",),
            session_id=SESSION_PARENT_DIR,
            workspace=WS_GROK_ENCODED,
            subagent_filter=FILTER_GROK,
        ),
        SourceManifest(
            id="reasonix",
            label="DeepSeek Reasonix",
            base=_base("reasonix", home / ".reasonix", None, native_env="REASONIX_HOME"),
            globs=("projects/*/sessions/*.jsonl",),
            session_id=SESSION_FILE_STEM,
            workspace=WS_REASONIX_PROJECT,
            subagent_filter=FILTER_REASONIX,
        ),
    ]


def manifest_from_dict(
    source_id: str,
    data: dict,
    base: Path,
) -> SourceManifest | None:
    """Build a manifest from a ``sources.custom.<id>`` config entry.

    *base* is already resolved (expanduser + relative-to-tape-root).
    Returns ``None`` for invalid entries (missing base/globs, unknown
    strategy values).
    """
    if not isinstance(data, dict):
        return None
    globs = data.get("globs")
    if not isinstance(globs, list) or not globs or not all(isinstance(g, str) and g for g in globs):
        return None
    session_id = data.get("session_id", SESSION_FILE_STEM)
    workspace = data.get("workspace", WS_PARENT_DIR)
    subagent_filter = data.get("subagent_filter", "")
    if (
        session_id not in _SESSION_STRATEGIES
        or workspace not in _WORKSPACE_STRATEGIES
        or subagent_filter not in _FILTERS
    ):
        return None
    return SourceManifest(
        id=str(source_id),
        label=str(data.get("label") or source_id),
        base=Path(base).expanduser(),
        globs=tuple(globs),
        session_id=session_id,
        workspace=workspace,
        subagent_filter=subagent_filter,
    )


# ── registry ─────────────────────────────────────────────────────────────────

class SourceRegistry:
    """Per-source discovery strategies over a list of manifests."""

    def __init__(self, sources: Sequence[SourceManifest] = ()):
        self._sources = list(sources)

    @property
    def sources(self) -> list[SourceManifest]:
        return list(self._sources)

    def match(self, transcript: Path) -> SourceManifest | None:
        """Return the manifest whose base contains *transcript*.

        Longest-prefix wins so that a nested custom source is preferred
        over a broader built-in whose base is a parent directory.
        """
        path = Path(transcript).resolve()
        best: SourceManifest | None = None
        best_len = -1
        for source in self._sources:
            try:
                path.relative_to(source.base.resolve())
            except ValueError:
                continue
            length = len(source.base.resolve().parts)
            if length > best_len:
                best, best_len = source, length
        return best

    def all_paths(self) -> Iterator[Path]:
        """Yield every transcript path matching any manifest."""
        for source in self._sources:
            for pattern in source.globs:
                yield from _glob(pattern, source.base)

    # ── per-source strategies ────────────────────────────────────────────

    def session_id_of(self, transcript: Path) -> str | None:
        """Derive session id from the matched manifest's strategy."""
        source = self.match(transcript)
        if source is None:
            return None
        return _session_id(Path(transcript), source)

    def workspace_label_of(self, transcript: Path) -> str | None:
        source = self.match(transcript)
        return _workspace_label(Path(transcript), source) if source else None

    def should_index(self, transcript: Path) -> bool:
        source = self.match(transcript)
        if source is None:
            return True
        return _should_index(Path(transcript), source)


# ── strategy implementations ─────────────────────────────────────────────────

def _session_id(path: Path, source: SourceManifest) -> str:
    if source.session_id == SESSION_PARENT_DIR:
        return path.parent.name
    if source.session_id == SESSION_KIMI_DIR:
        for parent in path.parents:
            if parent.name.startswith("session_"):
                return parent.name
        return path.stem
    return path.stem


def _workspace_label(path: Path, source: SourceManifest) -> str:
    if source.workspace == WS_VSCODE:
        return _vscode_workspace_label(path)
    if source.workspace == WS_CODEX_CWD:
        cwd = _codex_cwd(path)
        return Path(cwd).name if cwd else "unknown"
    if source.workspace == WS_KIMI_WD:
        sessions = source.base.resolve() / "sessions"
        for parent in path.parents:
            if parent.parent == sessions:
                match = _KIMI_WD_RE.match(parent.name)
                return match.group("label") if match else parent.name
        return "unknown"
    if source.workspace == WS_GROK_ENCODED:
        encoded = path.parent.parent.name
        return Path(unquote(encoded)).name or "unknown"
    if source.workspace == WS_REASONIX_PROJECT:
        # Reasonix encodes the cwd by replacing every "/" with "-":
        # /home/gcap0n1 → -home-gcap0n1; /run/media/x/Back-Up/Proj → -run-media-x-Back-Up-Proj.
        # Decoding is lossy for names containing "-", so we take the last
        # component after re-splitting — the workspace name.
        encoded = path.parent.parent.name
        decoded = encoded[1:].replace("-", "/") if encoded.startswith("-") else encoded
        return Path(decoded).name or "unknown"
    return path.parent.name or "unknown"


def _should_index(path: Path, source: SourceManifest) -> bool:
    if source.subagent_filter == FILTER_GROK:
        return not _grok_is_subagent(path.parent)
    if source.subagent_filter == FILTER_CODEX:
        return not _codex_is_subagent(path)
    if source.subagent_filter == FILTER_REASONIX:
        # Skip telemetry/recovery sidecar files; keep the conversation file.
        return not (
            path.name.endswith(".events.jsonl")
            or path.name.endswith(".conflicts.jsonl")
        )
    return True


# ── helpers ──────────────────────────────────────────────────────────────────

def _glob(pattern: str, base: Path) -> Iterator[Path]:
    full = Path(pattern)
    if full.is_absolute():
        yield from Path(full.parent).glob(full.name)
    else:
        yield from base.glob(pattern)


def _vscode_workspace_label(path: Path) -> str:
    """Legacy Copilot workspace label: workspace.json folder name or hash[0:8]."""
    hash_dir = Path(path).resolve().parent.parent.parent
    workspace_json = hash_dir / "workspace.json"
    if workspace_json.exists():
        try:
            data = json.loads(workspace_json.read_text(encoding="utf-8"))
            folder = data.get("folder") or data.get("workspace")
            if folder:
                return Path(folder).name
        except (json.JSONDecodeError, OSError):
            pass
    return hash_dir.name[:8]


def legacy_workspace_label(path: Path) -> str:
    """Public fallback for unmatched transcripts (same as vscode logic)."""
    return _vscode_workspace_label(path)


def _codex_cwd(transcript: Path) -> str | None:
    try:
        with Path(transcript).open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "session_meta":
                    continue
                payload = event.get("payload") or {}
                cwd = payload.get("cwd") if isinstance(payload, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def _grok_is_subagent(session_dir: Path) -> bool:
    summary = session_dir / "summary.json"
    if not summary.exists():
        return False
    try:
        data = json.loads(summary.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if data.get("parent_session_id"):
        return True
    text = str(data.get("session_summary") or "")
    return "Harness" in text or "Goal Plan Writer" in text or "verifier" in text.lower()


def _codex_is_subagent(transcript: Path) -> bool:
    try:
        with Path(transcript).open("r", encoding="utf-8", errors="replace") as stream:
            for _ in range(20):
                line = stream.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "session_meta":
                    continue
                payload = event.get("payload") or {}
                if not isinstance(payload, dict):
                    return False
                if payload.get("forked_from_id") or payload.get("agent_nickname"):
                    return True
                source = payload.get("source")
                return isinstance(source, dict) and "subagent" in source
    except OSError:
        return False
    return False