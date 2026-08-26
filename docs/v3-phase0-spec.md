# NeuralTape v3 — Fase 0 Specification

**Version:** 0.3 (post-review, post Q2/Q4 resolution)
**Status:** Approved — In implementazione
**Author:** Lex per Guglielmo
**Date:** 2026-07-14
**Language:** Italian (content), English (code/IDs)
**Depends on:** `_roadmap-v3-review.md` (v0.2), decisioni Q2=C e Q4=C
**Produces:** foundation layer per Fase 1 (Cognition Core)

---

## 0. Scope

Fase 0 **non** produce cognition. Produce le fondamenta su cui Fase 1 costruisce:
- separation of concerns tra progetti
- sicurezza del payload LLM
- storage persistente per episodi (vita settimane/mesi)
- bus per eventi (minimale, 2 source types)
- policy di costo/fallback
- test dei failure modes

**Out of scope per Fase 0:** layered memory logic, current-focus generator, working-set
generator, git adapter completo, MCP, REST. Tutta Fase 1+.

**Regola:** ogni modulo v3 convive con v2.2. v2.2 resta attivo finché Fase 1 non lo
sostituisce esplicitamente. Feature flag `NEURALTAPE_V3=1` attiva componenti v3.

---

## 1. Decisions locked (da review)

| Q | Decisione |
|---|-----------|
| Q1 (confidence) | **D — Combinazione pesata.** `0.5·git_coherence + 0.3·working-set_overlap + 0.2·llm_judge`. Se nessun commit recente (<24h) → `confidence * 0.85` + `confidence_note: "inferred, no recent commit"`. |
| Q2 (MemPalace) | **C — Ibrido con interface.** NeuralTape implementa working/episodic/semantic nativi. Identity layer = `_Lex/identity.md` + `soul.md` (EterCervo). Backend di consolidamento è un'interfaccia (`ConsolidationBackend`); default SQLite, MemPalace futuro come backend alternativo. |
| Q3 (Storage) | **Confermato SQLite** via stdlib `sqlite3`. Schema v1 in Fase 0, migrazioni in Fase 1+. |
| Q4 (Project ID) | **C — Config esplicita.** Ogni progetto ha `.neuraltape/project.yaml` con `project_id` human-readable. Fallback: hash del path root + warning. |
| Q5 (Trigger focus) | **D — Ibrido.** Idle-trigger (riusa polling v2.2, 10 min) + invalidation immediata su branch switch. Nessuna rigenerazione eager costosa. |
| Q6 (Success metrics) | **M1 + M2 primarie.** M1: tempo a context target <30s. M2: accuracy handoff target ≥90%. M3/M4 come monitoraggio secondario. |


---

## 2. Module layout

```
NeuralTape/
├── lex/
│   ├── v22/                ← esistente, invariato
│   └── v3/                 ← NUOVO (Fase 0)
│       ├── __init__.py
│       ├── project.py      ← project identity (Q4=C)
│       ├── redaction.py    ← secret redaction (D0.1)
│       ├── storage.py      ← SQLite layer (D0.3)
│       ├── events.py       ← EventBus minimale (D0.4)
│       ├── cost.py         ← cost/fallback policy (D0.5)
│       └── config.py       ← v3 config loader
├── docs/
│   ├── v3-phase0-spec.md   ← questo file
│   └── MCP.md              ← (Fase 3)
├── tape/
│   └── v3/                 ← runtime data v3
│       ├── neuraltape.db   ← SQLite (creato a runtime)
│       └── .state/
└── tests/
    └── v3/                 ← test suite Fase 0 (D0.6)
        ├── test_redaction.py
        ├── test_project.py
        ├── test_storage.py
        ├── test_events.py
        └── test_cost.py
```

### Convenzioni di codice (allineate a v2.2)

- Python stdlib + `pyyaml`. **Zero nuove dipendenze.**
- SQLite via `sqlite3` (stdlib).
- `from __future__ import annotations` in cima a ogni modulo.
- `logging.getLogger("neural-tape-v3")`.
- Type hints `Path | None`, `list[dict]`, ecc.
- Docstring con banner: `"""Module — breve descrizione."""`.
- Import via `importlib.util` nel `run.py` orchestratore (stesso trucco di v22 per via del trattino nel path).

---

## 3. Componenti

### D0.1 — Secret Redaction Layer (`redaction.py`)

**Scopo:** nessun secret raggiunge il classifier LLM (DeepSeek, cloud).

