"""Home Library: a small self-hosted catalog for the books in your house."""
import csv
import io
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, db, lookup
from .auth import editor, owner, viewer

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
log = logging.getLogger("library")

STATIC_DIR = Path(__file__).parent / "static"
MAX_COVER_BYTES = 8 * 1024 * 1024


@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    log.info("Home Library ready, data in %s (running as add-on: %s)", db.DATA_DIR, auth.IS_ADDON)
    yield


app = FastAPI(title="Home Library", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)

OPEN_PATHS = {"/api/health", "/api/auth/me", "/api/auth/login", "/api/auth/setup", "/api/auth/logout"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def gatekeeper(request: Request, call_next):
    """Sign-in wall for the API and covers, plus a CSRF header check on writes.

    The front end sends X-Library-Request on every write. Browsers won't add a custom
    header to a cross-site form post, so a page elsewhere can't act as a signed-in user.
    """
    path = request.url.path
    is_api = path.startswith("/api/")
    if is_api and request.method not in SAFE_METHODS and request.headers.get(auth.CSRF_HEADER) != "1":
        return JSONResponse({"detail": "Missing request header"}, status_code=403)
    open_path = path in OPEN_PATHS or path.startswith("/api/auth/invite/")
    if (is_api and not open_path) or path.startswith("/covers/"):
        if auth.resolve(request) is None:
            return JSONResponse({"detail": "Sign in to continue"}, status_code=401)
    response = await call_next(request)
    if is_api:
        response.headers["Cache-Control"] = "no-store"
    return response


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


class LoginIn(BaseModel):
    username: str
    password: str


class AccountIn(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    display_name: str = Field(min_length=1, max_length=60)
    password: str


class PasswordIn(BaseModel):
    current: str = ""
    new: str
    username: str | None = None   # lets a Home Assistant user add a library sign-in


class UserPatch(BaseModel):
    role: str | None = None
    disabled: bool | None = None
    display_name: str | None = None


class InviteIn(BaseModel):
    role: str = "viewer"
    note: str = ""


class SettingsIn(BaseModel):
    public_url: str = ""


class RequestIn(BaseModel):
    message: str = ""


class ApproveIn(BaseModel):
    due_at: date | None = None


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


def _decorate(conn, books: list[dict], user: dict | None = None) -> list[dict]:
    """Attach tags, shelf and active loan to each book in one pass.

    Viewers see whether a book is out, but not who has it (unless it's them),
    and don't see private notes.
    """
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
    full = user is None or auth.can(user, "editor")
    for b in books:
        b["tags"] = tags.get(b["id"], [])
        b["shelf"] = shelves.get(b["shelf_id"])
        b["cover_url"] = _cover_url(b["cover"])
        loan = loans.get(b["id"])
        if full:
            b["active_loan"] = loan
        else:
            mine = bool(loan and loan.get("user_id") == user["id"])
            b["active_loan"] = None if not loan else {
                "out": True, "mine": mine, "due_at": loan["due_at"] if mine else None,
                "borrower": user["display_name"] if mine else None}
            b["notes"] = ""
    return books


def _get_book(conn, book_id: int, user: dict | None = None) -> dict:
    b = db.row(conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)))
    if not b:
        raise HTTPException(404, "Book not found")
    return _decorate(conn, [b], user)[0]


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


async def _create_book(data: BookIn, user: dict) -> dict:
    _check_status(data.status)
    cover = await lookup.download_cover(data.cover_urls, db.COVERS_DIR) if data.cover_urls else ""
    values = data.model_dump(include=set(BOOK_COLUMNS))
    values["copies"] = max(1, values.get("copies") or 1)
    with db.connect() as conn:
        cols = list(values) + ["cover", "added_by"]
        cur = conn.execute(
            f"INSERT INTO books ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [values[c] for c in values] + [cover, user["id"]],
        )
        book_id = cur.lastrowid
        _set_tags(conn, book_id, data.tags)
        return _get_book(conn, book_id)


