"""Home Library: a small self-hosted catalog for the books in your house."""
import csv
import io
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, lookup

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
log = logging.getLogger("library")

STATIC_DIR = Path(__file__).parent / "static"
MAX_COVER_BYTES = 8 * 1024 * 1024

@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    log.info("Home Library ready, data in %s", db.DATA_DIR)
    yield


app = FastAPI(title="Home Library", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)


# ---------------------------------------------------------------- models

class BookIn(BaseModel):
    title: str = Field(min_length=1)
    subtitle: str = ""
    authors: str = ""
    publisher: str = ""
    published: str = ""
    pages: int | None = None
    language: str = ""
    description: str = ""
    subjects: str = ""
    series: str = ""
    series_index: float | None = None
    format: str = ""
    status: str = "owned"
    copies: int = 1
    shelf_id: int | None = None
    notes: str = ""
    isbn13: str | None = None
    isbn10: str | None = None
    source: str = ""
    tags: list[str] = []
    cover_urls: list[str] = []


class BookPatch(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    authors: str | None = None
    publisher: str | None = None
    published: str | None = None
    pages: int | None = None
    language: str | None = None
    description: str | None = None
    subjects: str | None = None
    series: str | None = None
    series_index: float | None = None
    format: str | None = None
    status: str | None = None
    copies: int | None = None
    shelf_id: int | None = None
    notes: str | None = None
    isbn13: str | None = None
    isbn10: str | None = None
    tags: list[str] | None = None


class QuickAdd(BaseModel):
    code: str
    shelf_id: int | None = None
    status: str = "owned"
    tags: list[str] = []
    allow_duplicate: bool = False


class ShelfIn(BaseModel):
    name: str = Field(min_length=1)
    room: str = ""


class TagIn(BaseModel):
    name: str = Field(min_length=1)
    color: str = "#8a6d3b"


class LoanIn(BaseModel):
    borrower: str = Field(min_length=1)
    loaned_at: date | None = None
    due_at: date | None = None
    notes: str = ""


BOOK_COLUMNS = ("title", "subtitle", "authors", "publisher", "published", "pages", "language",
                "description", "subjects", "series", "series_index", "format", "status",
                "copies", "shelf_id", "notes", "isbn13", "isbn10", "source")
STATUSES = {"owned", "wishlist"}


# ---------------------------------------------------------------- helpers

def _check_status(s: str | None):
    if s is not None and s not in STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(STATUSES)}")


def _set_tags(conn, book_id: int, names: list[str]):
    conn.execute("DELETE FROM book_tags WHERE book_id = ?", (book_id,))
    for name in {n.strip() for n in names if n and n.strip()}:
        conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO book_tags(book_id, tag_id) VALUES (?, ?)", (book_id, tag_id))


def _cover_url(name: str) -> str:
    return f"covers/{name}" if name else ""


def _decorate(conn, books: list[dict]) -> list[dict]:
    """Attach tags, shelf and active loan to each book in one pass."""
    if not books:
        return books
    ids = [b["id"] for b in books]
    marks = ",".join("?" * len(ids))
    tags: dict[int, list] = {}
    for r in conn.execute(
        f"SELECT bt.book_id, t.id, t.name, t.color FROM book_tags bt JOIN tags t ON t.id = bt.tag_id "
        f"WHERE bt.book_id IN ({marks}) ORDER BY t.name", ids):
        tags.setdefault(r["book_id"], []).append({"id": r["id"], "name": r["name"], "color": r["color"]})
    loans = {r["book_id"]: dict(r) for r in conn.execute(
        f"SELECT * FROM loans WHERE returned_at IS NULL AND book_id IN ({marks})", ids)}
    shelves = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM shelves")}
    for b in books:
        b["tags"] = tags.get(b["id"], [])
        b["active_loan"] = loans.get(b["id"])
        b["shelf"] = shelves.get(b["shelf_id"])
        b["cover_url"] = _cover_url(b["cover"])
    return books


def _get_book(conn, book_id: int) -> dict:
    b = db.row(conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)))
    if not b:
        raise HTTPException(404, "Book not found")
    return _decorate(conn, [b])[0]


def _find_owned(conn, isbn13: str | None) -> list[dict]:
    if not isbn13:
        return []
    found = db.rows(conn.execute("SELECT * FROM books WHERE isbn13 = ?", (isbn13,)))
    return _decorate(conn, found)


def _delete_cover_file(name: str):
    if name:
        try:
            (db.COVERS_DIR / name).unlink(missing_ok=True)
        except OSError:
            pass


