"""Sign-in, roles, invites, borrow requests, and the Home Assistant trust boundary."""
from conftest import CSRF, OWNER_PW


def invite(client, role="viewer", note="") -> str:
    r = client.post("/api/invites", json={"role": role, "note": note})
    assert r.status_code == 201, r.text
    assert r.json()["url"].endswith("/#/invite/" + r.json()["token"])
    return r.json()["token"]


def join(new_client, token, username, name, password="friend password"):
    c = new_client()
    r = c.post(f"/api/auth/invite/{token}", json={"username": username, "display_name": name, "password": password})
    assert r.status_code == 200, r.text
    return c


# ---------------------------------------------------------------- the wall

def test_signed_out_sees_nothing(app, new_client):
    c = new_client()
    assert c.get("/api/books").status_code == 401
    assert c.get("/covers/anything.jpg").status_code == 401
    assert c.get("/api/export.csv").status_code == 401
    me = c.get("/api/auth/me").json()
    assert me["user"] is None and me["setup_needed"] is True
    assert c.get("/").status_code == 200          # the app shell loads so it can show sign-in
    assert c.get("/api/health").status_code == 200


def test_setup_only_once(client, new_client):
    assert client.get("/api/auth/me").json()["user"]["role"] == "owner"
    other = new_client()
    assert other.get("/api/auth/me").json()["setup_needed"] is False
    r = other.post("/api/auth/setup", json={"username": "sneaky", "display_name": "X", "password": "whatever123"})
    assert r.status_code == 403


def test_writes_need_csrf_header(client):
    from fastapi.testclient import TestClient
    bare = TestClient(client.app, cookies=client.cookies)
    assert bare.post("/api/books", json={"title": "Dune"}).status_code == 403
    assert client.post("/api/books", json={"title": "Dune"}).status_code == 201


def test_login_logout_and_throttle(client, new_client):
    c = new_client()
    assert c.post("/api/auth/login", json={"username": "cam", "password": "wrong"}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "CAM", "password": OWNER_PW}).status_code == 200
    assert c.get("/api/books").status_code == 200
    c.post("/api/auth/logout")
    assert c.get("/api/books").status_code == 401

    for _ in range(10):
        c.post("/api/auth/login", json={"username": "cam", "password": "nope"})
    assert c.post("/api/auth/login", json={"username": "cam", "password": OWNER_PW}).status_code == 429


def test_password_change(client, new_client):
    assert client.post("/api/auth/password", json={"current": "bad", "new": "new password 1"}).status_code == 400
    assert client.post("/api/auth/password", json={"current": OWNER_PW, "new": "short"}).status_code == 400
    assert client.post("/api/auth/password", json={"current": OWNER_PW, "new": "new password 1"}).status_code == 200
    c = new_client()
    assert c.post("/api/auth/login", json={"username": "cam", "password": "new password 1"}).status_code == 200


# ---------------------------------------------------------------- invites and roles

def test_viewer_permissions(client, new_client):
    book = client.post("/api/books", json={"title": "Dune", "notes": "Signed, don't lend"}).json()
    client.post(f"/api/books/{book['id']}/loans", json={"borrower": "Sam"})

    friend = join(new_client, invite(client, "viewer"), "jess", "Jess")
    assert friend.get("/api/auth/me").json()["user"]["role"] == "viewer"

    listed = friend.get("/api/books").json()["books"][0]
    assert listed["notes"] == ""                                       # private notes hidden
    assert listed["active_loan"] == {"out": True, "mine": False, "due_at": None, "borrower": None}  # not who
    assert friend.get(f"/api/books/{book['id']}").json()["loans"] == []
    assert friend.get("/api/loans").json() == []                      # only their own loans

    for method, url, body in [
        ("post", "/api/books", {"title": "X"}),
        ("patch", f"/api/books/{book['id']}", {"title": "X"}),
        ("delete", f"/api/books/{book['id']}", None),
        ("post", "/api/shelves", {"name": "X"}),
        ("post", "/api/quick-add", {"code": "9780547928227"}),
        ("get", "/api/lookup/9780547928227", None),
        ("get", "/api/export.csv", None),
        ("get", "/api/users", None),
        ("post", "/api/invites", {"role": "owner"}),
    ]:
        kwargs = {"json": body} if body is not None else {}
        assert getattr(friend, method)(url, **kwargs).status_code == 403, (method, url)


