"""Everything the library tells Home Assistant about.

Web requests call emit() after their database work commits. A single background
worker thread then does the slow parts (calling Home Assistant, publishing to MQTT,
sending phone notifications) so the app never waits on the network.
"""
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime

from . import db

log = logging.getLogger("library.events")

EVENT_PREFIX = "home_library_"


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None or v == "" else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    notify_services: list[str] = field(default_factory=list)
    notify_on_request: bool = True
    notify_on_overdue: bool = True
    notify_on_return: bool = False
    overdue_hour: int = 9
    loan_days: int = 30
    mqtt_enabled: bool = True


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        services = [s.strip().removeprefix("notify.") for s in os.environ.get("NOTIFY_SERVICES", "").split(",") if s.strip()]
        _settings = Settings(
            notify_services=services,
            notify_on_request=_bool("NOTIFY_ON_REQUEST", True),
            notify_on_overdue=_bool("NOTIFY_ON_OVERDUE", True),
            notify_on_return=_bool("NOTIFY_ON_RETURN", False),
            overdue_hour=max(0, min(23, int(os.environ.get("OVERDUE_CHECK_HOUR", "9") or 9))),
            loan_days=max(1, int(os.environ.get("LOAN_DAYS", "30") or 30)),
            mqtt_enabled=_bool("MQTT_ENABLED", True),
        )
    return _settings


# ---------------------------------------------------------------- details for each event

def _links(conn, book_id: int | None) -> dict:
    out = {}
    public = db.get_setting(conn, "public_url").rstrip("/")
    if public:
        out["url"] = f"{public}/#/book/{book_id}" if book_id else f"{public}/"
    from . import ha  # local import to avoid a cycle at startup
    if ha.panel_path():
        out["ha_path"] = ha.panel_path()
    return out


def _book(conn, book_id: int) -> dict:
    b = db.row(conn.execute("SELECT id, title, authors FROM books WHERE id = ?", (book_id,))) or {}
    return {"book_id": book_id, "title": b.get("title", ""), "authors": b.get("authors", "")}


def describe(kind: str, data: dict) -> dict | None:
    """Fill in human-friendly details. Returns None if the thing no longer exists."""
    with db.connect() as conn:
        if kind == "borrow_requested":
            r = db.row(conn.execute(
                "SELECT r.*, u.display_name FROM borrow_requests r JOIN users u ON u.id = r.user_id WHERE r.id = ?",
                (data["request_id"],)))
            if not r:
                return None
            return {"request_id": r["id"], "requester": r["display_name"], "message": r["message"],
                    **_book(conn, r["book_id"]), **_links(conn, r["book_id"])}
        if kind == "request_declined":
            r = db.row(conn.execute(
                "SELECT r.*, u.display_name FROM borrow_requests r JOIN users u ON u.id = r.user_id WHERE r.id = ?",
                (data["request_id"],)))
            if not r:
                return None
            return {"request_id": r["id"], "requester": r["display_name"], "via": data.get("via", "app"),
                    **_book(conn, r["book_id"])}
        if kind in ("book_lent", "book_returned", "loan_overdue"):
            loan = db.row(conn.execute("SELECT * FROM loans WHERE id = ?", (data["loan_id"],)))
            if not loan:
                return None
            out = {"loan_id": loan["id"], "borrower": loan["borrower"], "loaned_at": loan["loaned_at"],
                   "due_at": loan["due_at"], **_book(conn, loan["book_id"]), **_links(conn, loan["book_id"])}
            if kind == "book_lent":
                out["request_id"] = data.get("request_id")
                out["via"] = data.get("via", "app")
            if kind == "book_returned":
                out["returned_at"] = loan["returned_at"]
                out["days_out"] = (date.fromisoformat(loan["returned_at"]) - date.fromisoformat(loan["loaned_at"])).days
            if kind == "loan_overdue":
                out["days_overdue"] = (date.today() - date.fromisoformat(loan["due_at"])).days
            return out
        if kind == "book_added":
            b = db.row(conn.execute(
                "SELECT b.id, b.title, b.authors, b.status, u.display_name AS added_by FROM books b "
                "LEFT JOIN users u ON u.id = b.added_by WHERE b.id = ?", (data["book_id"],)))
            if not b:
                return None
            return {"book_id": b["id"], "title": b["title"], "authors": b["authors"], "status": b["status"],
                    "added_by": b["added_by"], **_links(conn, b["id"])}
        if kind == "member_joined":
            u = db.row(conn.execute("SELECT display_name, role FROM users WHERE id = ?", (data["user_id"],)))
            if not u:
                return None
            return {"name": u["display_name"], "role": u["role"]}
    return dict(data)


