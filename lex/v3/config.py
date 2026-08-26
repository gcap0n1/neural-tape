"""config — v3 config loader.

Extends v2.2 config.yaml with a `v3:` section. Falls back to safe defaults if
the section is missing, so v3 never crashes because of an old config file.

Feature flag resolution order (first wins):
    1. Env NEURALTAPE_V3=1|0 (explicit override)
    2. config.yaml: v3.enabled
    3. Default: False (v3 dormant, v2.2 untouched)
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

from transcript_sources import SourceManifest, manifest_from_dict  # noqa: E402

log = logging.getLogger("neural-tape-v3")

DEFAULTS = {
    "enabled": False,
    "storage": {"db_path": "tape/v3/neuraltape.db"},
    "cost": {
        "daily_limit_calls": 0,
        "daily_limit_tokens": 0,
        "fallback_notify_interval_hours": 24,
    },
    "events": {"enabled_sources": ["transcript", "git.commit"]},
    "redaction": {"extra_patterns": [], "allowlist": []},
    "memory": {
        "promote_threshold_working_to_episodic": 0.6,
        "promote_threshold_episodic_to_semantic": 0.8,
        "promote_min_age_hours": 4,
        "promote_min_similar_episodes": 2,
        "promote_min_sessions_for_semantic": 3,
        "working_ttl_hours": 48,
    },
    "persona": {"assistant": "lex", "user": "Guglielmo"},
    "sources": {"disabled": [], "custom": {}},
}


@dataclass
class StorageConfig:
    db_path: Path

@dataclass
class CostConfig:
    daily_limit_calls: int  # 0 = unlimited
    daily_limit_tokens: int  # 0 = unlimited
    fallback_notify_interval_hours: int


def _limit(value) -> int:
    """Parse a daily cap. 0 / None / 'unlimited' means no cap."""
    if value is None or value == "":
        return 0
    if isinstance(value, str) and value.strip().lower() in ("unlimited", "none", "inf", "infinite"):
        return 0
    return max(0, int(value))

@dataclass
class EventsConfig:
    enabled_sources: list[str]

@dataclass
class RedactionConfig:
    extra_patterns: list[tuple[str, str]] = field(default_factory=list)
    allowlist: list[str] = field(default_factory=list)

@dataclass
class MemoryConfig:
    promote_threshold_working_to_episodic: float
    promote_threshold_episodic_to_semantic: float
    promote_min_age_hours: float
    promote_min_similar_episodes: int
    promote_min_sessions_for_semantic: int
    working_ttl_hours: float

@dataclass
class PersonaConfig:
    assistant: str
    user: str

@dataclass
class V3Config:
    enabled: bool
    tape_root: Path               # NeuralTape/ root, used to resolve relative paths
    storage: StorageConfig
    cost: CostConfig
    events: EventsConfig
    redaction: RedactionConfig
    memory: MemoryConfig
    persona: PersonaConfig
    sources: list[SourceManifest] = field(default_factory=list)
    disabled_sources: set[str] = field(default_factory=set)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (override wins)."""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolve_path(p: str | Path, tape_root: Path) -> Path:
    path = Path(p).expanduser()
    if path.is_absolute():
        return path
    return (tape_root / path).resolve()


def load(tape_root: Path, config_path: Path | None = None) -> V3Config:
    """Load v3 config. Never raises on missing/invalid section — uses defaults."""
    tape_root = tape_root.resolve()

    raw: dict = {}
    if config_path is None:
        config_path = tape_root / "config.yaml"
    if config_path.exists():
        try:
            full = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            raw = full.get("v3", {}) or {}
        except (yaml.YAMLError, OSError) as e:
            log.warning("config.yaml unreadable (%s); using v3 defaults", e)

    merged = _deep_merge(DEFAULTS, raw)

    # Feature flag resolution: env overrides config
    env_flag = os.environ.get("NEURALTAPE_V3")
    if env_flag is not None:
        enabled = env_flag.strip().lower() in ("1", "true", "yes", "on")
        log.info("NEURALTAPE_V3=%s → enabled=%s (env override)", env_flag, enabled)
    else:
        enabled = bool(merged["enabled"])

    # extra_patterns: list of [regex, kind] pairs → tuples
    extra = []
    for entry in merged["redaction"].get("extra_patterns", []):
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            extra.append((str(entry[0]), str(entry[1])))
        else:
            log.warning("Ignoring malformed redaction.extra_patterns entry: %r", entry)

    # allowlist: substrings that must never be redacted (checked per match)
    allowlist = [
        str(entry) for entry in merged["redaction"].get("allowlist", [])
        if str(entry).strip()
    ]

    # sources: custom manifests + disabled built-ins (see transcript_sources)
    sources_cfg = merged.get("sources") or {}
    disabled_sources = {
        str(entry) for entry in sources_cfg.get("disabled") or [] if str(entry).strip()
    }
    sources: list[SourceManifest] = []
    custom = sources_cfg.get("custom")
    if isinstance(custom, dict):
        for source_id, data in custom.items():
            if not isinstance(data, dict):
                log.warning("Ignoring source %r: entry is not a mapping", source_id)
                continue
            base = data.get("base")
            if not isinstance(base, str) or not base.strip():
                log.warning("Ignoring source %r: missing base", source_id)
                continue
            manifest = manifest_from_dict(
                str(source_id), data, _resolve_path(base.strip(), tape_root),
            )
            if manifest is None:
                log.warning("Ignoring source %r: invalid manifest", source_id)
                continue
            sources.append(manifest)

    # persona: assistant/owner names used in prompt and archive frontmatter
    persona_cfg = merged.get("persona") or {}
    persona = PersonaConfig(
        assistant=str(persona_cfg.get("assistant") or "lex").strip() or "lex",
        user=str(persona_cfg.get("user") or "Guglielmo").strip() or "Guglielmo",
    )

    return V3Config(
        enabled=enabled,
        tape_root=tape_root,
        storage=StorageConfig(
            db_path=_resolve_path(merged["storage"]["db_path"], tape_root),
        ),
        cost=CostConfig(
            daily_limit_calls=_limit(merged["cost"]["daily_limit_calls"]),
            daily_limit_tokens=_limit(merged["cost"]["daily_limit_tokens"]),
            fallback_notify_interval_hours=int(merged["cost"]["fallback_notify_interval_hours"]),
        ),
        events=EventsConfig(
            enabled_sources=list(merged["events"]["enabled_sources"]),
        ),
        redaction=RedactionConfig(extra_patterns=extra, allowlist=allowlist),
        memory=MemoryConfig(
            promote_threshold_working_to_episodic=float(merged["memory"]["promote_threshold_working_to_episodic"]),
            promote_threshold_episodic_to_semantic=float(merged["memory"]["promote_threshold_episodic_to_semantic"]),
            promote_min_age_hours=float(merged["memory"]["promote_min_age_hours"]),
            promote_min_similar_episodes=int(merged["memory"]["promote_min_similar_episodes"]),
            promote_min_sessions_for_semantic=int(merged["memory"]["promote_min_sessions_for_semantic"]),
            working_ttl_hours=float(merged["memory"]["working_ttl_hours"]),
        ),
        persona=persona,
        sources=sources,
        disabled_sources=disabled_sources,
    )