#!/usr/bin/env python3
"""
Neural Tape — Pre-Load
Generates session-context.md before AI assistant session starts.
"""

import re
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

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


# ── BM25 Search (ported from agentmemory ranking) ─────────────────────

def _tokenize(text: str) -> List[str]:
    """Simple whitespace + lowercasing tokenizer."""
    return re.findall(r'\w+', text.lower())


def bm25_score(query_tokens: List[str], doc_tokens: List[str],
               avg_dl: float, k1: float = 1.5, b: float = 0.75) -> float:
    """Compute BM25 score for a single document.
    
    Pure Python, no external deps. Ported from agentmemory ranking logic.
    """
    dl = len(doc_tokens)
    if dl == 0 or not query_tokens:
        return 0.0

    # Term frequency map
    tf = {}
    for t in doc_tokens:
        tf[t] = tf.get(t, 0) + 1

    score = 0.0
    for qt in query_tokens:
        if qt not in tf:
            continue
        term_tf = tf[qt]
        numerator = term_tf * (k1 + 1)
        denominator = term_tf + k1 * (1 - b + b * (dl / avg_dl))
        score += numerator / denominator
    return score


class BM25Ranker:
    """BM25 ranker for insight retrieval. No external deps."""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs: List[Dict] = []
        self.doc_tokens: List[List[str]] = []
        self.avg_dl: float = 0.0

    def index(self, documents: List[Dict], text_key: str = "content"):
        """Index documents for ranking. text_key specifies which field to search."""
        self.docs = documents
        self.doc_tokens = []
        total_len = 0
        for doc in documents:
            text = doc.get(text_key, "") + " " + doc.get("type", "")
            tokens = _tokenize(text)
            self.doc_tokens.append(tokens)
            total_len += len(tokens)
        self.avg_dl = total_len / max(len(documents), 1)

    def search(self, query: str, top_k: int = 10) -> List[Dict]:
        """Search indexed documents. Returns top_k results with BM25 scores."""
        if not self.docs:
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return self.docs[:top_k]

        scored = []
        for i, doc in enumerate(self.docs):
            score = bm25_score(query_tokens, self.doc_tokens[i],
                               self.avg_dl, self.k1, self.b)
            # Blend BM25 with decay strength (70% BM25, 30% decay)
            decay_score = doc.get("strength", 0.5)
            blended = 0.7 * score + 0.3 * decay_score
            scored.append((blended, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [doc for _, doc in scored[:top_k]]


class Config:
    def __init__(self, path: Path):
        with open(path, "r", encoding="utf-8") as f:
            self.raw = yaml.safe_load(f)
        self.paths = self.raw.get("paths", {})
        self.pre_load = self.raw.get("pre_load", {})
        self.deja_vu = self.raw.get("deja_vu", {})

    def get_archive_dir(self) -> Path:
        root = self.paths.get("neural_tape_root", ".")
        return Path(root) / "tape" / "archive"

    def get_wiki_dir(self) -> Optional[Path]:
        p = self.paths.get("etercervo_wiki", "")
        return Path(p) if p else None

    def get_lex_memory(self) -> Optional[Path]:
        p = self.paths.get("lex_memory", "")
        return Path(p) if p else None

    def get_output_path(self) -> Path:
        root = self.paths.get("neural_tape_root", ".")
        return Path(root) / "session-context.md"


class PreLoad:
    """Generate session context for AI assistant startup."""

    def __init__(self, config: Config):
        self.config = config
        self.archive_dir = config.get_archive_dir()
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

    @staticmethod
    def _normalize_project_name(workspace_or_project: str) -> str:
        """Normalize a workspace label into a project name.

        Accepts both legacy labels ('MyWorkspace.code-workspace') and already
        normalized values ('MyWorkspace'). Strips common VS Code suffixes.
        """
        if not workspace_or_project:
            return "default"
        name = str(workspace_or_project)
        for suffix in ("-Workspace.code-workspace", ".code-workspace", ".code.json"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        return name or "default"

    @staticmethod
    def _parse_timestamp(frontmatter: Dict[str, Any]) -> Optional[datetime]:
        """Parse a robust timestamp from frontmatter.

        Priority: 'timestamp' (ISO-8601) -> 'date' (YYYY-MM-DD at midnight) -> None.
        Tolerates 'Z' suffix, missing timezone, and date-only strings.
        """
        ts_raw = frontmatter.get("timestamp", "")
        if isinstance(ts_raw, datetime):
            return ts_raw.astimezone() if ts_raw.tzinfo else ts_raw.replace(tzinfo=None)
        ts_str = str(ts_raw or "").strip()

        if ts_str:
            try:
                parsed = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=None)
            except ValueError:
                pass

        date_raw = frontmatter.get("date", "")
        date_str = str(date_raw or "").strip()
        if date_str:
            for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
                try:
                    return datetime.strptime(date_str, fmt)
                except ValueError:
                    continue
        return None

    def _read_insights(self, project: str, lookback_days: int, assistant: str = None) -> List[Dict]:
        """Read archive insights filtered by project, recency, confidence, and decay.

        Reads from archive/{category}/ (current structure, fixed from old {assistant}/{category}/).
        Applies Ebbinghaus decay: old insights rank lower and are auto-forged below threshold.

        Schema (standardized 2026-07-18):
          Required:  type, date|timestamp
          Optional:  project, workspace, confidence, assistant, session, status, source
        Backward-compatible: missing fields get sensible defaults so legacy v2.2
        archives written before standardization still rank and surface.
        """
        insights: List[Dict] = []
        cutoff = datetime.now().astimezone() - timedelta(days=lookback_days)

        if not self.archive_dir.exists():
            return insights

        # Read from archive/{category}/ (current structure, single tier)
        category_dirs = [d for d in self.archive_dir.iterdir() if d.is_dir() and d.name != "index"]

        for category_dir in category_dirs:
            for fpath in category_dir.glob("*.md"):
                try:
                    text = fpath.read_text(encoding="utf-8")
                    frontmatter = self._extract_frontmatter(text)

                    ts = self._parse_timestamp(frontmatter)
                    if ts is None:
                        continue  # Unparseable: skip silently (don't pollute ranking)
                    ts_aware = ts if ts.tzinfo else ts.replace(tzinfo=datetime.now().astimezone().tzinfo)
                    if ts_aware < cutoff:
                        continue

                    confidence = str(frontmatter.get("confidence", "") or "medium").lower()
                    if confidence == "low":
                        continue

                    # Decay + auto-forget (ported from agentmemory)
                    ts_str = ts.isoformat()
                    strength = decay_strength(ts_str)
                    if is_below_threshold(strength):
                        continue  # Auto-forget: skip insights below threshold

                    # Project resolution: 'project' field -> 'workspace' (normalized) -> 'default'
                    proj_raw = frontmatter.get("project")
                    if proj_raw:
                        proj = str(proj_raw)
                    else:
                        workspace = frontmatter.get("workspace", "")
                        proj = self._normalize_project_name(workspace) if workspace else "default"

                    if project != "default" and proj.lower() != project.lower():
                        continue

                    if assistant:
                        ins_assistant = str(frontmatter.get("assistant", "") or "lex").lower()
                        if ins_assistant != assistant.lower():
                            continue

                    insights.append({
                        "file": str(fpath.relative_to(self.archive_dir.parent)),
                        "type": frontmatter.get("type", "meta"),
                        "timestamp": ts_str,
                        "confidence": confidence,
                        "content": self._extract_title(text) or frontmatter.get("trigger", "") or fpath.stem,
                        "project": proj,
                        "assistant": str(frontmatter.get("assistant", "") or "lex"),
                        "strength": round(strength, 3),  # Decay-aware ranking key
                    })
                except Exception:
                    continue

        # Sort: strength (decay-aware) first, then recency, then confidence
        insights.sort(key=lambda x: (x.get("strength", 0), x["timestamp"], x["confidence"] == "high"), reverse=True)
        return insights

    def _rank_insights(self, insights: List[Dict], query: str = None, top_k: int = 10) -> List[Dict]:
        """Rank insights using BM25 if query provided, otherwise use decay-based sort.
        
        When query is given: 70% BM25 relevance + 30% decay strength.
        When no query: pure decay-based ranking (existing behavior).
        """
        if not query or not insights:
            return insights[:top_k]

        ranker = BM25Ranker()
        ranker.index(insights, text_key="content")
        return ranker.search(query, top_k=top_k)

    def _extract_frontmatter(self, text: str) -> Dict[str, Any]:
        """Extract YAML frontmatter from markdown."""
        if not text.startswith("---"):
            return {}
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}
        try:
            return yaml.safe_load(parts[1]) or {}
        except Exception:
            return {}

    def _extract_title(self, text: str) -> Optional[str]:
        """Extract first H1 from markdown body."""
        m = re.search(r"^# (.+)$", text, re.MULTILINE)
        return m.group(1) if m else None

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
        
        If query is provided, BM25 ranking is used (70% relevance + 30% decay).
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
            ranking_method = "BM25 + decay"
        else:
            insights = all_insights[:max_insights]
            ranking_method = "decay-based"
        patterns = self._detect_patterns(insights)[:max_patterns]

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