# ---------------------------------------------------------------- snapshot for sensors

def snapshot() -> dict:
    today = date.today().isoformat()
    with db.connect() as conn:
        one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
        loans = db.rows(conn.execute(
            "SELECT l.id, l.borrower, l.loaned_at, l.due_at, b.title FROM loans l JOIN books b ON b.id = l.book_id "
            "WHERE l.returned_at IS NULL ORDER BY l.due_at IS NULL, l.due_at, l.loaned_at"))
        for l in loans:
            l["overdue"] = bool(l["due_at"] and l["due_at"] < today)
        requests = db.rows(conn.execute(
            "SELECT r.id, r.message, r.created_at, u.display_name AS requester, b.title FROM borrow_requests r "
            "JOIN users u ON u.id = r.user_id JOIN books b ON b.id = r.book_id "
            "WHERE r.status = 'pending' ORDER BY r.created_at"))
        last = db.row(conn.execute(
            "SELECT id, title, authors, created_at FROM books WHERE status = 'owned' ORDER BY created_at DESC, id DESC LIMIT 1"))
        overdue = [l for l in loans if l["overdue"]]
        return {
            "books": one("SELECT COUNT(*) FROM books WHERE status = 'owned'"),
            "copies": one("SELECT COALESCE(SUM(copies), 0) FROM books WHERE status = 'owned'"),
            "wishlist": one("SELECT COUNT(*) FROM books WHERE status = 'wishlist'"),
            "shelves": one("SELECT COUNT(*) FROM shelves"),
            "members": one("SELECT COUNT(*) FROM users WHERE disabled = 0"),
            "on_loan": len(loans),
            "loans": loans[:25],
            "overdue": len(overdue),
            "overdue_loans": overdue[:25],
            "requests": len(requests),
            "request_list": requests[:25],
            "last_added": last["title"] if last else "",
            "last_added_info": ({"authors": last["authors"], "added_at": last["created_at"].replace(" ", "T") + "+00:00",
                                 **_links(conn, last["id"])}
                                if last else {}),
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }


# ---------------------------------------------------------------- notifications

def _notifications_for(kind: str, d: dict) -> list[tuple[str, str, dict]]:
    """(title, message, data) for the built-in phone notifications."""
    s = settings()
    link = d.get("url") or d.get("ha_path")
    nav = {"url": link, "clickAction": link} if link else {}
    if kind == "borrow_requested" and s.notify_on_request:
        msg = f"{d['title']}" + (f" by {d['authors']}" if d.get("authors") else "")
        if d.get("message"):
            msg += f'\n"{d["message"]}"'
        rid = d["request_id"]
        return [(f"{d['requester']} wants to borrow a book", msg, {
            **nav, "tag": f"home_library_request_{rid}", "group": "home_library",
            "actions": [
                {"action": f"HOME_LIBRARY_APPROVE_{rid}", "title": f"Lend it ({s.loan_days} days)"},
                {"action": f"HOME_LIBRARY_DECLINE_{rid}", "title": "Decline"},
            ],
        })]
    if kind == "book_returned" and s.notify_on_return:
        return [("Book returned", f"{d['borrower']} returned {d['title']}", {**nav, "group": "home_library"})]
    if kind in ("book_lent", "request_declined") and d.get("request_id"):
        # Clear the request notification on every phone once it's been answered anywhere.
        return [("", "clear_notification", {"tag": f"home_library_request_{d['request_id']}"})]
    return []


