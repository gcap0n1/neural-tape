"""Golden recall eval (Fase 4) — deterministic, hermetic, zero-LLM.

Reads ``golden_set.json``, builds a fresh in-memory-per-case SQLite DB from
the case docs, and asserts the recall contract for every case:

- contains_topk : needle must appear within the top-k of search()
- first         : needle must be the #1 hit
- absent        : needle must NOT surface (forgotten / soft-deleted)
- no_crash      : hostile query must not raise; returns a list

Exit code 0 = all green. Any failure prints the case name and exits 1.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from neuraltape.v3.storage import Episode, Storage  # noqa: E402

SET_PATH = Path(__file__).resolve().parent / "golden_set.json"


def _seed(case: dict, tmp: Path) -> Storage:
    s = Storage(tmp / "eval.db")
    t0 = time.time() - 3600  # everything comfortably inside lookback windows
    for doc in case["docs"]:
        ep = Episode(
            project_id="p1",
            kind=doc.get("kind", "episodic"),
            source_type="manual",
            title=doc["title"],
            body=doc.get("body", ""),
            category=doc.get("category", "tool"),
            confidence=doc.get("confidence", 0.8),
            entities=doc.get("entities", []),
            pinned=bool(doc.get("pinned", False)),
        )
        ep.created_at = ep.updated_at = t0 - float(doc.get("age_offset", 0))
        s.put_episode(ep)
        if doc.get("forget"):
            s.feedback(ep.id, "wrong")
    return s


def run_case(case: dict, top_k_default: int) -> tuple[bool, str]:
    tmp = Path(tempfile.mkdtemp(prefix="nt-eval-"))
    try:
        s = _seed(case, tmp)
        top_k = int(case.get("top_k", top_k_default))
        hits = s.search(case["query"], limit=top_k)
        ids_titles = [h.episode.title for h in hits]
        needle = case.get("needle_title")
        kind = case["assert"]

        if kind == "no_crash":
            return True, "query ostile gestita senza eccezioni"
        if needle is None:
            return False, "caso senza needle_title"

        if kind == "contains_topk":
            ok = needle in ids_titles
            return ok, (f"needle in top-{len(ids_titles)}" if ok
                        else f"needle assente; ottenuti: {ids_titles}")
        if kind == "first":
            ok = bool(ids_titles) and ids_titles[0] == needle
            return ok, (f"primo hit = needle" if ok
                        else f"primo hit = {ids_titles[:1]}")
        if kind == "absent":
            ok = needle not in ids_titles
            return ok, ("needle correttamente tacito" if ok
                        else f"needle emerso indebitamente: {ids_titles}")
        return False, f"tipo assert sconosciuto: {kind}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    fixture = json.loads(SET_PATH.read_text(encoding="utf-8"))
    top_k_default = int(fixture.get("top_k_default", 5))
    failures: list[str] = []
    print(f"Golden recall eval — {len(fixture['cases'])} casi\n")
    for case in fixture["cases"]:
        try:
            ok, detail = run_case(case, top_k_default)
        except Exception as exc:  # a crash IS a failure of the contract
            ok, detail = False, f"eccezione: {exc!r}"
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {case['name']} — {detail}")
        if not ok:
            failures.append(case["name"])

    print(f"\nTotale: {len(fixture['cases'])} — "
          f"falliti: {len(failures)} {failures if failures else ''}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
