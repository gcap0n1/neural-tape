"""Fase 3 item 3: idempotent hook installers/uninstallers (Claude writer +
snippet-only agents) and item 4: zero-LLM run gate."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from neuraltape.cli import main as cli_main  # noqa: E402
from neuraltape.v3.installers import (  # noqa: E402
    MARKER,
    install,
    uninstall,
)
from neuraltape.v3.storage import Episode, Storage  # noqa: E402


def test_claude_install_creates_config_and_is_idempotent():
    tmp = Path(tempfile.mkdtemp(prefix="nt-inst-"))
    try:
        home = tmp / "home"
        r1 = install("claude", "p1", None, home=home, apply=True)
        assert r1.action == "wrote"
        cfg = home / ".claude" / "settings.json"
        data = json.loads(cfg.read_text(encoding="utf-8"))
        entries = data["hooks"]["SessionStart"]
        assert len(entries) == 1 and MARKER in \
            entries[0]["hooks"][0]["command"]

        r2 = install("claude", "p1", None, home=home, apply=True)
        assert r2.action == "merged"
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert len(data["hooks"]["SessionStart"]) == 1  # no duplication
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_claude_install_preserves_foreign_hooks_and_baks():
    tmp = Path(tempfile.mkdtemp(prefix="nt-inst-bak-"))
    try:
        home = tmp / "home"
        cfg_dir = home / ".claude"
        cfg_dir.mkdir(parents=True)
        cfg = cfg_dir / "settings.json"
        original = {"hooks": {"SessionStart": [{"matcher": "remote",
                                                "hooks": [{"type": "command",
                                                           "command": "echo esterno"}]}]}}
        cfg.write_text(json.dumps(original), encoding="utf-8")

        r = install("claude", "p1", None, home=home, apply=True)
        assert r.action == "merged" and r.backup
        assert Path(r.backup).read_text(encoding="utf-8") == \
            json.dumps(original)

        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert len(data["hooks"]["SessionStart"]) == 2
        assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == \
            "echo esterno"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_claude_dry_run_never_writes():
    tmp = Path(tempfile.mkdtemp(prefix="nt-inst-dry-"))
    try:
        home = tmp / "home"
        r = install("claude", "p1", None, home=home, apply=False)
        assert r.action == "would-merge"
        assert not (home / ".claude" / "settings.json").exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_claude_uninstall_removes_only_our_entries():
    tmp = Path(tempfile.mkdtemp(prefix="nt-inst-un-"))
    try:
        home = tmp / "home"
        install("claude", "p1", None, home=home, apply=True)

        # Foreign hook added AFTER ours (different file content).
        cfg = home / ".claude" / "settings.json"
        data = json.loads(cfg.read_text(encoding="utf-8"))
        data["hooks"]["SessionStart"].append(
            {"matcher": "", "hooks": [{"type": "command",
                                       "command": "echo esterno"}]})
        cfg.write_text(json.dumps(data), encoding="utf-8")

        r = uninstall("claude", home=home, apply=True)
        assert r.action == "removed" and r.backup
        data = json.loads(cfg.read_text(encoding="utf-8"))
        cmds = [h["command"]
                for g in data["hooks"]["SessionStart"]
                for h in g["hooks"]]
        assert cmds == ["echo esterno"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_snippet_only_agents_never_write():
    tmp = Path(tempfile.mkdtemp(prefix="nt-inst-snip-"))
    try:
        for agent in ("kimi", "grok", "opencode", "reasonix"):
            r = install(agent, "p1", None, home=tmp / "home", apply=True)
            assert r.action == "snippet-only", agent
            assert r.snippet or r.notes
        # nothing written anywhere under home
        assert not any((tmp / "home").rglob("*")) if (tmp / "home").exists() \
            else True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_install_smoke():
    tmp = Path(tempfile.mkdtemp(prefix="nt-inst-cli-"))
    try:
        rc = cli_main(["install", "--agent", "claude", "--project", "p1",
                       "--home", str(tmp / "home"), "--apply"])
        assert rc == 0
        assert (tmp / "home" / ".claude" / "settings.json").exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- zero-LLM gate

def _seed(db_path: Path) -> None:
    s = Storage(db_path)
    ep = Episode(project_id="p1", kind="semantic", source_type="transcript",
                 title="Preflight bind reale porta 8002", body="bind reale",
                 category="decision", confidence=0.95)
    ep.created_at = ep.updated_at = time.time() - 60
    s.put_episode(ep)


def test_zero_llm_run_waters_mark_without_api_key():
    """NEURALTAPE_ZERO_LLM=1: run_once completes with NO api key and writes
    the CLASSIFIED_EVENT watermark with zero episodes."""
    tmp = Path(tempfile.mkdtemp(prefix="nt-zero-"))
    try:
        subprocess.run(["git", "init"], cwd=tmp, capture_output=True,
                       check=True)
        db = tmp / "tape" / "v3" / "neuraltape.db"
        _seed(db)
        transcript = tmp / "session-x.jsonl"
        # Small synthetic wire transcript; parser tolerates unknown lines.
        transcript.write_text(
            "\n".join(json.dumps({"type": "assistant",
                                  "message": {"content": [{"type": "text",
                                                           "text": f"riga {i} del preflight test"}]}})
                      for i in range(40)),
            encoding="utf-8",
        )

        env = dict(os.environ)
        env["NEURALTAPE_ZERO_LLM"] = "1"
        env["NEURALTAPE_V3"] = "1"
        env.pop("LLM_API_KEY", None)
        env.pop("DEEPSEEK_API_KEY", None)

        code = (
            "import sys; sys.path.insert(0, %r); "
            "from pathlib import Path; "
            "from neuraltape.v3.run import run_once; "
            "r = run_once(transcript_path=Path(%r), "
            "             project_root=Path(%r), tape_root=Path(%r)); "
            "print('EP', r.episodes_written)" % (
                str(ROOT), str(transcript), str(tmp), str(tmp))
        )
        rc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, env=env, timeout=120,
        )
        assert rc.returncode == 0, rc.stderr[-800:]
        assert "EP 0" in rc.stdout

        con_events = __import__("sqlite3").connect(db)
        rows = con_events.execute(
            "SELECT source_type, payload FROM event_log WHERE "
            "source_type LIKE '%classified%' ORDER BY id DESC LIMIT 1"
        ).fetchall()
        con_events.close()
        assert rows, "watermark must exist"
        assert rows[0][1] and "episodes_written\": 0" in rows[0][1]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