def _overdue_digest(items: list[dict]) -> tuple[str, str, dict]:
    lines = [f"{i['title']}: {i['borrower']}, due {i['due_at']}" for i in items[:6]]
    if len(items) > 6:
        lines.append(f"and {len(items) - 6} more")
    title = "1 library book is overdue" if len(items) == 1 else f"{len(items)} library books are overdue"
    link = items[0].get("url") or items[0].get("ha_path")
    return (title, "\n".join(lines), {"group": "home_library", "tag": "home_library_overdue",
                                      **({"url": link, "clickAction": link} if link else {})})


# ---------------------------------------------------------------- worker

_queue: "queue.Queue[tuple[str, dict]]" = queue.Queue()
_worker: threading.Thread | None = None
_stop = threading.Event()
_dirty = threading.Event()
recent: list[dict] = []   # last few events, shown on the owner's status panel


def emit(kind: str, data: dict) -> None:
    """Queue an event. Safe to call from any thread; never blocks on the network."""
    _queue.put((kind, data))


def touch() -> None:
    """Something changed that only affects the sensors (e.g. a book was edited)."""
    _dirty.set()


def _handle(kind: str, data: dict) -> None:
    from . import ha, mqtt_bridge
    details = describe(kind, data)
    _dirty.set()
    if details is None:
        return
    recent.insert(0, {"event": EVENT_PREFIX + kind, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                      "title": details.get("title") or details.get("name", "")})
    del recent[10:]
    ha.fire_event(EVENT_PREFIX + kind, details)
    for title, message, extra in _notifications_for(kind, details):
        ha.notify(settings().notify_services, title, message, extra)
    mqtt_bridge.publish_event(kind, details)


def check_overdue(force: bool = False) -> int:
    """Once a day (at the configured hour) announce every overdue loan."""
    from . import ha
    now = datetime.now()
    today = date.today().isoformat()
    with db.connect() as conn:
        if not force and (now.hour < settings().overdue_hour or db.get_setting(conn, "overdue_checked") == today):
            return 0
        db.set_setting(conn, "overdue_checked", today)
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM loans WHERE returned_at IS NULL AND due_at IS NOT NULL AND due_at < ? ORDER BY due_at", (today,))]
    items = []
    for loan_id in ids:
        d = describe("loan_overdue", {"loan_id": loan_id})
        if d:
            items.append(d)
            ha.fire_event(EVENT_PREFIX + "loan_overdue", d)
    if items and settings().notify_on_overdue:
        ha.notify(settings().notify_services, *_overdue_digest(items))
    _dirty.set()
    return len(items)


def _run() -> None:
    from . import mqtt_bridge
    last_publish = 0.0
    last_overdue_check = 0.0
    while not _stop.is_set():
        try:
            kind, data = _queue.get(timeout=1.0)
            _handle(kind, data)
        except queue.Empty:
            pass
        except Exception:  # never let one bad event stop the worker
            log.exception("Event handling failed")
        now = time.monotonic()
        try:
            # Coalesce bursts (e.g. a scanning session) into one sensor update per second,
            # and refresh every 10 minutes anyway so "overdue" flips over at midnight.
            if (_dirty.is_set() and now - last_publish >= 1.0) or now - last_publish >= 600:
                _dirty.clear()
                last_publish = now
                mqtt_bridge.publish_state(snapshot())
            if now - last_overdue_check >= 300:
                last_overdue_check = now
                check_overdue()
        except Exception:
            log.exception("Background update failed")


def start() -> None:
    global _worker
    if _worker and _worker.is_alive():
        return
    _stop.clear()
    _worker = threading.Thread(target=_run, name="library-events", daemon=True)
    _worker.start()


def stop() -> None:
    _stop.set()
    if _worker:
        _worker.join(timeout=5)


def drain(timeout: float = 5.0) -> None:
    """Wait until queued events are handled (used by tests)."""
    end = time.monotonic() + timeout
    while not _queue.empty() and time.monotonic() < end:
        time.sleep(0.02)
    time.sleep(0.1)
