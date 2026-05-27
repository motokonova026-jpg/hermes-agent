"""SQLite-backed mapping store for the story_doc plugin.

Profile-aware: opens at ``get_hermes_home() / "story_doc" / "mappings.sqlite3"``.
The ``HERMES_HOME`` env var is read at construction time, so each Hermes
profile gets its own isolated database (Momo, Willow, Motoko, etc.).

Single connection, ``check_same_thread=False``, guarded by an ``RLock`` --
adequate for the single-process Hermes gateway where contention is
negligible. We use explicit BEGIN / COMMIT / ROLLBACK around mutations
rather than relying on Python's ``sqlite3`` autocommit-on-statement
behaviour, mirroring the discipline in ``hermes_state.SessionDB``.

PR #1 scope: schema + CRUD only. No tools depend on this yet -- ``hermes
story list`` is the only consumer. PR #2 will add ``oauth.py`` and the
first Docs-backed tool, which will then read/write through this store.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from hermes_constants import get_hermes_home

# Bump when schema changes. PR #1 ships v1.
_SCHEMA_VERSION = 1

# Statements run on every open. CREATE ... IF NOT EXISTS is idempotent;
# safe to apply on a fresh db AND a v1 db.
_SCHEMA_SQL: tuple = (
    """
    CREATE TABLE IF NOT EXISTS story_docs (
        story_key        TEXT PRIMARY KEY,
        doc_id           TEXT NOT NULL,
        title            TEXT,
        created_at       INTEGER NOT NULL,
        updated_at       INTEGER NOT NULL,
        word_count       INTEGER NOT NULL DEFAULT 0,
        last_revision_id TEXT,
        summary          TEXT,
        metadata         TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_story_docs_doc_id ON story_docs(doc_id);",
    """
    CREATE TABLE IF NOT EXISTS story_outline (
        story_key   TEXT NOT NULL,
        section_id  TEXT NOT NULL,
        title       TEXT,
        summary     TEXT,
        word_start  INTEGER,
        word_end    INTEGER,
        PRIMARY KEY (story_key, section_id)
    );
    """,
)


class StoryDocError(Exception):
    """Raised by StoryDocStore for user-actionable errors.

    Reuses the simple-message convention from other Hermes integrations
    (e.g. ``SpotifyError``): callers can ``str(exc)`` and surface the
    text to the user without further wrapping.
    """


