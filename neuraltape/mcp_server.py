"""MCP stdio server — frozen six-verb surface over the v4 SQLite memory.

Implements the Model Context Protocol (stdio transport, newline-delimited
JSON-RPC 2.0) with the frozen verb set from the v4 roadmap:

    recall      — RRF-fused full-text + entity search
    remember    — write a manual episode (source_type='manual')
    synthesize  — cited answer skeleton + deterministic gap analysis
    feedback    — helpful | not_helpful | stale | wrong
    forget      — soft kill-switch (sets expires_at; never deletes)
    handoff     — Agent Handoff bundle generation for a project

Zero new dependencies: everything rides on Storage/AgentHandoffBundle.
Run: ``neuraltape serve`` (or ``python -m neuraltape.mcp_server``).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from .v3.storage import Storage, heuristic_entities

log = logging.getLogger("neural-tape-v3")

SERVER_INFO = {"name": "neural-tape", "version": "3.3.0"}

TOOLS: list[dict] = [
    {
        "name": "recall",
        "description": ("Full-text recall fused with entity matching (RRF). "
                        "Query tokens join with AND for precision; use "
                        "combine='OR' for broad sweeps."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "project": {"type": ["string", "null"]},
                "kind": {"enum": ["working", "episodic", "semantic", None]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "combine": {"enum": ["AND", "OR"]},
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": "Persist a manual insight into layered memory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "category": {"enum": ["pattern", "decision", "anti-pattern",
                                       "preference", "tool", "warning",
                                       "neutral"]},
                "kind": {"enum": ["working", "episodic", "semantic"]},
                "entities": {"type": "array", "items": {"type": "string"},
                              "maxItems": 5},
            },
            "required": ["project", "title", "body"],
        },
    },
    {
        "name": "synthesize",
        "description": ("Cited answer skeleton for a question plus the "
                        "deterministic list of uncovered terms (gaps)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "project": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["question"],
        },
    },
    {
        "name": "feedback",
        "description": ("Reinforce or retire an episode: helpful | "
                        "not_helpful | stale | wrong (wrong/stale act as "
                        "TTL kill switch and unpin)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "episode_id": {"type": "string"},
                "verdict": {"enum": ["helpful", "not_helpful",
                                      "stale", "wrong"]},
            },
            "required": ["episode_id", "verdict"],
        },
    },
    {
        "name": "forget",
        "description": ("Soft-forget an episode: sets expires_at=now so it "
                        "stops surfacing. The row is never deleted — audit "
                        "trail preserved. Pinned rows are refused: unpin "
                        "them explicitly first (authority guard)."),
        "inputSchema": {
            "type": "object",
            "properties": {"episode_id": {"type": "string"}},
            "required": ["episode_id"],
        },
    },
    {
        "name": "handoff",
        "description": ("Agent handoff bundle lifecycle. Default: generate a "
                        "fresh PENDING bundle (non-consuming read of "
                        "artifacts). consume=true: return the pending "
                        "markdown and mark it consumed (single-use; error "
                        "if already consumed). regenerate=true: force a "
                        "fresh pending bundle first."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "project_root": {"type": ["string", "null"]},
                "consume": {"type": "boolean"},
                "regenerate": {"type": "boolean"},
            },
            "required": ["project"],
        },
    },
]

TOOL_NAMES = {t["name"] for t in TOOLS}


class _MethodNotFound(Exception):
    """Internal sentinel: JSON-RPC -32601 (method not found)."""


class McpServer:
    """Protocol-agnostic MCP core: dict request in, dict/None out."""

    def __init__(self, db_path: Path, project_root: Path | None = None):
        self.db_path = Path(db_path)
        self.default_project_root = (project_root or Path.cwd()).resolve()
        self._storage: Storage | None = None

    # ---- storage lifecycle ---------------------------------------------

    def storage(self) -> Storage:
        if self._storage is None:
            if not self.db_path.exists():
                raise FileNotFoundError(
                    f"NeuralTape DB not found: {self.db_path} (use serve --db)")
            self._storage = Storage(self.db_path)
        return self._storage

    # ---- protocol -------------------------------------------------------

    def handle(self, request: dict) -> dict | None:
        if request.get("method", "").startswith("notifications/"):
            return None
        rid = request.get("id")
        try:
            result = self._dispatch(request.get("method", ""),
                                    request.get("params") or {})
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except FileNotFoundError as exc:
            return self._error(rid, -32002, str(exc))
        except _MethodNotFound as exc:
            return self._error(rid, -32601, str(exc))
        except (ValueError, KeyError) as exc:
            return self._error(rid, -32602, str(exc))
        except Exception as exc:  # pragma: no cover — defensive boundary
            log.exception("mcp tool failure")
            return self._error(rid, -32603, f"internal error: {exc}")

    @staticmethod
    def _error(rid, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": code, "message": message}}

    def _dispatch(self, method: str, params: dict):
        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            }
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            name = params.get("name")
            if name not in TOOL_NAMES:
                raise ValueError(f"unknown tool: {name!r}")
            payload = self._call_tool(name, params.get("arguments") or {})
            return {"content": [{"type": "text",
                                  "text": json.dumps(payload,
                                                     ensure_ascii=False,
                                                     indent=2)}],
                    "isError": False}
        raise _MethodNotFound(f"unknown method: {method!r}")

    # ---- verbs ----------------------------------------------------------

    def _call_tool(self, name: str, a: dict):
        if name == "recall":
            return self._verb_recall(a)
        if name == "remember":
            return self._verb_remember(a)
        if name == "synthesize":
            return self._verb_synthesize(a)
        if name == "feedback":
            return self.storage().feedback(a["episode_id"], a["verdict"])
        if name == "forget":
            return self._verb_forget(a)
        if name == "handoff":
            return self._verb_handoff(a)
        raise ValueError(f"unhandled tool {name!r}")  # pragma: no cover

    def _verb_recall(self, a: dict) -> dict:
        hits = self.storage().search(
            a["query"], project_id=a.get("project"),
            kind=a.get("kind"), limit=int(a.get("limit", 10)),
            combine=a.get("combine", "AND"),
        )
        return {
            "count": len(hits),
            "results": [
                {
                    "episode_id": h.episode.id,
                    "score": h.rank,
                    "title": h.episode.title,
                    "category": h.episode.category,
                    "kind": h.episode.kind,
                    "confidence": h.episode.confidence,
                    "pinned": h.episode.pinned,
                    "project": h.episode.project_id,
                    "body": (h.episode.body or "")[:400],
                    "entities": h.episode.entities,
                }
                for h in hits
            ],
        }

    def _verb_remember(self, a: dict) -> dict:
        from .v3.storage import Episode
        entities = list(a.get("entities") or [])
        if not entities:
            entities = heuristic_entities(a["title"], a.get("body", ""))
        ep = Episode(
            project_id=a["project"], kind=a.get("kind", "episodic"),
            source_type="manual", title=a["title"],
            body=(a.get("body") or "").strip(),
            category=a.get("category", "decision"),
            confidence=1.0, entities=entities,
        )
        eid = self.storage().put_episode(ep)
        return {"episode_id": eid, "entities": ep.entities,
                "pinned": False, "expires_at": None}

    def _verb_synthesize(self, a: dict) -> dict:
        import re as _re
        question = a["question"]
        terms = _re.findall(r"\w+", question, flags=_re.UNICODE)
        lowered = [t.casefold() for t in terms]
        storage = self.storage()
        hits = storage.search(question, project_id=a.get("project"),
                              limit=int(a.get("limit", 6)))
        if not hits and len(lowered) > 1:
            hits = storage.search(question, project_id=a.get("project"),
                                  limit=int(a.get("limit", 6)), combine="OR")

        covered: set[str] = set()
        for h in hits:
            hay = f"{h.episode.title}\n{h.episode.body or ''}".casefold()
            covered.update(t for t in lowered if t in hay)
        gaps = [t for t in dict.fromkeys(terms)
                if len(t) >= 4 and t.casefold() not in covered]

        return {
            "question": question,
            "citations": [
                {
                    "episode_id": h.episode.id,
                    "title": h.episode.title,
                    "confidence": h.episode.confidence,
                    "score": h.rank,
                    "pinned": h.episode.pinned,
                    "evidence": (h.episode.raw_payload or {}).get("evidence", "")
                    if isinstance(h.episode.raw_payload, dict) else "",
                    "excerpt": " ".join((h.episode.body or "").split())[:200],
                }
                for h in hits
            ],
            "gaps": gaps or [],
            "note": ("memoria muta sulla domanda" if not hits
                     else "nessun gap terminologico evidente" if not gaps
                     else "termini senza copertura: vedi gaps"),
        }

    def _verb_forget(self, a: dict) -> dict:
        import time as _time
        eid = a["episode_id"]
        row = self.storage().get_episode(eid)
        if row is None:
            raise KeyError(f"unknown episode id {eid!r}")
        if row.pinned:
            raise ValueError("refusing to forget a PINNED episode — "
                             "unpin it explicitly first (authority guard)")
        state = self.storage().feedback(eid, "stale")
        return {"episode_id": eid, "forgotten": True, **state}

    def _verb_handoff(self, a: dict) -> dict:
        from .v3.handoff import build_bundle

        project = a["project"]
        project_root = Path(a.get("project_root") or self.default_project_root)
        storage = self.storage()
        bundle = build_bundle(storage, project, project_root,
                              self.db_path.parent / "projects" / project)

        if a.get("regenerate") or not (bundle.output_dir / "agent-handoff.md").exists():
            bundle.generate()

        artifacts = {
            "json": str(bundle.output_dir / "agent-handoff.json"),
            "markdown": str(bundle.output_dir / "agent-handoff.md"),
        }
        if a.get("consume"):
            md, state = bundle.consume(consumer="mcp")
            return {"project": project, "consumed": True,
                    "state": state, "markdown": md, "artifacts": artifacts}
        state = bundle.state()
        return {"project": project,
                "consumed": bool(state.get("consumed_at")),
                "state": state, "artifacts": artifacts}


def serve(db_path: Path, project_root: Path | None = None) -> int:
    """Newline-delimited JSON-RPC loop over stdin/stdout. Blocks forever."""
    server = McpServer(db_path, project_root)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = McpServer._error(None, -32700, f"parse error: {exc}")
        else:
            response = server.handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    db = sys.argv[sys.argv.index("--db") + 1] if "--db" in sys.argv \
        else Path.cwd() / "tape" / "v3" / "neuraltape.db"
    sys.exit(serve(Path(db)))
