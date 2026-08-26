"""Test per lex/v3/memory.py (D1.2)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lex" / "v3"))

from storage import Episode, Storage  # type: ignore[import-not-found]
from memory import MemoryPromoter  # type: ignore[import-not-found]


def _storage() -> Storage:
    d = Path(tempfile.mkdtemp(prefix="nt-v3-mem-"))
    return Storage(d / "test.db")


def _promoter_with_thresholds(storage: Storage, min_age_hours: float = 0,
                               min_similar: int = 2) -> MemoryPromoter:
    """Create a MemoryPromoter with explicit config."""
    from dataclasses import dataclass, field
    @dataclass
    class V3Config:
        memory_config: dict = field(default_factory=lambda: {
            "promote_threshold_working_to_episodic": 0.6,
            "promote_threshold_episodic_to_semantic": 0.8,
            "promote_min_age_hours": min_age_hours,
            "promote_min_similar_episodes": min_similar,
            "promote_min_sessions_for_semantic": 2,
            "working_ttl_hours": 48,
        })
        tape_root: Path = Path("/tmp")
        enabled: bool = True
    return MemoryPromoter(storage, V3Config())


def test_sweep_promotes_working_to_episodic():
    """Working episode with high confidence and age > min gets promoted."""
    import time
    s = _storage()
    old = time.time() - 3600 * 6  # 6 hours ago (past min_age_hours=0)
    ep = Episode(project_id="zeus", kind="working", source_type="transcript",
                 title="important fix", confidence=0.85, created_at=old)
    eid = s.put_episode(ep)

    p = _promoter_with_thresholds(s, min_age_hours=0)
    stats = p.tick(project_id="zeus")

    got = s.get_episode(eid)
    assert got is not None
    assert stats.promoted_to_episodic >= 1, f"expected promotion, stats={stats}"
    assert got.kind == "episodic"


def test_sweep_archives_low_confidence_old_episodes():
    """Working episode with low confidence and old age gets archived."""
    import time
    s = _storage()
    very_old = time.time() - 3600 * 72  # 72h > TTL
    ep = Episode(project_id="zeus", kind="working", source_type="transcript",
                 title="noisy signal", confidence=0.2, created_at=very_old)
    s.put_episode(ep)

    p = _promoter_with_thresholds(s, min_age_hours=0)
    stats = p.tick(project_id="zeus")

    assert stats.archived >= 1, f"expected archival, stats={stats}"


def test_sweep_skips_working_young_high_confidence():
    """Working episode with high confidence but very recent: needs min_age to pass."""
    import time
    s = _storage()
    now = time.time()
    ep = Episode(project_id="zeus", kind="working", source_type="transcript",
                 title="brand new insight", confidence=0.9, created_at=now)
    s.put_episode(ep)

    # Set min_age_hours = 1, so the 0-hour-old episode should NOT be promoted
    p = _promoter_with_thresholds(s, min_age_hours=1, min_similar=10)
    stats = p.tick(project_id="zeus")

    got = s.get_episode(ep.id)
    assert got is not None
    # Should remain working because age < min_age_hours and not enough similar
    assert got.kind == "working", f"expected working, got {got.kind}"
    assert stats.promoted_to_episodic == 0


def test_sweep_multiple_projects():
    """Episodes from different projects must not cross-contaminate."""
    import time
    s = _storage()
    old = time.time() - 3600 * 6
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="t",
                          title="zeus fix", confidence=0.9, created_at=old))
    s.put_episode(Episode(project_id="cais-lp", kind="working", source_type="t",
                          title="cais fix", confidence=0.9, created_at=old))

    p = _promoter_with_thresholds(s, min_age_hours=0)
    stats_z = p.tick(project_id="zeus")
    stats_c = p.tick(project_id="cais-lp")

    assert stats_z.promoted_to_episodic >= 1
    assert stats_c.promoted_to_episodic >= 1


def test_tick_none_sweeps_all_projects():
    """tick(project_id=None) must discover projects from storage, not return []."""
    import time
    s = _storage()
    old = time.time() - 3600 * 6
    s.put_episode(Episode(project_id="zeus", kind="working", source_type="t",
                          title="zeus fix", confidence=0.9, created_at=old))
    s.put_episode(Episode(project_id="cais-lp", kind="working", source_type="t",
                          title="cais fix", confidence=0.9, created_at=old))

    p = _promoter_with_thresholds(s, min_age_hours=0)
    stats = p.tick()
    assert stats.promoted_to_episodic == 2
    assert s.get_episode(
        s.query_episodes("zeus")[0].id
    ).kind == "episodic"
    assert s.get_episode(
        s.query_episodes("cais-lp")[0].id
    ).kind == "episodic"


def test_mem_config_reads_v3config_memory():
    """MemoryPromoter must read thresholds from V3Config.memory (regression:
    _mem_config used to look for a non-existent 'memory_config' attribute)."""
    import config as v3config

    d = Path(tempfile.mkdtemp(prefix="nt-v3-cfg-"))
    cfg = v3config.load(tape_root=d, config_path=d / "missing.yaml")
    p = MemoryPromoter(_storage(), cfg)

    # Defaults must match the values previously hardcoded in memory.py.
    assert p.threshold_work_to_episodic == 0.6
    assert p.threshold_episodic_to_semantic == 0.8
    assert p.min_age_hours == 4
    assert p.min_similar_episodes == 2
    assert p.min_sessions_for_semantic == 3
    assert p.working_ttl_hours == 48


def test_mem_config_yaml_override():
    """A v3.memory section in config.yaml overrides the defaults."""
    import config as v3config

    d = Path(tempfile.mkdtemp(prefix="nt-v3-cfg-"))
    (d / "config.yaml").write_text(
        "v3:\n  memory:\n    working_ttl_hours: 12\n    promote_min_age_hours: 1\n",
        encoding="utf-8",
    )
    cfg = v3config.load(tape_root=d, config_path=d / "config.yaml")
    p = MemoryPromoter(_storage(), cfg)

    assert p.working_ttl_hours == 12
    assert p.min_age_hours == 1
    # Untouched keys keep their defaults.
    assert p.threshold_work_to_episodic == 0.6
