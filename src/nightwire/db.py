"""SQLite access and migrations.

One file in /config, WAL mode so the indexer can write while the UI reads.
Migrations are plain .sql files applied in filename order and recorded in
schema_migrations, so a rebuilt container never re-applies one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _applied(conn: sqlite3.Connection) -> set[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  name TEXT PRIMARY KEY,"
        "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    conn.commit()
    return {row["name"] for row in conn.execute("SELECT name FROM schema_migrations")}


def migrate(conn: sqlite3.Connection, migrations_dir: Path | None = None) -> list[str]:
    """Apply any unapplied migrations in order. Returns the ones applied."""
    directory = MIGRATIONS_DIR if migrations_dir is None else migrations_dir
    done = _applied(conn)
    applied: list[str] = []
    for sql_file in sorted(directory.glob("*.sql")):
        if sql_file.name in done:
            continue
        conn.executescript(sql_file.read_text())
        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (sql_file.name,))
        conn.commit()
        applied.append(sql_file.name)
    return applied


def enabled_cameras(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Cameras that should have a running recorder."""
    return list(conn.execute("SELECT * FROM cameras WHERE record = 1 ORDER BY sort_order, id"))


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return default if row is None else row["value"]