def _check_role(role: str):
    if role not in auth.ROLES:
        raise HTTPException(400, f"Role must be one of {', '.join(auth.ROLES)}")


def _clean_username(name: str) -> str:
    name = name.strip()
    if not name or any(c.isspace() for c in name) or len(name) < 2:
        raise HTTPException(400, "Usernames need at least 2 characters and no spaces")
    return name


def _active_owner_count(conn, excluding: int | None = None) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'owner' AND disabled = 0 AND id != ?",
                        (excluding or -1,)).fetchone()[0]


def _public_url(conn, request: Request) -> str | None:
    url = db.get_setting(conn, "public_url").rstrip("/")
    if url:
        return url
    if auth.is_ingress(request):
        return None  # the ingress address only works for people signed in to HA
    return str(request.base_url).rstrip("/")


# ---------------------------------------------------------------- auth

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request):
    user = auth.resolve(request)
    return {
        "user": auth.public_user(user),
        "via": user["via"] if user else None,
        "setup_needed": False if user else auth.setup_needed(),
        "is_addon": auth.IS_ADDON,
    }


@app.post("/api/auth/setup")
def setup(body: AccountIn, request: Request, response: Response):
    if not auth.setup_needed():
        raise HTTPException(403, "This library already has an owner")
    auth.check_password_rules(body.password)
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO users(username, display_name, password_hash, role) VALUES (?, ?, ?, 'owner')",
            (_clean_username(body.username), body.display_name.strip(), auth.hash_password(body.password)))
        token = auth.create_session(conn, cur.lastrowid)
        user = db.row(conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)))
    auth.set_session_cookie(response, request, token)
    return {"user": auth.public_user(user)}


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    key = f"{auth.client_ip(request)}|{body.username.lower()}"
    auth.throttle_check(key)
    auth.throttle_check(auth.client_ip(request) + "|*")
    with db.connect() as conn:
        user = db.row(conn.execute("SELECT * FROM users WHERE username = ?", (body.username.strip(),)))
        ok = auth.verify_password(body.password, user and user["password_hash"])
        if not ok or user["disabled"]:
            auth.throttle_fail(key)
            auth.throttle_fail(auth.client_ip(request) + "|*")
            raise HTTPException(401, "That username and password don't match")
        token = auth.create_session(conn, user["id"])
    auth.throttle_clear(key)
    auth.set_session_cookie(response, request, token)
    return {"user": auth.public_user(user)}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        with db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (auth.token_hash(token),))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/auth/password")
def change_password(body: PasswordIn, request: Request, user: dict = Depends(viewer)):
    auth.check_password_rules(body.new)
    with db.connect() as conn:
        if user["password_hash"]:
            if not auth.verify_password(body.current, user["password_hash"]):
                raise HTTPException(400, "Your current password isn't right")
            username = user["username"]
        else:
            # Home Assistant users adding a library sign-in for use outside HA.
            username = _clean_username(body.username or "")
            if conn.execute("SELECT 1 FROM users WHERE username = ? AND id != ?", (username, user["id"])).fetchone():
                raise HTTPException(409, "That username is taken")
        conn.execute("UPDATE users SET username = ?, password_hash = ? WHERE id = ?",
                     (username, auth.hash_password(body.new), user["id"]))
        # Sign out other password sessions, keep this one.
        keep = request.cookies.get(auth.SESSION_COOKIE)
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                     (user["id"], auth.token_hash(keep) if keep else ""))
    return {"ok": True}


def _load_invite(conn, token: str) -> dict:
    inv = db.row(conn.execute(
        "SELECT * FROM invites WHERE token_hash = ? AND used_at IS NULL AND expires_at > datetime('now')",
        (auth.token_hash(token),)))
    if not inv:
        raise HTTPException(404, "This invite link has expired or was already used. Ask for a new one.")
    return inv


