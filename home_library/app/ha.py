"""Talking to Home Assistant through the Supervisor (add-on installs only).

* fire_event / notify use Home Assistant's REST API.
* A websocket listener watches for taps on our notification buttons
  (HOME_LIBRARY_APPROVE_<id> / HOME_LIBRARY_DECLINE_<id>) and acts on them, so no
  reply automation is needed.
* mqtt_credentials asks the Supervisor for the Mosquitto add-on's login, the same way
  other add-ons such as Zigbee2MQTT do.
"""
import asyncio
import json
import logging
import os
import re
import threading

import httpx

from . import auth, db

log = logging.getLogger("library.ha")

SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor").rstrip("/")
ACTION_RE = re.compile(r"^HOME_LIBRARY_(APPROVE|DECLINE)_(\d+)$")

state = {
    "api": "not an add-on",       # ok | error: ... | not an add-on
    "listener": "not running",    # connected | connecting | error: ...
    "last_action": None,
}
_addon_info: dict = {}


def token() -> str:
    return os.environ.get("SUPERVISOR_TOKEN", "")


def available() -> bool:
    return bool(token())


def _client() -> httpx.Client:
    return httpx.Client(base_url=SUPERVISOR_URL, timeout=10,
                        headers={"Authorization": f"Bearer {token()}"})


def _supervisor_get(path: str) -> dict | None:
    if not available():
        return None
    try:
        with _client() as c:
            r = c.get(path)
        if r.status_code == 200:
            body = r.json()
            return body.get("data", body)
        log.info("Supervisor %s -> HTTP %s", path, r.status_code)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Supervisor %s failed: %s", path, e)
    return None


def addon_info() -> dict:
    global _addon_info
    if not _addon_info and available():
        _addon_info = _supervisor_get("/addons/self/info") or {}
    return _addon_info


def panel_path() -> str | None:
    """Path to the Library panel inside the Home Assistant app."""
    slug = addon_info().get("slug")
    return f"/hassio/ingress/{slug}" if slug else None


def mqtt_credentials() -> dict | None:
    """Broker details from the Supervisor (needs `services: mqtt:want`)."""
    d = _supervisor_get("/services/mqtt")
    if not d or not d.get("host"):
        return None
    return {"host": d["host"], "port": int(d.get("port") or 1883), "username": d.get("username"),
            "password": d.get("password"), "ssl": bool(d.get("ssl"))}


def _core_post(path: str, payload: dict) -> bool:
    if not available():
        return False
    try:
        with _client() as c:
            r = c.post(f"/core/api{path}", json=payload)
        if r.status_code < 300:
            state["api"] = "ok"
            return True
        state["api"] = f"error: HTTP {r.status_code} from Home Assistant"
        log.warning("Home Assistant %s -> HTTP %s %s", path, r.status_code, r.text[:200])
    except httpx.HTTPError as e:
        state["api"] = f"error: {e}"
        log.warning("Home Assistant %s failed: %s", path, e)
    return False


def fire_event(event_type: str, data: dict) -> bool:
    return _core_post(f"/events/{event_type}", data)


def notify(services: list[str], title: str, message: str, data: dict | None = None) -> int:
    sent = 0
    payload = {"message": message, **({"title": title} if title else {}), **({"data": data} if data else {})}
    for svc in services:
        if _core_post(f"/services/notify/{svc}", payload):
            sent += 1
    return sent


# ---------------------------------------------------------------- button taps

def _library_user_for(ha_user_id: str | None) -> dict | None:
    if not ha_user_id:
        return None
    with db.connect() as conn:
        return db.row(conn.execute("SELECT * FROM users WHERE ha_user_id = ?", (ha_user_id,)))


def handle_action(action: str, ha_user_id: str | None) -> str:
    """Act on a notification button. Returns a short description (for logs/tests)."""
    from . import ops
    m = ACTION_RE.match(action or "")
    if not m:
        return "ignored"
    verb, request_id = m.group(1), int(m.group(2))
    user = _library_user_for(ha_user_id)
    # A Home Assistant user who is linked to a library Friend account can't approve.
    # Unlinked HA users are trusted: only HA users and automations can tap these.
    if user and (user["disabled"] or not auth.can(user, "editor")):
        log.warning("Ignored %s from %s: not allowed to lend books", action, user["display_name"])
        return "not allowed"
    decided_by = user["id"] if user else None
    try:
        if verb == "APPROVE":
            ops.approve_request(request_id, decided_by, ops.default_due_date(), via="notification")
            result = "approved"
        else:
            ops.decline_request(request_id, decided_by, via="notification")
            result = "declined"
    except Exception as e:  # already handled, book already out, etc.
        detail = getattr(e, "detail", str(e))
        log.info("Notification action %s: %s", action, detail)
        result = f"skipped: {detail}"
    state["last_action"] = {"action": action, "result": result}
    return result


async def _listen_forever():
    import websockets
    url = SUPERVISOR_URL.replace("http://", "ws://").replace("https://", "wss://") + "/core/websocket"
    delay = 2
    while True:
        try:
            state["listener"] = "connecting"
            async with websockets.connect(url, max_size=2 ** 22, ping_interval=30) as ws:
                hello = json.loads(await ws.recv())
                if hello.get("type") == "auth_required":
                    await ws.send(json.dumps({"type": "auth", "access_token": token()}))
                    reply = json.loads(await ws.recv())
                    if reply.get("type") != "auth_ok":
                        raise RuntimeError(f"Home Assistant refused the connection: {reply.get('message', reply)}")
                await ws.send(json.dumps({"id": 1, "type": "subscribe_events",
                                          "event_type": "mobile_app_notification_action"}))
                state["listener"] = "connected"
                delay = 2
                log.info("Listening for notification button taps")
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "event":
                        continue
                    ev = msg.get("event", {})
                    action = (ev.get("data") or {}).get("action", "")
                    if ACTION_RE.match(action):
                        user_id = (ev.get("context") or {}).get("user_id")
                        result = await asyncio.to_thread(handle_action, action, user_id)
                        log.info("Notification button %s -> %s", action, result)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            state["listener"] = f"error: {e}"
            log.warning("Home Assistant websocket: %s (retrying in %ss)", e, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


_thread: threading.Thread | None = None


def start_listener() -> None:
    global _thread
    if not available() or (_thread and _thread.is_alive()):
        return
    state["api"] = "ok"
    _thread = threading.Thread(target=lambda: asyncio.run(_listen_forever()), name="ha-listener", daemon=True)
    _thread.start()
