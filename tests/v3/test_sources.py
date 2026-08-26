"""Tests for the data-driven transcript source registry (Fase 1)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from nt_v3.transcript_sources import (
    SESSION_PARENT_DIR,
    WS_PARENT_DIR,
    SourceManifest,
    SourceRegistry,
    builtin_sources,
)
from nt_v3.transcript_watcher import TranscriptWatcher


def test_builtins_have_six_sources():
    with tempfile.TemporaryDirectory(prefix="nt-src-") as tmp:
        home = Path(tmp)
        sources = builtin_sources(home)
        assert [s.id for s in sources] == [
            "copilot", "codex", "kimi", "grok", "reasonix", "omp",
        ]
        home_r = home.resolve()
        for s in sources:
            assert s.base.resolve() == home_r or home_r in s.base.resolve().parents
        assert builtin_sources(home)[1].globs == (
            "sessions/**/*.jsonl", "archived_sessions/*.jsonl",
        )


def test_env_override_moves_codex_base():
    with tempfile.TemporaryDirectory(prefix="nt-src-") as tmp:
        home = Path(tmp)
        custom = home / "codex-custom"
        sources = builtin_sources(home, env={"NEURALTAPE_CODEX_HOME": str(custom)})
        codex = next(s for s in sources if s.id == "codex")
        assert codex.base == custom
        # Other sources keep their defaults.
        grok = next(s for s in sources if s.id == "grok")
        assert grok.base == home / ".grok"


def test_registry_matches_by_base_and_prefers_longest_prefix():
    with tempfile.TemporaryDirectory(prefix="nt-src-") as tmp:
        home = Path(tmp)
        broad = SourceManifest(
            id="broad", label="Broad", base=home, globs=("**/*.jsonl",),
        )
        registry = SourceRegistry([broad, *builtin_sources(home)])

        copilot = home / ".config/Code/User/workspaceStorage/abc/GitHub.copilot-chat/transcripts/x.jsonl"
        assert registry.match(copilot).id == "copilot"

        stray = home / "misc" / "x.jsonl"
        assert registry.match(stray).id == "broad"
        assert registry.match(Path("/etc/hosts")) is None


def test_registry_kimi_session_id_and_workspace_label():
    with tempfile.TemporaryDirectory(prefix="nt-src-") as tmp:
        home = Path(tmp)
        wire = (
            home / ".kimi-code" / "sessions" / "wd_etercervo_59c41eae8e68"
            / "session_0a6fbae6-964c-46a2-8125-c8ce343fbc4b"
            / "agents" / "main" / "wire.jsonl"
        )
        registry = SourceRegistry(builtin_sources(home))
        assert registry.match(wire).id == "kimi"
        assert registry.session_id_of(wire) == "session_0a6fbae6-964c-46a2-8125-c8ce343fbc4b"
        assert registry.workspace_label_of(wire) == "etercervo"


def test_registry_codex_cwd_label_and_grok_encoded_label():
    with tempfile.TemporaryDirectory(prefix="nt-src-") as tmp:
        home = Path(tmp)
        rollout = home / ".codex" / "sessions" / "2026" / "08" / "r.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text(
            json.dumps({"type": "session_meta", "payload": {"cwd": "/work/Zeus"}}) + "\n",
            encoding="utf-8",
        )
        chat = home / ".grok" / "sessions" / "%2Fwork%2FMyWorkspace" / "u1" / "chat_history.jsonl"
        registry = SourceRegistry(builtin_sources(home))
        assert registry.workspace_label_of(rollout) == "Zeus"
        assert registry.workspace_label_of(chat) == "MyWorkspace"
        assert registry.session_id_of(chat) == "u1"


def test_watcher_discovers_custom_source():
    with tempfile.TemporaryDirectory(prefix="nt-src-") as tmp:
        home = Path(tmp)
        reasonix = home / ".reasonix" / "sessions" / "sess-1" / "conversation.jsonl"
        reasonix.parent.mkdir(parents=True, exist_ok=True)
        reasonix.write_text("{}\n", encoding="utf-8")

        manifest = SourceManifest(
            id="reasonix",
            label="DeepSeek Reasonix",
            base=home / ".reasonix" / "sessions",
            globs=("**/*.jsonl",),
            session_id=SESSION_PARENT_DIR,
            workspace=WS_PARENT_DIR,
        )
        watcher = TranscriptWatcher(home=home, extra_sources=[manifest])
        found = {path for _, path in watcher.find_all_transcripts(max_age_minutes=60)}

        assert reasonix.resolve() in found
        assert watcher.session_id_of(reasonix) == "sess-1"
        assert watcher.get_workspace_label(reasonix) == "sess-1"


def test_watcher_respects_disabled_sources():
    with tempfile.TemporaryDirectory(prefix="nt-src-") as tmp:
        home = Path(tmp)
        rollout = home / ".codex" / "sessions" / "2026" / "08" / "r.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text(
            json.dumps({"type": "session_meta", "payload": {"cwd": "/work/Zeus"}}) + "\n",
            encoding="utf-8",
        )

        watcher = TranscriptWatcher(home=home, disabled={"codex"})
        found = {path for _, path in watcher.find_all_transcripts(max_age_minutes=60)}
        assert rollout.resolve() not in found


def test_config_parses_custom_and_disabled_sources():
    from nt_v3.config import load

    with tempfile.TemporaryDirectory(prefix="nt-src-") as tmp:
        root = Path(tmp)
        cfg_path = root / "config.yaml"
        cfg_path.write_text(
            """
v3:
  enabled: true
  sources:
    disabled: [codex, grok]
    custom:
      reasonix:
        label: DeepSeek Reasonix
        base: "~/.reasonix/sessions"
        globs: ["**/*.jsonl"]
        session_id: parent_dir
        workspace: parent_dir
""",
            encoding="utf-8",
        )
        cfg = load(root, config_path=cfg_path)
        assert cfg.disabled_sources == {"codex", "grok"}
        assert [s.id for s in cfg.sources] == ["reasonix"]
        assert cfg.sources[0].base == Path("~/.reasonix/sessions").expanduser().resolve()
        assert cfg.sources[0].globs == ("**/*.jsonl",)
        assert cfg.sources[0].session_id == SESSION_PARENT_DIR


def test_config_ignores_invalid_custom_source():
    from nt_v3.config import load

    with tempfile.TemporaryDirectory(prefix="nt-src-") as tmp:
        root = Path(tmp)
        cfg_path = root / "config.yaml"
        cfg_path.write_text(
            """
v3:
  enabled: true
  sources:
    custom:
      broken_globs:
        base: "/tmp/x"
        globs: "not-a-list"
      missing_base:
        globs: ["*.jsonl"]
      bad_strategy:
        base: "/tmp/y"
        globs: ["*.jsonl"]
        session_id: telepathy
""",
            encoding="utf-8",
        )
        cfg = load(root, config_path=cfg_path)
        assert cfg.sources == []
        assert cfg.disabled_sources == set()