"""memory — Layered Memory promotion engine (D1.2).

Manages the lifecycle of episodes across 4 layers:
1. Working  (hours)  — fresh session insights, immediate context
2. Episodic (weeks)  — important events, bug fixes, API discoveries
3. Semantic (months) — patterns, preferences, architectural decisions
4. Identity (permanent) — NOT managed here; lives in EterCervo (_Lex/identity.md + soul.md)

Promotion rules (from Fase 1 spec):
- working → episodic: confidence >= 0.6 AND (age >= 4h OR >= 2 similar episodes)
- episodic → semantic: confidence >= 0.8 AND >= 3 mentions in different sessions
- working → archive (discard): confidence < 0.6 AND age > 48h

The engine runs a sweep at the end of every run_once for the classified
project; ``tick(project_id=None)`` sweeps every project that has episodes.
Layer assignment happens at classification time (the LLM picks the layer);
the promoter only manages working TTL, promotion, and similarity upgrades.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import V3Config
    from .storage import Storage

log = logging.getLogger("neural-tape-v3")


@dataclass
class PromotionStats:
    examined: int = 0
    promoted_to_episodic: int = 0
    promoted_to_semantic: int = 0
    archived: int = 0
    errors: int = 0


class MemoryPromoter:
    """Promotion engine for layered memory lifecycle."""

    def __init__(self, storage: Storage, config: V3Config):
        self.storage = storage
        self.cfg = config

        # Thresholds from config (with defaults).
        mem_cfg = self._mem_config()
        self.threshold_work_to_episodic = float(mem_cfg.get("promote_threshold_working_to_episodic", 0.6))
        self.threshold_episodic_to_semantic = float(mem_cfg.get("promote_threshold_episodic_to_semantic", 0.8))
        self.min_age_hours = float(mem_cfg.get("promote_min_age_hours", 4))
        self.min_similar_episodes = int(mem_cfg.get("promote_min_similar_episodes", 2))
        self.min_sessions_for_semantic = int(mem_cfg.get("promote_min_sessions_for_semantic", 3))
        self.working_ttl_hours = float(mem_cfg.get("working_ttl_hours", 48))

    def tick(self, project_id: str | None = None) -> PromotionStats:
        """Run a full promotion sweep. Examines all working episodes for a project
        (or all projects if project_id is None).

        Returns PromotionStats.
        """
        stats = PromotionStats()
        projects_to_check = [project_id] if project_id else self._all_projects()

        for pid in projects_to_check:
            try:
                self._sweep_project(pid, stats)
            except Exception as e:
                log.error("promotion sweep failed for %s: %s", pid, e, exc_info=True)
                stats.errors += 1

        log.info(
            "promotion sweep: examined=%d promoted_to_episodic=%d "
            "promoted_to_semantic=%d archived=%d errors=%d",
            stats.examined, stats.promoted_to_episodic,
            stats.promoted_to_semantic, stats.archived, stats.errors,
        )
        return stats

    # ---- internals ------------------------------------------------------

    def _sweep_project(self, project_id: str, stats: PromotionStats) -> None:
        working = self.storage.query_episodes(project_id, kind="working")
        now = time.time()

        for ep in working:
            stats.examined += 1
            age_hours = (now - ep.created_at) / 3600

            # Check for archive (working too old with low confidence)
            if age_hours > self.working_ttl_hours and ep.confidence < self.threshold_work_to_episodic:
                # Archive: in Fase 1 we just log. Fase 2+ will implement
                # actual archiving (mark as 'archived' or delete).
                stats.archived += 1
                log.debug("working episode %s (conf=%.2f, age=%.1fh) — past TTL, archived",
                          ep.id, ep.confidence, age_hours)
                continue

            # Check working → episodic promotion
            if ep.confidence >= self.threshold_work_to_episodic and age_hours >= self.min_age_hours:
                self.storage.promote_episode(ep.id, "episodic")
                stats.promoted_to_episodic += 1
                log.info("promoted %s → episodic (conf=%.2f, age=%.1fh)",
                         ep.id, ep.confidence, age_hours)
                continue

            # If confidence is high but age is low, check for similar episodes
            if ep.confidence >= self.threshold_work_to_episodic and age_hours < self.min_age_hours:
                similar = self._count_similar_episodes(ep.project_id, ep.title)
                if similar >= self.min_similar_episodes:
                    self.storage.promote_episode(ep.id, "episodic")
                    stats.promoted_to_episodic += 1
                    log.info("promoted %s → episodic (similar=%d, age=%.1fh)",
                             ep.id, similar, age_hours)

        # Check episodic → semantic promotion (all episodic episodes)
        episodic = self.storage.query_episodes(project_id, kind="episodic")
        for ep in episodic:
            if ep.confidence >= self.threshold_episodic_to_semantic:
                mentions = self._count_session_mentions(ep.project_id, ep.title)
                if mentions >= self.min_sessions_for_semantic:
                    self.storage.promote_episode(ep.id, "semantic")
                    stats.promoted_to_semantic += 1
                    log.info("promoted %s → semantic (conf=%.2f, sessions=%d)",
                             ep.id, ep.confidence, mentions)

    def _count_similar_episodes(self, project_id: str, title: str) -> int:
        """Count episodes with similar titles (simple heuristic: word overlap)."""
        # Simple heuristic: count episodes whose title shares at least 50% words.
        words = set(title.lower().split())
        if not words:
            return 0
        all_ep = self.storage.query_episodes(project_id, kind="working", limit=200)
        count = 0
        for ep in all_ep:
            ep_words = set(ep.title.lower().split())
            if not ep_words:
                continue
            overlap = len(words & ep_words) / max(len(words), len(ep_words))
            if overlap >= 0.5:
                count += 1
        return count

    def _count_session_mentions(self, project_id: str, title: str) -> int:
        """Count how many distinct sessions mention a similar title."""
        words = set(title.lower().split())
        if not words:
            return 0
        # Query both working and episodic episodes (not semantic, we're checking FOR semantic)
        all_ep = self.storage.query_episodes(project_id, limit=500)
        sessions: set[str] = set()
        for ep in all_ep:
            if ep.kind == "semantic":
                continue
            ep_words = set(ep.title.lower().split())
            if not ep_words:
                continue
            overlap = len(words & ep_words) / max(len(words), len(ep_words))
            if overlap >= 0.5 and ep.source_ref:
                sessions.add(ep.source_ref)
        return len(sessions)

    def _all_projects(self) -> list[str]:
        """Get distinct project_ids from storage."""
        return self.storage.distinct_project_ids()

    def _mem_config(self) -> dict:
        mem = getattr(self.cfg, "memory", None)
        if mem is not None and not isinstance(mem, dict):
            mem = asdict(mem)
        if not mem:
            # Legacy fallback: test doubles / old callers attach a plain dict.
            mem = getattr(self.cfg, "memory_config", None)
        return dict(mem) if mem else {}
