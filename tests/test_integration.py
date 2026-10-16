"""Home Assistant events, notifications, button taps and sensor data (network stubbed)."""
import sqlite3

import pytest

from conftest import CSRF


@pytest.fixture()
def hass(app, monkeypatch):
    """Capture what the library would send to Home Assistant."""
    from app import events, ha, mqtt_bridge
    sent = {"events": [], "notify": [], "mqtt_events": []}
    monkeypatch.setattr(ha, "available", lambda: True)
    monkeypatch.setattr(ha, "fire_event", lambda t, d: sent["events"].append((t, d)) or True)
    monkeypatch.setattr(ha, "notify", lambda svcs, title, msg, data=None: sent["notify"].append(
        {"services": list(svcs), "title": title, "message": msg, "data": data or {}}) or len(svcs))
    monkeypatch.setattr(ha, "_addon_info", {"slug": "abc_home_library"})
    monkeypatch.setattr(mqtt_bridge, "publish_event", lambda k, d: sent["mqtt_events"].append(k))
    monkeypatch.setenv("NOTIFY_SERVICES", "mobile_app_cam, notify.mobile_app_spouse")
    monkeypatch.setenv("NOTIFY_ON_RETURN", "true")
    monkeypatch.setenv("LOAN_DAYS", "21")
    events._settings = None

    def run():
        while not events._queue.empty():
            events._handle(*events._queue.get_nowait())
        return sent
    sent["run"] = run
    return sent


def _friend(client, new_client, name="Jess"):
    token = client.post("/api/invites", json={"role": "viewer"}).json()["token"]
    c = new_client()
    c.post(f"/api/auth/invite/{token}", json={"username": name.lower(), "display_name": name, "password": "password123"})
    return c


def test_settings_from_addon_options(hass):
    from app import events
    s = events.settings()
    assert s.notify_services == ["mobile_app_cam", "mobile_app_spouse"]   # "notify." prefix tolerated
    assert s.loan_days == 21 and s.notify_on_return is True


def test_request_event_and_actionable_notification(client, new_client, hass):
    client.put("/api/settings", json={"public_url": "https://library.example.com"})
    book = client.post("/api/books", json={"title": "Dune", "authors": "Frank Herbert"}).json()
    jess = _friend(client, new_client)
    req = jess.post(f"/api/books/{book['id']}/requests", json={"message": "Sunday?"}).json()
    sent = hass["run"]()
    kinds = [t for t, _ in sent["events"]]
    assert kinds == ["home_library_book_added", "home_library_member_joined", "home_library_borrow_requested"]
    data = sent["events"][-1][1]
    assert data == {"request_id": req["id"], "requester": "Jess", "message": "Sunday?", "book_id": book["id"],
                    "title": "Dune", "authors": "Frank Herbert", "url": f"https://library.example.com/#/book/{book['id']}",
                    "ha_path": "/hassio/ingress/abc_home_library"}
    n = sent["notify"][-1]
    assert n["services"] == ["mobile_app_cam", "mobile_app_spouse"]
    assert n["title"] == "Jess wants to borrow a book" and '"Sunday?"' in n["message"]
    assert [a["action"] for a in n["data"]["actions"]] == [f"HOME_LIBRARY_APPROVE_{req['id']}", f"HOME_LIBRARY_DECLINE_{req['id']}"]
    assert n["data"]["actions"][0]["title"] == "Lend it (21 days)"
    assert n["data"]["tag"] == f"home_library_request_{req['id']}"
    assert "borrow_requested" in sent["mqtt_events"]


def test_notification_tap_approves_and_clears(app, new_client, hass, monkeypatch):
    from app import auth, ha
    monkeypatch.setattr(auth, "IS_ADDON", True)
    monkeypatch.setattr(auth, "INGRESS_IP", "testclient")
    cam = new_client({"X-Remote-User-Id": "ha-cam"}); cam.get("/api/auth/me")       # owner via sidebar
    kid = new_client({"X-Remote-User-Id": "ha-kid"}); kid_id = kid.get("/api/auth/me").json()["user"]["id"]
    cam.patch(f"/api/users/{kid_id}", json={"role": "viewer"})
    book = cam.post("/api/books", json={"title": "Dune"}).json()
    token = cam.post("/api/invites", json={"role": "viewer"}).json()["token"]
    monkeypatch.setattr(auth, "INGRESS_IP", "172.30.32.2")   # friends don't come through the sidebar
    jess = new_client()
    jess.post(f"/api/auth/invite/{token}", json={"username": "jess", "display_name": "Jess", "password": "password123"})
    req = jess.post(f"/api/books/{book['id']}/requests", json={}).json()
    hass["run"]()

    assert ha.handle_action(f"HOME_LIBRARY_APPROVE_{req['id']}", "ha-kid") == "not allowed"
    assert ha.handle_action("SOMETHING_ELSE", "ha-cam") == "ignored"
    assert ha.handle_action(f"HOME_LIBRARY_APPROVE_{req['id']}", "ha-cam") == "approved"
    assert ha.handle_action(f"HOME_LIBRARY_APPROVE_{req['id']}", "ha-cam").startswith("skipped")   # double tap
    loans = jess.get("/api/loans").json()
    assert len(loans) == 1 and loans[0]["title"] == "Dune"
    sent = hass["run"]()
    lent = [d for t, d in sent["events"] if t == "home_library_book_lent"][-1]
    assert lent["via"] == "notification" and lent["borrower"] == "Jess"
    clears = [n for n in sent["notify"] if n["message"] == "clear_notification"]
    assert clears and clears[-1]["data"]["tag"] == f"home_library_request_{req['id']}"