@app.get("/api/auth/invite/{token}")
def invite_info(token: str):
    with db.connect() as conn:
        inv = _load_invite(conn, token)
        reset_user = None
        if inv["for_user_id"]:
            reset_user = db.row(conn.execute("SELECT username, display_name FROM users WHERE id = ?", (inv["for_user_id"],)))
        return {"role": inv["role"], "reset": bool(reset_user), "user": reset_user,
                "library_owner": (db.row(conn.execute(
                    "SELECT display_name FROM users WHERE role = 'owner' ORDER BY id LIMIT 1")) or {}).get("display_name")}


@app.post("/api/auth/invite/{token}")
def accept_invite(token: str, body: AccountIn, request: Request, response: Response):
    auth.throttle_check(auth.client_ip(request) + "|invite")
    auth.check_password_rules(body.password)
    with db.connect() as conn:
        try:
            inv = _load_invite(conn, token)
        except HTTPException:
            auth.throttle_fail(auth.client_ip(request) + "|invite")
            raise
        if inv["for_user_id"]:
            user_id = inv["for_user_id"]
            conn.execute("UPDATE users SET password_hash = ?, disabled = 0 WHERE id = ?",
                         (auth.hash_password(body.password), user_id))
            if not conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()[0]:
                conn.execute("UPDATE users SET username = ? WHERE id = ?", (_clean_username(body.username), user_id))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        else:
            username = _clean_username(body.username)
            if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                raise HTTPException(409, "That username is taken. Try another.")
            cur = conn.execute(
                "INSERT INTO users(username, display_name, password_hash, role) VALUES (?, ?, ?, ?)",
                (username, body.display_name.strip(), auth.hash_password(body.password), inv["role"]))
            user_id = cur.lastrowid
        conn.execute("UPDATE invites SET used_at = datetime('now') WHERE id = ?", (inv["id"],))
        session = auth.create_session(conn, user_id)
        user = db.row(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)))
    auth.set_session_cookie(response, request, session)
    return {"user": auth.public_user(user)}


# ---------------------------------------------------------------- people (owner)

@app.get("/api/users", dependencies=[Depends(owner)])
def list_users():
    with db.connect() as conn:
        return [auth.public_user(u) for u in db.rows(conn.execute(
            "SELECT * FROM users ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'editor' THEN 1 ELSE 2 END, "
            "display_name COLLATE NOCASE"))]


@app.patch("/api/users/{user_id}")
def update_user(user_id: int, body: UserPatch, me_: dict = Depends(owner)):
    with db.connect() as conn:
        target = db.row(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)))
        if not target:
            raise HTTPException(404, "No such person")
        demoting = (body.role and body.role != "owner") or body.disabled
        if target["role"] == "owner" and demoting and _active_owner_count(conn, excluding=user_id) == 0:
            raise HTTPException(400, "The library needs at least one owner. Make someone else an owner first.")
        if body.role is not None:
            _check_role(body.role)
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (body.role, user_id))
        if body.disabled is not None:
            conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(body.disabled), user_id))
            if body.disabled:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        if body.display_name is not None and body.display_name.strip():
            conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (body.display_name.strip(), user_id))
        return auth.public_user(db.row(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))))


@app.delete("/api/users/{user_id}", status_code=204)
def delete_user(user_id: int, me_: dict = Depends(owner)):
    if user_id == me_["id"]:
        raise HTTPException(400, "You can't remove yourself")
    with db.connect() as conn:
        target = db.row(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)))
        if target and target["role"] == "owner" and _active_owner_count(conn, excluding=user_id) == 0:
            raise HTTPException(400, "The library needs at least one owner")
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def _new_invite(conn, request: Request, creator: dict, role: str, note: str = "", for_user_id: int | None = None):
    token = secrets.token_urlsafe(24)
    cur = conn.execute(
        "INSERT INTO invites(token_hash, role, note, for_user_id, created_by, expires_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now', ?))",
        (auth.token_hash(token), role, note.strip(), for_user_id, creator["id"], f"+{auth.INVITE_DAYS} days"))
    base = _public_url(conn, request)
    inv = db.row(conn.execute("SELECT * FROM invites WHERE id = ?", (cur.lastrowid,)))
    inv.pop("token_hash")
    inv["token"] = token
    inv["url"] = f"{base}/#/invite/{token}" if base else None
    return inv