async def _create_book(data: BookIn) -> dict:
    _check_status(data.status)
    cover = await lookup.download_cover(data.cover_urls, db.COVERS_DIR) if data.cover_urls else ""
    values = data.model_dump(include=set(BOOK_COLUMNS))
    values["copies"] = max(1, values.get("copies") or 1)
    with db.connect() as conn:
        cols = list(values) + ["cover"]
        cur = conn.execute(
            f"INSERT INTO books ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [values[c] for c in values] + [cover],
        )
        book_id = cur.lastrowid
        _set_tags(conn, book_id, data.tags)
        return _get_book(conn, book_id)


# ---------------------------------------------------------------- lookup

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/lookup/{code}")
async def lookup_code(code: str):
    result = await lookup.lookup_isbn(code)
    with db.connect() as conn:
        result["owned"] = _find_owned(conn, result.get("isbn13"))
    return result


@app.get("/api/search")
async def search_online(q: str = Query(min_length=2)):
    return await lookup.search_title(q)


@app.post("/api/quick-add")
async def quick_add(body: QuickAdd):
    """Scan-and-go: look up a code and add it in one call."""
    result = await lookup.lookup_isbn(body.code)
    with db.connect() as conn:
        owned = _find_owned(conn, result.get("isbn13"))
    if owned and not body.allow_duplicate:
        return {"result": "duplicate", "book": owned[0], "lookup": result}
    if not result.get("found"):
        return {"result": "not_found", "lookup": result}
    book = await _create_book(BookIn(
        **{k: v for k, v in result.items() if k in BookIn.model_fields and v is not None},
        shelf_id=body.shelf_id, status=body.status, tags=body.tags,
    ))
    return {"result": "added", "book": book}


# ---------------------------------------------------------------- books

SORTS = {
    "added": "b.created_at DESC, b.id DESC",
    "title": "b.title COLLATE NOCASE",
    "author": "b.authors COLLATE NOCASE, b.title COLLATE NOCASE",
    "series": "b.series = '', b.series COLLATE NOCASE, b.series_index, b.title COLLATE NOCASE",
    "published": "b.published DESC",
}


@app.get("/api/books")
def list_books(
    q: str = "",
    shelf: int | None = None,
    tag: int | None = None,
    status: str | None = None,
    loaned: bool | None = None,
    sort: str = "added",
    limit: int = Query(500, le=5000),
    offset: int = 0,
):
    where, args = [], []
    if q.strip():
        for term in q.split():
            like = f"%{term}%"
            where.append(
                "(b.title LIKE ? OR b.subtitle LIKE ? OR b.authors LIKE ? OR b.series LIKE ? "
                "OR b.isbn13 LIKE ? OR b.isbn10 LIKE ? OR b.publisher LIKE ? OR b.notes LIKE ? "
                "OR EXISTS (SELECT 1 FROM book_tags bt JOIN tags t ON t.id = bt.tag_id "
                "           WHERE bt.book_id = b.id AND t.name LIKE ?))")
            args += [like] * 9
    if shelf is not None:
        if shelf == 0:
            where.append("b.shelf_id IS NULL")
        else:
            where.append("b.shelf_id = ?")
            args.append(shelf)
    if tag is not None:
        where.append("EXISTS (SELECT 1 FROM book_tags bt WHERE bt.book_id = b.id AND bt.tag_id = ?)")
        args.append(tag)
    if status:
        _check_status(status)
        where.append("b.status = ?")
        args.append(status)
    if loaned is not None:
        clause = "EXISTS (SELECT 1 FROM loans l WHERE l.book_id = b.id AND l.returned_at IS NULL)"
        where.append(clause if loaned else f"NOT {clause}")
    sql = "SELECT b.* FROM books b"
    if where:
        sql += " WHERE " + " AND ".join(where)
    order = SORTS.get(sort, SORTS["added"])
    with db.connect() as conn:
        total = conn.execute(sql.replace("SELECT b.*", "SELECT COUNT(*)", 1), args).fetchone()[0]
        books = db.rows(conn.execute(f"{sql} ORDER BY {order} LIMIT ? OFFSET ?", args + [limit, offset]))
        return {"total": total, "books": _decorate(conn, books)}


@app.post("/api/books", status_code=201)
async def create_book(body: BookIn):
    return await _create_book(body)


@app.get("/api/books/{book_id}")
def get_book(book_id: int):
    with db.connect() as conn:
        b = _get_book(conn, book_id)
        b["loans"] = db.rows(conn.execute(
            "SELECT * FROM loans WHERE book_id = ? ORDER BY loaned_at DESC, id DESC", (book_id,)))
        return b


