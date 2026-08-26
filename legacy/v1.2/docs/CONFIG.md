# Neural Tape — Configuration Reference

## paths

| Key | Description | Example |
|-----|-------------|---------|
| `kimi_logs` | Directory with Kimi Code log files | `C:\Users\hp\.kimi\logs` |
| `neural_tape_root` | Root of Neural Tape project | `.` or `D:\EterCervo\neural-tape` |
| `etervelo_wiki` | Path to EterCervo wiki | `D:\EterCervo\Wiki` |
| `lex_memory` | Path to Lex memory file | `D:\EterCervo\_Lex\memory.md` |

## assistants

Each assistant has its own block:

| Key | Description | Default |
|-----|-------------|---------|
| `enabled` | Enable this assistant | `true` |
| `log_format` | Format identifier | `kimi_cli` |
| `log_pattern` | File glob for logs | `*.log` |
| `watch_mode` | `watchdog` or `polling` | `watchdog` |
| `poll_interval` | Seconds between polls | `2.0` |
| `patterns` | Regex patterns (see below) | — |

## log_parser.patterns

Each pattern has:

| Key | Description |
|-----|-------------|
| `regex` | Python regex with capture groups |
| `category` | `bug_found`, `eureka`, `warning`, `code_change`, `meta` |
| `confidence` | `high`, `medium`, `low` |
| `threshold` | Optional numeric threshold |

## pre_load

| Key | Description | Default |
|-----|-------------|---------|
| `max_insights` | Max insights in session context | `10` |
| `max_patterns` | Max recurring patterns | `5` |
| `lookback_days` | Days to look back in archive | `7` |
| `include_etercervo` | Link to wiki pages | `true` |
| `include_lex_memory` | Include Lex memory tail | `true` |

## deja_vu

| Key | Description | Default |
|-----|-------------|---------|
| `similarity_threshold` | Minimum similarity for alert | `0.75` |
| `normalization` | List of regex replacements | see config.yaml |

## post_capture

| Key | Description | Default |
|-----|-------------|---------|
| `default_action` | `prompt`, `auto_promote`, `auto_skip` | `prompt` |
| `review_ui` | `interactive` or `batch` | `interactive` |
