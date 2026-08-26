# Neural Tape v2.2 — PRD Definitivo

> ⚠️ **DOCUMENTO STORICO.** v2.2 è dismessa dal 2026-07-20 (`neural-tape-v22.timer` disabled).
> La pipeline attiva è **v3** (`neural-tape-v3.timer`, SQLite `tape/v3/neuraltape.db` + mirror
> markdown `tape/archive/`). Questo PRD resta come riferimento architetturale del design
> transcript-reader; per lo stato corrente vedi `README.md` e `docs/BOUNDARIES.md`.

**Version:** 2.2 (Transcript-Reader)
**Status:** Approved — Implementazione
**Author:** Lex per Guglielmo
**Date:** 2026-07-08
**Language:** Italian (content), English (code/comments)
**Supersedes:** PRD v2.0 (API inesistenti), PRD v2.1 (assunzioni superate)

---

## 1. Executive Summary

Neural Tape v2.2 è il primo design che **funziona davvero** per come Guglielmo lavora oggi.

### Perché v2.0 e v2.1 hanno fallito

| PRD | Assunzione | Realtà |
|-----|-----------|--------|
| v2.0 | `vscode.chat.onDidReceiveMessage` cattura Copilot | ❌ API inesistente (privacy by design) |
| v2.1 | `@neural-tape` participant cattura esplicita | ⚠️ Richiede azione manuale di Guglielmo |

Entrambi cercavano di **catturare la conversazione dall'interno dell'estensione VS Code**. Sbagliato.

### Perché v2.2 funziona

VS Code **scrive già la conversazione completa su disco**, automaticamente:

```
~/.config/Code/User/workspaceStorage/<HASH>/GitHub.copilot-chat/transcripts/<session-id>.jsonl
```

Verificato sulla sessione di oggi (177 righe JSONL):
- 4 `user.message` — i prompt di Guglielmo, parola per parola
- 31 `assistant.message` — risposte complete + `reasoningText` (processo decisionale di Lex)
- 42 `tool.execution_*` — ogni tool call con nome + argomenti

**Non serve catturare nulla. La conversazione è già lì.** Serve leggerla e classificarla.

### Principio fondamentale

> La classificazione delle memorie è **lavoro cognitivo di Lex**, automatico. Guglielmo non tocca nulla.

---

## 2. Architettura

```
┌─────────────────────────────────────────────────────────────┐
│  Guglielmo lavora con Copilot/Lex normalmente               │
│  (ZERO azioni manuali, ZERO @participant)                   │
└──────────────────────┬──────────────────────────────────────┘
                       │ VS Code scrive automaticamente
                       ▼
         transcripts/<session>.jsonl
         (user + assistant + reasoning + tool calls)
                       │
                       │ CRON PYTHON — ogni 5 min (cheap poll)
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  NEURAL TAPE v2.2 (Python cron, ~6 moduli)                  │
│                                                              │
│  1. TranscriptWatcher                                        │
│     ├── auto-detect transcript più recente (multi-workspace) │
│     └── offset persistente (riusa v1.2)                      │
│                                                              │
│  2. SessionDetector                                          │
│     ├── conta nuove righe dall'ultimo offset (gratis)        │
│     └── idle detection: se +0 righe da 10 min → classifica   │
│                                                              │
│  3. TranscriptParser                                         │
│     └── JSONL → testo strutturato leggibile per LLM          │
│                                                              │
│  4. LLMClassifier  ← UNICA chiamata LLM, solo a sessione fredda │
│     ├── prompt di classificazione (6 categorie)              │
│     └── output JSON strutturato                              │
│                                                              │
│  5. MemoryWriter                                             │
│     ├── _Lex/memory.md (append per categoria)                │
│     └── tape/archive/<type>/ (insight estesi)                │
│                                                              │
│  6. Notifier                                                 │
│     └── notify-send: "🧠 3 insight catturati"                │
└──────────────────────────────────────────────────────────────┘
```

### Flusso operativo

```
CRON ogni 5 min (sempre attivo):
  1. Trova transcript più recente modificato negli ultimi 60 min
  2. Conta righe nuove rispetto all'offset salvato
  3. SE 0 righe nuove E ultima attività > 10 min fa:
       → sessione in pausa/terminata
       → LLMClassifier sul delta completo (UNICA chiamata LLM)
       → MemoryWriter scrive insight
       → Notifier: popup desktop
       → reset offset
     ALTRIMENTI:
       → sessione attiva, aspetta prossimo poll (gratis)
```

