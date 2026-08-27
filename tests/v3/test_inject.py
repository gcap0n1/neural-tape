"""Fase 3 slice 2: single-use handoff lifecycle, injection plans, transports."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import shutil
import time
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from neuraltape.cli import main as cli_main  # noqa: E402
from neuraltape.mcp_server import McpServer  # noqa: E402
from neuraltape.v3.handoff import build_bundle  # noqa: E402
from neuraltape.v3.inject import plan_for  # noqa: E402
from neuraltape.v3.storage import Episode, Storage  # noqa: E402


def _git_repo(tmp: Path) -> Path:
    subprocess.run(["git", "init"], cwd=tmp, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@nt.local"],
                   cwd=tmp, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "NT"],
                   cwd=tmp, capture_output=True, check=True)
    return tmp


def _seed(db_path: Path) -> None:
    s = Storage(db_path)
    ep = Episode(project_id="p1", kind="semantic", source_type="transcript",
                 title="Preflight bind reale porta 8002",
                 body="Il preflight deve usare il bind reale del servizio.",
                 category="decision", confidence=0.95)
    ep.created_at = ep.updated_at = time.time() - 60
    s.put_episode(ep)


# ------------------------------------------------------------------- plans

def test_injection_plans_match_roadmap_contracts():
    kimi = plan_for("kimi", "p1", "x.db")
    assert kimi.event == "UserPromptSubmit"
    assert "--agent kimi" in kimi.command and "--project p1" in kimi.command

    grok = plan_for("grok", "p1", "x.db")
    assert grok.command is None and "consume=true" in grok.notes

    codex = plan_for("codex", "p1", "x.db")
    assert codex.event == "finalize-session"
    assert "--agent codex" in codex.command

    raised = False
    try:
        plan_for("agent-inesistente", "p1", "x.db")
    except ValueError:
        raised = True
    assert raised


# ------------------------------------------------------- state machine core

def test_single_use_lifecycle_via_bundle():
    tmp = Path(tempfile.mkdtemp(prefix="nt-su-"))
    try:
        _git_repo(tmp)
        db = tmp / "su.db"
        _seed(db)
        bundle = build_bundle(Storage(db), "p1", tmp)

        bundle.generate()
        md, state = bundle.pending_markdown()
        assert md and "Preflight" in md
        assert state["consumed_at"] is None

        consumed_md, state = bundle.consume(consumer="kimi")
        assert consumed_md == md and state["consumer"] == "kimi"
        assert state["consumed_at"] is not None

        assert bundle.pending_markdown()[0] is None
        raised = False
        try:
            bundle.consume(consumer="again")
        except ValueError:
            raised = True
        assert raised

        bundle.generate()                                # regenerate resets
        assert bundle.pending_markdown()[0] is not None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- MCP surface

def test_mcp_handoff_consume_and_regenerate():
    tmp = Path(tempfile.mkdtemp(prefix="nt-mcp-su-"))
    try:
        _git_repo(tmp)
        db = tmp / "mcp.db"
        _seed(db)
        srv = McpServer(db, project_root=tmp)

        base = _call(srv, "handoff", {"project": "p1", "project_root": str(tmp)})
        assert base["consumed"] is False

        out = _call(srv, "handoff", {"project": "p1", "project_root": str(tmp),
                                     "consume": True})
        assert out["consumed"] is True and "Preflight" in out["markdown"]

        err = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "handoff",
                                     "arguments": {"project": "p1",
                                                   "project_root": str(tmp),
                                                   "consume": True}}})
        assert err["error"]["code"] == -32602

        reg = _call(srv, "handoff", {"project": "p1", "project_root": str(tmp),
                                     "regenerate": True})
        assert reg["consumed"] is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _call(server: McpServer, name: str, args: dict) -> dict:
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})
    assert "error" not in resp, resp
    return json.loads(resp["result"]["content"][0]["text"])


# ----------------------------------------------------------------- CLI flow

def test_hook_inject_prints_and_consumes():
    tmp = Path(tempfile.mkdtemp(prefix="nt-hook-"))
    try:
        _git_repo(tmp)
        db = tmp / "hook.db"
        _seed(db)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["hook-inject", "--agent", "kimi",
                           "--project", "p1", "--db", str(db)])
        assert rc == 0 and "Preflight" in buf.getvalue()

        rc2 = cli_main(["hook-inject", "--agent", "kimi",
                        "--project", "p1", "--db", str(db)])
        assert rc2 == 1

        # --no-consume after regenerate: prints without touching state.
        bundle = build_bundle(Storage(db), "p1", tmp)
        bundle.generate()
        state_before = bundle.state()["consumed_at"]
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            rc3 = cli_main(["hook-inject", "--agent", "kimi",
                            "--project", "p1", "--db", str(db),
                            "--no-consume"])
        assert rc3 == 0 and "Preflight" in buf2.getvalue()
        assert bundle.state()["consumed_at"] == state_before
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------ real transport loop

def test_serve_transport_smoke():
    tmp = Path(tempfile.mkdtemp(prefix="nt-serve-"))
    try:
        _git_repo(tmp)
        db = tmp / "serve.db"
        _seed(db)
        frames = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "handoff",
                                   "arguments": {"project": "p1",
                                                 "project_root": str(tmp),
                                                 "consume": True}}}),
        ]) + "\n"
        proc = subprocess.run(
            [sys.executable, "-m", "neuraltape.mcp_server", "--db", str(db)],
            input=frames, capture_output=True, text=True, timeout=60, cwd=tmp,
            env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, proc.stderr
        lines = [json.loads(l) for l in proc.stdout.strip().splitlines() if l]
        assert len(lines) == 2, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert lines[0]["result"]["serverInfo"]["name"] == "neural-tape"
        payload = json.loads(lines[1]["result"]["content"][0]["text"])
        assert payload["consumed"] is True and "Preflight" in payload["markdown"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