**API:**
```python
class Redactor:
    def __init__(self, extra_patterns: list[tuple[str, str]] | None = None): ...
    def redact(self, text: str) -> tuple[str, list[RedactionEvent]]:
        """Returns (redacted_text, events). Each event: {kind, match_start, match_len}."""
```

**Pattern built-in (regex, case-insensitive dove opportuno):**
- AWS access key: `AKIA[0-9A-Z]{16}`
- AWS secret: pattern contextuale dopo `aws_secret_access_key`
- GCP service account JSON: `"type":\s*"service_account"`
- Generic API key assignments: `(api[_-]?key|secret|token|password|passwd|pwd)\s*[=:]\s*["']?[A-Za-z0-9_\-]{16,}["']?`
- JWT: `eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+`
- Bearer tokens: `[Bb]earer\s+[A-Za-z0-9_\-\.]{20,}`
- Private keys PEM: `-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----`
- `.env` style assignments catturati nel corpo del transcript
- GitHub tokens: `gh[pousr]_[A-Za-z0-9]{36}`
- Slack tokens: `xox[baprs]-[A-Za-z0-9-]+`
- Generic password in URL: `://[^/\s]+:[^/@\s]+@`

**Sostituzione:** `[REDACTED:<kind>]` (preserva kind per debug, non il valore).

**Comportamento:**
- Patterns ordinati da più specifico a più generico (evita doppia sostituzione).
- Stats: conta redazioni per kind, logga summary a INFO, dettaglio a DEBUG.
- Test fixtures: frammenti di transcript contenenti ognuno dei pattern sopra.

**Exit criterion:** un transcript contenente `sk-abc123...` e `AKIAIOSF...` esce dal redactor con entrambi i valori sostituiti.

---

### D0.2 — Multi-project Scoping (`project.py`)

**Scopo:** ogni workspace ha `project_id` stabile, isolato, human-readable.

**API:**
```python
@dataclass
class Project:
    project_id: str          # es. "zeus"
    root: Path               # path assoluto canonical
    source: str              # "config" | "fallback-hash"
    config_path: Path | None # path a .neuraltape/project.yaml se esiste

class ProjectResolver:
    def __init__(self, workspace_roots: list[Path]): ...
    def resolve(self, root: Path) -> Project: ...
    def resolve_by_transcript(self, transcript_path: Path) -> Project: ...
```

**Formato `.neuraltape/project.yaml`** (uno per workspace):
```yaml
project_id: zeus              # obbligatorio, [a-z0-9-]+, ≤32 char
display_name: Zeus            # opzionale
kind: django-app              # opzionale, informativo
```

**Algoritmo di resolve:**
1. `realpath(root)` → canonical path (risolve symlink, importa per dischi esterni).
2. Cerca `.neuraltape/project.yaml` nella root.
3. Se esiste e valido → `source="config"`, `project_id` dal file.
4. Se non esiste → `project_id = "auto-" + sha256(canonical_path)[:10]`, `source="fallback-hash"`, logga WARNING.

**Validazione project_id:**
- Regex `^[a-z0-9][a-z0-9-]{0,31}$`.
- No collisioni tra project_id configurati (errore a startup).

**Bootstrapping (una tantum per i 6 workspace):** script `lex/v3/bootstrap_projects.py` che crea `.neuraltape/project.yaml` con ID proposti:
- `etercervo`, `zeus`, `cais-lp`, `tec-andrea`, `s4all-bot`, `neuraltape`.

**Exit criterion:** `ProjectResolver().resolve(Path(".../Zeus")).project_id == "zeus"` da qualsiasi cwd.

---

### D0.3 — Storage Layer (`storage.py`)

**Scopo:** storage persistente per episodi (lifetime settimane/mesi). SQLite via stdlib.

**API:**
```python
class Storage:
    def __init__(self, db_path: Path): ...
    def put_episode(self, ep: Episode) -> str: ...        # returns episode_id
    def get_episode(self, episode_id: str) -> Episode | None: ...
    def query_episodes(self, project_id: str, *, kind: str | None = None,
                       since: float | None = None, limit: int = 100) -> list[Episode]: ...
    def promote_episode(self, episode_id: str, new_layer: str) -> bool: ...
    def stats(self, project_id: str | None = None) -> dict: ...
```

