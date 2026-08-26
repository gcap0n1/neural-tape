#!/usr/bin/env python3
"""
Neural Tape — Archive Cleanup
Rimuove insight generici e duplicati dall'archivio.

Criteri di rimozione:
1. Titolo generico ("BUG_FOUND — Auto-captured" / "INSIGHT — Auto-captured")
   E body < 100 caratteri (solo match grezzo, nessuna elaborazione)
2. Duplicati: file con body identico (hash SHA-256) — ne viene tenuto solo 1
3. Confidence "low" — rumore, non aggiunge valore

Uso:
    python tools/cleanup_archive.py              # dry-run (mostra cosa farebbe)
    python tools/cleanup_archive.py --apply      # esegue la pulizia
    python tools/cleanup_archive.py --verbose    # output dettagliato
"""

import hashlib
import json
import re
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Set, Tuple

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml")
    sys.exit(1)


# ── Config ──────────────────────────────────────────────────────────────

ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "tape" / "archive"
REPORT_FILE = Path(__file__).resolve().parent.parent / "tape" / "cleanup-report.md"

# Titoli considerati "generici" (senza elaborazione)
GENERIC_TITLES = {
    "BUG_FOUND — Auto-captured",
    "INSIGHT — Auto-captured",
    "WARNING — Auto-captured",
    "EUREKA — Auto-captured",
    "CODE_CHANGE — Auto-captured",
}

# Soglia: body più corto di questo → generico
BODY_GENERIC_THRESHOLD = 100  # caratteri


# ── Helpers ─────────────────────────────────────────────────────────────

def extract_frontmatter(text: str) -> Dict:
    """Estrai YAML frontmatter dal markdown."""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}


def extract_body(text: str) -> str:
    """Estrai il body markdown (dopo il secondo ---)."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return text.strip()


def extract_title(text: str) -> str:
    """Estrai il titolo H1 dal body."""
    m = re.search(r"^# (.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def body_hash(body: str) -> str:
    """SHA-256 del body normalizzato (per dedup)."""
    normalized = re.sub(r"\s+", " ", body.lower().strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def is_generic(title: str, body: str) -> bool:
    """Determina se un insight è generico (senza elaborazione)."""
    if title not in GENERIC_TITLES:
        return False
    # Body deve essere corto e senza struttura
    # (solo una parola, un session ID, o un messaggio di errore grezzo)
    clean_body = re.sub(r"^# .+$", "", body, flags=re.MULTILINE).strip()
    # Rimuovi la sezione Context
    clean_body = re.sub(r"## Context.*", "", clean_body, flags=re.DOTALL).strip()
    return len(clean_body) < BODY_GENERIC_THRESHOLD


# ── Main ────────────────────────────────────────────────────────────────

def scan_archive(archive_dir: Path) -> List[Dict]:
    """Scansiona l'archivio e ritorna info su ogni file."""
    files_info = []
    if not archive_dir.exists():
        return files_info

    for category_dir in sorted(archive_dir.iterdir()):
        if not category_dir.is_dir() or category_dir.name.startswith("."):
            continue
        for fpath in sorted(category_dir.glob("*.md")):
            try:
                text = fpath.read_text(encoding="utf-8")
                fm = extract_frontmatter(text)
                title = extract_title(text)
                body = extract_body(text)
                files_info.append({
                    "path": fpath,
                    "category": category_dir.name,
                    "filename": fpath.name,
                    "title": title,
                    "confidence": str(fm.get("confidence", "")).lower(),
                    "type": fm.get("type", ""),
                    "timestamp": str(fm.get("timestamp", "")),
                    "body": body,
                    "body_hash": body_hash(body),
                    "body_len": len(body),
                })
            except Exception as e:
                print(f"  [ERRORE] {fpath}: {e}")
    return files_info


def find_generic_and_duplicates(files_info: List[Dict], verbose: bool = False) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """Identifica file generici, duplicati, e low-confidence."""
    to_remove_generic = []
    to_remove_low_conf = []
    to_remove_duplicates = []

    # Raggruppa per body_hash per trovare duplicati
    by_hash: Dict[str, List[Dict]] = {}
    for fi in files_info:
        by_hash.setdefault(fi["body_hash"], []).append(fi)

    for fi in files_info:
        reason = None

        # 1. Low confidence
        if fi["confidence"] == "low":
            reason = "low-confidence"

        # 2. Generic (titolo generico + body corto)
        elif is_generic(fi["title"], fi["body"]):
            reason = "generic"

        # 3. Duplicate (primo rimane, gli altri vengono rimossi)
        elif len(by_hash[fi["body_hash"]]) > 1:
            # Tieni solo il primo per hash
            first = by_hash[fi["body_hash"]][0]
            if fi["path"] != first["path"]:
                reason = "duplicate"

        if reason:
            fi["remove_reason"] = reason
            if reason == "low-confidence":
                to_remove_low_conf.append(fi)
            elif reason == "generic":
                to_remove_generic.append(fi)
            elif reason == "duplicate":
                to_remove_duplicates.append(fi)

            if verbose:
                print(f"  [{reason:12s}] {fi['category']}/{fi['filename']}")
                if reason == "generic":
                    print(f"               title: {fi['title']}")
                    print(f"               body:  {fi['body'][:80]}...")
                elif reason == "duplicate":
                    print(f"               hash:  {fi['body_hash']}")

    return to_remove_generic, to_remove_duplicates, to_remove_low_conf


