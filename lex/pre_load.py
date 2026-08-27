#!/usr/bin/env python3
"""
Neural Tape — Pre-Load
Generates session-context.md before AI assistant session starts.
"""

import re
import argparse
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

# Import the shipped pipeline straight from this checkout.
_ROOT = Path(__file__).resolve().parent.parent  # NeuralTape repo root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from neuraltape.v3.markdown_export import (  # noqa: E402
    _archive_subdir,
    _confidence_label,
    _slugify,
)
from neuraltape.v3.storage import Storage  # noqa: E402

try:
    import yaml
except ImportError:
    raise SystemExit("PyYAML required: pip install pyyaml")


# ── Decay + Auto-Forget (ported from agentmemory) ──────────────────────

def decay_strength(created_ts: str, strength: float = 1.0, decay_days: int = 30) -> float:
    """Apply Ebbinghaus-style exponential decay.
    
    Returns strength in [0.1, strength]. Older insights decay faster.
    Insights younger than decay_days retain near-full strength.
    """
    try:
        ts = datetime.fromisoformat(str(created_ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return strength  # If timestamp is invalid, keep full strength
    days = max(0, (datetime.now(ts.tzinfo) - ts).days) if ts.tzinfo else max(0, (datetime.now() - ts).days)
    periods = days / decay_days
    return max(0.1, strength * pow(0.9, periods))


def is_below_threshold(strength: float, threshold: float = 0.1) -> bool:
    """Check if an insight is below forget threshold."""
    return strength <= threshold




class Config:
    def __init__(self, path: Path):
        with open(path, "r", encoding="utf-8") as f:
            self.raw = yaml.safe_load(f)
        self.paths = self.raw.get("paths", {})
        self.pre_load = self.raw.get("pre_load", {})
        self.deja_vu = self.raw.get("deja_vu", {})


    def get_wiki_dir(self) -> Optional[Path]:
        p = self.paths.get("etercervo_wiki", "")
        return Path(p) if p else None

    def get_lex_memory(self) -> Optional[Path]:
        p = self.paths.get("lex_memory", "")
        return Path(p) if p else None

    def get_output_path(self) -> Path:
        root = self.paths.get("neural_tape_root", ".")
        return Path(root) / "session-context.md"

    def get_db_path(self) -> Path:
        root = self.paths.get("neural_tape_root", ".")
        return Path(root) / "tape" / "v3" / "neuraltape.db"


class PreLoad:
    """Generate session context for AI assistant startup."""

    def __init__(self, config: Config):
        self.config = config
        self.db_path = config.get_db_path()
        self.wiki_dir = config.get_wiki_dir()
        self.lex_memory = config.get_lex_memory()
        self.output_path = config.get_output_path()

    def _detect_project(self) -> str:
        """Auto-detect project from cwd or git."""
        cwd = Path.cwd()
        if (cwd / ".git").exists():
            return cwd.name
        return "default"

    def _detect_branch(self) -> str:
        """Auto-detect git branch."""
        git_head = Path.cwd() / ".git" / "HEAD"
        if git_head.exists():
            try:
                content = git_head.read_text(encoding="utf-8").strip()
                if content.startswith("ref: refs/heads/"):
                    return content.split("/")[-1]
            except Exception:
                pass
        return "unknown"



    def _read_insights(self, project: str, lookback_days: int, assistant: str = None) -> List[Dict]:
        """Read insights straight from the v3 SQLite DB (recall era).

        Replaces the markdown mirror scan: one indexed source of truth,
        ``kind``/confidence floats preserved end-to-end. Confidence labels are
        derived exactly like markdown_export so mirror pages and context agree;
        ``low`` insights are dropped, decay auto-forgets below threshold, and
        project matching stays case-insensitive ('default' sees everything).
        """
        if not self.db_path.exists():
            raise SystemExit(
                f"[pre_load] NeuralTape v3 DB non trovata: {self.db_path}\n"
                "Attiva la pipeline v3 (neural-tape-v3.timer) o verifica paths.neural_tape_root."
            )

        storage = Storage(self.db_path)
        # Fetch across ALL projects inside the window and match case-insensitively
        # below: episode ids are lowercase-ish ('neuraltape') while the caller may
        # pass a cwd-derived label ('NeuralTape'). SQL-level equality would break
        # that historical contract.
        cutoff_epoch = (datetime.now().astimezone() - timedelta(days=lookback_days)).timestamp()
        episodes = storage.query_episodes(None, since=cutoff_epoch, limit=5000)

        insights: List[Dict] = []
        for ep in episodes:
            if project != "default" and str(ep.project_id).lower() != project.lower():
                continue  # defensive case-insensitive match on top of SQL equality

            label = _confidence_label(ep.confidence or 0.0)
            if label == "low":
                continue

            ts_dt = datetime.fromtimestamp(ep.created_at).astimezone()
            ts_str = ts_dt.isoformat()
            strength = decay_strength(ts_str)
            if is_below_threshold(strength):
                continue  # Auto-forget below threshold

            payload = ep.raw_payload if isinstance(ep.raw_payload, dict) else {}
            persona = str(payload.get("assistant") or "lex")

            if assistant and persona.lower() != assistant.lower():
                continue

            date_str = ts_dt.strftime("%Y-%m-%d")
            fname = f"{date_str}-{ep.id[:8]}-{_slugify(ep.title or '')}.md"
            rel_dir = _archive_subdir(ep.category or "neutral")

            insights.append({
                "id": ep.id,
                "file": f"tape/archive/{rel_dir}/{fname}",
                "type": ep.category or "meta",
                "timestamp": ts_str,
                "confidence": label,
                "content": ep.title or (ep.body or "")[:60] or ep.id,
                "project": ep.project_id,
                "assistant": persona,
                "strength": round(strength, 3),  # Decay-aware ranking key
                "pinned": bool(ep.pinned),
            })

        # Sort: pinned authority first, then decay-aware strength, recency,
        # confidence.
        insights.sort(key=lambda x: (x["pinned"], x["strength"], x["timestamp"],
                                     x["confidence"] == "high"), reverse=True)
        return insights

    def _rank_insights(self, insights: List[Dict], query: str = None, top_k: int = 10) -> List[Dict]:
        """Rank insights via FTS5 BM blended with decay strength (70% / 30%).

        Candidates come from ``Storage.search`` (syntax-safe match expression).
        Episodes that hit no term are excluded from the ranked set; if nothing
        matches, fall back to the decay-based order so the morning context
        never comes back empty.
        """
        if not query or not insights:
            return insights[:top_k]
        try:
            hits = Storage(self.config.get_db_path()).search(query, limit=top_k * 6)
        except ValueError:
            return insights[:top_k]
        rank_by_id = {h.episode.id: h.rank for h in hits}
        matched = [i for i in insights if i["id"] in rank_by_id]
        if not matched:
            return insights[:top_k]
        ranks = [rank_by_id[i["id"]] for i in matched]
        rmin, rmax = min(ranks), max(ranks)
        span = (rmax - rmin) or 1.0
        for i in matched:
            norm = (rank_by_id[i["id"]] - rmin) / span
            i["score"] = round(0.7 * (1.0 - norm) + 0.3 * i["strength"], 4)
        matched.sort(key=lambda x: x["score"], reverse=True)
        return matched[:top_k]


    def _detect_patterns(self, insights: List[Dict], min_occurrences: int = 2) -> List[Dict]:
        """Detect recurring patterns by type + content similarity."""
        from collections import Counter
        # Exclude code_change from pattern detection (too noisy)
        filtered = [i for i in insights if i["type"] != "code_change"]
        type_counts = Counter(i["type"] for i in filtered)
        patterns = []
        for t, count in type_counts.items():
            if count >= min_occurrences:
                related = [i for i in filtered if i["type"] == t][:5]
                patterns.append({
                    "name": f"{t}-pattern",
                    "type": t,
                    "count": count,
                    "first_seen": related[-1]["timestamp"] if related else "",
                    "last_seen": related[0]["timestamp"] if related else "",
                    "examples": [i["content"] for i in related[:3]],
                })
        return patterns

    def _read_lex_memory(self, lines: int = 30) -> List[str]:
        """Read last N lines from Lex memory."""
        if not self.lex_memory or not self.lex_memory.exists():
            return []
        try:
            text = self.lex_memory.read_text(encoding="utf-8")
            all_lines = text.strip().split("\n")
            return all_lines[-lines:]
        except Exception:
            return []

    def generate(self, project: str = None, branch: str = None, query: str = None) -> Path:
        """Generate session-context.md.
        
        If query is provided, FTS5 BM25 ranking is used (70% relevance + 30% decay).
        Otherwise, pure decay-based ranking is used.
        """
        project = project or self._detect_project()
        branch = branch or self._detect_branch()

        max_insights = self.config.pre_load.get("max_insights", 10)
        max_patterns = self.config.pre_load.get("max_patterns", 5)
        lookback_days = self.config.pre_load.get("lookback_days", 7)
        include_lex = self.config.pre_load.get("include_lex_memory", True)

        all_insights = self._read_insights(project, lookback_days, assistant=None)
        
        # Use BM25 ranking if query provided, otherwise decay-based
        if query:
            insights = self._rank_insights(all_insights, query=query, top_k=max_insights)
            ranking_method = "FTS5 BM25 + decay"
        else:
            insights = all_insights[:max_insights]
            ranking_method = "decay-based"
        patterns = self._detect_patterns(insights)[:max_patterns]

        # Reinforce what the context actually surfaces (usage counters).
        try:
            Storage(self.config.get_db_path()).touch_access(
                [i["id"] for i in insights])
        except Exception as exc:  # non-fatal: context must never hard-fail
            print(f"[NeuralTape] touch_access skipped: {exc}")

        # Build context file
        query_info = f" (query: {query})" if query else ""
        lines = [
            "---",
            f"generated: {datetime.now().isoformat()}",
            f"project: {project}",
            f"branch: {branch or 'unknown'}",
            f"ranking: {ranking_method}{query_info}",
            "source: neural-tape",
            f"expires: {(datetime.now() + timedelta(days=1)).isoformat()}",
            "---",
            "",
            "# Session Context — Neural Tape",
            "",
            f"## Active Insights ({len(insights)})",
            "| Date | Type | Content | Assistant | Confidence | File |",
            "|------|------|---------|-----------|------------|------|",
        ]

        for ins in insights:
            date = ins["timestamp"][:10] if ins["timestamp"] else "?"
            content_preview = ins["content"][:60] if ins["content"] else "..."
            assistant_name = ins.get("assistant", "unknown")
            lines.append(f"| {date} | {ins['type']} | {content_preview}... | {assistant_name} | {ins['confidence']} | {ins['file']} |")

        # Assistant summary
        from collections import Counter
        assistant_counts = Counter(i.get("assistant", "unknown") for i in insights)
        lines.extend([
            "",
            "## Assistant Summary",
        ])
        for assistant_name, count in assistant_counts.most_common():
            lines.append(f"- **{assistant_name}**: {count} insights")

        lines.extend([
            "",
            f"## Recurring Patterns ({len(patterns)})",
        ])

        for pat in patterns:
            last_date = pat['last_seen'][:10] if pat['last_seen'] else '?'
            lines.append(f"- **{pat['name']}**: {pat['count']} occurrences (last: {last_date})")
            for ex in pat["examples"]:
                lines.append(f"  - {ex[:80]}")

        lines.extend([
            "",
            "## Deja Vu Alerts",
            "| Similarity | Reference | Preview |",
            "|------------|-----------|---------|",
            "| — | — | Run `deja_vu.py` to check |",
            "",
            "## Vault Links",
        ])

        if self.wiki_dir and self.wiki_dir.exists():
            lines.append(f"- [[{project}]] — Project wiki page")
            lines.append("- [[Memory]] — Operational patterns")
        else:
            lines.append("- Wiki not configured — set `etercervo_wiki` in config.yaml")
        if include_lex and self.lex_memory:
            mem_lines = self._read_lex_memory(20)
            if mem_lines:
                lines.extend([
                    "",
                    "## Lex Memory (last 20 lines)",
                    "```",
                ])
                lines.extend(mem_lines)
                lines.append("```")

        content = "\n".join(lines)
        self.output_path.write_text(content, encoding="utf-8")
        print(f"[NeuralTape] Session context generated: {self.output_path}")
        return self.output_path


def main():
    parser = argparse.ArgumentParser(description="Neural Tape Pre-Load")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--project", default=None, help="Project name")
    parser.add_argument("--branch", default=None, help="Git branch")
    parser.add_argument("--query", default=None, help="BM25 search query for relevance ranking")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        script_dir = Path(__file__).parent.parent
        config_path = script_dir / args.config

    config = Config(config_path)
    pl = PreLoad(config)
    pl.generate(project=args.project, branch=args.branch, query=args.query)


if __name__ == "__main__":
    main()
