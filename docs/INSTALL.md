# Neural Tape — Install

## Requirements

- Python 3.11+
- An assistant with local JSONL transcripts (Copilot, Codex, Kimi Code,
  Grok Build, DeepSeek Reasonix, or a custom source via config)
- An OpenAI-compatible LLM endpoint (any provider; zero-LLM capture mode planned)

## Install

```bash
git clone https://github.com/<org>/neural-tape.git
cd neural-tape
python -m venv .venv && .venv/bin/pip install -e .
# or from PyPI:
# pip install neural-tape
```

## Environment

Create local runtime files. Do not commit them.

```bash
cp .env.example .env
cp config.example.yaml config.yaml
```

Set the LLM values in `.env`:

```bash
LLM_API_KEY=your-key-here
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash
```

## Validate

Classify one transcript id (or unique prefix) once, without recurring timers:

```bash
.venv/bin/neuraltape --once <session-id-prefix> --project-root /path/to/project -v
```

Or check module health and config status:

```bash
.venv/bin/neuraltape --selfcheck
.venv/bin/neuraltape --status
```

## Run automatically (Linux, user systemd)

Install and enable the 5-minute timer:

```bash
mkdir -p ~/.config/systemd/user
cp neuraltape/v3/neural-tape-v3.{service,timer} ~/.config/systemd/user/
# adapt paths inside the .service file to your checkout
systemctl --user daemon-reload
systemctl --user enable --now neural-tape-v3.timer
```

Status and logs:

```bash
systemctl --user status neural-tape-v3.timer
journalctl --user -u neural-tape-v3.service
```

Classification only happens when a transcript is idle and has grown since
its last classification; already-classified sessions are skipped
idempotently.

## Configuration

See `config.example.yaml`: `v3.persona` controls the assistant/owner names
in the classifier prompt and archive frontmatter, `v3.sources` adds custom
agent manifests or disables built-in ones, and `paths.etercervo_wiki`
points at your knowledge vault for the pre-load generator.