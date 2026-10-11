"""SQLite connection management and schema migrations.

- Readers (API, MCP) open the DB read-only via a `file:...?mode=ro` URI so a
  bug in a query path can never mutate data.
- The writer (ingest) uses WAL so readers keep working during a sync.
- Migrations are numbered `.sql` files applied in order, tracked with
  `PRAGMA user_version`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path


class DatabaseNotInitialized(RuntimeError):
    pass


def _apply_pragmas(conn: sqlite3.Connection, *, readonly: bool) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        if not path.exists():
            raise DatabaseNotInitialized(f"database not found at {path}; run `s2docs sync` first")
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn, readonly=readonly)
    return conn


def _migrations() -> list[tuple[int, str]]:
    pkg = resources.files("s2docs.db") / "migrations"
    out = []
    for entry in pkg.iterdir():
        if entry.name.endswith(".sql"):
            out.append((int(entry.name.split("_", 1)[0]), entry.read_text(encoding="utf-8")))
    return sorted(out)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in _migrations():
        if version <= current:
            continue
        # executescript commits implicitly; wrap so a failing migration rolls back.
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        current = version
    return current


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


class Database:
    """Factory for short-lived connections. Cheap to create; safe to share."""

    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.path, readonly=True)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def writer(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.path)
        try:
            migrate(conn)
            yield conn
        finally:
            conn.close()
