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
"""

SCHEMA_VERSION = "1"


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )


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