**Risultato:** classificazione near-real-time, costo LLM minimo (1 chiamata per sessione fredda, non ogni 5 min).

---

## 3. Component Specification

### 3.1 TranscriptWatcher (`lex/watcher.py`)

Auto-detect del transcript attivo (multi-workspace, zero config hardcoded).

```python
import os
import glob
import time
from pathlib import Path

VSCODE_BASE = Path.home() / ".config" / "Code" / "User" / "workspaceStorage"

class TranscriptWatcher:
    """Find the most recently active Copilot transcript across all workspaces."""

    def find_active_transcript(self, max_age_minutes: int = 60) -> Path | None:
        """
        Return the most recently modified transcript file.
        Returns None if no transcript has been touched in max_age_minutes.
        """
        candidates = []
        pattern = str(VSCODE_BASE / "*" / "GitHub.copilot-chat" / "transcripts" / "*.jsonl")
        for path_str in glob.glob(pattern):
            path = Path(path_str)
            mtime = path.stat().st_mtime
            age_min = (time.time() - mtime) / 60
            if age_min < max_age_minutes:
                candidates.append((mtime, path))

        if not candidates:
            return None

        # Most recently modified wins
        candidates.sort(reverse=True)
        return candidates[0][1]

    def get_workspace_label(self, transcript: Path) -> str:
        """Extract a human-readable workspace label from the path."""
        # workspaceStorage/<HASH>/GitHub.copilot-chat/transcripts/<session>.jsonl
        hash_dir = transcript.parent.parent.parent.name
        # Resolve hash to folder name via workspace.json if available
        ws_json = VSCODE_BASE / hash_dir / "workspace.json"
        if ws_json.exists():
            try:
                import json
                data = json.loads(ws_json.read_text())
                folder = data.get("folder", "")
                if folder:
                    return Path(folder).name
            except Exception:
                pass
        return hash_dir[:8]
```

### 3.2 SessionDetector (`lex/session_detector.py`)

Decide QUANDO classificare (idle detection, zero sprecare token).

```python
import json
import time
from pathlib import Path

class SessionDetector:
    """Detect when a session has gone cold and is ready for classification."""

    def __init__(self, state_file: Path):
        self.state_file = state_file

    def load_state(self) -> dict:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        return {}

    def save_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=2))

    def should_classify(self, transcript: Path, idle_threshold_min: int = 10) -> tuple[bool, int, int]:
        """
        Returns (should_classify, new_lines_count, offset).
        Classifies when: new lines exist AND session idle > threshold.
        """
        state = self.load_state()
        key = str(transcript)
        last_offset = state.get(key, {}).get("offset", 0)
        last_mtime = state.get(key, {}).get("mtime", 0)

        current_size = transcript.stat().st_size
        current_mtime = transcript.stat().st_mtime

        # Count new lines since offset (byte-based, robust to rotation)
        if current_size < last_offset:
            # Log rotation or truncation — reset
            last_offset = 0

        if current_size == last_offset:
            # No new content
            idle_min = (time.time() - current_mtime) / 60
            return (idle_min >= idle_threshold_min and last_offset > 0, 0, last_offset)

        # Count new lines
        new_lines = self._count_new_lines(transcript, last_offset)
        return (False, new_lines, last_offset)

    def _count_new_lines(self, transcript: Path, offset: int) -> int:
        """Count lines after byte offset (cheap, no parse)."""
        count = 0
        with open(transcript, "rb") as f:
            f.seek(offset)
            for _ in f:
                count += 1
        return count

    def mark_classified(self, transcript: Path) -> None:
        """Update offset to current file size after successful classification."""
        state = self.load_state()
        key = str(transcript)
        state[key] = {
            "offset": transcript.stat().st_size,
            "mtime": transcript.stat().st_mtime,
            "classified_at": time.time(),
        }
        self.save_state(state)
```

### 3.3 TranscriptParser (`lex/transcript_parser.py`)

Converte il JSONL in testo strutturato leggibile per l'LLM.