@app.post("/api/users/{user_id}/reset")
def reset_link(user_id: int, request: Request, me_: dict = Depends(owner)):
    with db.connect() as conn:
        target = db.row(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)))
        if not target:
            raise HTTPException(404, "No such person")
        conn.execute("UPDATE invites SET used_at = datetime('now') WHERE for_user_id = ? AND used_at IS NULL", (user_id,))
        return _new_invite(conn, request, me_, target["role"], f"Password reset for {target['display_name']}", user_id)


@app.get("/api/invites", dependencies=[Depends(owner)])
def list_invites():
    with db.connect() as conn:
        out = db.rows(conn.execute(
            "SELECT id, role, note, for_user_id, created_at, expires_at FROM invites "
            "WHERE used_at IS NULL AND expires_at > datetime('now') ORDER BY created_at DESC"))
    return out


@app.post("/api/invites", status_code=201)
def create_invite(body: InviteIn, request: Request, me_: dict = Depends(owner)):
    _check_role(body.role)
    with db.connect() as conn:
        return _new_invite(conn, request, me_, body.role, body.note)


@app.delete("/api/invites/{invite_id}", status_code=204, dependencies=[Depends(owner)])
def revoke_invite(invite_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM invites WHERE id = ?", (invite_id,))


@app.get("/api/settings", dependencies=[Depends(owner)])
def get_settings(request: Request):
    with db.connect() as conn:
        return {"public_url": db.get_setting(conn, "public_url"), "via_ingress": auth.is_ingress(request),
                "ha_user_role": auth.HA_USER_ROLE}


@app.put("/api/settings", dependencies=[Depends(owner)])
def put_settings(body: SettingsIn):
    url = body.public_url.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(400, "The address should start with https://")
    with db.connect() as conn:
        db.set_setting(conn, "public_url", url)
    return {"public_url": url}


# ---------------------------------------------------------------- lookup

@app.get("/api/lookup/{code}", dependencies=[Depends(editor)])
async def lookup_code(code: str):
    result = await lookup.lookup_isbn(code)
    with db.connect() as conn:
        result["owned"] = _find_owned(conn, result.get("isbn13"))
    return result


@app.get("/api/search", dependencies=[Depends(editor)])
async def search_online(q: str = Query(min_length=2)):
    return await lookup.search_title(q)


@app.post("/api/quick-add")
async def quick_add(body: QuickAdd, user: dict = Depends(editor)):
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
    ), user)
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
    user: dict = Depends(viewer),
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
        return {"total": total, "books": _decorate(conn, books, user)}


@app.post("/api/books", status_code=201)
async def create_book(body: BookIn, user: dict = Depends(editor)):
    return await _create_book(body, user)


@app.get("/api/books/{book_id}")
def get_book(book_id: int, user: dict = Depends(viewer)):
    with db.connect() as conn:
        b = _get_book(conn, book_id, user)
        if auth.can(user, "editor"):
            b["loans"] = db.rows(conn.execute(
                "SELECT * FROM loans WHERE book_id = ? ORDER BY loaned_at DESC, id DESC", (book_id,)))
            added = conn.execute("SELECT display_name FROM users WHERE id = ?", (b.get("added_by"),)).fetchone()
            b["added_by_name"] = added[0] if added else None
            b["requests"] = db.rows(conn.execute(
                "SELECT r.*, u.display_name FROM borrow_requests r JOIN users u ON u.id = r.user_id "
                "WHERE r.book_id = ? AND r.status = 'pending' ORDER BY r.created_at", (book_id,)))
        else:
            b["loans"] = []
            b["my_request"] = db.row(conn.execute(
                "SELECT * FROM borrow_requests WHERE book_id = ? AND user_id = ? AND status = 'pending'",
                (book_id, user["id"])))
        return b


@app.patch("/api/books/{book_id}", dependencies=[Depends(editor)])
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


@app.delete("/api/books/{book_id}", status_code=204, dependencies=[Depends(editor)])
def delete_book(book_id: int):
    with db.connect() as conn:
        b = _get_book(conn, book_id)
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    _delete_cover_file(b["cover"])


