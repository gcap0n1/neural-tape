"""C3/Fase3 tests: MCP stdio core — frozen six-verb surface over Storage."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from neuraltape.mcp_server import McpServer  # noqa: E402
from neuraltape.v3.storage import Episode, Storage  # noqa: E402


def _server(tmp: Path, with_db: bool = True) -> McpServer:
    db = tmp / "mcp.db" if with_db else tmp / "missing" / "mcp.db"
    if with_db:
        s = Storage(db)
        ep = Episode(project_id="p1", kind="semantic", source_type="transcript",
                     title="Preflight bind reale porta 8002",
                     body="Il preflight deve usare il bind reale del servizio.",
                     category="decision", confidence=0.95,
                     raw_payload={"evidence": "deve usare il bind reale"})
        ep.created_at = ep.updated_at = time.time() - 60
        s.put_episode(ep)
    return McpServer(db, project_root=tmp)


def _call(server: McpServer, name: str, args: dict) -> dict:
    resp = server.handle({"jsonrpc": "2.0", "id": 1,
                          "method": "tools/call",
                          "params": {"name": name, "arguments": args}})
    assert "error" not in resp, resp
    return json.loads(resp["result"]["content"][0]["text"])


def test_initialize_and_tools_list():
    tmp = Path(tempfile.mkdtemp(prefix="nt-mcp-"))
    try:
        srv = _server(tmp)
        init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {}})
        assert init["result"]["serverInfo"]["name"] == "neural-tape"
        tools = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in tools["result"]["tools"]}
        assert names == {"recall", "remember", "synthesize",
                         "feedback", "forget", "handoff"}
        assert srv.handle({"jsonrpc": "2.0",
                           "method": "notifications/initialized"}) is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_recall_remember_feedback_forget_flow():
    tmp = Path(tempfile.mkdtemp(prefix="nt-mcp-flow-"))
    try:
        srv = _server(tmp)
        rec = _call(srv, "recall", {"query": "preflight bind", "limit": 5})
        assert rec["count"] == 1

        new = _call(srv, "remember", {
            "project": "p1",
            "title": "Decisione architettura NexusRouter",
            "body": "Scelto il routing deterministico a due fasi.",
            "category": "decision", "kind": "semantic",
            "entities": ["NexusRouter"]})
        assert new["episode_id"] and "NexusRouter" in new["entities"]

        fb = _call(srv, "feedback", {"episode_id": new["episode_id"],
                                     "verdict": "helpful"})
        assert fb["access_count"] == 1

        _call(srv, "forget", {"episode_id": new["episode_id"]})
        assert _call(srv, "recall", {"query": "NexusRouter"})["count"] == 0
        assert _call(srv, "recall", {"query": "preflight bind"})["count"] == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_synthesize_gaps_and_db_missing_error():
    tmp = Path(tempfile.mkdtemp(prefix="nt-mcp-syn-"))
    try:
        srv = _server(tmp)
        syn = _call(srv, "synthesize",
                    {"question": "come funziona il preflight del bind?"})
        assert syn["citations"], "expected at least the seeded citation"
        assert syn["citations"][0]["evidence"]

        missing = _call(srv, "synthesize",
                        {"question": "quantizzazione quantistica lunare"})
        assert missing["citations"] == [] and missing["gaps"]

        broken = _server(tmp, with_db=False)
        resp = broken.handle({"jsonrpc": "2.0", "id": 9,
                              "method": "tools/call",
                              "params": {"name": "recall",
                                         "arguments": {"query": "x"}}})
        assert resp["error"]["code"] == -32002
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



def test_forget_refuses_pinned_and_unknown_protocol():
    tmp = Path(tempfile.mkdtemp(prefix="nt-mcp-pin-"))
    try:
        srv = _server(tmp)
        eid = _call(srv, "remember", {"project": "p1", "title": "regola vitale",
                                      "body": "non toccare"})["episode_id"]
        srv.storage().pin_episode(eid)
        resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "forget",
                                      "arguments": {"episode_id": eid}}})
        assert "error" in resp and "PINNED" in resp["error"]["message"]

        resp = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "pentagram",
                                      "arguments": {}}})
        assert "error" in resp and resp["error"]["code"] == -32602

        resp = srv.handle({"jsonrpc": "2.0", "id": 3,
                           "method": "metodo/inesistente"})
        assert resp["error"]["code"] == -32601
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_handoff_generates_artifacts():
    tmp = Path(tempfile.mkdtemp(prefix="nt-mcp-hand-"))
    try:
        subprocess.run(["git", "init"], cwd=tmp, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@nt.local"],
                       cwd=tmp, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "NT"],
                       cwd=tmp, capture_output=True, check=True)
        srv = _server(tmp)
        out = _call(srv, "handoff", {"project": "p1",
                                     "project_root": str(tmp)})
        jpath = out["artifacts"]["json"]
        mpath = out["artifacts"]["markdown"]
        assert Path(jpath).exists() and Path(mpath).exists()
        data = json.loads(Path(jpath).read_text(encoding="utf-8"))
        assert data["project_id"] == "p1"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