def generate_report(
    total: int,
    generic: List[Dict],
    duplicates: List[Dict],
    low_conf: List[Dict],
    kept: int,
    report_path: Path,
):
    """Genera report markdown della pulizia."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "---",
        f"generated: {now}",
        f"total_before: {total}",
        f"removed_generic: {len(generic)}",
        f"removed_duplicates: {len(duplicates)}",
        f"removed_low_conf: {len(low_conf)}",
        f"total_removed: {len(generic) + len(duplicates) + len(low_conf)}",
        f"kept: {kept}",
        "---",
        "",
        "# Archive Cleanup Report",
        "",
        f"**Data:** {now}",
        f"**Totale iniziale:** {total} file",
        f"**Rimossi (generici):** {len(generic)}",
        f"**Rimossi (duplicati):** {len(duplicates)}",
        f"**Rimossi (low-confidence):** {len(low_conf)}",
        f"**Rimasti:** {kept} file",
        "",
        "## Rimossi per Generici",
        "",
    ]
    for fi in generic:
        lines.append(f"- `{fi['category']}/{fi['filename']}` — body: {fi['body'][:60]}")

    lines.extend(["", "## Rimossi per Duplicato", ""])
    for fi in duplicates:
        lines.append(f"- `{fi['category']}/{fi['filename']}` — hash: {fi['body_hash']}")

    lines.extend(["", "## Rimossi per Low-Confidence", ""])
    for fi in low_conf:
        lines.append(f"- `{fi['category']}/{fi['filename']}` — trigger: {fi.get('type', '?')}")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport salvato: {report_path}")


def main():
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    apply = "--apply" in sys.argv

    print("=" * 60)
    print("NEURAL TAPE — ARCHIVE CLEANUP")
    print("=" * 60)

    if not apply:
        print("\n[DRY-RUN] Nessuna modifica. Usa --apply per eseguire.\n")

    # 1. Scansiona
    files_info = scan_archive(ARCHIVE_DIR)
    total = len(files_info)
    print(f"\nFile totali nell'archivio: {total}")

    if total == 0:
        print("Archivio vuoto, niente da fare.")
        return

    # 2. Identifica rimozioni
    generic, duplicates, low_conf = find_generic_and_duplicates(files_info, verbose=verbose)
    to_remove = generic + duplicates + low_conf
    kept = total - len(to_remove)

    print(f"\nDa rimuovere:")
    print(f"  Generici:      {len(generic)}")
    print(f"  Duplicati:     {len(duplicates)}")
    print(f"  Low-confidence: {len(low_conf)}")
    print(f"  TOTALE:        {len(to_remove)}")
    print(f"\nDa tenere:       {kept}")

    # 3. Esegui rimozione
    if apply and to_remove:
        print(f"\nRimozione {len(to_remove)} file...")
        removed = 0
        for fi in to_remove:
            try:
                fi["path"].unlink()
                removed += 1
                if verbose:
                    print(f"  [RIMOSSO] {fi['category']}/{fi['filename']}")
            except Exception as e:
                print(f"  [ERRORE] {fi['path']}: {e}")
        print(f"\nRimossi: {removed}/{len(to_remove)}")

        # Pulisci directory vuote
        for category_dir in ARCHIVE_DIR.iterdir():
            if category_dir.is_dir() and not any(category_dir.iterdir()):
                category_dir.rmdir()
                if verbose:
                    print(f"  [DIR VUOTA RIMOSSA] {category_dir.name}")

    # 4. Genera report
    generate_report(total, generic, duplicates, low_conf, kept, REPORT_FILE)

    print("\n" + "=" * 60)
    if not apply:
        print("DRY-RUN completato. Riesegui con --apply per pulire.")
    else:
        print(f"PULIZIA COMPLETATA: {total} → {kept} file")
    print("=" * 60)


if __name__ == "__main__":
    main()
