# Neural Tape — Architecture Deep Dive

## Memory Model (Human-Inspired)

| Neural Tape Layer | Human Memory | Duration | Capacity | Content |
|-------------------|-------------|----------|----------|---------|
| **Log Parser** | Sensory | Real-time | Unlimited | Raw log lines |
| **Staging** | Working | Session | ~10 items | Unverified insights |
| **Archive** | Short-term | Days-weeks | ~100 items | Verified patterns |
| **EterCervo Wiki** | Long-term | Permanent | Unlimited | Curated knowledge |

## Data Flow

```
Session Start
    │
    ▼
+-------------+     +------------------+
│ pre_load.py │────▶│ session-context.md │────▶ Lex startup
+-------------+     +------------------+
    │
Guglielmo works with Kimi Code
    │
    ▼ (logs written to .kimi/logs/)
+-------------+     +------------------+
│ log_parser  │────▶│ tape/staging/    │
│ (watchdog)  │     +------------------+
+-------------+            │
                           ▼
                     +-------------+
                     │ deja_vu.py  │────▶ similarity alerts
                     +-------------+
                           │
Session End                ▼
                     +------------------+
                     │ post_capture.py   │
                     │ (interactive)     │────▶ [promote] → archive/
                     +------------------+        [discard] → deleted
```

## Insight Lifecycle

1. **Capture** — Log parser detects pattern → writes to `tape/staging/`
2. **Detection** — Deja Vu checks similarity against `tape/archive/`
3. **Injection** — Pre-load selects top insights → `session-context.md`
4. **Review** — Post-capture interactive review → promote or discard
5. **Archive** — Promoted insights move to `tape/archive/{category}/`

## File Format

All insights use **YAML frontmatter + Markdown body**:

```yaml
---
type: bug_found
session_id: fec8e159-b87b-4b56-916c-a0fc00eaeefd
project: EterCervo
timestamp: 2026-06-07T08:24:20
confidence: high
trigger: shell_error
source: log-parser
status: staging
related: []
---
```

This format is:
- Human-readable
- Git-friendly
- Tool-agnostic
- Easy to parse

## Similarity Algorithm (Deja Vu)

1. **Normalize** — Remove session IDs, timestamps, call IDs, paths
2. **Keyword Jaccard** — 60% weight
3. **SequenceMatcher** — 40% weight
4. **Thresholds:**
   - >90% = identical
   - >75% = similar (alert)
   - >50% = related
   - <50% = no alert

## Why File-Based?

- **Portability** — No database to install
- **Git-friendly** — Archive can be versioned
- **Human-readable** — Inspect with any text editor
- **Fast enough** — Keyword matching scales to ~1000 insights
- **Future-proof** — Can swap to embeddings later without changing the API