```python
import json
from pathlib import Path

class TranscriptParser:
    """Parse Copilot transcript JSONL into a structured text for LLM classification."""

    def parse_delta(self, transcript: Path, offset: int) -> str:
        """
        Read transcript from byte offset, return structured text:
        [USER] ...
        [LEX reasoning] ...
        [LEX] ...
        [TOOL: read_file] ...
        """
        lines = []
        with open(transcript, "rb") as f:
            f.seek(offset)
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                parsed = self._format_event(event)
                if parsed:
                    lines.append(parsed)
        return "\n".join(lines)

    def _format_event(self, event: dict) -> str | None:
        etype = event.get("type")
        data = event.get("data", {})
        ts = event.get("timestamp", "")[:19]

        if etype == "user.message":
            content = data.get("content", "") if isinstance(data, dict) else str(data)
            return f"[{ts}] [USER]\n{content}\n"

        if etype == "assistant.message":
            if not isinstance(data, dict):
                return None
            parts = []
            reasoning = data.get("reasoningText", "")
            content = data.get("content", "")
            if reasoning:
                # Lex's internal reasoning — HIGH value for classification
                parts.append(f"[{ts}] [LEX reasoning]\n{reasoning[:2000]}\n")
            if content:
                parts.append(f"[{ts}] [LEX]\n{content[:3000]}\n")
            return "\n".join(parts) if parts else None

        if etype in ("tool.execution_start", "tool.execution_complete"):
            if not isinstance(data, dict):
                return None
            name = data.get("toolName", "?")
            args = data.get("arguments", {})
            # Compact tool summary (truncate large args)
            args_str = json.dumps(args, ensure_ascii=False)[:200]
            status = "→" if etype.endswith("start") else "✓"
            return f"[{ts}] [TOOL {status} {name}] {args_str}\n"

        return None
```

### 3.4 LLMClassifier (`lex/classifier.py`) — IL CUORE

L'unica chiamata LLM, solo a sessione fredda. Prompt ingegnerizzato per le 6 categorie di `_Lex/memory.md`.

```python
import json
import os
from openai import OpenAI

CLASSIFIER_PROMPT = """Sei Lex, l'agente AI di Guglielmo. Hai appena concluso una sessione di lavoro.
Rileggi la trascrizione sottostante (user message, tuo reasoning, tue risposte, tool call).

Estrai SOLO gli insight degni di essere ricordati a lungo termine. Sii SEVERO:
- Ignora saluti, chiacchiere, routine, operazioni meccaniche.
- Salva solo: decisioni architetturali, pattern ricorrenti, anti-pattern (cose fallite),
  preferenze di Guglielmo, quirk di tool/API/framework, warning critici, eureka moment.

Per ogni insight, restituisci un oggetto JSON con:
- "category": una tra "pattern", "decision", "anti-pattern", "preference", "tool", "warning"
- "description": titolo breve (5-10 parole)
- "context": 1 riga di contesto (perché è successo)
- "implication": cosa significa per future raccomandazioni

Categorie di riferimento (allineate a _Lex/memory.md):
- pattern: flussi di lavoro ricorrenti di Guglielmo
- decision: decisioni architetturali con razionale
- anti-pattern: cose che falliscono o vengono respinte
- preference: preferenze e shift di Guglielmo
- tool: quirk di API/framework/librerie
- warning: errori critici da evitare

Se NON ci sono insight degni, restituisci: {"insights": []}

Rispondi SOLO con JSON valido, niente markdown fences.

Trascrizione:
---
{transcript}
---"""

class LLMClassifier:
    """Classify a transcript delta into structured insights via LLM."""

    def __init__(self):
        api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
        base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
        if not api_key:
            raise RuntimeError("LLM_API_KEY not set (DEEPSEEK_API_KEY or OPENAI_API_KEY)")
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = os.environ.get("LLM_MODEL", "deepseek-chat")

    def classify(self, transcript_text: str) -> list[dict]:
        """Return list of insight dicts, or empty list if nothing noteworthy."""
        # Truncate to ~8K tokens to stay cheap
        if len(transcript_text) > 30000:
            transcript_text = transcript_text[:30000] + "\n[...truncated...]"

        prompt = CLASSIFIER_PROMPT.format(transcript=transcript_text)

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,  # Low temp for consistent classification
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content
        try:
            data = json.loads(content)
            return data.get("insights", [])
        except json.JSONDecodeError:
            # Log and return empty rather than crash
            return []
```

### 3.5 MemoryWriter (`lex/memory_writer.py`)

Scrivi negli stessi formati esistenti: `_Lex/memory.md` + `tape/archive/`.

