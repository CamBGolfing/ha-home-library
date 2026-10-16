"""Library actions shared by the web API and the Home Assistant integration.

Approving a request from a phone notification, from a dashboard button, or from the
Loans page all go through the same code, so they behave identically.
"""
from datetime import date, timedelta

from fastapi import HTTPException

from . import db, events


def start_loan(conn, book_id: int, borrower: str, loaned_at: date | None, due_at: date | None,
               notes: str = "", user_id: int | None = None) -> dict:
    if conn.execute("SELECT 1 FROM loans WHERE book_id = ? AND returned_at IS NULL", (book_id,)).fetchone():
        raise HTTPException(409, "This book is already loaned out")
    cur = conn.execute(
        "INSERT INTO loans(book_id, borrower, loaned_at, due_at, notes, user_id) VALUES (?, ?, ?, ?, ?, ?)",
        (book_id, borrower.strip(), (loaned_at or date.today()).isoformat(),
         due_at.isoformat() if due_at else None, notes, user_id))
    return db.row(conn.execute("SELECT * FROM loans WHERE id = ?", (cur.lastrowid,)))


def pending_request(conn, request_id: int) -> dict:
    r = db.row(conn.execute("SELECT * FROM borrow_requests WHERE id = ? AND status = 'pending'", (request_id,)))
    if not r:
        raise HTTPException(404, "That request was already handled")
    return r


def approve_request(request_id: int, decided_by: int | None, due_at: date | None, via: str = "app") -> dict:
    with db.connect() as conn:
        r = pending_request(conn, request_id)
        who = conn.execute("SELECT display_name FROM users WHERE id = ?", (r["user_id"],)).fetchone()[0]
        loan = start_loan(conn, r["book_id"], who, None, due_at, r["message"], r["user_id"])
        conn.execute("UPDATE borrow_requests SET status = 'approved', decided_at = datetime('now'), decided_by = ? "
                     "WHERE id = ?", (decided_by, request_id))
    events.emit("book_lent", {"loan_id": loan["id"], "request_id": request_id, "via": via})
    return loan


def decline_request(request_id: int, decided_by: int | None, via: str = "app") -> None:
    with db.connect() as conn:
        pending_request(conn, request_id)
        conn.execute("UPDATE borrow_requests SET status = 'declined', decided_at = datetime('now'), decided_by = ? "
                     "WHERE id = ?", (decided_by, request_id))
    events.emit("request_declined", {"request_id": request_id, "via": via})


def oldest_pending_request(lendable_only: bool = True) -> dict | None:
    sql = ("SELECT r.* FROM borrow_requests r WHERE r.status = 'pending'"
           + (" AND NOT EXISTS (SELECT 1 FROM loans l WHERE l.book_id = r.book_id AND l.returned_at IS NULL)"
              if lendable_only else "")
           + " ORDER BY r.created_at, r.id LIMIT 1")
    with db.connect() as conn:
        return db.row(conn.execute(sql))


def default_due_date() -> date:
    return date.today() + timedelta(days=events.settings().loan_days)
