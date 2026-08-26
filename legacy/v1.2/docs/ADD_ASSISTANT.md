# Neural Tape — Adding a New AI Assistant

Neural Tape is assistant-agnostic. To add QwenCode, OpenCode, Claude Code, or any other assistant:

## Step 1: Find the Log Directory

Determine where the assistant writes its logs:

| Assistant | Typical Log Path |
|-----------|-----------------|
| Kimi Code | `~/.kimi/logs/` |
| QwenCode | `~/.qwencode/logs/` |
| OpenCode | `~/.opencode/logs/` |
| Claude Code | `~/.claude/logs/` |

## Step 2: Add to config.yaml

```yaml
assistants:
  kimi:
    enabled: true
    log_format: "kimi_cli"
    log_pattern: "*.log"
    watch_mode: watchdog
    poll_interval: 2.0

  qwencode:  # ← NEW
    enabled: true
    log_format: "qwencode_cli"
    log_pattern: "*.log"
    watch_mode: polling
    poll_interval: 5.0
    patterns:
      session_new:
        regex: 'Session started: ([a-f0-9-]+)'
        category: meta
        confidence: low
      shell_error:
        regex: 'ERROR.*Command failed: (.*)'
        category: bug_found
        confidence: high
```

## Step 3: Define Patterns

Each assistant has its own log format. Define regex patterns for:

- `bug_found` — errors, shell failures, tool failures
- `warning` — context compaction, memory pressure
- `eureka` — large outputs, long reasoning steps
- `code_change` — file writes, edits
- `meta` — session start, config load

## Step 4: Test

```bash
python lex/log_parser.py --assistant qwencode --once
```

Verify that `tape/staging/` receives new `.md` files.

## Step 5: All Other Scripts Work Automatically

`pre_load.py`, `deja_vu.py`, and `post_capture.py` are assistant-agnostic. They read from `tape/staging/` and `tape/archive/` regardless of the source assistant.