```python
from datetime import datetime
from pathlib import Path

CATEGORY_SECTIONS = {
    "pattern": "## Pattern",
    "decision": "## Decision",
    "anti-pattern": "## Anti-pattern",
    "preference": "## Preference",
    "tool": "## Tool",
    "warning": "## Warning",
}

class MemoryWriter:
    """Write classified insights to memory.md and tape/archive/."""

    def __init__(self, memory_file: Path, tape_root: Path):
        self.memory_file = memory_file
        self.tape_root = tape_root

    def write(self, insights: list[dict], session_label: str) -> int:
        """Write insights. Returns count written."""
        count = 0
        today = datetime.now().strftime("%Y-%m-%d")

        for insight in insights:
            category = insight.get("category", "neutral")
            if category not in CATEGORY_SECTIONS:
                continue

            # 1. Append to memory.md (compact, 2-4 lines)
            self._append_to_memory(today, category, insight)

            # 2. Append extended version to tape/archive/<category>/
            self._append_to_tape(today, category, insight, session_label)

            # 3. Update Recent Context (top of memory.md)
            self._update_recent_context(today, category, insight)

            count += 1

        return count

    def _append_to_memory(self, date: str, category: str, insight: dict) -> None:
        section = CATEGORY_SECTIONS[category]
        entry = f"\n## [{date}] {category} | {insight['description']}\n"
        entry += f"- Context: {insight['context']}\n"
        entry += f"- Implication: {insight['implication']}\n"

        content = self.memory_file.read_text()
        # Find section and append (create if missing)
        if section in content:
            idx = content.index(section)
            # Find next section or end of file
            next_section_idx = self._find_next_section(content, idx + len(section))
            content = content[:next_section_idx] + entry + content[next_section_idx:]
        else:
            content += f"\n{section}\n{entry}"
        self.memory_file.write_text(content)

    def _append_to_tape(self, date: str, category: str, insight: dict, session: str) -> None:
        archive_dir = self.tape_root / "archive" / category.replace("-", "_")
        archive_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{date}-{session[:8]}-{category.replace('-', '')}.md"
        filepath = archive_dir / filename

        content = f"""---
type: {category}
date: {date}
session: {session}
status: auto-classified
---

# {insight['description']}

## Context
{insight['context']}

## Implication
{insight['implication']}

## Source
Auto-extracted by Neural Tape v2.2 from Copilot transcript.
"""
        filepath.write_text(content)

    def _update_recent_context(self, date: str, category: str, insight: dict) -> None:
        """Add to Recent Context section (top), keep max 10 entries."""
        # Implementation: insert after '## Recent Context' line,
        # trim to last 10 entries.
        # (Compact version for PRD — full impl handles dedup + trimming)
        pass

    def _find_next_section(self, content: str, start: int) -> int:
        """Find the start of the next '## ' section after given index."""
        remaining = content[start:]
        next_idx = remaining.find("\n## ")
        if next_idx == -1:
            return len(content)
        return start + next_idx + 1  # +1 for the newline
```

### 3.6 Notifier (`lex/notifier.py`)

Popup desktop (Linux `notify-send`).

```python
import subprocess
import shutil
import sys

class Notifier:
    """Desktop notification (Linux notify-send, macOS osascript fallback)."""

    def notify(self, title: str, message: str) -> None:
        if shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", "-i", "dialog-information", title, message],
                check=False,
            )
        elif sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
                check=False,
            )
        # Silent fallback: just print
        print(f"[{title}] {message}")
```

### 3.7 Orchestrator (`lex/v22_main.py`) — Entry point cron

