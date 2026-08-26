# Neural Tape <-> Lex Memory Boundary Contract

> Version: 3.0
> Date: 2026-07-21
> Status: Active

## The Contract

| Dimension | Neural Tape v3 | `_Lex/memory.md` |
|---|---|---|
| Source | Copilot / Codex / Kimi Code / Grok Build / Reasonix JSONL | Curated operational memory |
| Author | Automated v3 pipeline (classifier + layered memory) | Lex, manually via `tools/lex-capture.py` |
| Input | `user.message`, `assistant.message`, `reasoningText`, tool events, git signals | High-value patterns, decisions, warnings, preferences |
| Curation | LLM classifier, layered episodes (working/episodic/semantic) + confidence | `Recent Context` + category sections |
| Storage | SQLite `tape/v3/neuraltape.db` + markdown mirror `tape/archive/<category>/` | `_Lex/memory.md` |
| Trigger | `neural-tape-v3.timer` (systemd user, every 5 minutes) | Startup read + manual writes by Lex |

## Rules

### 1. Guglielmo does not classify manually

Memory selection is Lex work. Neural Tape reads the session transcript and decides what is worth keeping.

### 2. Neural Tape does NOT write `_Lex/memory.md`

Since v3 is the active pipeline (2026-07-20), `_Lex/memory.md` is **manual curated memory only**: Lex appends entries via `tools/lex-capture.py` (or Edit/Write for long entries). The automatic source of truth is SQLite (`tape/v3/neuraltape.db`) plus the markdown mirror in `tape/archive/<category>/`, exported per episode by `lex/v3/markdown_export.py`.

### 3. Runtime data stays local

Do not commit `.env`, `session-context.md`, `tape/.state/`, `tape/v3/`, transcript-derived session files, or archive content. Only placeholders are tracked.

### 4. `pre_load.py` remains the startup bridge

Lex still runs `neural-tape/lex/pre_load.py` at startup and reads `session-context.md` to rebuild short-term context. `pre_load.py` reads the automatic insights from `tape/archive/` (markdown mirror of the v3 DB) and merges them with the curated `_Lex/memory.md`.

### 5. Verification is via the v3 systemd journal

The source of truth for pipeline health is the v3 service journal, not the old v2.2 cron log:

```bash
systemctl --user status neural-tape-v3.timer
journalctl --user -u neural-tape-v3.service -n 40 --no-pager
```

`tape/.state/v22-cron.log` is frozen legacy history.

### 6. v2.2 is disabled, v1.2 is legacy

`neural-tape-v22.timer` is disabled since 2026-07-20 (rollback path retained via `tools/rollback_to_v22.sh`). `log_parser.py`, `post_capture.py`, and `deja_vu.py` live under `legacy/v1.2/` and are not part of the workflow.

## Operational Flow

```text
Lex session (Copilot / Codex / Kimi Code / Grok Build / Reasonix)
    -> transcript JSONL on disk
    -> neural-tape-v3.timer every 5 minutes (run-cron-v3.sh)
    -> idle/growth detection (skip already-classified, unchanged transcripts)
    -> v3 classifier extracts layered insights (working/episodic/semantic + confidence)
    -> Storage: SQLite tape/v3/neuraltape.db
    -> markdown_export.py mirrors each episode to tape/archive/<category>/
    -> resume.py + handoff.py regenerate project continuity artifacts (Fase 2)
    -> pre_load.py reads archive + memory.md -> session-context.md at next startup
```

## Changelog

- 2026-08-26: Source registry data-driven (`transcript_sources.py`), persona config + `[USER]`/`[ASSISTANT]` markers, no hardcoded `/run/media` paths, Reasonix adapter (OpenAI-chat events, `.events`/`.conflicts` sidecars skipped).
- 2026-08-14: DeepSeek daily cap is 0 (unlimited). Watcher adds Grok Build `chat_history.jsonl` and skips Codex/Grok subagent transcripts.
- 2026-07-21 v3.0: v3 is the active pipeline. `_Lex/memory.md` becomes manual curated memory only; automatic insights live in SQLite + `tape/archive/` mirror. Verification moves to `journalctl --user -u neural-tape-v3.service`.
- 2026-07-08 v2.2: Switched from CLI-log staging/review to automatic VS Code transcript classification.
- 2026-06-07 v1.0: Initial Neural Tape / Lex Memory boundary.
