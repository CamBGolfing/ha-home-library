"""Shared fixtures. Network lookups are stubbed so tests run offline."""
import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "home_library"))

HOBBIT_OL = {
    "title": "The Hobbit", "subtitle": "", "authors": "J.R.R. Tolkien",
    "publisher": "Houghton Mifflin Harcourt", "published": "2012", "pages": 300,
    "subjects": "Fantasy", "isbn10": "054792822X", "isbn13": "9780547928227",
    "cover_urls": ["https://covers.example/hobbit-L.jpg"], "url": "",
}
HOBBIT_GB = {
    "title": "The Hobbit", "subtitle": "Or There and Back Again", "authors": "J. R. R. Tolkien",
    "publisher": "", "published": "2012-09-18", "pages": 320, "language": "en",
    "description": "Bilbo Baggins is a hobbit.", "subjects": "Fiction",
    "isbn10": None, "isbn13": "9780547928227", "cover_urls": [], "url": "",
}
CSRF = {"x-library-request": "1"}
OWNER_PW = "correct horse battery"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    os.environ["LIBRARY_DATA_DIR"] = str(tmp_path)
    os.environ.pop("SUPERVISOR_TOKEN", None)
    os.environ.pop("LIBRARY_TRUST_INGRESS", None)
    from app import auth, db, lookup, main
    for m in (db, auth, lookup, main):
        importlib.reload(m)
    auth._failures.clear()
    from app import events
    events._settings = None
    while not events._queue.empty():
        events._queue.get_nowait()

    async def fake_ol(_c, isbn):
        return dict(HOBBIT_OL) if isbn == "9780547928227" else None

    async def fake_gb(_c, q, limit=1):
        return [dict(HOBBIT_GB)] if "9780547928227" in q or "054792822X" in q else []

    async def fake_cover(urls, covers_dir):
        if not urls:
            return ""
        (covers_dir / "fake.jpg").write_bytes(b"x" * 3000)
        return "fake.jpg"

    monkeypatch.setattr(lookup, "openlibrary_isbn", fake_ol)
    monkeypatch.setattr(lookup, "google_query", fake_gb)
    monkeypatch.setattr(lookup, "download_cover", fake_cover)
    db.init_db()
    return main


@pytest.fixture()
def new_client(app):
    from fastapi.testclient import TestClient
    made = []

    def make(headers=None):
        c = TestClient(app.app, headers={**CSRF, **(headers or {})})
        made.append(c)
        return c
    yield make
    for c in made:
        c.close()


@pytest.fixture()
def client(new_client):
    """A signed-in owner on a standalone (non-Home Assistant) install."""
    c = new_client()
    r = c.post("/api/auth/setup", json={"username": "cam", "display_name": "Cameron", "password": OWNER_PW})
    assert r.status_code == 200, r.text
    return c
