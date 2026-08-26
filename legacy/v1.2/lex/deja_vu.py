#!/usr/bin/env python3
"""
Neural Tape — Deja Vu
Similarity detection between new insights and archived ones.
"""

import re
import argparse
from pathlib import Path
from difflib import SequenceMatcher
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

try:
    import yaml
except ImportError:
    raise SystemExit("PyYAML required: pip install pyyaml")


@dataclass
class Match:
    similarity: float
    archived_file: Path
    alert_level: str  # identical | similar | related


class Config:
    def __init__(self, path: Path):
        with open(path, "r", encoding="utf-8") as f:
            self.raw = yaml.safe_load(f)
        self.paths = self.raw.get("paths", {})
        self.deja_vu = self.raw.get("deja_vu", {})

    def get_archive_dir(self) -> Path:
        root = self.paths.get("neural_tape_root", ".")
        return Path(root) / "tape" / "archive"

    def get_staging_dir(self) -> Path:
        root = self.paths.get("neural_tape_root", ".")
        return Path(root) / "tape" / "staging"


class DejaVu:
    """Detect similar insights across sessions."""

    def __init__(self, config: Config):
        self.config = config
        self.threshold = config.deja_vu.get("similarity_threshold", 0.75)
        self.normalization_rules = config.deja_vu.get("normalization", [])
        self.archive_dir = config.get_archive_dir()

    def _normalize(self, text: str) -> str:
        """Normalize text for comparison."""
        result = text
        for rule in self.normalization_rules:
            try:
                pattern = rule.get("pattern", "")
                replacement = rule.get("replacement", "")
                if pattern:
                    result = re.sub(pattern, replacement, result)
            except re.error:
                continue
        # Also lowercase and strip extra whitespace
        result = result.lower()
        result = re.sub(r'\s+', ' ', result)
        return result.strip()

    def _keyword_similarity(self, a: str, b: str) -> float:
        """Jaccard similarity on keyword sets."""
        def keywords(text: str) -> set:
            # Extract words, ignore short/common ones
            words = re.findall(r'\b[a-z]{4,}\b', text.lower())
            stopwords = {'this', 'that', 'with', 'from', 'have', 'been', 'were', 'error', 'failed'}
            return set(w for w in words if w not in stopwords)

        set_a = keywords(a)
        set_b = keywords(b)
        if not set_a and not set_b:
            return 1.0
        if not set_a or not set_b:
            return 0.0
        intersection = set_a & set_b
        union = set_a | set_b
        return len(intersection) / len(union) if union else 0.0

    def _sequence_similarity(self, a: str, b: str) -> float:
        """SequenceMatcher ratio."""
        return SequenceMatcher(None, a, b).ratio()

    def similarity(self, a: str, b: str) -> float:
        """Weighted similarity: 60% keyword + 40% sequence."""
        norm_a = self._normalize(a)
        norm_b = self._normalize(b)
        kw_sim = self._keyword_similarity(norm_a, norm_b)
        seq_sim = self._sequence_similarity(norm_a, norm_b)
        return 0.6 * kw_sim + 0.4 * seq_sim

    def _extract_body(self, text: str) -> str:
        """Extract markdown body (after frontmatter)."""
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                return parts[2]
        return text

    def check_file(self, staging_file: Path) -> List[Match]:
        """Check a staging file against all archived insights."""
        if not staging_file.exists():
            return []

        staging_text = staging_file.read_text(encoding="utf-8")
        staging_body = self._extract_body(staging_text)

        matches = []
        if not self.archive_dir.exists():
            return matches

        for category_dir in self.archive_dir.iterdir():
            if not category_dir.is_dir():
                continue
            for archive_file in category_dir.glob("*.md"):
                try:
                    archive_text = archive_file.read_text(encoding="utf-8")
                    archive_body = self._extract_body(archive_text)
                    sim = self.similarity(staging_body, archive_body)

                    if sim >= self.threshold:
                        if sim >= 0.90:
                            level = "identical"
                        elif sim >= 0.75:
                            level = "similar"
                        else:
                            level = "related"
                        matches.append(Match(
                            similarity=sim,
                            archived_file=archive_file,
                            alert_level=level,
                        ))
                except Exception:
                    continue

        matches.sort(key=lambda m: m.similarity, reverse=True)
        return matches

    def check_all_staging(self) -> Dict[Path, List[Match]]:
        """Check all staging files."""
        staging_dir = self.config.get_staging_dir()
        results = {}
        if not staging_dir.exists():
            return results
        for f in staging_dir.glob("*.md"):
            results[f] = self.check_file(f)
        return results

    def update_session_context(self, session_context_path: Path, matches: Dict[Path, List[Match]]):
        """Update session-context.md with deja vu alerts."""
        if not session_context_path.exists():
            return

        text = session_context_path.read_text(encoding="utf-8")
        # Replace the placeholder deja vu section
        alert_lines = [
            "## Deja Vu Alerts",
            "| Similarity | Reference | Preview |",
            "|------------|-----------|---------|",
        ]

        for f, ms in matches.items():
            for m in ms[:3]:  # top 3 per file
                ref = str(m.archived_file.relative_to(self.archive_dir.parent))
                alert_lines.append(f"| {m.similarity:.0%} | {ref} | {m.alert_level} |")

        if not any(ms for ms in matches.values()):
            alert_lines.append("| — | — | No matches detected |")

        # Simple replacement: find ## Deja Vu Alerts section and replace until next ##
        replacement = "\n".join(alert_lines) + "\n\n"
        new_text = re.sub(
            r'## Deja Vu Alerts\n.*?\n(?=## |$)',
            lambda m: replacement,
            text,
            flags=re.DOTALL,
        )
        session_context_path.write_text(new_text, encoding="utf-8")
        print(f"[NeuralTape] Updated {session_context_path} with deja vu alerts.")


def main():
    parser = argparse.ArgumentParser(description="Neural Tape Deja Vu")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--file", default=None, help="Specific staging file to check")
    parser.add_argument("--update-context", action="store_true", help="Update session-context.md")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        script_dir = Path(__file__).parent.parent
        config_path = script_dir / args.config

    config = Config(config_path)
    dv = DejaVu(config)

    if args.file:
        fpath = Path(args.file)
        matches = dv.check_file(fpath)
        print(f"[NeuralTape] {fpath.name}: {len(matches)} matches")
        for m in matches:
            print(f"  - {m.similarity:.0%} {m.alert_level}: {m.archived_file}")
    else:
        all_matches = dv.check_all_staging()
        total = sum(len(ms) for ms in all_matches.values())
        print(f"[NeuralTape] Checked {len(all_matches)} staging files, {total} matches found.")

        if args.update_context:
            root = config.paths.get("neural_tape_root", ".")
            ctx_path = Path(root) / "session-context.md"
            dv.update_session_context(ctx_path, all_matches)


if __name__ == "__main__":
    main()
