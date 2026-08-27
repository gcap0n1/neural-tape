# Neural Tape

**Agent-agnostic layered memory for AI coding agents.**

Neural Tape reads the JSONL transcripts your coding agents already write on
disk (VS Code GitHub Copilot, Codex, Kimi Code, Grok Build, DeepSeek
Reasonix — extensible via config), waits until the session is idle, then
classifies the durable insights with an OpenAI-compatible LLM and persists
them as layered episodes:

- **working** → immediately useful session details (hours)
- **episodic** → important events, fixes, API discoveries (weeks)
- **semantic** → recurring patterns, stable preferences, architectural decisions

Episodes live in SQLite (`tape/v3/neuraltape.db`) with an
Obsidian-friendly markdown mirror under `tape/archive/<category>/`, so your
notes vault can consume them directly. A zero-LLM capture mode is planned.

```
Agent transcripts  ->  5-minute idle detection  ->  LLM classifier  ->  SQLite + markdown archive
```

## Current status

v3 is the active pipeline (live since 2026-07-20). v2.2 is disabled; legacy
versions are kept under `legacy/` in the development fork, not shipped in
the public distribution.

## Quickstart

```bash
pip install neural-tape            # or: git clone + pip install -e .
cp .env.example .env               # LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
cp config.example.yaml config.yaml

neuraltape --selfcheck             # smoke-test every pipeline module
neuraltape --status                # print resolved configuration
neuraltape --once <session-id> --project-root /path/to/project
```

On Linux, run the idle-check loop with the shipped systemd user unit
(`neuraltape/v3/neural-tape-v3.timer`, every 5 minutes). See `docs/INSTALL.md`.

## Supported sources

| Agent | Store | Notes |
|-------|-------|-------|
| VS Code Copilot | `~/.config/Code/User/workspaceStorage/*/GitHub.copilot-chat/transcripts/` | legacy + notebook schemas |
| Codex CLI | `~/.codex/sessions/`, `~/.codex/archived_sessions/` | subagent rollouts skipped |
| Kimi Code | `~/.kimi-code/sessions/*/*/agents/main/wire.jsonl` | subagent wires skipped |
| Grok Build | `~/.grok/sessions/<enc-cwd>/<uuid>/chat_history.jsonl` | subagent sessions skipped |
| DeepSeek Reasonix | `~/.reasonix/projects/<enc-cwd>/sessions/*.jsonl` | `.events`/`.conflicts` sidecars skipped |
| Oh My Pi | `~/.omp/agent/sessions/<enc-cwd>/<session>.jsonl` | session cwd label, tool outputs excluded |

Sources are **data-driven manifests** in `neuraltape/v3/transcript_sources.py`.
Add an agent of your own by writing a `v3.sources.custom.<id>` entry in
`config.yaml` — no code changes needed. Disable built-ins via
`v3.sources.disabled`. Bases are overridable via `NEURALTAPE_<ID>_HOME`
(and `REASONIX_HOME` for Reasonix).

## Persona and redaction

Nothing about *your* identity is hardcoded. `v3.persona` (assistant name,
owner name) drives the classifier prompt and the archive frontmatter;
parsed transcripts use persona-neutral `[USER]` / `[ASSISTANT]` markers.
Incoming transcripts are redacted before any LLM call (Stripe, GitHub PAT,
Google keys, Telegram tokens, AWS, Slack, Meta, generic env/credential
paths — with a per-match allowlist).

## Development

```bash
python tests/v3/run_all.py          # full suite, no pytest required
```

## License

MIT — see [LICENSE](LICENSE).