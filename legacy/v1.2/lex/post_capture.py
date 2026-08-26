#!/usr/bin/env python3
"""
Neural Tape — Post-Capture
Interactive review of staging insights at session end.
"""

import os
import re
import argparse
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

try:
    import yaml
except ImportError:
    raise SystemExit("PyYAML required: pip install pyyaml")


@dataclass
class StagingSummary:
    total: int
    by_category: Dict[str, int]
    files: List[Path]


class Config:
    def __init__(self, path: Path):
        with open(path, "r", encoding="utf-8") as f:
            self.raw = yaml.safe_load(f)
        self.paths = self.raw.get("paths", {})
        self.post_capture = self.raw.get("post_capture", {})

    def get_staging_dir(self) -> Path:
        root = self.paths.get("neural_tape_root", ".")
        return Path(root) / "tape" / "staging"

    def get_archive_dir(self) -> Path:
        root = self.paths.get("neural_tape_root", ".")
        return Path(root) / "tape" / "archive"


class PostCapture:
    """Interactive review of staging insights."""

    def __init__(self, config: Config):
        self.config = config
        self.staging_dir = config.get_staging_dir()
        self.archive_dir = config.get_archive_dir()
        self.default_action = config.post_capture.get("default_action", "prompt")
        self.review_ui = config.post_capture.get("review_ui", "interactive")

    def _input(self, prompt: str, default: str = "") -> str:
        """Read input safely; return default when stdin is closed."""
        try:
            return input(prompt).strip().lower()
        except EOFError:
            if default:
                print(f"[NeuralTape] No interactive stdin; using default action: {default}")
            else:
                print("[NeuralTape] No interactive stdin; continuing.")
            return default

    def summary(self) -> StagingSummary:
        """Return summary of staging insights."""
        files = []
        by_category: Dict[str, int] = {}
        if self.staging_dir.exists():
            for f in sorted(self.staging_dir.glob("*.md")):
                files.append(f)
                cat = self._extract_category(f) or "unknown"
                by_category[cat] = by_category.get(cat, 0) + 1
        return StagingSummary(total=len(files), by_category=by_category, files=files)

    def _extract_frontmatter(self, text: str) -> Dict[str, Any]:
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    return yaml.safe_load(parts[1]) or {}
                except Exception:
                    pass
        return {}

    def _extract_category(self, fpath: Path) -> Optional[str]:
        try:
            text = fpath.read_text(encoding="utf-8")
            fm = self._extract_frontmatter(text)
            return fm.get("type")
        except Exception:
            return None

    def _extract_title(self, fpath: Path) -> str:
        try:
            text = fpath.read_text(encoding="utf-8")
            m = re.search(r"^# (.+)$", text, re.MULTILINE)
            return m.group(1) if m else fpath.stem
        except Exception:
            return fpath.stem

    def _get_confidence(self, fpath: Path) -> str:
        """Estrai la confidence dal frontmatter di un file staging."""
        try:
            text = fpath.read_text(encoding="utf-8")
            fm = self._extract_frontmatter(text)
            return str(fm.get("confidence", "")).lower()
        except Exception:
            return ""

    def _is_high_value(self, fpath: Path) -> bool:
        """Verifica se un insight ha valore (confidence high o medium)."""
        confidence = self._get_confidence(fpath)
        return confidence in ("high", "medium")

    def promote(self, insight_file: Path) -> Path:
        """Promote insight from staging to archive."""
        text = insight_file.read_text(encoding="utf-8")
        fm = self._extract_frontmatter(text)
        category = fm.get("type", "meta")
        target_dir = self.archive_dir / category
        target_dir.mkdir(parents=True, exist_ok=True)

        # Generate canonical filename: {date}-{slug}.md
        ts_raw = fm.get("timestamp", "")
        if isinstance(ts_raw, __import__('datetime').datetime):
            ts_raw = ts_raw.isoformat()
        ts = str(ts_raw)[:10].replace("-", "")
        if not ts:
            ts = insight_file.stem.split("-")[0]
        slug = re.sub(r'[^a-z0-9]+', '-', self._extract_title(insight_file).lower()).strip('-')
        if len(slug) > 40:
            slug = slug[:40]
        fname = f"{ts}-{slug}.md"
        target = target_dir / fname

        # Avoid overwrite
        counter = 1
        original_target = target
        while target.exists():
            target = original_target.with_name(f"{ts}-{slug}-{counter}.md")
            counter += 1

        target.write_text(text, encoding="utf-8")
        insight_file.unlink()
        print(f"  -> Promoted to archive/{category}/{target.name}")
        return target

    def discard(self, insight_file: Path):
        """Discard staging insight."""
        insight_file.unlink()
        print(f"  -> Discarded.")

    def modify(self, insight_file: Path) -> Path:
        """Open insight in default editor, then promote."""
        editor = os.environ.get("EDITOR", "notepad" if os.name == "nt" else "nano")
        os.system(f'{editor} "{insight_file}"')
        self._input("Press Enter after editing...", default="")
        return self.promote(insight_file)

    def review_interactive(self):
        """Interactive review loop. Falls back to auto-promote when stdin is not a TTY."""
        if not sys.stdin.isatty():
            print("[NeuralTape] Non-interactive stdin detected; auto-promoting.")
            self.auto_promote_all()
            return
        self._review_interactive_body()

    def _review_interactive_body(self):
        """Core interactive review logic (separated for EOFError handling)."""
        summary = self.summary()
        if summary.total == 0:
            print("[NeuralTape] No staging insights to review.")
            return

        print(f"\n[NeuralTape] Session closed — {summary.total} insights captured:\n")
        for i, f in enumerate(summary.files, 1):
            cat = self._extract_category(f) or "?"
            title = self._extract_title(f)
            print(f"{i}. [{cat}] {title}")
            print(f"   File: {f.name}")

        print("\nActions: [r]eview all / [p]romote all / [s]kip all / [q]uit")
        choice = self._input("> ", default="q")

        if choice == "q":
            return
        elif choice == "p":
            for f in summary.files:
                self.promote(f)
            return
        elif choice == "s":
            for f in summary.files:
                self.discard(f)
            return
        elif choice == "r":
            for i, f in enumerate(summary.files, 1):
                cat = self._extract_category(f) or "?"
                title = self._extract_title(f)
                print(f"\nReviewing {i}/{summary.total}: [{cat}] {title}")
                print("-" * 60)
                try:
                    body = f.read_text(encoding="utf-8").split("---", 2)[-1].strip()
                    print(body[:500])
                    if len(body) > 500:
                        print("...")
                except Exception:
                    print("(could not read content)")
                print("-" * 60)

                while True:
                    action = self._input("[p]romote / [m]odify / [s]carta / [n]ext: ", default="n")
                    if action == "p":
                        self.promote(f)
                        break
                    elif action == "m":
                        self.modify(f)
                        break
                    elif action == "s":
                        self.discard(f)
                        break
                    elif action == "n":
                        break
                    else:
                        print("Invalid choice.")
        else:
            print("Invalid choice.")

    def auto_promote_all(self):
        """Auto-promote high-value staging insights without interaction.
        
        Filters: only confidence high/medium are promoted.
        Low-confidence insights are discarded (noise reduction).
        """
        summary = self.summary()
        if summary.total == 0:
            print("[NeuralTape] No staging insights to auto-promote.")
            return

        # Filter: only high/medium confidence
        high_value = [f for f in summary.files if self._is_high_value(f)]
        low_value = [f for f in summary.files if not self._is_high_value(f)]

        if low_value:
            print(f"[NeuralTape] Discarding {len(low_value)} low-confidence insights (noise).")
            for f in low_value:
                f.unlink()

        if not high_value:
            print("[NeuralTape] No high-value insights to promote.")
            return

        print(f"[NeuralTape] Auto-promoting {len(high_value)} high-value insights...")
        for f in high_value:
            self.promote(f)
        print(f"[NeuralTape] Auto-promoted {len(high_value)} insights.")

    def run_non_interactive(self):
        """Run safely when stdin has no TTY, as in session shutdown hooks."""
        action = str(self.default_action or "prompt").lower().replace("-", "_")
        if action in {"prompt", "promote", "auto_promote"}:
            print("[NeuralTape] Non-interactive stdin detected; auto-promoting staging insights.")
            self.auto_promote_all()
            return
        if action in {"skip", "keep"}:
            summary = self.summary()
            print(f"[NeuralTape] Non-interactive stdin detected; keeping {summary.total} staging insights.")
            return
        raise SystemExit(f"Unsupported non-interactive post_capture.default_action: {self.default_action}")

    def update_archive_index(self):
        """Generate archive/index.md catalog."""
        index_path = self.archive_dir / "index.md"
        lines = [
            "---",
            "type: index",
            f"updated: {__import__('datetime').datetime.now().isoformat()}",
            "---",
            "",
            "# Archive Index",
            "",
        ]

        for category_dir in sorted(self.archive_dir.iterdir()):
            if not category_dir.is_dir() or category_dir.name == ".gitkeep":
                continue
            lines.append(f"## {category_dir.name}")
            for f in sorted(category_dir.glob("*.md")):
                title = self._extract_title(f)
                lines.append(f"- [{title}]({category_dir.name}/{f.name})")
            lines.append("")

        index_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"[NeuralTape] Archive index updated: {index_path}")


def main():
    parser = argparse.ArgumentParser(description="Neural Tape Post-Capture")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--review", action="store_true", help="Interactive review")
    parser.add_argument("--auto-promote", action="store_true", help="Auto-promote all staging insights without interaction")
    parser.add_argument("--index", action="store_true", help="Update archive index")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        script_dir = Path(__file__).parent.parent
        config_path = script_dir / args.config

    config = Config(config_path)
    pc = PostCapture(config)

    if args.index:
        pc.update_archive_index()
    elif args.auto_promote:
        pc.auto_promote_all()
        pc.update_archive_index()
    elif args.review or not any([args.index, args.auto_promote]):
        if sys.stdin.isatty():
            pc.review_interactive()
        else:
            pc.run_non_interactive()
        pc.update_archive_index()


if __name__ == "__main__":
    main()
