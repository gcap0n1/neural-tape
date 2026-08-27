"""storage — SQLite persistent layer for episodes (D0.3).

Episodes are the atomic unit of memory: a classified insight, a captured event
promoted to memory, a manual note. They have a ``kind`` (working/episodic/semantic)
that determines lifetime and retrieval priority.

Design choices:
- sqlite3 from stdlib (zero new dependencies, matches v2.2 philosophy).
- WAL journal mode for concurrent reader during cron writes.
- schema_version table with in-place migrations (v1->v2 added dedup_key,
  v2->v3 seeds the FTS5 recall index; v3->v4 adds entities and episode
  lifecycle columns),
- Connection-per-operation via context manager: cron is short-lived, no pool needed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, NamedTuple, Optional

log = logging.getLogger("neural-tape-v3")

SCHEMA_VERSION = 4


def episode_dedup_key(title: str, body: str) -> str:
    """Cross-run dedup fingerprint for an insight.

    Normalized (whitespace-collapsed, casefolded) title + exact body. The same
    insight re-extracted from a different session (or a re-classified transcript
    after rotation) produces the same key and is skipped at persist time.
    """
    norm_title = " ".join(title.casefold().split())
    payload = f"{norm_title}\x00{body or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_ENTITY_STOPWORDS = {
    "the", "this", "that", "when", "why", "how", "what", "and", "for",
    "with", "from", "not", "new", "one", "two", "three", "session",
    "transcript", "insight", "neural", "tape", "user", "assistant",
}
_CAMEL_RE = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")
_ACRONYM_RE = re.compile(r"\b([A-Z][A-Z0-9]{2,})\b")
_TITLECASE_RE = re.compile(
    r"\b([A-Z][a-zà-ú]{2,}(?:\s+[A-Z][a-zà-ú0-9]{2,}){1,3})\b"
)


def heuristic_entities(title: str, body: str = "", cap: int = 5) -> list[str]:
    """Deterministic LLM-free entity seed drawn from an insight's text.

    Catches CamelCase compounds, acronyms and TitleCase multi-word names.
    A span containing any filler word is rejected wholesale; whole-span and
    per-word stopword checks keep the seed clean. Feeds the derived index
    until a real ``entities`` value arrives from the classifier or a manual
    note. Deterministic and unit-testable.
    """
    found: dict[str, str] = {}
    for rx in (_CAMEL_RE, _TITLECASE_RE, _ACRONYM_RE):
        for span in (m.group(1) for m in rx.finditer(f"{title}\n{body}")):
            key = span.casefold()
            if len(key) < 3 or key in _ENTITY_STOPWORDS:
                continue
            if any(w.casefold() in _ENTITY_STOPWORDS for w in span.split()):
                continue
            found.setdefault(key, span)
    return sorted(found.values(), key=lambda s: (-len(s), s))[:cap]

_SCHEMA_SQL = [
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)",

    """CREATE TABLE IF NOT EXISTS episodes (
        id              TEXT PRIMARY KEY,
        project_id      TEXT NOT NULL,
        kind            TEXT NOT NULL,
        source_type     TEXT NOT NULL,
        source_ref      TEXT,
        category        TEXT,
        title           TEXT NOT NULL,
        body            TEXT,
        confidence      REAL DEFAULT 0.0,
        created_at      REAL NOT NULL,
        updated_at      REAL NOT NULL,
        raw_payload     TEXT,
        dedup_key       TEXT,
        entities        TEXT,
        pinned          INTEGER NOT NULL DEFAULT 0,
        access_count    INTEGER NOT NULL DEFAULT 0,
        last_accessed_at REAL,
        expires_at      REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ep_proj_kind ON episodes(project_id, kind)",
    "CREATE INDEX IF NOT EXISTS idx_ep_created ON episodes(created_at)",

    # FTS5 recall index (schema v3). External content: the text lives only in
    # ``episodes``; these triggers keep the index coherent on every write path
    # (cron puts, manual notes, future deletes). Tokenizer folds accents so
    # Italian queries match diacritic-free variants and vice versa.
    """CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
        title, body, category,
        content='episodes', content_rowid='rowid',
        tokenize="unicode61 remove_diacritics 2"
    )""",
    """CREATE TRIGGER IF NOT EXISTS episodes_fts_ai AFTER INSERT ON episodes BEGIN
        INSERT INTO episodes_fts(rowid, title, body, category)
        VALUES (new.rowid, new.title, new.body, new.category);
    END""",
    """CREATE TRIGGER IF NOT EXISTS episodes_fts_ad AFTER DELETE ON episodes BEGIN
        INSERT INTO episodes_fts(episodes_fts, rowid, title, body, category)
        VALUES ('delete', old.rowid, old.title, old.body, old.category);
    END""",
    """CREATE TRIGGER IF NOT EXISTS episodes_fts_au AFTER UPDATE OF title, body, category ON episodes BEGIN
        INSERT INTO episodes_fts(episodes_fts, rowid, title, body, category)
        VALUES ('delete', old.rowid, old.title, old.body, old.category);
        INSERT INTO episodes_fts(rowid, title, body, category)
        VALUES (new.rowid, new.title, new.body, new.category);
    END""",
    # idx_ep_dedup is created in _bootstrap after the schema-version handling,
    # because on a v1 DB the dedup_key column only exists post-migration.

    """CREATE TABLE IF NOT EXISTS focus_history (
        project_id      TEXT NOT NULL,
        captured_at     REAL NOT NULL,
        goal            TEXT,
        branch          TEXT,
        confidence      REAL,
        raw_payload     TEXT,
        PRIMARY KEY (project_id, captured_at)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_focus_proj ON focus_history(project_id, captured_at DESC)",

    """CREATE TABLE IF NOT EXISTS event_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id      TEXT NOT NULL,
        source_type     TEXT NOT NULL,
        source_ref      TEXT,
        captured_at     REAL NOT NULL,
        payload         TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_evt_proj ON event_log(project_id, captured_at DESC)",

    # Derived entity index (schema v4): lowercase canonical form -> episodes.
    # Kept transactional by put_episode; populated for historical rows by the
    # v3->v4 backfill. Enables zero-LLM entity-assisted recall.
    """CREATE TABLE IF NOT EXISTS episode_entities (
        entity_lower   TEXT NOT NULL,
        episode_id     TEXT NOT NULL,
        project_id     TEXT NOT NULL,
        PRIMARY KEY (entity_lower, episode_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ee_ent ON episode_entities(entity_lower)",
]

VALID_KINDS = {"working", "episodic", "semantic"}


@dataclass
class Episode:
    project_id: str
    kind: str               # 'working' | 'episodic' | 'semantic'
    source_type: str        # 'transcript' | 'git.commit' | 'manual' | ...
    title: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    source_ref: str | None = None
    category: str | None = None
    body: str | None = None
    confidence: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    raw_payload: dict | None = None
    dedup_key: str | None = None
    # Canonical entity names from the classifier (heuristic seed historically).
    entities: list[str] = field(default_factory=list)
    # Lifecycle bookkeeping (schema v4): authority, reinforcement, TTL.
    pinned: bool = False
    access_count: int = 0
    last_accessed_at: float | None = None
    expires_at: float | None = None

class Hit(NamedTuple):
    """Recall result: episode plus fused relevance score (HIGHER = better).

    Score is the sum of reciprocal ranks across BM25 and entity streams
    (RRF, k=60); single-stream results inherit that stream's ordering.
    """
    episode: Episode
    rank: float


def rrf_fuse(rank_lists: dict[str, list[str]], *, k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion over named ordered-id streams.

    Two-pass and fully deterministic: final score desc, then earliest stream
    position wins ties, then id. Degenerates to the single stream's order.
    Returns (episode_id, fused_score) pairs in ranked order.
    """
    gains: dict[str, float] = {}
    first_seen: dict[str, tuple[int, int]] = {}
    for si, (_, ids) in enumerate(sorted(rank_lists.items())):
        for pos, eid in enumerate(ids):
            gains[eid] = gains.get(eid, 0.0) + 1.0 / (k + pos + 1)
            key = (si, pos)
            if eid not in first_seen or key < first_seen[eid]:
                first_seen[eid] = key
    return sorted(
        ((eid, gains[eid]) for eid in gains),
        key=lambda kv: (-kv[1], first_seen[kv[0]][0], first_seen[kv[0]][1], kv[0]),
    )

class Storage:
    """SQLite-backed storage for episodes, focus history, and event log."""
    # Shared column list so every read path (get/query/search) stays aligned
    # with schema v4 shape including entities and lifecycle fields.
    _COLS = ("id, project_id, kind, source_type, source_ref, category, "
             "title, body, confidence, created_at, updated_at, raw_payload, "
             "dedup_key, entities, pinned, access_count, last_accessed_at, expires_at")

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._bootstrap()

    # ---- public API -----------------------------------------------------

    def put_episode(self, ep: Episode) -> str:
        """Insert or update (by id); keeps the derived entity index coherent.

        ON CONFLICT deliberately does NOT touch pinned/access_count/
        last_accessed_at/expires_at: refreshing content must never erase the
        lifecycle state accumulated by the user.
        """
        if ep.kind not in VALID_KINDS:
            raise ValueError(f"Invalid episode kind {ep.kind!r}; expected one of {sorted(VALID_KINDS)}")
        ep.updated_at = time.time()
        entities_json = json.dumps(ep.entities, ensure_ascii=False) if ep.entities else None
        with self._conn() as c:
            c.execute(
                """INSERT INTO episodes
                   (id, project_id, kind, source_type, source_ref, category,
                    title, body, confidence, created_at, updated_at, raw_payload,
                    dedup_key, entities, pinned, access_count, last_accessed_at,
                    expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     kind=excluded.kind, category=excluded.category,
                     title=excluded.title, body=excluded.body,
                     confidence=excluded.confidence, updated_at=excluded.updated_at,
                     raw_payload=excluded.raw_payload, entities=excluded.entities""",
                (ep.id, ep.project_id, ep.kind, ep.source_type, ep.source_ref,
                 ep.category, ep.title, ep.body, ep.confidence,
                 ep.created_at, ep.updated_at,
                 json.dumps(ep.raw_payload) if ep.raw_payload is not None else None,
                 ep.dedup_key, entities_json,
                 int(ep.pinned), int(ep.access_count), ep.last_accessed_at,
                 ep.expires_at),
            )
            c.execute("DELETE FROM episode_entities WHERE episode_id = ?", (ep.id,))
            for ent_key in sorted({e.casefold() for e in ep.entities if e.strip()}):
                c.execute(
                    "INSERT OR IGNORE INTO episode_entities(entity_lower, episode_id, project_id)"
                    " VALUES (?,?,?)",
                    (ent_key, ep.id, ep.project_id),
                )
        return ep.id

    def has_episode_by_dedup(self, project_id: str, dedup_key: str) -> bool:
        """Return whether an episode with this dedup fingerprint already exists."""
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM episodes WHERE project_id = ? AND dedup_key = ? LIMIT 1",
                (project_id, dedup_key),
            ).fetchone()
        return row is not None

    def get_episode(self, episode_id: str) -> Episode | None:
        with self._conn() as c:
            row = c.execute(
                f"SELECT {self._COLS} FROM episodes WHERE id = ?", (episode_id,)
            ).fetchone()
        return self._row_to_episode(row) if row else None

    def query_episodes(self, project_id: Optional[str], *,
                       kind: str | None = None,
                       since: float | None = None,
                       limit: int = 100) -> list[Episode]:
        """Episodes for one project, newest first; ``None`` spans all projects."""
        if kind is not None and kind not in VALID_KINDS:
            raise ValueError(f"Invalid kind filter {kind!r}")
        sql = f"SELECT {self._COLS} FROM episodes"
        params: list = []
        if project_id is not None:
            sql += " WHERE project_id = ?"
            params.append(project_id)
        if kind is not None:
            sql += " AND kind = ?" if params else " WHERE kind = ?"
            params.append(kind)
        if since is not None:
            sql += " AND created_at >= ?" if params else " WHERE created_at >= ?"
            params.append(since)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [self._row_to_episode(r) for r in rows]

    def search(self, query: str, *,
               project_id: str | None = None,
               kind: str | None = None,
               min_confidence: float | None = None,
               since: float | None = None,
               limit: int = 20,
               combine: str = "AND") -> list[Hit]:
        """FTS5 recall fused with entity-assisted matching via RRF.

        Stream A: BM25 over the accent-folded index (syntax-safe match).
        Stream B: canonical-entity exact or term-prefix matches. Both honor
        the same SQL filters; fusion degenerates to pure BM25 ordering when
        no entity matches exist. ``combine`` selects how query tokens join:
        "AND" for precise greps (default), "OR" for broad recall sweeps.
        """
        terms = re.findall(r"\w+", query, flags=re.UNICODE)
        if not terms:
            raise ValueError(f"Empty search query: {query!r}")
        if combine not in {"AND", "OR"}:
            raise ValueError(f"Invalid combine operator {combine!r}")
        match_expr = f" {combine} ".join(f'"{t}"' for t in terms)
        if kind is not None and kind not in VALID_KINDS:
            raise ValueError(f"Invalid kind filter {kind!r}")
        base_sel = ("SELECT e.id, e.project_id, e.kind, e.source_type, "
                    "e.source_ref, e.category, e.title, e.body, e.confidence, "
                    "e.created_at, e.updated_at, e.raw_payload, e.dedup_key, "
                    "e.entities, e.pinned, e.access_count, e.last_accessed_at, "
                    "e.expires_at")
        filt_sql = ""
        filt_params: list = []
        if project_id is not None:
            filt_sql += " AND e.project_id = ?"
            filt_params.append(project_id)
        if kind is not None:
            filt_sql += " AND e.kind = ?"
            filt_params.append(kind)
        if min_confidence is not None:
            filt_sql += " AND e.confidence >= ?"
            filt_params.append(float(min_confidence))
        if since is not None:
            filt_sql += " AND e.created_at >= ?"
            filt_params.append(since)
        # TTL sweep at SQL level: expired non-pinned rows never surface.
        filt_sql += " AND (e.expires_at IS NULL OR e.expires_at > ? OR e.pinned = 1)"
        filt_params.append(time.time())

        depth = max(limit * 6, 50)
        lowered = [t.casefold() for t in terms]
        per_term = "(ee.entity_lower = ? OR instr(ee.entity_lower, ?) = 1)"
        cond_b = "(" + " OR ".join([per_term] * len(lowered)) + ")"
        params_b = [p for t in lowered for p in (t, t)]
        with self._conn() as c:
            rows_a = c.execute(
                f"{base_sel}, bm25(episodes_fts) AS rank "
                "FROM episodes_fts JOIN episodes AS e ON e.rowid = episodes_fts.rowid "
                "WHERE episodes_fts MATCH ?" + filt_sql +
                " ORDER BY rank LIMIT ?",
                [match_expr] + filt_params + [depth],
            ).fetchall()
            rows_b = c.execute(
                f"{base_sel} FROM episode_entities ee "
                "JOIN episodes AS e ON e.id = ee.episode_id "
                "WHERE " + cond_b + filt_sql +
                " ORDER BY e.created_at DESC LIMIT ?",
                params_b + filt_params + [depth],
            ).fetchall()
            # Authority stream: pinned episodes whose recorded entities touch
            # any query token. Independent third lane into the RRF fusion.
            cond_p = ("(" +
                      " OR ".join(["instr(lower(e.entities), ?) > 0"] * len(lowered)) +
                      ")")
            rows_pin = c.execute(
                f"{base_sel} FROM episodes AS e "
                "WHERE e.pinned = 1 AND " + cond_p + filt_sql +
                " ORDER BY e.created_at DESC LIMIT ?",
                lowered + filt_params + [depth],
            ).fetchall()

        fused = rrf_fuse({"bm25": [r["id"] for r in rows_a],
                          "entities": [r["id"] for r in rows_b],
                          "authority": [r["id"] for r in rows_pin]})[:limit]
        by_id = {r["id"]: r for r in list(rows_a) + list(rows_b) + list(rows_pin)}
        return [
            Hit(self._row_to_episode(by_id[eid]), round(score, 6))
            for eid, score in fused if eid in by_id
        ]

    def promote_episode(self, episode_id: str, new_kind: str) -> bool:
        """Change an episode's kind (e.g. working → episodic). Returns True if updated."""
        if new_kind not in VALID_KINDS:
            raise ValueError(f"Invalid new kind {new_kind!r}")
        with self._conn() as c:
            cur = c.execute(
                "UPDATE episodes SET kind = ?, updated_at = ? WHERE id = ?",
                (new_kind, time.time(), episode_id),
            )
            return cur.rowcount > 0

    def pin_episode(self, episode_id: str, pinned: bool = True) -> bool:
        """Toggle the authority flag. Pinned episodes get a recall stream of
        their own (fusion lift) and are immune to TTL expiry sweeps."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE episodes SET pinned = ? WHERE id = ?",
                (int(pinned), episode_id),
            )
            return cur.rowcount > 0

    def feedback(self, episode_id: str,
                 verdict: str) -> dict[str, int | float | None]:
        """Reinforcement loop: helpful / not_helpful / stale / wrong.

        helpful      -> access_count +1, last_accessed_at refreshed
        not_helpful  -> access_count -1 floored at 0
        stale|wrong  -> set expires_at to now (TTL kill switch; pinned rows
                        are unpinned first, since authority on a wrong note
                        would keep resurrecting it)
        Never deletes: the audit trail stays intact.
        """
        now = time.time()
        if verdict == "helpful":
            sql = ("UPDATE episodes SET access_count = access_count + 1, "
                   "last_accessed_at = ? WHERE id = ?")
            params: tuple = (now, episode_id)
        elif verdict == "not_helpful":
            sql = ("UPDATE episodes SET access_count = MAX(access_count - 1, 0), "
                   "last_accessed_at = ? WHERE id = ?")
            params = (now, episode_id)
        elif verdict in {"stale", "wrong"}:
            sql = ("UPDATE episodes SET expires_at = ?, pinned = 0 "
                   "WHERE id = ?")
            params = (now, episode_id)
        else:
            raise ValueError(f"Invalid feedback verdict {verdict!r}; expected "
                             "'helpful' | 'not_helpful' | 'stale' | 'wrong'")
        with self._conn() as c:
            cur = c.execute(sql, params)
            if cur.rowcount == 0:
                raise KeyError(f"unknown episode id {episode_id!r}")
            row = c.execute(
                "SELECT access_count, last_accessed_at, expires_at, pinned "
                "FROM episodes WHERE id = ?", (episode_id,)
            ).fetchone()
        return {"access_count": row["access_count"],
                "last_accessed_at": row["last_accessed_at"],
                "expires_at": row["expires_at"],
                "pinned": row["pinned"]}

    def touch_access(self, episode_ids: list[str]) -> int:
        """Reinforce usage counters for recalled episodes (batch, best-effort).
        Returns how many rows were actually updated."""
        if not episode_ids:
            return 0
        now = time.time()
        updated = 0
        with self._conn() as c:
            for eid in set(episode_ids):
                cur = c.execute(
                    "UPDATE episodes SET access_count = access_count + 1, "
                    "last_accessed_at = ? WHERE id = ?",
                    (now, eid),
                )
                updated += cur.rowcount
        return updated

    def stats(self, project_id: str | None = None) -> dict:
        sql = "SELECT kind, COUNT(*) FROM episodes"
        params: list = []
        if project_id is not None:
            sql += " WHERE project_id = ?"
            params.append(project_id)
        sql += " GROUP BY kind"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return {kind: count for kind, count in rows}

    def distinct_project_ids(self) -> list[str]:
        """All project ids that currently have episodes (sorted)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT project_id FROM episodes ORDER BY project_id"
            ).fetchall()
        return [row[0] for row in rows]

    # ---- event_log raw access (used by EventBus in events.py) ----------

    def append_event(self, *, project_id: str, source_type: str,
                     source_ref: str | None, captured_at: float,
                     payload: dict) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO event_log
                   (project_id, source_type, source_ref, captured_at, payload)
                   VALUES (?,?,?,?,?)""",
                (project_id, source_type, source_ref, captured_at,
                 json.dumps(payload, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def query_events(self, project_id: str, *,
                     source_type: str | None = None,
                     source_ref: str | None = None,
                     since: float | None = None,
                     limit: int = 100) -> list[dict]:
        sql = ("SELECT id, project_id, source_type, source_ref, captured_at, payload "
               "FROM event_log WHERE project_id = ?")
        params: list = [project_id]
        if source_type is not None:
            sql += " AND source_type = ?"
            params.append(source_type)
        if source_ref is not None:
            sql += " AND source_ref = ?"
            params.append(source_ref)
        if since is not None:
            sql += " AND captured_at >= ?"
            params.append(since)
        sql += " ORDER BY captured_at DESC LIMIT ?"
        params.append(int(limit))
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [
            {
                "id": r[0], "project_id": r[1], "source_type": r[2],
                "source_ref": r[3], "captured_at": r[4],
                "payload": json.loads(r[5]),
            }
            for r in rows
        ]

    def has_event(self, project_id: str, *, source_type: str,
                  source_ref: str | None) -> bool:
        """Return whether an event marker already exists for this source."""
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM event_log "
                "WHERE project_id = ? AND source_type = ? AND source_ref IS ? "
                "LIMIT 1",
                (project_id, source_type, source_ref),
            ).fetchone()
        return row is not None

    # ---- internals ------------------------------------------------------

    def _bootstrap(self) -> None:
        with self._conn() as c:
            for stmt in _SCHEMA_SQL:
                c.execute(stmt)
            # Ensure schema version is recorded, walking the upgrade ladder.
            row = c.execute("SELECT version FROM schema_version").fetchone()
            current = row[0] if row else None
            columns = {r[1] for r in c.execute("PRAGMA table_info(episodes)")}
            # current is None on a fresh DB *and* on a pre-versioned v1 DB
            # (no schema_version rows). The latter still lacks dedup_key.
            seeding = False
            if current is None:
                if "dedup_key" not in columns:
                    self._migrate_v1_to_v2(c)
                seeding = True
                self._migrate_v3_to_v4(c)   # fresh/legacy unversioned -> v4 shape
            elif current == 1:
                self._migrate_v1_to_v2(c)
                seeding = True
                self._migrate_v3_to_v4(c)   # then hop straight to the v4 shape
            elif current == 2:
                # v2 -> v3 objects exist above; bring columns to the v4 shape.
                seeding = True
                self._migrate_v3_to_v4(c)
            elif current == 3:
                # v3 -> v4: add entity/lifecycle columns, then seed them.
                self._migrate_v3_to_v4(c)
            elif current != SCHEMA_VERSION:
                raise RuntimeError(
                    f"DB schema version mismatch: DB has v{current}, code expects v{SCHEMA_VERSION}."
                )

            if seeding:
                # Safety copy (.bak-v3) of any populated DB, then backfill the
                # fresh FTS index straight from the episodes table.
                self._snapshot_before("v3")
                c.execute("INSERT INTO episodes_fts(episodes_fts) VALUES('rebuild')")

            if row is None:
                c.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            else:
                c.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            if current is not None and current != SCHEMA_VERSION:
                # Non-fresh upgrade to v4 also snapshots (.bak-v4) before
                # seeding the derived entity index for historical rows.
                self._snapshot_before("v4")
                self._backfill_entities(c)

            # Safe on both fresh and migrated DBs: the column always exists here.
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_ep_dedup ON episodes(project_id, dedup_key)"
            )
            self._backfill_dedup_keys(c)

    def _migrate_v3_to_v4(self, conn: sqlite3.Connection) -> None:
        """Add entities/lifecycle columns in place on a schema-v3 DB."""
        columns = {r[1] for r in conn.execute("PRAGMA table_info(episodes)")}
        adds = [
            ("entities", "TEXT"),
            ("pinned", "INTEGER NOT NULL DEFAULT 0"),
            ("access_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_accessed_at", "REAL"),
            ("expires_at", "REAL"),
        ]
        for name, decl in adds:
            if name not in columns:
                conn.execute(f"ALTER TABLE episodes ADD COLUMN {name} {decl}")

    @staticmethod
    def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
        """Add the episodes.dedup_key column (+ index) to a schema-v1 DB."""
        columns = {r[1] for r in conn.execute("PRAGMA table_info(episodes)")}
        if "dedup_key" not in columns:
            conn.execute("ALTER TABLE episodes ADD COLUMN dedup_key TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ep_dedup ON episodes(project_id, dedup_key)"
        )

    def _snapshot_before(self, tag: str) -> None:
        """Consistent VACUUM INTO snapshot (``.bak-<tag>``) of a populated DB.

        Best effort: a failed snapshot logs a warning instead of blocking
        pipeline startup; an existing ``.bak-<tag>`` file is never clobbered.
        """
        dst = self.db_path.with_name(self.db_path.name + f".bak-{tag}")
        try:
            with self._conn() as c:
                if c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0:
                    return  # fresh/dev DB: nothing valuable to protect yet
                if dst.exists():
                    return
                c.execute("VACUUM INTO ?", (str(dst),))
                log.info("pre-%s backup written to %s", tag, dst)
        except sqlite3.Error:
            log.warning("could not create pre-%s backup at %s", tag, dst,
                        exc_info=True)

    @staticmethod
    def _backfill_entities(conn: sqlite3.Connection) -> None:
        """Seed canonical entities heuristically for historical rows (v4)."""
        rows = conn.execute(
            "SELECT id, project_id, title, body FROM episodes WHERE entities IS NULL"
        ).fetchall()
        for row in rows:
            ents = heuristic_entities(row["title"], row["body"])
            conn.execute(
                "UPDATE episodes SET entities = ? WHERE id = ?",
                (json.dumps(ents, ensure_ascii=False) if ents else "[]",
                 row["id"]),
            )
            for key in sorted({e.casefold() for e in ents}):
                conn.execute(
                    "INSERT OR IGNORE INTO episode_entities"
                    "(entity_lower, episode_id, project_id) VALUES (?,?,?)",
                    (key, row["id"], row["project_id"]),
                )
        if rows:
            log.info("heuristic-seeded entities on %d episode(s)", len(rows))

    @staticmethod
    def _backfill_dedup_keys(conn: sqlite3.Connection) -> None:
        """Populate dedup_key on rows created before schema v2 (NULL keys)."""
        rows = conn.execute(
            "SELECT id, title, body FROM episodes "
            "WHERE dedup_key IS NULL OR dedup_key = ''"
        ).fetchall()
        for row in rows:
            key = episode_dedup_key(row["title"] or "", row["body"] or "")
            conn.execute(
                "UPDATE episodes SET dedup_key = ? WHERE id = ?",
                (key, row["id"]),
            )
        if rows:
            log.info("backfilled dedup_key on %d episode(s)", len(rows))

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.db_path, isolation_level=None)  # autocommit
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=NORMAL")
        try:
            yield c
        finally:
            c.close()

    @staticmethod
    def _row_to_episode(row: sqlite3.Row) -> Episode:
        raw = row["raw_payload"]
        raw_entities = row["entities"]
        return Episode(
            id=row["id"],
            project_id=row["project_id"],
            kind=row["kind"],
            source_type=row["source_type"],
            source_ref=row["source_ref"],
            category=row["category"],
            title=row["title"],
            body=row["body"],
            confidence=row["confidence"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            raw_payload=json.loads(raw) if raw else None,
            dedup_key=row["dedup_key"],
            entities=json.loads(raw_entities) if raw_entities else [],
            pinned=bool(row["pinned"]),
            access_count=int(row["access_count"]),
            last_accessed_at=row["last_accessed_at"],
            expires_at=row["expires_at"],
        )
