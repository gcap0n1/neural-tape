# Neural Tape — Installation Guide

## Requirements

- Python 3.10+
- pip
- Windows / Linux / macOS

## Step 1: Install Dependencies

```bash
cd neural-tape
pip install -r requirements.txt
```

## Step 2: Configure Paths

Edit `config.yaml`:

```yaml
paths:
  kimi_logs: "C:\Users\hp\.kimi\logs"      # ← your Kimi Code log path
  neural_tape_root: "."                           # or "D:\EterCervo\neural-tape"
  etervelo_wiki: "D:\EterCervo\Wiki"          # optional
  lex_memory: "D:\EterCervo\_Lex\memory.md"  # optional
```

## Step 3: Test Pre-Load

```bash
python lex/pre_load.py
```

This generates `session-context.md` in the project root.

## Step 4: Test Log Parser (One-shot)

```bash
python lex/log_parser.py --once
```

Processes existing logs and writes insights to `tape/staging/`.

## Step 5: Start Background Watcher

**Windows (watchdog):**
```bash
python lex/log_parser.py --watch
```

**Fallback (polling):**
```bash
python lex/log_parser.py --polling --interval 5.0
```

## Step 6: Review at Session End

```bash
python lex/post_capture.py --review
```

Interactive menu: promote, modify, or discard captured insights.

## Integration with Lex / agents.md

Add to your `agents.md` startup routine (see Section 2.x in the updated agents.md):

```markdown
### 2.x Neural Tape Pre-Load
Before loading wiki context, execute:
```bash
python D:\EterCervo\neural-tape\lex\pre_load.py
```
Read `neural-tape/session-context.md` after `_Lex/memory.md`.
```

## Adding New Assistants

See [ADD_ASSISTANT.md](ADD_ASSISTANT.md).