@app.post("/api/books/{book_id}/cover", dependencies=[Depends(editor)])
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


@app.post("/api/books/{book_id}/cover/refresh", dependencies=[Depends(editor)])
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

@app.get("/api/shelves", dependencies=[Depends(viewer)])
def list_shelves():
    with db.connect() as conn:
        return db.rows(conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM books b WHERE b.shelf_id = s.id) AS book_count "
            "FROM shelves s ORDER BY s.room COLLATE NOCASE, s.name COLLATE NOCASE"))


@app.post("/api/shelves", status_code=201, dependencies=[Depends(editor)])
def create_shelf(body: ShelfIn):
    with db.connect() as conn:
        cur = conn.execute("INSERT INTO shelves(name, room) VALUES (?, ?)", (body.name.strip(), body.room.strip()))
        return db.row(conn.execute("SELECT * FROM shelves WHERE id = ?", (cur.lastrowid,)))


@app.patch("/api/shelves/{shelf_id}", dependencies=[Depends(editor)])
def update_shelf(shelf_id: int, body: ShelfIn):
    with db.connect() as conn:
        cur = conn.execute("UPDATE shelves SET name = ?, room = ? WHERE id = ?",
                           (body.name.strip(), body.room.strip(), shelf_id))
        if not cur.rowcount:
            raise HTTPException(404, "Shelf not found")
        return db.row(conn.execute("SELECT * FROM shelves WHERE id = ?", (shelf_id,)))


@app.delete("/api/shelves/{shelf_id}", status_code=204, dependencies=[Depends(editor)])
def delete_shelf(shelf_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM shelves WHERE id = ?", (shelf_id,))


# ---------------------------------------------------------------- tags

@app.get("/api/tags", dependencies=[Depends(viewer)])
def list_tags():
    with db.connect() as conn:
        return db.rows(conn.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM book_tags bt WHERE bt.tag_id = t.id) AS book_count "
            "FROM tags t ORDER BY t.name"))


@app.post("/api/tags", status_code=201, dependencies=[Depends(editor)])
def create_tag(body: TagIn):
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO tags(name, color) VALUES (?, ?)", (body.name.strip(), body.color))
        return db.row(conn.execute("SELECT * FROM tags WHERE name = ?", (body.name.strip(),)))


@app.patch("/api/tags/{tag_id}", dependencies=[Depends(editor)])
def update_tag(tag_id: int, body: TagIn):
    with db.connect() as conn:
        cur = conn.execute("UPDATE tags SET name = ?, color = ? WHERE id = ?", (body.name.strip(), body.color, tag_id))
        if not cur.rowcount:
            raise HTTPException(404, "Tag not found")
        return db.row(conn.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)))


@app.delete("/api/tags/{tag_id}", status_code=204, dependencies=[Depends(editor)])
def delete_tag(tag_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))


# ---------------------------------------------------------------- loans

@app.get("/api/loans")
def list_loans(active: bool = True, user: dict = Depends(viewer)):
    sql = "SELECT l.*, b.title, b.authors, b.cover FROM loans l JOIN books b ON b.id = l.book_id"
    where, args = [], []
    if not auth.can(user, "editor"):
        where.append("l.user_id = ?")
        args.append(user["id"])
    if active:
        where.append("l.returned_at IS NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (" ORDER BY l.due_at IS NULL, l.due_at, l.loaned_at" if active
            else " ORDER BY l.loaned_at DESC, l.id DESC LIMIT 200")
    today = date.today().isoformat()
    with db.connect() as conn:
        out = db.rows(conn.execute(sql, args))
    for l in out:
        l["cover_url"] = _cover_url(l.pop("cover"))
        l["overdue"] = bool(l["due_at"] and not l["returned_at"] and l["due_at"] < today)
    return out


