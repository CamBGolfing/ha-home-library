"""Library name and logo."""
import struct
import zlib

from conftest import CSRF


def png(size=16, rgb=(200, 40, 40)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))  # noqa: E731
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def logo_files(data=None):
    data = data or png()
    return {"logo_512": ("a.png", data, "image/png"), "logo_192": ("b.png", data, "image/png")}


def test_defaults_are_public(new_client):
    c = new_client()   # signed out: the sign-in page needs the name and logo
    b = c.get("/api/branding").json()
    assert b["name"] == "Home Library" and b["custom_logo"] is False
    assert c.get("/" + b["icon_url"]).headers["content-type"] == "image/png"
    page = c.get("/").text
    assert "<title>Home Library</title>" in page and "{{" not in page
    assert c.get("/manifest.webmanifest").json()["name"] == "Home Library"


def test_owner_renames_and_it_shows_everywhere(client, new_client):
    r = client.put("/api/branding", json={"name": "The Boote Library", "short_name": "Bootes"})
    assert r.status_code == 200 and r.json()["short_name"] == "Bootes"
    page = new_client().get("/").text
    assert "<title>The Boote Library</title>" in page
    assert 'content="Bootes"' in page
    m = new_client().get("/manifest.webmanifest").json()
    assert m["name"] == "The Boote Library" and m["short_name"] == "Bootes"


def test_short_name_defaults_sensibly(client):
    assert client.put("/api/branding", json={"name": "Reads", "short_name": ""}).json()["short_name"] == "Reads"
    long = client.put("/api/branding", json={"name": "The Very Long Family Library", "short_name": ""}).json()
    assert long["short_name"] == "Library" and long["custom_short"] is False


def test_name_is_escaped(client, new_client):
    client.put("/api/branding", json={"name": '<script>alert(1)</script>"x'})
    page = new_client().get("/").text
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page


def test_logo_upload_and_reset(client, new_client):
    data = png(rgb=(10, 200, 10))
    b = client.post("/api/branding/logo", files=logo_files(data)).json()
    assert b["custom_logo"] is True and b["version"] != "default"
    anon = new_client()
    served = anon.get("/" + b["icon_url"])
    assert served.content == data and "immutable" in served.headers["cache-control"]
    assert b["version"] in anon.get("/").text                          # favicon/touch icon cache-busted
    assert anon.get("/manifest.webmanifest").json()["icons"][0]["src"].startswith("branding/logo-512.png")

    after = client.delete("/api/branding/logo").json()
    assert after["custom_logo"] is False
    assert anon.get("/branding/logo-192.png").content != data


def test_logo_must_be_png(client):
    bad = client.post("/api/branding/logo", files=logo_files(b"<svg onload=alert(1)></svg>"))
    assert bad.status_code == 400
    big = client.post("/api/branding/logo", files=logo_files(b"\x89PNG\r\n\x1a\n" + b"0" * (3 * 1024 * 1024)))
    assert big.status_code == 413
    assert client.get("/api/branding").json()["custom_logo"] is False
    assert client.get("/branding/logo-64.png").status_code == 404


def test_only_owner_can_change_branding(client, new_client):
    token = client.post("/api/invites", json={"role": "editor"}).json()["token"]
    ed = new_client()
    ed.post(f"/api/auth/invite/{token}", json={"username": "mom", "display_name": "Mom", "password": "password123"})
    assert ed.put("/api/branding", json={"name": "Mine now"}).status_code == 403
    assert ed.post("/api/branding/logo", files=logo_files()).status_code == 403
    assert ed.delete("/api/branding/logo").status_code == 403
    assert new_client().put("/api/branding", json={"name": "Anon"}, headers=CSRF).status_code == 401


# ---------------------------------------------------------------- appearance

def test_library_default_is_light_and_owner_can_change_it(client, new_client):
    assert client.get("/api/branding").json()["default_theme"] == "light"
    assert 'data-theme="light"' in new_client().get("/").text
    client.put("/api/branding", json={"name": "Home Library", "default_theme": "auto"})
    assert 'data-theme="auto"' in new_client().get("/").text
    assert client.put("/api/branding", json={"name": "X", "default_theme": "purple"}).status_code == 400
    # Leaving it out keeps the current default.
    client.put("/api/branding", json={"name": "Renamed"})
    assert client.get("/api/branding").json()["default_theme"] == "auto"


def test_everyone_picks_their_own_appearance(client, new_client):
    token = client.post("/api/invites", json={"role": "viewer"}).json()["token"]
    friend = new_client()
    friend.post(f"/api/auth/invite/{token}", json={"username": "jess", "display_name": "Jess", "password": "password123"})
    assert friend.get("/api/auth/me").json()["user"]["theme"] == ""          # follows the library default
    assert friend.put("/api/auth/appearance", json={"theme": "dark"}).status_code == 200
    assert friend.get("/api/auth/me").json()["user"]["theme"] == "dark"
    assert client.get("/api/auth/me").json()["user"]["theme"] == ""          # only theirs changed
    assert friend.put("/api/auth/appearance", json={"theme": "neon"}).status_code == 400
    friend.put("/api/auth/appearance", json={"theme": ""})
    assert friend.get("/api/auth/me").json()["user"]["theme"] == ""
    assert new_client().put("/api/auth/appearance", json={"theme": "dark"}).status_code == 401