@app.patch("/api/books/{book_id}")
def update_book(book_id: int, body: BookPatch):
    changes = body.model_dump(exclude_unset=True)
    _check_status(changes.get("status"))
    tags = changes.pop("tags", None)
    if "title" in changes and not (changes["title"] or "").strip():
        raise HTTPException(400, "Title can't be empty")
    if "copies" in changes:
        changes["copies"] = max(1, changes["copies"] or 1)
    with db.connect() as conn:
        _get_book(conn, book_id)
        if changes:
            sets = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE books SET {sets}, updated_at = datetime('now') WHERE id = ?",
                         list(changes.values()) + [book_id])
        if tags is not None:
            _set_tags(conn, book_id, tags)
        return _get_book(conn, book_id)


@app.delete("/api/books/{book_id}", status_code=204)
def delete_book(book_id: int):
    with db.connect() as conn:
        b = _get_book(conn, book_id)
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    _delete_cover_file(b["cover"])


@app.post("/api/books/{book_id}/cover")
async def upload_cover(book_id: int, file: UploadFile = File(...)):
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "Please upload an image")
    data = await file.read(MAX_COVER_BYTES + 1)
    if len(data) > MAX_COVER_BYTES:
        raise HTTPException(413, "Image is too large (8 MB max)")
    ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(file.content_type, ".jpg")
    name = f"{uuid.uuid4().hex}{ext}"
    (db.COVERS_DIR / name).write_bytes(data)
    with db.connect() as conn:
        old = _get_book(conn, book_id)["cover"]
        conn.execute("UPDATE books SET cover = ?, updated_at = datetime('now') WHERE id = ?", (name, book_id))
        book = _get_book(conn, book_id)
    _delete_cover_file(old)
    return book


@app.post("/api/books/{book_id}/cover/refresh")
async def refresh_cover(book_id: int):
    with db.connect() as conn:
        b = _get_book(conn, book_id)
    if not b["isbn13"]:
        raise HTTPException(400, "This book has no ISBN to look a cover up with")
    result = await lookup.lookup_isbn(b["isbn13"])
    name = await lookup.download_cover(result.get("cover_urls") or [], db.COVERS_DIR)
    if not name:
        raise HTTPException(404, "No cover found online")
    with db.connect() as conn:
        conn.execute("UPDATE books SET cover = ?, updated_at = datetime('now') WHERE id = ?", (name, book_id))
        book = _get_book(conn, book_id)
    _delete_cover_file(b["cover"])
    return book


# ---------------------------------------------------------------- shelves

@app.get("/api/shelves")
def list_shelves():
    with db.connect() as conn:
        return db.rows(conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM books b WHERE b.shelf_id = s.id) AS book_count "
            "FROM shelves s ORDER BY s.room COLLATE NOCASE, s.name COLLATE NOCASE"))


@app.post("/api/shelves", status_code=201)
def create_shelf(body: ShelfIn):
    with db.connect() as conn:
        cur = conn.execute("INSERT INTO shelves(name, room) VALUES (?, ?)", (body.name.strip(), body.room.strip()))
        return db.row(conn.execute("SELECT * FROM shelves WHERE id = ?", (cur.lastrowid,)))


@app.patch("/api/shelves/{shelf_id}")
def update_shelf(shelf_id: int, body: ShelfIn):
    with db.connect() as conn:
        cur = conn.execute("UPDATE shelves SET name = ?, room = ? WHERE id = ?",
                           (body.name.strip(), body.room.strip(), shelf_id))
        if not cur.rowcount:
            raise HTTPException(404, "Shelf not found")
        return db.row(conn.execute("SELECT * FROM shelves WHERE id = ?", (shelf_id,)))


@app.delete("/api/shelves/{shelf_id}", status_code=204)
def delete_shelf(shelf_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM shelves WHERE id = ?", (shelf_id,))


# ---------------------------------------------------------------- tags

@app.get("/api/tags")
def list_tags():
    with db.connect() as conn:
        return db.rows(conn.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM book_tags bt WHERE bt.tag_id = t.id) AS book_count "
            "FROM tags t ORDER BY t.name"))


@app.post("/api/tags", status_code=201)
def create_tag(body: TagIn):
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO tags(name, color) VALUES (?, ?)", (body.name.strip(), body.color))
        return db.row(conn.execute("SELECT * FROM tags WHERE name = ?", (body.name.strip(),)))


@app.patch("/api/tags/{tag_id}")
def update_tag(tag_id: int, body: TagIn):
    with db.connect() as conn:
        cur = conn.execute("UPDATE tags SET name = ?, color = ? WHERE id = ?", (body.name.strip(), body.color, tag_id))
        if not cur.rowcount:
            raise HTTPException(404, "Tag not found")
        return db.row(conn.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)))


