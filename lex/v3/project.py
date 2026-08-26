"""project — project identity resolver (Q4=C).

Strategy (decided with Guglielmo 2026-07-14):
    1. realpath(root) — canonical path (resolves symlinks, critical because
       projects live on an external disk mounted under /run/media/<user>/).
    2. Look for .neuraltape/project.yaml in the root.
    3. If valid → project_id from config (human-readable, stable).
    4. If missing → project_id = "auto-" + sha256(canonical)[:10], source="fallback-hash",
       log WARNING.

project_id validation: ^[a-z0-9][a-z0-9-]{0,31}$ (lowercase, digits, hyphens, ≤32 chars).
No collisions allowed between configured IDs (raises at resolver build time).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger("neural-tape-v3")

PROJECT_CONFIG_DIRNAME = ".neuraltape"
PROJECT_CONFIG_FILENAME = "project.yaml"
ID_REGEX = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
HASH_PREFIX_LEN = 10


def default_projects_base() -> Path:
    """Base directory containing workspace folders.

    Resolution order:
        1. NEURALTAPE_PROJECTS_ROOT env (explicit override)
        2. the first ``/run/media/<user>/Back-Up`` directory (external-disk
           layout; survives user/mount renames)
        3. ``$HOME`` (portable fallback)
    """
    env = os.environ.get("NEURALTAPE_PROJECTS_ROOT")
    if env and env.strip():
        return Path(env).expanduser()
    media = Path("/run/media")
    if media.is_dir():
        try:
            for child in media.iterdir():
                candidate = child / "Back-Up"
                if candidate.is_dir():
                    return candidate
        except OSError:
            pass
    return Path.home()


@dataclass(frozen=True)
class Project:
    project_id: str
    root: Path
    source: str                 # "config" | "fallback-hash"
    config_path: Path | None    # path to .neuraltape/project.yaml if present
    display_name: str | None = None
    kind: str | None = None


class ProjectResolver:
    """Resolves a workspace root path to a stable Project identity."""

    def __init__(self, workspace_roots: list[Path] | None = None):
        # Pre-validate known roots to catch duplicate project_ids at startup.
        self._known: dict[Path, Project] = {}
        self._ids_in_use: dict[str, Path] = {}
        if workspace_roots:
            for root in workspace_roots:
                proj = self.resolve(root)
                # resolve() already registered it; nothing else to do.

    def resolve(self, root: Path) -> Project:
        """Resolve a workspace root to a Project. Idempotent (caches result)."""
        canonical = self._canonical(root)
        if canonical in self._known:
            return self._known[canonical]

        config_path = canonical / PROJECT_CONFIG_DIRNAME / PROJECT_CONFIG_FILENAME
        project = self._try_config(canonical, config_path) or self._fallback_hash(canonical)

        # Collision check: same project_id from a different root is an error.
        prev = self._ids_in_use.get(project.project_id)
        if prev is not None and prev != canonical:
            raise ValueError(
                f"project_id '{project.project_id}' is used by two different roots: "
                f"{prev} and {canonical}. project_id must be unique."
            )
        self._ids_in_use[project.project_id] = canonical
        self._known[canonical] = project
        return project

    def resolve_by_transcript(self, transcript_path: Path) -> Project:
        """Infer a project from a transcript path under workspaceStorage.

        VS Code layout: .../<workspaceHash>/GitHub.copilot-chat/transcripts/<id>.jsonl
        We cannot reverse the hash, so callers must pass an explicit workspace root.
        This helper is a stub for Fase 1; Fase 0 only needs resolve(root).
        """
        raise NotImplementedError(
            "resolve_by_transcript is Fase 1; Fase 0 uses resolve(root) with explicit roots."
        )

    # ---- internals ------------------------------------------------------

    @staticmethod
    def _canonical(root: Path) -> Path:
        # realpath() resolves symlinks. If the path doesn't exist (rare), fall back to absolute.
        try:
            return root.resolve(strict=False)
        except (OSError, RuntimeError):
            return root.absolute()

    def _try_config(self, canonical: Path, config_path: Path) -> Project | None:
        if not config_path.exists():
            return None
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError) as e:
            log.warning("project.yaml at %s unreadable (%s); falling back to hash", config_path, e)
            return None

        project_id = str(data.get("project_id", "")).strip()
        if not ID_REGEX.match(project_id):
            log.warning(
                "project_id %r in %s is invalid (must match %s); falling back to hash",
                project_id, config_path, ID_REGEX.pattern,
            )
            return None

        return Project(
            project_id=project_id,
            root=canonical,
            source="config",
            config_path=config_path,
            display_name=data.get("display_name"),
            kind=data.get("kind"),
        )

    def _fallback_hash(self, canonical: Path) -> Project:
        digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()
        project_id = f"auto-{digest[:HASH_PREFIX_LEN]}"
        log.warning(
            "No %s/%s found at %s; using fallback project_id='%s'. "
            "Create a config file for a stable, human-readable id.",
            PROJECT_CONFIG_DIRNAME, PROJECT_CONFIG_FILENAME, canonical, project_id,
        )
        return Project(
            project_id=project_id,
            root=canonical,
            source="fallback-hash",
            config_path=None,
        )


# ---- bootstrap helper (una tantum per i 6 workspace) --------------------

# Proposed IDs for the 6 currently-open workspace folders.
# Used by bootstrap_projects.py; safe to override per-workspace.
DEFAULT_BOOTSTRAP_IDS = {
    "EterCervo": "etercervo",
    "Zeus": "zeus",
    "cais-lp": "cais-lp",
    "tec-andrea-v2": "tec-andrea",
    "S4all_BOT": "s4all-bot",
    "NeuralTape": "neuraltape",
}


def write_project_config(root: Path, project_id: str, *,
                         display_name: str | None = None,
                         kind: str | None = None,
                         force: bool = False) -> Path:
    """Create .neuraltape/project.yaml in root. Returns the path written.

    Raises ValueError if project_id is invalid, or FileExistsError if a config
    already exists and force=False.
    """
    if not ID_REGEX.match(project_id):
        raise ValueError(f"Invalid project_id {project_id!r}; must match {ID_REGEX.pattern}")

    config_dir = root / PROJECT_CONFIG_DIRNAME
    config_path = config_dir / PROJECT_CONFIG_FILENAME
    if config_path.exists() and not force:
        raise FileExistsError(f"{config_path} already exists (use force=True to overwrite)")

    config_dir.mkdir(parents=True, exist_ok=True)
    data = {"project_id": project_id}
    if display_name:
        data["display_name"] = display_name
    if kind:
        data["kind"] = kind
    config_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return config_path