**Schema SQLite (v3.0):**
```sql
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);

CREATE TABLE episodes (
    id              TEXT PRIMARY KEY,        -- uuid4 hex
    project_id      TEXT NOT NULL,
    kind            TEXT NOT NULL,           -- 'working' | 'episodic' | 'semantic'
    source_type     TEXT NOT NULL,           -- 'transcript' | 'git.commit' | 'manual'
    source_ref      TEXT,                    -- es. transcript path o commit sha
    category        TEXT,                    -- 'pattern'|'decision'|'anti-pattern'|...
    title           TEXT NOT NULL,
    body            TEXT,
    confidence      REAL DEFAULT 0.0,
    created_at      REAL NOT NULL,           -- epoch seconds
    updated_at      REAL NOT NULL,
    raw_payload     TEXT                     -- JSON, opzionale (per debug/audit)
);
CREATE INDEX idx_ep_proj_kind ON episodes(project_id, kind);
CREATE INDEX idx_ep_created   ON episodes(created_at);

CREATE TABLE focus_history (
    project_id      TEXT NOT NULL,
    captured_at     REAL NOT NULL,
    goal            TEXT,
    branch          TEXT,
    confidence      REAL,
    raw_payload     TEXT,
    PRIMARY KEY (project_id, captured_at)
);
CREATE INDEX idx_focus_proj ON focus_history(project_id, captured_at DESC);

CREATE TABLE event_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL,
    source_type     TEXT NOT NULL,           -- 'transcript' | 'git.commit'
    source_ref      TEXT,
    captured_at     REAL NOT NULL,
    payload         TEXT NOT NULL            -- JSON
);
CREATE INDEX idx_evt_proj ON event_log(project_id, captured_at DESC);
```

**Politiche:**
- DB path: `tape/v3/neuraltape.db`.
- WAL mode abilitata (`PRAGMA journal_mode=WAL`).
- Migration via `schema_version` table. Fase 0 = versione 1.
- Tutte le scritture in transazione.
- Connection riaperta per operazione (cron短, no pool necessario). Oppure context manager `with Storage(...) as s:`.

**Exit criterion:** un episodio scritto e letto 10 min dopo conserva tutti i campi; `stats()` ritorna conteggi corretti per kind e project.

---

### D0.4 — EventBus minimale (`events.py`)

**Scopo:** primitives per pubblicare eventi da sources diverse. In Fase 0 solo 2 source types.

**API:**
```python
@dataclass
class Event:
    project_id: str
    source_type: str          # 'transcript' | 'git.commit'
    source_ref: str           # es. transcript basename o commit sha
    captured_at: float        # epoch
    payload: dict             # struttura libera per source type

class EventBus:
    def __init__(self, storage: Storage): ...
    def publish(self, event: Event) -> int: ...   # returns event_log id
    def query(self, project_id: str, *, source_type: str | None = None,
              since: float | None = None, limit: int = 100) -> list[Event]: ...
```

**Source types Fase 0:**
- `transcript`: payload `{transcript_path, new_bytes, new_lines, session_id}`.
- `git.commit`: payload `{sha, author, message_short, branch, files_changed_count}`.

**Source types Fase 2 (definiti ora, implementati dopo):**
- `git.branch_switch`, `git.merge`, `test.pass`, `test.fail`, `build`, `docker.restart`, `todo.completed`.

**EventBus scrive in `event_log` table** (D0.3). Non fa promozione a episode: quello è lavoro del classificatore Fase 1.

**Exit criterion:** pubblicando 1 evento `transcript` + 1 `git.commit` per `zeus`, la query ritorna entrambi in ordine temporale corretto.

---

### D0.5 — Cost & Fallback Policy (`cost.py`)

**Scopo:** DeepSeek è a pagamento. Evitare run-away, gestire outages.

**API:**
```python
@dataclass
class CostBudget:
    daily_limit_calls: int
    daily_limit_tokens: int

class CostPolicy:
    def __init__(self, budget: CostBudget, state_dir: Path): ...
    def can_call(self) -> tuple[bool, str]: ...      # (allowed, reason)
    def record_call(self, tokens_used: int) -> None: ...
    def status(self) -> dict: ...                    # {calls_today, tokens_today, resets_at}
```

**Default budget** (da config):
- `daily_limit_calls: 0` (0 = unlimited; positive integers re-enable the cap)
- `daily_limit_tokens: 0`

**Stato persistente:** `tape/v3/.state/cost-state.json` con `{date, calls, tokens}`. Reset a mezzanotte locale.

**Fallback mode (classifier LLM non raggiungibile):**
- v22 oggi fallisce e basta. v3 deve:
  1. Loggare ERROR con dettaglio.
  2. Marcare la session come `deferred` (non classified, retry prossima run).
  3. Notificare una volta ogni 24h (non spam). Stato in `cost-state.json` `last_notification_epoch`.
