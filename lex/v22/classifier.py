"""LLMClassifier — extract insights from a transcript via LLM.

Single LLM call per cold session. Aligned to VS Code's own config:
endpoint z.ai paas/v4 + model deepseek-v4-flash-free (set via env, overridable).

Reads API key from .env (neural-tape/.env) so the cron can authenticate without
touching VS Code's encrypted secret storage.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("neural-tape-v22")

CLASSIFIER_PROMPT = """Sei Lex, l'agente AI senior developer di Guglielmo. Hai appena concluso una sessione di lavoro in VS Code. Rileggi la trascrizione sottostante, che include i messaggi dell'utente, il tuo ragionamento interno ([LEX reasoning]), le tue risposte ([LEX]) e i tool chiamati ([TOOL]).

Il tuo compito: estrarre SOLO gli insight degni di essere ricordati a lungo termine nella memoria operativa di Lex (_Lex/memory.md). Sii SEVERO e selettivo:
- IGNORA saluti, chiacchiere, routine, operazioni meccaniche (lettura file, comandi banali).
- IGNORA fix banali oAlreadyKNOWN.
- SALVA solo ciò che farà risparmiare tempo o evitare errori in FUTURE sessioni.

Categorie (allineate a _Lex/memory.md):
- "pattern": flussi di lavoro ricorrenti di Guglielmo, abitudini, modi di operare
- "decision": decisioni architetturali o strategiche con razionale (perché si è scelto X e non Y)
- "anti-pattern": cose che falliscono, vengono respinte, o si rivelano errori (con lezione)
- "preference": preferenze e shift di Guglielmo (linguaggio, tool, approccio)
- "tool": quirk di API/framework/librerie scoperti (vincoli reali, signature corrette, limiti)
- "warning": errori critici, insidie, situazioni da evitare

Per ogni insight degno, restituisci un oggetto con:
- "category": una delle 6 sopra
- "description": titolo breve 5-12 parole (azione o fatto, non vago)
- "context": 1 riga — quando/perché è emerso
- "implication": 1 riga — cosa cambia per le future raccomandazioni di Lex

REGOLE:
- Massimo 7 insight (meglio pochi e giusti che tanti e superficiali).
- Se la sessione è routine senza apprendimenti reali, restituisci {"insights": []}.
- Non inventare: basati SOLO sul contenuto della trascrizione.
- Rispondi SOLO con JSON valido, nessun markdown, nessun commento.

Formato output:
{"insights": [{"category": "...", "description": "...", "context": "...", "implication": "..."}]}

Trascrizione:
---
{transcript}
---"""


def _load_env_file(env_path: Path) -> None:
    """Load simple KEY=value lines from a .env file into os.environ (no override)."""
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class LLMClassifier:
    """Classify a transcript into structured insights via an OpenAI-compatible LLM."""

    def __init__(self, etorcervo_root: Path, tape_root: Path | None = None):
        # Load .env for API key (cron runs without VS Code's secret storage)
        env_paths = []
        if os.environ.get("NEURAL_TAPE_ENV"):
            env_paths.append(Path(os.environ["NEURAL_TAPE_ENV"]).expanduser())
        if tape_root:
            env_paths.append(tape_root / ".env")
        env_paths.extend([etorcervo_root / "neural-tape" / ".env", etorcervo_root / ".env"])
        for env_path in env_paths:
            _load_env_file(env_path)

        api_key = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError(
                "No LLM_API_KEY found. Create .env from .env.example with:\n"
                "  LLM_API_KEY=your-key-here\n"
                "  LLM_BASE_URL=https://api.deepseek.com\n"
                "  LLM_MODEL=deepseek-v4-flash"
            )

        self.api_key = api_key
        self.base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
        self.model = os.environ.get("LLM_MODEL", "deepseek-v4-flash")

    def classify(self, transcript_text: str) -> list[dict]:
        """Return list of insight dicts. Empty list if nothing noteworthy or on error."""
        chunks = self._split_transcript(transcript_text)
        if len(chunks) > 1:
            log.info("Transcript split into %d chunks; classifying newest first", len(chunks))

        valid = []
        seen_descriptions: set[str] = set()

        for chunk in reversed(chunks):
            prompt = CLASSIFIER_PROMPT.replace("{transcript}", chunk)

            try:
                response = self._chat_completion(prompt)
            except Exception as e:
                log.error("LLM call failed: %s", e, exc_info=True)
                return valid

            content = response.get("choices", [{}])[0].get("message", {}).get("content") or ""
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                log.warning("LLM returned non-JSON. First 300 chars: %s", content[:300])
                continue

            insights = data.get("insights", [])
            if not isinstance(insights, list):
                log.warning("LLM 'insights' is not a list: %r", type(insights))
                continue

            for ins in insights:
                if not isinstance(ins, dict):
                    continue
                if not (ins.get("category") and ins.get("description")):
                    continue
                normalized = self._normalize_description(ins["description"])
                if normalized in seen_descriptions:
                    continue
                seen_descriptions.add(normalized)
                valid.append(ins)
                if len(valid) >= 5:
                    return valid

        return valid

    def _split_transcript(self, transcript_text: str, max_chars: int = 30000) -> list[str]:
        """Split transcript by lines without cutting JSON-derived records mid-line."""
        if len(transcript_text) <= max_chars:
            return [transcript_text]

        chunks: list[str] = []
        current: list[str] = []
        current_size = 0
        for line in transcript_text.splitlines(keepends=True):
            if current and current_size + len(line) > max_chars:
                chunks.append("".join(current))
                current = []
                current_size = 0
            current.append(line)
            current_size += len(line)

        if current:
            chunks.append("".join(current))
        return chunks

    def _normalize_description(self, description: str) -> str:
        return " ".join(description.lower().split())

    def _chat_completion(self, prompt: str) -> dict:
        """Call an OpenAI-compatible chat completions endpoint using stdlib only."""
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            self._chat_completions_url(),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body[:500]}") from e

    def _chat_completions_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"
