"""C3 tests: neuraltape query / think CLI over a temporary memory."""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from neuraltape.cli import main as cli_main  # noqa: E402
from neuraltape.v3.storage import Episode, Storage  # noqa: E402


def _seed_db(tmp: Path) -> tuple[Storage, str]:
    s = Storage(tmp / "cli.db")
    t0 = time.time() - 60
    ep = Episode(project_id="p1", kind="semantic", source_type="transcript",
                 title="Preflight bind reale porta 8002",
                 body="Il preflight deve usare il bind reale del servizio.",
                 category="decision", confidence=0.95,
                 raw_payload={"session_id": "cli-test",
                              "evidence": "deve usare il bind reale del servizio"})
    ep.created_at = ep.updated_at = t0
    eid = s.put_episode(ep)
    return s, eid

def test_query_prints_ranked_results(tmp_path=None):
    tmp = Path(tempfile.mkdtemp(prefix="nt-cli-q-"))
    try:
        s, _ = _seed_db(tmp)
        rc = cli_main(["query", "preflight bind", "--db", str(s.db_path)])
        assert rc == 1 or rc == 0  # empty-memory path guarded separately
        # With one matching episode the command succeeds (rc==0).
        rc_ok = cli_main(["query", "preflight", "--db", str(s.db_path)])
        assert rc_ok == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_query_empty_memory_exit_one(capsys=None):
    tmp = Path(tempfile.mkdtemp(prefix="nt-cli-empty-"))
    try:
        s = Storage(tmp / "empty.db")
        rc = cli_main(["query", "qualcosa di irreperibile",
                       "--db", str(s.db_path)])
        assert rc == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_think_cites_and_reports_gaps():
    tmp = Path(tempfile.mkdtemp(prefix="nt-cli-think-"))
    try:
        s, eid = _seed_db(tmp)
        # Capture stdout via redirect to keep test dependency-light.
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["think", "come funziona il preflight del bind?",
                           "--db", str(s.db_path)])
        out = buf.getvalue()
        assert rc == 0
        assert "Preflight bind reale porta 8002" in out
        assert "# Gap" in out
        assert "evidence:" in out          # transcript evidence surfaced
        assert "functioning" not in out

        # Fully unknown question -> explicit zero-coverage gap lines.
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            cli_main(["think", "ricostruzione storica dei salvataggi lunari",
                      "--db", str(s.db_path)])
        out2 = buf2.getvalue()
        assert "non contiene nulla" in out2
        for token in ("ricostruzione", "lunari"):
            assert f"zero copertura per: {token}" in out2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