def _start_loan(conn, book_id: int, borrower: str, loaned_at: date | None, due_at: date | None,
                notes: str = "", user_id: int | None = None) -> dict:
    if conn.execute("SELECT 1 FROM loans WHERE book_id = ? AND returned_at IS NULL", (book_id,)).fetchone():
        raise HTTPException(409, "This book is already loaned out")
    cur = conn.execute(
        "INSERT INTO loans(book_id, borrower, loaned_at, due_at, notes, user_id) VALUES (?, ?, ?, ?, ?, ?)",
        (book_id, borrower.strip(), (loaned_at or date.today()).isoformat(),
         due_at.isoformat() if due_at else None, notes, user_id))
    return db.row(conn.execute("SELECT * FROM loans WHERE id = ?", (cur.lastrowid,)))


@app.post("/api/books/{book_id}/loans", status_code=201, dependencies=[Depends(editor)])
def create_loan(book_id: int, body: LoanIn):
    with db.connect() as conn:
        _get_book(conn, book_id)
        # Link the loan to a library account when the name matches one, so it shows up for them.
        match = conn.execute("SELECT id FROM users WHERE display_name = ? COLLATE NOCASE AND disabled = 0",
                             (body.borrower.strip(),)).fetchall()
        user_id = match[0][0] if len(match) == 1 else None
        return _start_loan(conn, book_id, body.borrower, body.loaned_at, body.due_at, body.notes, user_id)


@app.post("/api/loans/{loan_id}/return", dependencies=[Depends(editor)])
def return_loan(loan_id: int):
    with db.connect() as conn:
        cur = conn.execute("UPDATE loans SET returned_at = date('now') WHERE id = ? AND returned_at IS NULL", (loan_id,))
        if not cur.rowcount:
            raise HTTPException(404, "No active loan with that id")
        return db.row(conn.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)))