def test_decline_tap_from_unlinked_ha_user(client, new_client, hass):
    from app import ha
    book = client.post("/api/books", json={"title": "Dune"}).json()
    jess = _friend(client, new_client)
    req = jess.post(f"/api/books/{book['id']}/requests", json={}).json()
    assert ha.handle_action(f"HOME_LIBRARY_DECLINE_{req['id']}", "someone-not-in-library") == "declined"
    assert jess.get("/api/requests").json()[0]["status"] == "declined"
    sent = hass["run"]()
    assert ("home_library_request_declined" in [t for t, _ in sent["events"]])


def test_dashboard_buttons(client, new_client, hass):
    from app import mqtt_bridge
    a = client.post("/api/books", json={"title": "Dune"}).json()
    b = client.post("/api/books", json={"title": "Emma"}).json()
    jess = _friend(client, new_client)
    sam = _friend(client, new_client, "Sam")
    jess.post(f"/api/books/{a['id']}/requests", json={})
    sam.post(f"/api/books/{b['id']}/requests", json={})
    mqtt_bridge._on_command("approve_oldest")
    assert [l["title"] for l in client.get("/api/loans").json()] == ["Dune"]
    mqtt_bridge._on_command("decline_oldest")
    assert client.get("/api/requests").json() == []
    mqtt_bridge._on_command("approve_oldest")          # nothing left: no error


def test_return_and_overdue_digest(client, app, hass):
    from app import db, events
    book = client.post("/api/books", json={"title": "Dune"}).json()
    loan = client.post(f"/api/books/{book['id']}/loans", json={"borrower": "Sam", "due_at": "2026-01-01"}).json()
    hass["run"]()
    assert events.check_overdue(force=True) == 1
    sent = hass["run"]()
    od = [d for t, d in sent["events"] if t == "home_library_loan_overdue"][0]
    assert od["borrower"] == "Sam" and od["days_overdue"] > 0
    digest = [n for n in sent["notify"] if "overdue" in n["title"]][0]
    assert digest["title"] == "1 library book is overdue" and "Dune: Sam" in digest["message"]
    assert events.check_overdue() == 0          # once a day
    client.post(f"/api/loans/{loan['id']}/return")
    sent = hass["run"]()
    assert [n["message"] for n in sent["notify"]][-1] == "Sam returned Dune"


def test_snapshot_for_sensors(client, new_client, hass):
    from app import events
    client.post("/api/books", json={"title": "Dune", "authors": "Frank Herbert"})
    client.post("/api/books", json={"title": "Emma", "status": "wishlist"})
    snap = events.snapshot()
    assert snap["books"] == 1 and snap["wishlist"] == 1 and snap["last_added"] == "Dune"
    assert snap["last_added_info"]["authors"] == "Frank Herbert"
    assert snap["on_loan"] == 0 and snap["overdue"] == 0 and snap["requests"] == 0


def test_discovery_payload(client, hass):
    from app import mqtt_bridge
    client.put("/api/branding", json={"name": "The Boote Library"})
    p = mqtt_bridge.discovery_payload()
    assert p["dev"]["name"] == "The Boote Library"
    assert p["dev"]["cu"] == "homeassistant://hassio/ingress/abc_home_library"
    ids = sorted(c["default_entity_id"] for c in p["cmps"].values())
    assert ids == ["binary_sensor.library_overdue", "button.library_decline_oldest_request",
                   "button.library_lend_oldest_request", "sensor.library_books", "sensor.library_borrow_requests",
                   "sensor.library_last_added", "sensor.library_on_loan"]
    assert all(c["unique_id"].startswith("home_library_") for c in p["cmps"].values())
    assert mqtt_bridge.discovery_topic().startswith("homeassistant/device/home_library_")


def test_status_endpoint_is_owner_only(client, new_client, hass):
    s = client.get("/api/integration").json()
    assert s["notify_services"] == ["mobile_app_cam", "mobile_app_spouse"] and "sensor.library_books" in s["entities"]
    friend = _friend(client, new_client)
    assert friend.get("/api/integration").status_code == 403
    assert client.post("/api/integration/test").json()["notifications_sent"] == 2