@app.delete("/api/tags/{tag_id}", status_code=204)
def delete_tag(tag_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))


# ---------------------------------------------------------------- loans

@app.get("/api/loans")
def list_loans(active: bool = True):
    sql = ("SELECT l.*, b.title, b.authors, b.cover FROM loans l JOIN books b ON b.id = l.book_id")
    if active:
        sql += " WHERE l.returned_at IS NULL ORDER BY l.due_at IS NULL, l.due_at, l.loaned_at"
    else:
        sql += " ORDER BY l.loaned_at DESC, l.id DESC LIMIT 200"
    today = date.today().isoformat()
    with db.connect() as conn:
        out = db.rows(conn.execute(sql))
    for l in out:
        l["cover_url"] = _cover_url(l.pop("cover"))
        l["overdue"] = bool(l["due_at"] and not l["returned_at"] and l["due_at"] < today)
    return out


@app.post("/api/books/{book_id}/loans", status_code=201)
def create_loan(book_id: int, body: LoanIn):
    with db.connect() as conn:
        _get_book(conn, book_id)
        if conn.execute("SELECT 1 FROM loans WHERE book_id = ? AND returned_at IS NULL", (book_id,)).fetchone():
            raise HTTPException(409, "This book is already loaned out")
        cur = conn.execute(
            "INSERT INTO loans(book_id, borrower, loaned_at, due_at, notes) VALUES (?, ?, ?, ?, ?)",
            (book_id, body.borrower.strip(), (body.loaned_at or date.today()).isoformat(),
             body.due_at.isoformat() if body.due_at else None, body.notes))
        return db.row(conn.execute("SELECT * FROM loans WHERE id = ?", (cur.lastrowid,)))


@app.post("/api/loans/{loan_id}/return")
def return_loan(loan_id: int):
    with db.connect() as conn:
        cur = conn.execute("UPDATE loans SET returned_at = date('now') WHERE id = ? AND returned_at IS NULL", (loan_id,))
        if not cur.rowcount:
            raise HTTPException(404, "No active loan with that id")
        return db.row(conn.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)))


@app.delete("/api/loans/{loan_id}", status_code=204)
def delete_loan(loan_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM loans WHERE id = ?", (loan_id,))


@app.get("/api/borrowers")
def borrowers():
    with db.connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT borrower FROM loans GROUP BY borrower ORDER BY MAX(loaned_at) DESC LIMIT 50")]


# ---------------------------------------------------------------- stats & export

@app.get("/api/stats")
def stats():
    today = date.today().isoformat()
    with db.connect() as conn:
        one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
        return {
            "books": one("SELECT COUNT(*) FROM books WHERE status = 'owned'"),
            "copies": one("SELECT COALESCE(SUM(copies), 0) FROM books WHERE status = 'owned'"),
            "wishlist": one("SELECT COUNT(*) FROM books WHERE status = 'wishlist'"),
            "authors": one("SELECT COUNT(DISTINCT authors) FROM books WHERE authors != '' AND status = 'owned'"),
            "loaned": one("SELECT COUNT(*) FROM loans WHERE returned_at IS NULL"),
            "overdue": one("SELECT COUNT(*) FROM loans WHERE returned_at IS NULL AND due_at < ?", today),
            "shelves": one("SELECT COUNT(*) FROM shelves"),
            "unshelved": one("SELECT COUNT(*) FROM books WHERE shelf_id IS NULL AND status = 'owned'"),
        }


EXPORT_FIELDS = ["id", "title", "subtitle", "authors", "isbn13", "isbn10", "publisher", "published",
                 "pages", "series", "series_index", "format", "status", "copies", "shelf", "room",
                 "tags", "loaned_to", "notes", "created_at"]


@app.get("/api/export.csv")
def export_csv():
    with db.connect() as conn:
        books = _decorate(conn, db.rows(conn.execute("SELECT * FROM books ORDER BY title COLLATE NOCASE")))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
    w.writeheader()
    for b in books:
        w.writerow({
            **b,
            "shelf": (b["shelf"] or {}).get("name", ""),
            "room": (b["shelf"] or {}).get("room", ""),
            "tags": "; ".join(t["name"] for t in b["tags"]),
            "loaned_to": (b["active_loan"] or {}).get("borrower", ""),
        })
    buf.seek(0)
    name = f"home-library-{date.today().isoformat()}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- static

db.DATA_DIR.mkdir(parents=True, exist_ok=True)
db.COVERS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/covers", StaticFiles(directory=db.COVERS_DIR), name="covers")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})
