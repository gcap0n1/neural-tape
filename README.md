# Neural Tape

Automatic memory layer for Lex sessions.

Neural Tape reads the JSONL transcripts written by VS Code GitHub Copilot, Codex, Kimi Code, Grok Build and DeepSeek Reasonix, waits until the session becomes idle, classifies the useful long-term insights with an OpenAI-compatible LLM, then persists them as layered episodes in SQLite (`tape/v3/neuraltape.db`) with a markdown mirror in `tape/archive/`. `_Lex/memory.md` is manual curated memory only (Lex writes it via `tools/lex-capture.py` in EterCervo).

## Version status

- **v3 is the live automatic pipeline since 2026-07-20.** The systemd user timer
	invokes `lex/v3/run-cron-v3.sh` → `lex/v3/run.py run_once`, persists layered
	episodes in SQLite, and regenerates per-project `current-focus.json` and
	`working-set.json` under `tape/v3/projects/`.
- **v2.2 is the rollback path.** The `neural-tape-v22.timer` unit is disabled but
	not masked; `tools/rollback_to_v22.sh` re-enables it. v2.2 wrote `_Lex/memory.md`
	plus the `tape/archive/` tree — kept read-only for history.
- **Activation gate.** v3 reached the switch after: cognition pipeline complete
	(EC2-5, Fase 2), 89/89 tests green, dry-run + systemd run verified on live
	session `bf56290c` (EterCervo). Residual formal gates from `docs/v3-phase1-spec.md`
	§9 (10-session validation, ZEUS confidence, EventBus commits, M2 baseline) are
	tracked as hardening, not blockers.

## What changed in v2.2

Previous versions watched assistant-specific CLI logs from Kimi Code, OpenCode, and ZCode. Guglielmo now works primarily in VS Code Chat, so v2.2 uses the real source of truth:

```text
~/.config/Code/User/workspaceStorage/<hash>/GitHub.copilot-chat/transcripts/<session-id>.jsonl
```

The flow is fully automatic:

```text
Copilot/Codex/Kimi/Grok/Reasonix transcript -> 5-minute user timer -> idle detection -> DeepSeek classifier -> SQLite + tape/archive
```

## Live components (v3, active since 2026-07-20)

```text
lex/pre_load.py             Startup context generator (reads tape/archive + _Lex/memory.md)
lex/v3/run.py               One-shot orchestrator (selfcheck, status, run_once)
lex/v3/run-cron-v3.sh       systemd/cron-safe wrapper invoked by neural-tape-v3.timer
lex/v3/transcript_watcher.py Discovers Copilot, Codex, Kimi Code, Grok Build and Reasonix JSONL stores
lex/v3/transcript_parser.py  Normalizes those schemas without system/tool-output ingestion
lex/v3/classifier.py        LLM classifier: layered insights (working/episodic/semantic) + confidence
lex/v3/storage.py           SQLite persistence (tape/v3/neuraltape.db)
lex/v3/markdown_export.py   Mirrors each episode to tape/archive/<category>/
lex/v3/memory.py            Layered memory (working -> episodic -> semantic)
lex/v3/focus.py             current-focus.json generator per project
lex/v3/workset.py           working-set.json generator per project
lex/v3/resume.py            Resume Project renderer (Fase 2)
lex/v3/handoff.py           Agent Handoff bundle (Fase 2)
lex/v3/events.py            EventBus minimale (transcript + git.commit)
lex/v3/redaction.py         Secret redaction before LLM payload
lex/v3/cost.py              Cost/fallback policy for LLM calls
```

Classifier output is evidence-gated: every insight must include an exact quote
found in the normalized transcript. Missing or fabricated evidence is rejected
before persistence; empty insight sets are a valid result.

The v2.2 modules under `lex/v22/` are retained read-only for rollback
(`tools/rollback_to_v22.sh`); they are not part of the active flow.

## Quick start

Create local `.env` and `config.yaml` files. Do not commit them.

```bash
cd /path/to/NeuralTape
cp .env.example .env
cp config.example.yaml config.yaml
```

Set `paths.neural_tape_root`, `paths.etervelo_wiki`, and `paths.lex_memory` in `config.yaml`.

Preview a session without writing memory (legacy v2.2 pipeline, rollback only):

```bash
ETERCERVO_ROOT=/path/to/EterCervo /usr/bin/python lex/v22/run.py --once <session-id-prefix> --preview -v
```

Run v3 manually for one exact session or unique session prefix:

```bash
NEURALTAPE_V3=1 /usr/bin/python lex/v3/run.py \
	--once <session-id-or-prefix> \
	--project-root /path/to/project
```

The project root is mandatory because VS Code sessions can span multiple workspace
roots. The command classifies at most the newest 30,000 parsed characters by default,
is idempotent per project/session, and refreshes the project context on every run.
Use `--max-transcript-chars` only for explicit experiments.

On Guglielmo's machine the active timer is v3 (v2.2 disabled 2026-07-20):

```bash
systemctl --user status neural-tape-v3.timer
systemctl --user list-timers neural-tape-v3.timer
tail -40 tape/.state/v22-cron.log     # legacy v2.2 log (frozen)
journalctl --user -u neural-tape-v3.service -n 40 --no-pager  # v3 live log
```

## Runtime data

Runtime files are intentionally ignored by Git:

- `.env`
- `config.yaml`
- `session-context.md`
- `tape/.state/`
- `tape/sessions/`
- `tape/staging/`
- `tape/archive/`
- `logs/`

Only `.gitkeep` placeholders are tracked for the tape directories.

## Legacy

The v1.2 log-parser pipeline is archived under `legacy/v1.2/`. The v2.2 LLM
classifier pipeline (`lex/v22/`) is in warm standby — disabled at the systemd
level but retained for rollback via `tools/rollback_to_v22.sh`. The active
workflow since 2026-07-20 is v3.

## License

MIT - see `LICENSE`.
