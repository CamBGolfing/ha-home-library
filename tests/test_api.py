"""Catalog API tests (as the owner). Run from the repo root: pytest"""


def test_isbn_normalize(app):
    from app.lookup import normalize
    assert normalize("978-0-547-92822-7") == {"isbn13": "9780547928227", "isbn10": "054792822X", "kind": "isbn"}
    assert normalize("054792822X")["isbn13"] == "9780547928227"
    assert normalize("9791032305690")["isbn10"] is None
    assert normalize("12345")["kind"] == "invalid"
    assert normalize("9780547928228")["kind"] == "invalid"  # bad checksum


def test_lookup_merges_sources(client):
    r = client.get("/api/lookup/054792822X").json()
    assert r["found"] and r["title"] == "The Hobbit"
    assert r["subtitle"] == "Or There and Back Again"       # filled from Google
    assert r["description"] == "Bilbo Baggins is a hobbit."  # Google description wins
    assert r["publisher"] == "Houghton Mifflin Harcourt"     # Open Library first
    assert r["isbn13"] == "9780547928227"
    assert r["owned"] == []


def test_lookup_not_found(client):
    r = client.get("/api/lookup/9780000000002").json()
    assert r["found"] is False


def test_quick_add_and_duplicate(client):
    shelf = client.post("/api/shelves", json={"name": "Top shelf", "room": "Office"}).json()
    r = client.post("/api/quick-add", json={"code": "9780547928227", "shelf_id": shelf["id"], "tags": ["Fantasy"]}).json()
    assert r["result"] == "added"
    book = r["book"]
    assert book["cover_url"] == "covers/fake.jpg"
    assert book["shelf"]["name"] == "Top shelf"
    assert [t["name"] for t in book["tags"]] == ["Fantasy"]

    dup = client.post("/api/quick-add", json={"code": "9780547928227"}).json()
    assert dup["result"] == "duplicate" and dup["book"]["id"] == book["id"]
    assert client.get("/api/lookup/9780547928227").json()["owned"][0]["id"] == book["id"]


def test_crud_search_filters(client):
    a = client.post("/api/books", json={"title": "Dune", "authors": "Frank Herbert", "series": "Dune", "series_index": 1, "tags": ["Sci-fi"]}).json()
    client.post("/api/books", json={"title": "Children of Dune", "authors": "Frank Herbert", "status": "wishlist"})
    client.post("/api/books", json={"title": "Goodnight Moon", "authors": "Margaret Wise Brown", "tags": ["Kids"]})

    assert client.get("/api/books").json()["total"] == 3
    assert client.get("/api/books", params={"q": "herbert"}).json()["total"] == 2
    assert client.get("/api/books", params={"q": "kids"}).json()["total"] == 1  # tag search
    assert client.get("/api/books", params={"status": "wishlist"}).json()["total"] == 1
    tag_id = a["tags"][0]["id"]
    assert client.get("/api/books", params={"tag": tag_id}).json()["total"] == 1
    assert client.get("/api/books", params={"shelf": 0}).json()["total"] == 3

    upd = client.patch(f"/api/books/{a['id']}", json={"notes": "Signed copy", "tags": ["Sci-fi", "Favorites"]}).json()
    assert upd["notes"] == "Signed copy" and len(upd["tags"]) == 2
    assert client.patch(f"/api/books/{a['id']}", json={"status": "bogus"}).status_code == 400

    assert client.delete(f"/api/books/{a['id']}").status_code == 204
    assert client.get(f"/api/books/{a['id']}").status_code == 404


def test_loans(client):
    b = client.post("/api/books", json={"title": "Dune"}).json()
    loan = client.post(f"/api/books/{b['id']}/loans", json={"borrower": "Sam", "due_at": "2000-01-01"}).json()
    assert client.post(f"/api/books/{b['id']}/loans", json={"borrower": "Alex"}).status_code == 409

    active = client.get("/api/loans").json()
    assert len(active) == 1 and active[0]["overdue"] is True
    assert client.get("/api/books", params={"loaned": True}).json()["total"] == 1
    assert client.get("/api/stats").json()["overdue"] == 1
    assert client.get("/api/borrowers").json() == ["Sam", "Cameron"]  # past borrowers, then members

    client.post(f"/api/loans/{loan['id']}/return")
    assert client.get("/api/loans").json() == []
    assert len(client.get(f"/api/books/{b['id']}").json()["loans"]) == 1


def test_cover_upload_and_export(client):
    b = client.post("/api/books", json={"title": "Dune", "authors": "Frank Herbert"}).json()
    r = client.post(f"/api/books/{b['id']}/cover", files={"file": ("c.png", b"\x89PNG" + b"0" * 100, "image/png")})
    assert r.status_code == 200 and r.json()["cover_url"].endswith(".png")
    assert client.get("/" + r.json()["cover_url"]).status_code == 200

    csv_text = client.get("/api/export.csv").text
    assert "Dune" in csv_text and csv_text.startswith("id,title")


def test_frontend_served(client):
    assert "Home Library" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/scanner.js").status_code == 200
    wasm = client.get("/static/vendor/zxing_reader.wasm")
    assert wasm.status_code == 200 and wasm.headers["content-type"] == "application/wasm"
    assert wasm.content[:4] == b"\0asm"