def test_editor_can_catalog_but_not_manage_people(client, new_client):
    ed = join(new_client, invite(client, "editor"), "mom", "Mom")
    assert ed.post("/api/books", json={"title": "Goodnight Moon"}).status_code == 201
    assert ed.post("/api/quick-add", json={"code": "9780547928227"}).json()["result"] == "added"
    assert ed.get("/api/users").status_code == 403
    assert ed.post("/api/invites", json={"role": "viewer"}).status_code == 403
    b = client.get("/api/books", params={"q": "goodnight"}).json()["books"][0]
    assert client.get(f"/api/books/{b['id']}").json()["added_by_name"] == "Mom"


def test_invite_single_use_and_revoke(client, new_client):
    token = invite(client)
    join(new_client, token, "a1", "A")
    again = new_client().post(f"/api/auth/invite/{token}", json={"username": "a2", "display_name": "B", "password": "password123"})
    assert again.status_code == 404

    t2 = invite(client, note="for Pat")
    inv_id = client.get("/api/invites").json()[0]["id"]
    client.delete(f"/api/invites/{inv_id}")
    assert new_client().get(f"/api/auth/invite/{t2}").status_code == 404

    t3 = invite(client)
    dup = new_client().post(f"/api/auth/invite/{t3}", json={"username": "A1", "display_name": "x", "password": "password123"})
    assert dup.status_code == 409   # usernames are case-insensitive


def test_disable_reset_and_last_owner(client, new_client):
    friend = join(new_client, invite(client), "jess", "Jess")
    jess_id = friend.get("/api/auth/me").json()["user"]["id"]
    me_id = client.get("/api/auth/me").json()["user"]["id"]

    client.patch(f"/api/users/{jess_id}", json={"disabled": True})
    assert friend.get("/api/books").status_code == 401                 # kicked out right away

    link = client.post(f"/api/users/{jess_id}/reset").json()
    info = new_client().get(f"/api/auth/invite/{link['token']}").json()
    assert info["reset"] and info["user"]["username"] == "jess"
    c = new_client()
    assert c.post(f"/api/auth/invite/{link['token']}", json={"username": "jess", "display_name": "Jess", "password": "brand new pw"}).status_code == 200
    assert c.get("/api/auth/me").json()["user"]["disabled"] == 0      # reset re-enables

    assert client.patch(f"/api/users/{me_id}", json={"role": "viewer"}).status_code == 400
    assert client.delete(f"/api/users/{me_id}").status_code == 400
    client.patch(f"/api/users/{jess_id}", json={"role": "owner"})
    assert client.patch(f"/api/users/{me_id}", json={"role": "editor"}).status_code == 200


# ---------------------------------------------------------------- borrow requests

def test_borrow_request_flow(client, new_client):
    book = client.post("/api/books", json={"title": "Dune"}).json()
    friend = join(new_client, invite(client), "jess", "Jess")

    r = friend.post(f"/api/books/{book['id']}/requests", json={"message": "Can I grab it Sunday?"})
    assert r.status_code == 201
    assert friend.post(f"/api/books/{book['id']}/requests", json={}).status_code == 409
    assert friend.get(f"/api/books/{book['id']}").json()["my_request"]["status"] == "pending"
    assert client.get("/api/stats").json()["requests"] == 1

    pending = client.get("/api/requests").json()
    assert pending[0]["display_name"] == "Jess" and pending[0]["message"] == "Can I grab it Sunday?"
    assert friend.post(f"/api/requests/{pending[0]['id']}/approve", json={}).status_code == 403
    loan = client.post(f"/api/requests/{pending[0]['id']}/approve", json={"due_at": "2030-01-01"}).json()
    assert loan["borrower"] == "Jess"

    mine = friend.get("/api/loans").json()
    assert len(mine) == 1 and mine[0]["title"] == "Dune"
    assert friend.get("/api/books").json()["books"][0]["active_loan"]["mine"] is True
    assert friend.get("/api/requests").json()[0]["status"] == "approved"