```python
#!/usr/bin/env python3
"""Neural Tape v2.2 — Cron entry point. Runs every 5 min."""
import sys
import logging
from pathlib import Path

# Setup paths
TAPE_ROOT = Path(__file__).resolve().parent.parent  # neural-tape/
ETERCERVO = TAPE_ROOT.parent  # EterCervo/
MEMORY_FILE = ETERCERVO / "_Lex" / "memory.md"
STATE_FILE = TAPE_ROOT / "tape" / ".state" / "v22-session-state.json"

sys.path.insert(0, str(TAPE_ROOT / "lex"))
from watcher import TranscriptWatcher
from session_detector import SessionDetector
from transcript_parser import TranscriptParser
from classifier import LLMClassifier
from memory_writer import MemoryWriter
from notifier import Notifier

logging.basicConfig(
    filename=str(TAPE_ROOT / "tape" / ".state" / "v22.log"),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("neural-tape-v22")


def main():
    watcher = TranscriptWatcher()
    detector = SessionDetector(STATE_FILE)
    parser = TranscriptParser()
    notifier = Notifier()

    # 1. Find active transcript (cheap)
    transcript = watcher.find_active_transcript(max_age_minutes=60)
    if not transcript:
        log.info("No active transcript in last 60 min. Idle.")
        return 0

    workspace_label = watcher.get_workspace_label(transcript)
    log.info(f"Active transcript: {transcript.name} (workspace: {workspace_label})")

    # 2. Check if session is cold (cheap — no LLM)
    should_classify, new_lines, offset = detector.should_classify(transcript)
    if not should_classify:
        if new_lines > 0:
            log.info(f"Session active: +{new_lines} new lines. Waiting for idle.")
        else:
            log.info("Session idle but not yet past threshold. Waiting.")
        return 0

    # 3. Parse delta (cheap)
    transcript_text = parser.parse_delta(transcript, offset)
    if not transcript_text.strip():
        log.info("Empty delta. Nothing to classify.")
        detector.mark_classified(transcript)
        return 0

    log.info(f"Session cold. Classifying {len(transcript_text)} chars of transcript.")

    # 4. Classify (THE ONLY EXPENSIVE STEP — 1 LLM call)
    try:
        classifier = LLMClassifier()
        insights = classifier.classify(transcript_text)
    except Exception as e:
        log.error(f"Classifier failed: {e}", exc_info=True)
        notifier.notify("Neural Tape", f"⚠️ Classification failed: {e}")
        return 1

    # 5. Write
    if insights:
        writer = MemoryWriter(MEMORY_FILE, TAPE_ROOT)
        written = writer.write(insights, workspace_label)
        log.info(f"Wrote {written} insights to memory.")
        notifier.notify(
            "Neural Tape 🧠",
            f"{written} insight(s) catturati dalla sessione {workspace_label}."
        )
    else:
        log.info("No noteworthy insights in this session.")
        notifier.notify("Neural Tape", "Sessione analizzata. Nessun insight degno.")

    # 6. Mark classified
    detector.mark_classified(transcript)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

---

## 4. Configurazione

### 4.1 Variabili d'ambiente

```bash
# Obbligatoria — LLM API key per classificazione
DEEPSEEK_API_KEY=sk-...        # preferito (economico)
# oppure
OPENAI_API_KEY=sk-...

# Opzionali
LLM_BASE_URL=https://api.deepseek.com   # default DeepSeek
LLM_MODEL=deepseek-chat                 # default
```

### 4.2 Cron setup (Linux)

```bash
# Edit crontab
crontab -e

# Run Neural Tape v2.2 every 5 minutes
*/5 * * * * cd /run/media/<user>/Back-Up/EterCervo && /usr/bin/python3 neural-tape/lex/v22_main.py >> neural-tape/tape/.state/v22-cron.log 2>&1
```

**Nota:** se l'ambiente Python usa un venv, sostituire `/usr/bin/python3` con il path del venv.

### 4.3 Costi stimati

Per sessione tipica (~30K char transcript = ~8K token):
- Input: ~8K token × $0.27/M (DeepSeek) ≈ **$0.002**
- Output: ~500 token × $1.10/M ≈ **$0.0006**
- **Totale per sessione: ~$0.003**

A 5 sessioni/giorno = **$0.015/giorno** = $0.45/mese. Trascurabile.

---

## 5. Migration Plan (v1.2 → v2.2)

### Cosa sopravvive

| v1.2 | v2.2 | Stato |
|------|------|-------|
| `tape/archive/` struttura | Invariata | ✅ |
| `tape/staging/` | Deprecato (classification diretta) | ⚠️ |
| `session-context.md` | Generato da `pre_load.py` (invariato) | ✅ |
| Offset/dedup persistente | Riadattato per transcript (stesso pattern) | ✅ |
| `config.yaml` | Sostituito da env vars + defaults | 🔄 |

### Cosa muore

| v1.2 | Perché |
|------|--------|
| `log_parser.py` (watchdog su Kimi/OpenCode logs) | 0% utilizzo di quegli assistant |
| `start-sessions.sh` | Niente da avviare |
| `end-sessions.sh` + `post_capture.py --auto-promote` | Classificazione automatica via LLM |
| Pattern regex `config.yaml` | Classificazione semantica via LLM |

### Azioni di migrazione

1. **Archivia v1.2:** `mv neural-tape/lex/ neural-tape/legacy/v1.2/` con README
2. **Crea v2.2:** nuovi moduli in `neural-tape/lex/` (section 3)
3. **Aggiorna agents.md:** rimuovi start-sessions/end-sessions dalla Startup Routine
4. **Setup cron:** crontab entry (section 4.2)
5. **Test:** sessione di prova → verifica insight scritti in memory.md
6. **Cleanup:** elimina staging vuoto, mantieni archive

### agents.md — Modifiche

**Sezione 2.5** diventa:

```markdown
### 2.5 Neural Tape v2.2 — Automatic Transcript Classifier