@app.delete("/api/loans/{loan_id}", status_code=204, dependencies=[Depends(editor)])
def delete_loan(loan_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM loans WHERE id = ?", (loan_id,))


@app.get("/api/borrowers", dependencies=[Depends(editor)])
def borrowers():
    with db.connect() as conn:
        names = [r[0] for r in conn.execute(
            "SELECT borrower FROM loans GROUP BY borrower ORDER BY MAX(loaned_at) DESC LIMIT 50")]
        for (n,) in conn.execute("SELECT display_name FROM users WHERE disabled = 0 ORDER BY display_name"):
            if n not in names:
                names.append(n)
        return names


# ---------------------------------------------------------------- borrow requests

def _request_rows(conn, where: str, args: list) -> list[dict]:
    out = db.rows(conn.execute(
        "SELECT r.*, u.display_name, b.title, b.authors, b.cover, "
        "  EXISTS (SELECT 1 FROM loans l WHERE l.book_id = r.book_id AND l.returned_at IS NULL) AS book_out "
        "FROM borrow_requests r JOIN users u ON u.id = r.user_id JOIN books b ON b.id = r.book_id "
        f"WHERE {where} ORDER BY r.created_at DESC LIMIT 200", args))
    for r in out:
        r["cover_url"] = _cover_url(r.pop("cover"))
        r["book_out"] = bool(r["book_out"])
    return out


@app.post("/api/books/{book_id}/requests", status_code=201)
def request_book(book_id: int, body: RequestIn, user: dict = Depends(viewer)):
    with db.connect() as conn:
        b = _get_book(conn, book_id)
        if b["status"] != "owned":
            raise HTTPException(400, "That book is on the wishlist, not on the shelf yet")
        if conn.execute("SELECT 1 FROM borrow_requests WHERE book_id = ? AND user_id = ? AND status = 'pending'",
                        (book_id, user["id"])).fetchone():
            raise HTTPException(409, "You've already asked for this one")
        cur = conn.execute("INSERT INTO borrow_requests(book_id, user_id, message) VALUES (?, ?, ?)",
                           (book_id, user["id"], body.message.strip()[:500]))
        return _request_rows(conn, "r.id = ?", [cur.lastrowid])[0]


@app.get("/api/requests")
def list_requests(user: dict = Depends(viewer)):
    with db.connect() as conn:
        if auth.can(user, "editor"):
            return _request_rows(conn, "r.status = 'pending'", [])
        return _request_rows(conn, "r.user_id = ? AND (r.status = 'pending' OR r.decided_at > datetime('now', '-30 days'))",
                             [user["id"]])


def _pending_request(conn, request_id: int) -> dict:
    r = db.row(conn.execute("SELECT * FROM borrow_requests WHERE id = ? AND status = 'pending'", (request_id,)))
    if not r:
        raise HTTPException(404, "That request was already handled")
    return r


@app.post("/api/requests/{request_id}/approve")
def approve_request(request_id: int, body: ApproveIn, user: dict = Depends(editor)):
    with db.connect() as conn:
        r = _pending_request(conn, request_id)
        who = conn.execute("SELECT display_name FROM users WHERE id = ?", (r["user_id"],)).fetchone()[0]
        loan = _start_loan(conn, r["book_id"], who, None, body.due_at, r["message"], r["user_id"])
        conn.execute("UPDATE borrow_requests SET status = 'approved', decided_at = datetime('now'), decided_by = ? "
                     "WHERE id = ?", (user["id"], request_id))
        return loan


@app.post("/api/requests/{request_id}/decline")
def decline_request(request_id: int, user: dict = Depends(editor)):
    with db.connect() as conn:
        _pending_request(conn, request_id)
        conn.execute("UPDATE borrow_requests SET status = 'declined', decided_at = datetime('now'), decided_by = ? "
                     "WHERE id = ?", (user["id"], request_id))
    return {"ok": True}


@app.post("/api/requests/{request_id}/cancel")
def cancel_request(request_id: int, user: dict = Depends(viewer)):
    with db.connect() as conn:
        r = _pending_request(conn, request_id)
        if r["user_id"] != user["id"]:
            raise HTTPException(403, "That isn't your request")
        conn.execute("UPDATE borrow_requests SET status = 'cancelled', decided_at = datetime('now') WHERE id = ?",
                     (request_id,))
    return {"ok": True}


# ---------------------------------------------------------------- stats & export

@app.get("/api/stats")
def stats(user: dict = Depends(viewer)):
    today = date.today().isoformat()
    with db.connect() as conn:
        one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
        out = {
            "books": one("SELECT COUNT(*) FROM books WHERE status = 'owned'"),
            "copies": one("SELECT COALESCE(SUM(copies), 0) FROM books WHERE status = 'owned'"),
            "wishlist": one("SELECT COUNT(*) FROM books WHERE status = 'wishlist'"),
            "authors": one("SELECT COUNT(DISTINCT authors) FROM books WHERE authors != '' AND status = 'owned'"),
            "shelves": one("SELECT COUNT(*) FROM shelves"),
        }
        if auth.can(user, "editor"):
            out |= {
                "loaned": one("SELECT COUNT(*) FROM loans WHERE returned_at IS NULL"),
                "overdue": one("SELECT COUNT(*) FROM loans WHERE returned_at IS NULL AND due_at < ?", today),
                "unshelved": one("SELECT COUNT(*) FROM books WHERE shelf_id IS NULL AND status = 'owned'"),
                "requests": one("SELECT COUNT(*) FROM borrow_requests WHERE status = 'pending'"),
            }
        else:
            out |= {
                "loaned": one("SELECT COUNT(*) FROM loans WHERE returned_at IS NULL AND user_id = ?", user["id"]),
                "overdue": one("SELECT COUNT(*) FROM loans WHERE returned_at IS NULL AND user_id = ? AND due_at < ?",
                               user["id"], today),
                "requests": one("SELECT COUNT(*) FROM borrow_requests WHERE status = 'pending' AND user_id = ?",
                                user["id"]),
            }
        return out


EXPORT_FIELDS = ["id", "title", "subtitle", "authors", "isbn13", "isbn10", "publisher", "published",
                 "pages", "series", "series_index", "format", "status", "copies", "shelf", "room",
                 "tags", "loaned_to", "notes", "created_at"]


@app.get("/api/export.csv", dependencies=[Depends(editor)])
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