def test_lending_by_name_links_to_member(client, new_client):
    book = client.post("/api/books", json={"title": "Dune"}).json()
    friend = join(new_client, invite(client), "jess", "Jess")
    client.post(f"/api/books/{book['id']}/loans", json={"borrower": "jess"})
    assert len(friend.get("/api/loans").json()) == 1


# ---------------------------------------------------------------- Home Assistant ingress

def ingress_mode(app, monkeypatch, ingress_ip="testclient"):
    from app import auth
    monkeypatch.setattr(auth, "IS_ADDON", True)
    monkeypatch.setattr(auth, "INGRESS_IP", ingress_ip)
    return auth


def test_ha_users_signed_in_automatically(app, new_client, monkeypatch):
    ingress_mode(app, monkeypatch)
    cam = new_client({"X-Remote-User-Id": "ha1", "X-Remote-User-Display-Name": "Cameron"})
    me = cam.get("/api/auth/me").json()
    assert me["via"] == "homeassistant" and me["user"]["role"] == "owner"   # first in = owner

    spouse = new_client({"X-Remote-User-Id": "ha2", "X-Remote-User-Display-Name": "Spouse"})
    assert spouse.get("/api/auth/me").json()["user"]["role"] == "editor"
    assert spouse.post("/api/books", json={"title": "Dune"}).status_code == 201

    # Same HA person again maps to the same library account.
    assert new_client({"X-Remote-User-Id": "ha1"}).get("/api/auth/me").json()["user"]["id"] == me["user"]["id"]
    # No setup screen on the add-on: the owner comes from the sidebar.
    assert cam.post("/api/auth/setup", json={"username": "sneaky", "display_name": "x", "password": "password123"}).status_code == 403


def test_ha_headers_ignored_from_anywhere_else(app, new_client, monkeypatch):
    ingress_mode(app, monkeypatch, ingress_ip="172.30.32.2")   # test client is NOT the Supervisor
    forged = new_client({"X-Remote-User-Id": "ha1", "X-Remote-User-Display-Name": "Cameron",
                         "X-Forwarded-For": "172.30.32.2"})
    assert forged.get("/api/books").status_code == 401
    assert forged.get("/api/auth/me").json()["user"] is None


def test_ha_headers_ignored_when_not_an_addon(app, new_client):
    forged = new_client({"X-Remote-User-Id": "ha1"})
    assert forged.get("/api/books").status_code == 401


def test_ha_user_can_add_a_password(app, new_client, monkeypatch):
    auth = ingress_mode(app, monkeypatch)
    cam = new_client({"X-Remote-User-Id": "ha1", "X-Remote-User-Display-Name": "Cameron"})
    r = cam.post("/api/auth/password", json={"username": "cam", "new": "outside password"})
    assert r.status_code == 200
    monkeypatch.setattr(auth, "INGRESS_IP", "172.30.32.2")
    outside = new_client()
    assert outside.post("/api/auth/login", json={"username": "cam", "password": "outside password"}).status_code == 200
    assert outside.get("/api/auth/me").json()["user"]["role"] == "owner"


def test_invite_from_ingress_needs_public_address(app, new_client, monkeypatch):
    ingress_mode(app, monkeypatch)
    cam = new_client({"X-Remote-User-Id": "ha1"})
    assert cam.post("/api/invites", json={"role": "viewer"}).json()["url"] is None
    cam.put("/api/settings", json={"public_url": "https://library.example.com/"})
    url = cam.post("/api/invites", json={"role": "viewer"}).json()["url"]
    assert url.startswith("https://library.example.com/#/invite/")
    assert cam.put("/api/settings", json={"public_url": "library.example.com"}).status_code == 400