- Trigger di fallback: HTTP 5xx, timeout, quota exceeded, `can_call() == False`.

**Exit criterion:** con `daily_limit_calls=2` e 3 richieste, la 3ª ritorna `(False, "daily call limit reached")` senza chiamare DeepSeek.

---

### D0.6 — Failure Modes Tests (`tests/v3/`)

**Scopo:** dimostrare che v3 non perde dati silenziosamente.

**Test suite (pytest, ma anche eseguibili senza pytest come script):**

| Test | Cosa verifica |
|------|--------------|
| `test_redaction.py::test_aws_key` | `AKIA...` → `[REDACTED:aws-access-key]` |
| `test_redaction.py::test_jwt` | Token JWT → redatto |
| `test_redaction.py::test_no_false_positive_code` | Codice legit (es. `def api_key_handler`) non viene redatto |
| `test_redaction.py::test_env_block` | Blocco `.env` style redatto |
| `test_project.py::test_config_id` | `.neuraltape/project.yaml` → `project_id` dal file |
| `test_project.py::test_fallback_hash` | No config → `auto-<hash>` + WARNING |
| `test_project.py::test_invalid_id` | `project_id="UPPER CASE"` → errore validazione |
| `test_storage.py::test_roundtrip` | put → get → campi identici |
| `test_storage.py::test_query_by_project` | Episodi di progetti diversi non si contaminano |
| `test_storage.py::test_promote` | `working` → `episodic` cambia kind |
| `test_events.py::test_publish_query` | 2 eventi → query ordinata per tempo |
| `test_events.py::test_unknown_source_type` | source_type non Fase-0 → errore |
| `test_cost.py::test_limit_calls` | Budget 2 → 3ª call rifiutata |
| `test_cost.py::test_reset_midnight` | Cambio data → contatori azzerati |

**Nota su pytest:** v2.2 non usa pytest (usa script diretti). Per coerenza, i test v3 sono **script Python autonomi** eseguibili con `python tests/v3/run_all.py` che ritornano exit code. (Pytest opzionale se già installato, ma non requisito.)

**Exit criterion:** `python tests/v3/run_all.py` ritorna 0 con tutti i test verdi.

---

## 4. Config extension (`config.yaml`)

Aggiunte a `config.yaml` (v2.2 keys restano invariati):

```yaml
v3:
  enabled: false                    # feature flag (true attiva componenti v3)
  storage:
    db_path: "tape/v3/neuraltape.db"
  cost:
    daily_limit_calls: 100
    daily_limit_tokens: 200000
    fallback_notify_interval_hours: 24
  events:
    enabled_sources:
      - transcript
      - git.commit
  redaction:
    extra_patterns: []              # list of [regex, kind]
```

Lettura via `lex/v3/config.py` (estende il loader esistente).

---

## 5. Exit criteria riassuntivi Fase 0

Tutti devono essere verdi prima di passare a Fase 1.

- [ ] Redactor: nessun secret nota (pattern built-in) raggiunge il classifier.
- [ ] ProjectResolver: 6 workspace → 6 project_id stabili e leggibili.
- [ ] Storage: round-trip episodio OK, isolamento per progetto OK.
- [ ] EventBus: 2 source types pubblicabili e queryabili.
- [ ] CostPolicy: budget rispettato, fallback non-spam.
- [ ] Tests: `python tests/v3/run_all.py` exit 0.
- [ ] Coesistenza: v2.2 cron non rotto dalla presenza di v3 (feature flag off di default).

---

## 6. Cosa Fase 0 **non** fa

- Non chiama LLM (quello è Fase 1, classifier v3).
- Non scrive in `_Lex/memory.md` (lo farà il nuovo writer in Fase 1).
- Non tocca v22 cron timer.
- Non espone MCP/REST (Fase 3).
- Non ha UI.

---

## 7. Ordine di implementazione

1. `lex/v3/__init__.py` + `config.py` (bootstrap).
2. `project.py` + `bootstrap_projects.py` (Q4=C).
3. `redaction.py` (D0.1).
4. `storage.py` (D0.3).
5. `events.py` (D0.4).
6. `cost.py` (D0.5).
7. `tests/v3/*` + `run_all.py` (D0.6).
8. Smoke test di coesistenza: `NEURALTAPE_V3=1 python lex/v3/run.py --selfcheck`.

---

**End of Fase 0 spec.**
