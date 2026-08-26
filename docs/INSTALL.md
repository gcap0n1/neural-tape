# Neural Tape v2.2 Install

## Requirements

- Python 3.10+
- A VS Code workspace using GitHub Copilot Chat transcripts
- An OpenAI-compatible API key, currently DeepSeek

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

Set these paths in `config.yaml`:

```yaml
paths:
	neural_tape_root: "/path/to/NeuralTape"
	etervelo_wiki: "/path/to/EterCervo"
	lex_memory: "/path/to/EterCervo/_Lex/memory.md"
```

## Validate

```bash
cd /path/to/NeuralTape
ETERCERVO_ROOT=/path/to/EterCervo /usr/bin/python lex/v22/run.py --once <session-id-prefix> --preview -v
```

`--preview` calls the classifier and prints extracted insights without writing memory or advancing the offset.

## Run automatically

Use the wrapper:

```bash
lex/v22/run-cron.sh
```

On Linux with user systemd:

```bash
systemctl --user status neural-tape-v22.timer
tail -40 tape/.state/v22-cron.log
```

The timer should run every 5 minutes. Classification only happens when the transcript is idle.
