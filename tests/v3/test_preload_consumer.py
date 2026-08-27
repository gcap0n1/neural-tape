"""Consumer contract: lex/pre_load.py reads Storage (SQLite) instead of the mirror."""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("nt_preload", ROOT / "lex" / "pre_load.py")
_pl = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("nt_preload", _pl)
_spec.loader.exec_module(_pl)

from neuraltape.v3.markdown_export import export_episode_to_markdown  # noqa: E402
from neuraltape.v3.storage import Episode, Storage  # noqa: E402


def _workspace(tmp: Path):
    return tmp, tmp / "config.yaml", tmp / "session-context.md"


def _write_config(tmp: Path):
    (tmp / "config.yaml").write_text(
        "paths:\n"
        f"  neural_tape_root: {tmp}\n"
        "pre_load:\n"
        "  max_insights: 5\n"
        "  max_patterns: 3\n"
        "  lookback_days: 7\n",
        encoding="utf-8",
    )


def _ep(project="p1", kind="semantic", category="decision", confidence=0.95,
        title="", body=None, created_at=None):
    ep = Episode(project_id=project, kind=kind, source_type="transcript",
                 title=title, body=body, category=category, confidence=confidence)
    if created_at is not None:
        ep.created_at = ep.updated_at = created_at
    return ep


def test_preload_reads_sqlite_and_links_mirror_files():
    tmp, cfg_p, out_p = _workspace(Path(tempfile.mkdtemp(prefix="nt-preload-")))
    try:
        _write_config(tmp)
        s = Storage(tmp / "tape" / "v3" / "neuraltape.db")
        t0 = time.time() - 60
        a = _ep(title="Preflight bind reale porta 8002", body="bind zucchero", created_at=t0)
        b_low = _ep(title="Bozza non confermata", body="zzz", confidence=0.35, created_at=t0 + 1)
        c_other = _ep(project="pB", title="solo pB deve sparire", body="xxx", created_at=t0 + 2)
        d_work = _ep(kind="working", category="tool", confidence=0.5,
                     title="Aggiornato indice episodi", body="yyy", created_at=t0 + 3)
        for ep in (a, b_low, c_other, d_work):
            s.put_episode(ep)

        # Mirror page for ONE episode: reconstructed file column must match it.
        real_page = export_episode_to_markdown(a, tmp / "tape" / "archive")

        config = _pl.Config(cfg_p)
        out = _pl.PreLoad(config).generate(project="p1")

        assert out == out_p and out.exists()
        text = out.read_text(encoding="utf-8")
        assert "ranking: decay-based" in text
        assert "Active Insights (2)" in text
        assert "Preflight bind reale" in text
        assert "Aggiornato indice episodi" in text
        assert "solo pB deve sparire" not in text      # other project filtered
        assert "Bozza non confermata" not in text      # low-confidence dropped

        file_cells = re.findall(r"\| tape/archive/[^|]+\|", text)
        assert file_cells, "expected mirror-relative file links in table"
        pattern = re.compile(r"tape/archive/\w+/\d{4}-\d{2}-\d{2}-[0-9a-f]{8}-[a-z0-9\-]+\.md")
        assert all(pattern.search(c) for c in file_cells)

        row_line = next(l for l in text.splitlines() if "Preflight bind reale" in l)
        reconstructed = re.findall(r"(tape/archive/[^|]+)", row_line)[0].strip()
        assert (tmp / reconstructed).exists(), "context path must point at the real mirror"
        assert Path(real_page).resolve() == (tmp / reconstructed).resolve()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_rank_blend_falls_back_when_fts_hits_nothing():
    tmp, cfg_p, _ = _workspace(Path(tempfile.mkdtemp(prefix="nt-preload-blend-")))
    try:
        _write_config(tmp)
        s = Storage(tmp / "tape" / "v3" / "neuraltape.db")
        t0 = time.time() - 60
        s.put_episode(_ep(title="cercami gap engine uno", body="dentro", created_at=t0))

        config = _pl.Config(cfg_p)
        pl = _pl.PreLoad(config)
        insights = pl._read_insights("p1", 7)

        # No-term query -> ValueError swallowed -> pure decay order preserved.
        ranked = pl._rank_insights(insights, query="termine assente del tutto", top_k=5)
        assert len(ranked) == 1 and ranked[0]["content"].startswith("cercami")

        # Hit path blends BM25 with strength and caps at top_k.
        extra = _ep(kind="episodic", category="warning", confidence=0.9,
                    title="secondo elemento senza termine", body="fuori tema",
                    created_at=t0 + 5)
        s.put_episode(extra)
        insights = pl._read_insights("p1", 7)
        ranked = pl._rank_insights(insights, query="cercami engine", top_k=5)
        assert ranked[0]["content"].startswith("cercami")
        assert {"score"} <= set(ranked[0].keys())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