class StoryDocStore:
    """SQLite-backed mapping store for story aliases and outline cache.

    Public API surface (PR #1):

    * ``upsert_story(...)`` -- create or update a row.
    * ``get_story(story_key)`` -- fetch one row as dict, or ``None``.
    * ``delete_story(story_key)`` -- remove a story plus its outline rows.
    * ``list_stories()`` -- all rows, alphabetical by ``story_key``.
    * ``bump_word_count(story_key, delta)`` -- atomic increment, clamped at 0.
    * ``set_outline(story_key, sections)`` -- replace outline rows.
    * ``get_outline(story_key)`` -- list of section dicts.
    * ``close()`` -- close the underlying connection (tests use this).

    The store is constructible with an explicit ``db_path`` for tests;
    production code should rely on the default which derives from
    ``HERMES_HOME``.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path: Path = (
            db_path
            if db_path is not None
            else get_hermes_home() / "story_doc" / "mappings.sqlite3"
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # isolation_level=None gives us autocommit; we use explicit BEGIN /
        # COMMIT / ROLLBACK around multi-statement mutations.
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        # WAL gives concurrent readers + a single writer without blocking;
        # negligible cost for our workload but a nicety if anything else
        # ever opens the file in read-only mode.
        try:
            self._conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            # Some filesystems (e.g. certain network mounts) don't support
            # WAL; fall back silently to the default rollback journal.
            pass
        self._init_schema()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        """Close the underlying connection. Safe to call multiple times."""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        """Apply schema and stamp ``user_version``.

        Idempotent: re-opening a v1 database is a no-op (the CREATE TABLE
        statements use IF NOT EXISTS, and PRAGMA user_version stays at 1).
        Future migrations should branch on the existing version.
        """
        with self._lock:
            current = int(
                self._conn.execute("PRAGMA user_version").fetchone()[0]
            )
            if current > _SCHEMA_VERSION:
                raise StoryDocError(
                    f"story_doc database at {self._db_path} reports schema "
                    f"version {current}, but this code only knows up to "
                    f"version {_SCHEMA_VERSION}. Was this db written by a "
                    "newer Hermes? Refusing to open."
                )
            for stmt in _SCHEMA_SQL:
                self._conn.execute(stmt)
            self._conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    # ------------------------------------------------------------------
    # Story CRUD
    # ------------------------------------------------------------------

    def upsert_story(
        self,
        *,
        story_key: str,
        doc_id: str,
        title: Optional[str] = None,
        word_count: Optional[int] = None,
        last_revision_id: Optional[str] = None,
        summary: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        mode: str = "upsert",
    ) -> Dict[str, Any]:
        """Insert or update a story row. Returns the resulting row as dict.

        ``mode='create'``: error if ``story_key`` already exists.
        ``mode='update'``: error if ``story_key`` does not exist.
        ``mode='upsert'``: insert if missing, update otherwise (default).

        ``created_at`` is preserved across updates; ``updated_at`` is
        stamped to the current epoch second on every call. Optional fields
        passed as ``None`` on update are NOT cleared -- they preserve the
        previous value via SQL ``COALESCE``. To clear a field, pass an
        empty string or run a separate update directly.
        """
        if mode not in ("create", "update", "upsert"):
            raise StoryDocError(
                f"unknown upsert mode {mode!r}; expected 'create', "
                "'update', or 'upsert'"
            )
        if not story_key or not isinstance(story_key, str):
            raise StoryDocError("story_key is required and must be a string")
        if not doc_id or not isinstance(doc_id, str):
            raise StoryDocError("doc_id is required and must be a string")

        now = int(time.time())
        meta_json = (
            json.dumps(metadata, ensure_ascii=False)
            if metadata is not None
            else None
        )

        with self._lock:
            existing = self._conn.execute(
                "SELECT story_key, created_at FROM story_docs WHERE story_key = ?",
                (story_key,),
            ).fetchone()

            if mode == "create" and existing:
                raise StoryDocError(
                    f"story_key {story_key!r} already exists; pick a "
                    "different alias, or use 'update' / 'revise' to modify "
                    "the existing story"
                )
            if mode == "update" and not existing:
                raise StoryDocError(
                    f"story_key {story_key!r} does not exist; use 'start' "
                    "to create it first"
                )

            if existing:
                # UPDATE preserves created_at; COALESCE preserves
                # caller-omitted fields.
                self._conn.execute(
                    """
                    UPDATE story_docs
                       SET doc_id           = ?,
                           title            = COALESCE(?, title),
                           word_count       = COALESCE(?, word_count),
                           last_revision_id = COALESCE(?, last_revision_id),
                           summary          = COALESCE(?, summary),
                           metadata         = COALESCE(?, metadata),
                           updated_at       = ?
                     WHERE story_key = ?
                    """,
                    (
                        doc_id,
                        title,
                        word_count,
                        last_revision_id,
                        summary,
                        meta_json,
                        now,
                        story_key,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO story_docs
                        (story_key, doc_id, title, created_at, updated_at,
                         word_count, last_revision_id, summary, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        story_key,
                        doc_id,
                        title,
                        now,
                        now,
                        int(word_count) if word_count is not None else 0,
                        last_revision_id,
                        summary,
                        meta_json,
                    ),
                )

        row = self.get_story(story_key)
        # Should be present right after the write; the only way it isn't is
        # a logic bug, in which case raising is correct.
        if row is None:  # pragma: no cover - defensive
            raise StoryDocError(
                f"upsert_story: row for {story_key!r} disappeared after write"
            )
        return row

    def get_story(self, story_key: str) -> Optional[Dict[str, Any]]:
        """Return one story row as a dict, or ``None`` if no such alias."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM story_docs WHERE story_key = ?",
                (story_key,),
            ).fetchone()
            return _row_to_dict(row) if row else None

    def delete_story(self, story_key: str) -> bool:
        """Delete a story and its outline rows.

        Returns True if the story existed (and was deleted), False if it
        didn't. Outline rows are always cleaned up first so a partial
        failure can't leave orphaned sections.
        """
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "DELETE FROM story_outline WHERE story_key = ?",
                    (story_key,),
                )
                cur = self._conn.execute(
                    "DELETE FROM story_docs WHERE story_key = ?",
                    (story_key,),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return cur.rowcount > 0

    def list_stories(self) -> List[Dict[str, Any]]:
        """Return every row, sorted alphabetically by ``story_key``."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM story_docs ORDER BY story_key ASC"
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def bump_word_count(self, story_key: str, delta: int) -> int:
        """Atomically add ``delta`` to ``word_count``; returns new count.

        Negative ``delta`` is allowed; the resulting count is clamped at 0
        (we never want to record a negative word count). Stamps
        ``updated_at`` as a side effect. Errors if the story doesn't
        exist.
        """
        now = int(time.time())
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                row = self._conn.execute(
                    "SELECT word_count FROM story_docs WHERE story_key = ?",
                    (story_key,),
                ).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    raise StoryDocError(
                        f"cannot bump word_count: story_key "
                        f"{story_key!r} not found"
                    )
                new_count = max(0, int(row["word_count"]) + int(delta))
                self._conn.execute(
                    "UPDATE story_docs SET word_count = ?, updated_at = ? "
                    "WHERE story_key = ?",
                    (new_count, now, story_key),
                )
                self._conn.execute("COMMIT")
                return new_count
            except StoryDocError:
                raise
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # Outline
    # ------------------------------------------------------------------

    def set_outline(
        self,
        story_key: str,
        sections: Iterable[Dict[str, Any]],
    ) -> int:
        """Replace the outline rows for a story; returns count written.

        Each section is a dict with at minimum an ``id`` field (also
        accepted as ``section_id``). Optional fields: ``title``,
        ``summary``, ``word_start``, ``word_end``. Errors if ``story_key``
        does not exist (avoids creating dangling outline rows for a story
        that was never persisted).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM story_docs WHERE story_key = ?",
                (story_key,),
            ).fetchone()
            if row is None:
                raise StoryDocError(
                    f"cannot set outline: story_key {story_key!r} not found"
                )
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "DELETE FROM story_outline WHERE story_key = ?",
                    (story_key,),
                )
                count = 0
                for s in sections:
                    section_id = str(
                        s.get("id") or s.get("section_id") or ""
                    ).strip()
                    if not section_id:
                        raise StoryDocError(
                            "outline section missing 'id' "
                            "(or 'section_id')"
                        )
                    self._conn.execute(
                        """
                        INSERT INTO story_outline
                            (story_key, section_id, title, summary,
                             word_start, word_end)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            story_key,
                            section_id,
                            s.get("title"),
                            s.get("summary"),
                            s.get("word_start"),
                            s.get("word_end"),
                        ),
                    )
                    count += 1
                self._conn.execute("COMMIT")
                return count
            except StoryDocError:
                self._conn.execute("ROLLBACK")
                raise
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_outline(self, story_key: str) -> List[Dict[str, Any]]:
        """Return outline sections for a story, in insertion order.

        Returns ``[]`` (not ``None``) for unknown or empty stories so the
        return type is uniformly a list and callers don't need to None-check.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT section_id, title, summary, word_start, word_end
                  FROM story_outline
                 WHERE story_key = ?
                 ORDER BY rowid ASC
                """,
                (story_key,),
            ).fetchall()
            return [
                {
                    "id": r["section_id"],
                    "title": r["title"],
                    "summary": r["summary"],
                    "word_start": r["word_start"],
                    "word_end": r["word_end"],
                }
                for r in rows
            ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict, decoding ``metadata`` JSON."""
    d = {k: row[k] for k in row.keys()}
    raw_meta = d.get("metadata")
    if raw_meta:
        try:
            d["metadata"] = json.loads(raw_meta)
        except (json.JSONDecodeError, TypeError):
            # Corrupt metadata shouldn't take down the read path.
            d["metadata"] = None
    return d
