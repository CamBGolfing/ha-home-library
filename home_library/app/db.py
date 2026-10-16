"""SQLite storage. Plain sqlite3, no ORM, so it stays light on a Pi."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("LIBRARY_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "library.db"
COVERS_DIR = DATA_DIR / "covers"

SCHEMA = """
CREATE TABLE IF NOT EXISTS shelves (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    room        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS books (
    id            INTEGER PRIMARY KEY,
    isbn13        TEXT,
    isbn10        TEXT,
    title         TEXT NOT NULL,
    subtitle      TEXT NOT NULL DEFAULT '',
    authors       TEXT NOT NULL DEFAULT '',
    publisher     TEXT NOT NULL DEFAULT '',
    published     TEXT NOT NULL DEFAULT '',
    pages         INTEGER,
    language      TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    subjects      TEXT NOT NULL DEFAULT '',
    series        TEXT NOT NULL DEFAULT '',
    series_index  REAL,
    format        TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'owned',  -- owned | wishlist
    copies        INTEGER NOT NULL DEFAULT 1,
    shelf_id      INTEGER REFERENCES shelves(id) ON DELETE SET NULL,
    notes         TEXT NOT NULL DEFAULT '',
    cover         TEXT NOT NULL DEFAULT '',       -- filename in covers dir
    source        TEXT NOT NULL DEFAULT '',       -- where metadata came from
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_books_isbn13 ON books(isbn13);
CREATE INDEX IF NOT EXISTS idx_books_shelf ON books(shelf_id);

CREATE TABLE IF NOT EXISTS tags (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color  TEXT NOT NULL DEFAULT '#8a6d3b'
);

CREATE TABLE IF NOT EXISTS book_tags (
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (book_id, tag_id)
);

CREATE TABLE IF NOT EXISTS loans (
    id           INTEGER PRIMARY KEY,
    book_id      INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    borrower     TEXT NOT NULL,
    loaned_at    TEXT NOT NULL DEFAULT (date('now')),
    due_at       TEXT,
    returned_at  TEXT,
    notes        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_loans_book ON loans(book_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY,
    username       TEXT UNIQUE COLLATE NOCASE,    -- NULL for Home Assistant-only users
    display_name   TEXT NOT NULL,
    password_hash  TEXT,
    role           TEXT NOT NULL DEFAULT 'viewer', -- owner | editor | viewer
    ha_user_id     TEXT UNIQUE,
    disabled       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen      TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    id           INTEGER PRIMARY KEY,
    token_hash   TEXT NOT NULL UNIQUE,
    role         TEXT NOT NULL DEFAULT 'viewer',
    note         TEXT NOT NULL DEFAULT '',
    for_user_id  INTEGER REFERENCES users(id) ON DELETE CASCADE,  -- set for password resets
    created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT NOT NULL,
    used_at      TEXT
);

CREATE TABLE IF NOT EXISTS borrow_requests (
    id          INTEGER PRIMARY KEY,
    book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | declined | cancelled
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at  TEXT,
    decided_by  INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON borrow_requests(status);
"""

SCHEMA_VERSION = "2"

# Columns added after the first release. Applied with ALTER TABLE when missing.
ADDED_COLUMNS = {
    "books": [("added_by", "INTEGER REFERENCES users(id) ON DELETE SET NULL")],
    "loans": [("user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL")],
    "users": [("theme", "TEXT NOT NULL DEFAULT ''")],   # '' = use the library default
}


def _migrate(conn) -> None:
    for table, cols in ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute("DELETE FROM sessions WHERE expires_at < datetime('now')")


def get_setting(conn, key: str, default: str = "") -> str:
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (f"setting.{key}",)).fetchone()
    return r[0] if r and r[0] is not None else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"setting.{key}", value))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def connect():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def row(cur) -> dict | None:
    r = cur.fetchone()
    return dict(r) if r else None
