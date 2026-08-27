"""Console entry point for the ``neuraltape`` command.

Two surfaces:
- ``query`` / ``think``  — C3 recall CLI over the v4 SQLite memory
- legacy pipeline flags   — --selfcheck / --status / --once, delegated to
                            ``neuraltape.v3.run`` exactly like before
"""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path

# The public package resolves its own layout; nothing to hack here.
from neuraltape.v3.storage import Storage  # noqa: E402


def _resolve_db(args) -> Path:
    if args.db:
        return Path(args.db)
    root = Path(__file__).resolve().parent.parent  # NeuralTape/ repo root
    return root / "tape" / "v3" / "neuraltape.db"


def _open_storage(args) -> Storage:
    db = _resolve_db(args)
    if not db.exists():
        raise SystemExit(
            f"[neuraltape] DB non trovata: {db}\n"
            "Avvia la pipeline v3 o passa --db PATH."
        )
    return Storage(db)


def _cmd_query(storage: Storage, ns) -> int:
    hits = storage.search(ns.query, project_id=ns.project,
                          kind=ns.kind, limit=ns.limit)
    if not hits:
        print(f"Nessun episodio per: {ns.query!r}")
        return 1
    print(f"{len(hits)} risultato(i) — {ns.query!r}\n")
    for h in hits:
        ep = h.episode
        title = ep.title or "(senza titolo)"
        preview = (ep.body or "").strip().replace("\n", " ")[:160]
        print(f"[{h.rank:.4f}] ({ep.category or 'meta'}/{ep.kind} "
              f"conf={ep.confidence:.2f} pin={'S' if ep.pinned else 'N'}) "
              f"project={ep.project_id}")
        print(f"    {title}")
        if preview:
            print(f"    » {preview}…")
        print()
    return 0


def _cmd_think(storage: Storage, ns) -> int:
    """Deterministic cited answer skeleton + honest gap analysis."""
    question = ns.question
    import re as _re
    terms = _re.findall(r"\w+", question, flags=_re.UNICODE)
    lowered = [t.casefold() for t in terms]
    hits = storage.search(question, project_id=ns.project, limit=ns.limit)
    if not hits and len(terms) > 1:
        # Broad recall sweep: a question full of tokens must not go mute just
        # because one token is uncovered. Retry any-token, ranked by fusion.
        hits = storage.search(question, project_id=ns.project,
                              limit=ns.limit, combine="OR")

    print("# Think — risposta con evidenza citabile\n")
    print(f"Domanda: {question!r}\n")
    if not hits:
        print("La memoria non contiene nulla che risponda alla domanda.\n")
    else:
        for i, h in enumerate(hits, start=1):
            ep = h.episode
            evidence = ""
            payload = ep.raw_payload if isinstance(ep.raw_payload, dict) else {}
            if payload.get("evidence"):
                evidence = f" (evidence: \"{payload['evidence']}\")"
            print(f"[{i}] {ep.title} — conf={ep.confidence:.2f} "
                  f"(score={h.rank:.4f}, pinned={'si' if ep.pinned else 'no'}, "
                  f"accessi={ep.access_count}){evidence}")
            body_line = " ".join((ep.body or "").split())[:200]
            if body_line:
                print(f"    {body_line}\n")

    # Deterministic gap analysis: which meaningful tokens did NO hit cover?
    covered: set[str] = set()
    for h in hits:
        hay = f"{h.episode.title}\n{h.episode.body or ''}".casefold()
        covered.update(t for t in lowered if t in hay)
    gaps = [t for t in terms if len(t) >= 4 and t.casefold() not in covered]
    print("\n# Gap — cosa la memoria NON sa ancora\n")
    if not hits:
        for t in dict.fromkeys(terms):
            print(f"- zero copertura per: {t}")
    elif gaps:
        for g in gaps:
            print(f"- nessun episodio copre il termine: {g}")
    else:
        print("- nessun gap terminologico evidente nella domanda")
    return 0


