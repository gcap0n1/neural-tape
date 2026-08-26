# Neural Tape v1.2 Legacy

This folder contains the old CLI-log based Neural Tape pipeline.

It watched Kimi Code, OpenCode, and ZCode logs, captured regex-based events into `tape/staging/`, and promoted them with `post_capture.py`.

## Why it was archived

Guglielmo's daily workflow moved to VS Code Chat. The old pipeline no longer had useful input because Kimi/OpenCode/ZCode usage dropped to 0% of the workday.

Neural Tape v2.2 now reads VS Code Copilot transcript JSONL files directly and classifies session insights automatically.

## Contents

```text
lex/log_parser.py      Legacy assistant log watcher
lex/post_capture.py    Legacy staging review/promote command
lex/deja_vu.py         Legacy similarity checker
tests/                 Legacy tests for the old parser pipeline
docs/                  Legacy installation/config/add-assistant docs
```

## Status

Do not run this flow by default. Use it only for historical reference or rollback.