Neural Tape v2.2 cattura e classifica automaticamente le sessioni Copilot
tramite cron Python. NESSUNA azione manuale richiesta.

- Cron ogni 5 min legge i transcript VS Code (`~/.config/Code/User/workspaceStorage/`)
- Idle detection: classifica solo a sessione fredda
- LLM estrae insight → scrive in `_Lex/memory.md` + `tape/archive/`
- Notifica desktop: "🧠 N insight catturati"

Niente più `start-sessions.sh` / `end-sessions.sh`. Tutto automatico.
```

**Sezione 13 (Mental Checklist)** — rimuovi:
- `Have I run bash _Lex/start-sessions.sh all'inizio della sessione?`
- `Have I run bash _Lex/end-sessions.sh prima di chiudere la sessione?`

---

## 6. Implementazione Roadmap

### Fase 1 — Motore (2-3 giorni)

| Task | Effort | Verifica |
|------|--------|----------|
| 6.1 `watcher.py` — auto-detect transcript | 2h | Trova transcript EterCervo |
| 6.2 `session_detector.py` — offset + idle | 2h | Conta righe nuove correttamente |
| 6.3 `transcript_parser.py` — JSONL → testo | 3h | Output leggibile per LLM |
| 6.4 `classifier.py` — prompt + LLM call | 4h | Output JSON strutturato |
| 6.5 `memory_writer.py` — memory.md + tape | 3h | Formato compatibile |
| 6.6 `notifier.py` — notify-send | 1h | Popup desktop |
| 6.7 `v22_main.py` — orchestrator | 2h | End-to-end run |
| 6.8 Test su sessione reale di oggi | 2h | 3-5 insight in memory.md |
| **Milestone** | | **Cron live, insight automatici** |

### Fase 2 — Polish (1-2 giorni, opzionale)

| Task | Effort |
|------|--------|
| 6.9 Dedup contro archive esistente (no doppioni) | 3h |
| 6.10 Trimming Recent Context (max 10) | 1h |
| 6.11 Log + monitoring (quante sessioni/giorno) | 2h |
| 6.12 Config avanzata (threshold, categorie custom) | 2h |

### Fase 3 — Estensione VS Code (futura, solo se serve)

Solo se dopo 1-2 settimane d'uso senti la mancanza di status bar + popup inline. Il motore resta Python.

---

## 7. Rischi & Mitigazioni

| Risk | Prob | Impact | Mitigation |
|------|------|--------|------------|
| Transcript path cambia in futura VS Code | Low | High | Auto-detect + fallback globs + alert |
| LLM classifica male (falsi positivi) | Medium | Medium | Prompt severo + soglia alta + Guglielmo può editare memory.md |
| Costo LLM cresce | Low | Low | Truncate transcript, idle detection, DeepSeek economico |
| Cron non gira (VPS vs locale) | Medium | Medium | Notify-send + log + test periodico |
| Privacy: reasoning esposto in memory.md | Low | Medium | reasoning usato SOLO per classificazione, non salvato in chiaro |

---

## 8. Success Metrics

Dopo 2 settimane d'uso:
- ✅ Cron gira senza crash 95%+ delle esecuzioni
- ✅ 3-5 insight/giorno classificati correttamente
- ✅ Guglielmo NON deve fare nulla manuale
- ✅ Pre-load legge gli insight nuovi a inizio sessione
- ✅ Costo LLM < $1/mese

---

## 9. Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-07-07 | v2.0 scartato | API VS Code inesistenti |
| 2026-07-08 | v2.1 scartato | Participant richiede azione manuale + 3 bug API |
| 2026-07-08 | Cron Python > Estensione VS Code | Stessa capacità di cattura, sforzo 1/3 |
| 2026-07-08 | Transcript JSONL come fonte dati | Già scritto da VS Code, zero azione utente |
| 2026-07-08 | Idle detection (10 min) > polling cieco | Costo LLM minimo, qualità insight alta |
| 2026-07-08 | DeepSeek come classificatore | Economico, OpenAI-compatible, quality sufficiente |
| 2026-07-08 | Classificazione automatica via LLM | Guglielmo non deve fare lavoro manuale |