def _cmd_hook_inject(ns) -> int:
    """Print the pending handoff markdown; consume it unless --no-consume."""
    from .v3.handoff import build_bundle

    storage = _open_storage(ns)
    bundle = build_bundle(storage, ns.project, Path.cwd())
    if ns.regenerate or not (bundle.output_dir / "agent-handoff.md").exists():
        bundle.generate()
    md, state = bundle.pending_markdown()
    if md is None:
        print(f"[hook-inject] no pending handoff for {ns.project!r} "
              f"(consumed at {state.get('consumed_at')})", file=sys.stderr)
        return 1
    if not ns.no_consume:
        _, state = bundle.consume(consumer=ns.agent)
    sys.stdout.write(md + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="neuraltape",
        description="NeuralTape v3 entry point (Fase 0 + Fase 1 + recall CLI)",
    )
    sub = p.add_subparsers(dest="command")

    q = sub.add_parser("query", help="ricerca full-text + entita sulla memoria")
    q.add_argument("query", help="testo libero da cercare")
    q.add_argument("--project", default=None)
    q.add_argument("--kind", choices=["working", "episodic", "semantic"], default=None)
    q.add_argument("--limit", type=int, default=10)
    q.add_argument("--db", default=None, help="percorso alternativo neuraltape.db")

    t = sub.add_parser("think", help="risposta citata + gap analysis deterministica")
    t.add_argument("question", help="domanda in linguaggio naturale")
    t.add_argument("--project", default=None)
    t.add_argument("--limit", type=int, default=6)
    t.add_argument("--db", default=None)

    sv = sub.add_parser("serve", help="server MCP stdio con i sei verbi frozen")
    sv.add_argument("--db", default=None, help="percorso neuraltape.db")
    sv.add_argument("--project-root", default=None)

    h = sub.add_parser("hook-inject",
                       help="stampa l'handoff pending (single-use) per un hook")
    h.add_argument("--agent", required=True, choices=["kimi", "codex"],
                   help="consumer label (kimi/codex; grok usa MCP)")
    h.add_argument("--project", required=True)
    h.add_argument("--db", default=None)
    h.add_argument("--regenerate", action="store_true",
                   help="rigenera un bundle pending prima dell'iniezione")
    h.add_argument("--no-consume", action="store_true",
                   help="stampa senza marcare consumed")

    i = sub.add_parser("install",
                       help="installa l'hook di iniezione per un agente")
    i.add_argument("--agent", required=True,
                   choices=["claude", "kimi", "grok", "opencode", "reasonix"])
    i.add_argument("--project", required=True)
    i.add_argument("--db", default=None)
    i.add_argument("--home", default=None,
                   help="override della home utente (test)")
    i.add_argument("--apply", action="store_true",
                   help="scrive davvero (default: dry-run)")
    u = sub.add_parser("uninstall", help="rimuove le entry NeuralTape")
    u.add_argument("--agent", required=True,
                   choices=["claude", "kimi", "grok", "opencode", "reasonix"])
    u.add_argument("--home", default=None)
    u.add_argument("--apply", action="store_true")

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        build_parser().print_help()
        return 0
    if argv[0] in ("query", "think"):
        ns = build_parser().parse_args(argv)
        storage = _open_storage(ns)
        if ns.command == "query":
            return _cmd_query(storage, ns)
        return _cmd_think(storage, ns)
    if argv[0] in ("install", "uninstall"):
        ns = build_parser().parse_args(argv)
        from .v3.installers import install, uninstall
        fn = install if ns.command == "install" else uninstall
        report = fn(ns.agent, project=getattr(ns, "project", None),
                    db_path=ns.db, home=ns.home, apply=ns.apply)
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return 0
    if argv[0] == "serve":
        ns = build_parser().parse_args(argv)
        from .mcp_server import serve as _serve
        return _serve(_resolve_db(ns), ns.project_root)
    if argv[0] == "hook-inject":
        ns = build_parser().parse_args(argv)
        return _cmd_hook_inject(ns)

    # Everything else (--selfcheck / --status / --once / -h) belongs to the
    # legacy pipeline surface, preserved verbatim.
    from neuraltape.v3.run import main as run_main

    return run_main()

if __name__ == "__main__":
    sys.exit(main())
