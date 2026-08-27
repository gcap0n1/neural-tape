#!/usr/bin/env python3
"""bootstrap_projects — create .neuraltape/project.yaml for the 6 workspace folders.

Una tantum script (Fase 0 setup, Q4=C). Run:

    python neuraltape/v3/bootstrap_projects.py                 # use default paths
    python neuraltape/v3/bootstrap_projects.py --dry-run       # preview, no writes
    python neuraltape/v3/bootstrap_projects.py --force         # overwrite existing
    python neuraltape/v3/bootstrap_projects.py --root /path --id custom-id

The script is idempotent: refuses to overwrite an existing config unless --force.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow direct execution: resolve sibling module imports.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from project import (  # type: ignore[import-not-found]
    DEFAULT_BOOTSTRAP_IDS,
    ProjectResolver,
    default_projects_base,
    write_project_config,
)

log = logging.getLogger("neural-tape-v3-bootstrap")

# Workspace base: NEURALTAPE_PROJECTS_ROOT env, external-disk layout, or $HOME.
_DEFAULT_BASE = default_projects_base()


def _bootstrap_ids() -> dict[str, str]:
    """Merged id map: local config.yaml `bootstrap_ids` wins over the
    neutral built-in example. The local file is machine-specific and is
    never published."""
    ids = dict(DEFAULT_BOOTSTRAP_IDS)
    try:
        import yaml

        cfg_path = HERE.parent.parent / "config.yaml"
        if cfg_path.exists():
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            extra = data.get("bootstrap_ids") or {}
            if isinstance(extra, dict):
                ids.update({str(k): str(v) for k, v in extra.items() if k and v})
    except Exception as exc:  # never fail the tool over a bad local config
        log.warning("bootstrap_ids from config.yaml unreadable (%s)", exc)
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description="Bootstrap .neuraltape/project.yaml files")
    ap.add_argument("--base", default=str(_DEFAULT_BASE),
                    help=f"base directory containing workspace folders (default: {_DEFAULT_BASE})")
    ap.add_argument("--root", action="append", default=[],
                    help="explicit workspace root (can repeat); overrides --base scan")
    ap.add_argument("--id", action="append", default=[], metavar="ROOT=ID",
                    help="override project_id for a root, e.g. --id /path/Zeus=zeus")
    ap.add_argument("--dry-run", action="store_true", help="preview only, no writes")
    ap.add_argument("--force", action="store_true", help="overwrite existing configs")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    # Build override map from --id flags.
    overrides: dict[Path, str] = {}
    for spec in args.id:
        if "=" not in spec:
            log.error("--id must be ROOT=ID, got %r", spec)
            return 2
        r, i = spec.split("=", 1)
        overrides[Path(r).expanduser().resolve()] = i.strip()

    # Collect target roots.
    ids = _bootstrap_ids()
    if args.root:
        roots = [Path(r).expanduser().resolve() for r in args.root]
    else:
        base = Path(args.base).expanduser().resolve()
        roots = [base / name for name in ids.keys()]
        roots = [r for r in roots if r.exists()]

    if not roots:
        log.error("No workspace roots found. Pass --root explicitly or check --base.")
        return 1

    log.info("Bootstrapping %d workspace(s):", len(roots))

    written = 0
    skipped = 0
    for root in roots:
        folder_name = root.name
        project_id = overrides.get(root) or ids.get(folder_name)
        if not project_id:
            log.warning("  %s: no project_id mapping (skipped)", root)
            skipped += 1
            continue

        # Idempotency check without --force.
        cfg = root / ".neuraltape" / "project.yaml"
        if cfg.exists() and not args.force:
            log.info("  %s → %s (EXISTS, skip; use --force)", root, project_id)
            skipped += 1
            continue

        if args.dry_run:
            log.info("  %s → %s (DRY RUN)", root, project_id)
            continue

        try:
            write_project_config(
                root, project_id,
                display_name=folder_name.replace("-", " ").replace("_", " ").title(),
                force=args.force,
            )
            written += 1
        except (ValueError, FileExistsError, OSError) as e:
            log.error("  %s → %s FAILED: %s", root, project_id, e)
            skipped += 1

    # Verify by resolving everything through ProjectResolver.
    log.info("--- Verification ---")
    resolver = ProjectResolver(workspace_roots=roots)
    for root in roots:
        proj = resolver.resolve(root)
        flag = "✓" if proj.source == "config" else "⚠"
        log.info("  %s %s → %s (%s)", flag, root.name, proj.project_id, proj.source)

    log.info("Done: %d written, %d skipped.", written, skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
