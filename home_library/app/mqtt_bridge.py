"""Home Library as an MQTT device with Home Assistant discovery.

One retained discovery message creates a "Home Library" device with sensors and
buttons. State is a single retained JSON document; each entity picks its value out of
it. Retained messages mean the entities have their values again right after a Home
Assistant restart, and the broker's last-will marks them unavailable if the library
stops.
"""
import json
import logging
import os
import secrets
import threading

from . import APP_VERSION, db

log = logging.getLogger("library.mqtt")

DISCOVERY_PREFIX = os.environ.get("MQTT_DISCOVERY_PREFIX", "homeassistant")
REPO_URL = "https://github.com/CamBGolfing/ha-home-library"

state = {"status": "off", "broker": None, "error": None}
_client = None
_lock = threading.Lock()
_last_state: dict | None = None


def _instance_id() -> str:
    with db.connect() as conn:
        iid = db.get_setting(conn, "instance_id")
        if not iid:
            iid = secrets.token_hex(4)
            db.set_setting(conn, "instance_id", iid)
        return iid


def base_topic() -> str:
    return f"home_library/{_instance_id()}"


def broker_config() -> dict | None:
    """Manual settings (env) win; otherwise ask the Supervisor for Mosquitto's login."""
    from . import events, ha
    if not events.settings().mqtt_enabled:
        return None
    host = os.environ.get("MQTT_HOST", "").strip()
    if host:
        return {"host": host, "port": int(os.environ.get("MQTT_PORT", "1883") or 1883),
                "username": os.environ.get("MQTT_USERNAME") or None,
                "password": os.environ.get("MQTT_PASSWORD") or None,
                "ssl": os.environ.get("MQTT_SSL", "").lower() in ("1", "true", "yes")}
    return ha.mqtt_credentials()


def discovery_payload() -> dict:
    from . import ha
    from .main import _branding
    b = _branding()
    iid = _instance_id()
    base = base_topic()
    st = f"{base}/state"
    avail = f"{base}/status"
    with db.connect() as conn:
        public = db.get_setting(conn, "public_url").rstrip("/")
    slug = ha.addon_info().get("slug")
    config_url = f"homeassistant://hassio/ingress/{slug}" if slug else (public or None)

    def uid(key):
        return f"home_library_{iid}_{key}"

    def attrs(expr):
        return {"json_attributes_topic": st, "json_attributes_template": "{{ " + expr + " | tojson }}"}

    cmps = {
        uid("books"): {
            "p": "sensor", "name": "Books", "unique_id": uid("books"), "default_entity_id": "sensor.library_books",
            "icon": "mdi:bookshelf", "state_topic": st, "value_template": "{{ value_json.books }}",
            "unit_of_measurement": "books", "state_class": "measurement",
            **attrs("{'copies': value_json.copies, 'wishlist': value_json.wishlist, "
                    "'shelves': value_json.shelves, 'members': value_json.members}"),
        },
        uid("on_loan"): {
            "p": "sensor", "name": "On loan", "unique_id": uid("on_loan"), "default_entity_id": "sensor.library_on_loan",
            "icon": "mdi:book-arrow-right", "state_topic": st, "value_template": "{{ value_json.on_loan }}",
            "unit_of_measurement": "books", "state_class": "measurement",
            **attrs("{'loans': value_json.loans}"),
        },
        uid("overdue"): {
            "p": "binary_sensor", "name": "Overdue books", "unique_id": uid("overdue"),
            "default_entity_id": "binary_sensor.library_overdue", "device_class": "problem",
            "state_topic": st, "value_template": "{{ 'ON' if value_json.overdue > 0 else 'OFF' }}",
            **attrs("{'count': value_json.overdue, 'loans': value_json.overdue_loans}"),
        },
        uid("requests"): {
            "p": "sensor", "name": "Borrow requests", "unique_id": uid("requests"),
            "default_entity_id": "sensor.library_borrow_requests", "icon": "mdi:hand-extended",
            "state_topic": st, "value_template": "{{ value_json.requests }}", "state_class": "measurement",
            **attrs("{'requests': value_json.request_list}"),
        },
        uid("last_added"): {
            "p": "sensor", "name": "Last added", "unique_id": uid("last_added"),
            "default_entity_id": "sensor.library_last_added", "icon": "mdi:book-plus",
            "state_topic": st, "value_template": "{{ value_json.last_added[:250] }}",
            **attrs("value_json.last_added_info"),
        },
        uid("approve_oldest"): {
            "p": "button", "name": "Lend to oldest request", "unique_id": uid("approve_oldest"),
            "default_entity_id": "button.library_lend_oldest_request", "icon": "mdi:book-check",
            "command_topic": f"{base}/command", "payload_press": "approve_oldest",
        },
        uid("decline_oldest"): {
            "p": "button", "name": "Decline oldest request", "unique_id": uid("decline_oldest"),
            "default_entity_id": "button.library_decline_oldest_request", "icon": "mdi:book-cancel",
            "command_topic": f"{base}/command", "payload_press": "decline_oldest",
        },
    }
    return {
        "dev": {"ids": [f"home_library_{iid}"], "name": b["name"], "mf": "Home Library",
                "mdl": "Book catalog", "sw": APP_VERSION, **({"cu": config_url} if config_url else {})},
        "o": {"name": "Home Library", "sw": APP_VERSION, "url": REPO_URL},
        "availability_topic": avail, "payload_available": "online", "payload_not_available": "offline",
        "cmps": cmps,
    }


def discovery_topic() -> str:
    return f"{DISCOVERY_PREFIX}/device/home_library_{_instance_id()}/config"


def _publish(topic: str, payload, retain: bool = False) -> None:
    if _client is None or state["status"] != "connected":
        return
    if not isinstance(payload, (str, bytes)):
        payload = json.dumps(payload, default=str)
    _client.publish(topic, payload, qos=1, retain=retain)


def publish_discovery() -> None:
    _publish(discovery_topic(), discovery_payload(), retain=True)


def publish_state(snap: dict) -> None:
    global _last_state
    _last_state = snap
    _publish(f"{base_topic()}/state", snap, retain=True)


def publish_event(kind: str, details: dict) -> None:
    """Also mirror events to MQTT, for people who prefer MQTT triggers."""
    _publish(f"{base_topic()}/event/{kind}", details)


def _on_command(payload: str) -> None:
    from . import events, ops
    cmd = payload.strip().lower()
    r = ops.oldest_pending_request(lendable_only=(cmd == "approve_oldest"))
    if not r:
        log.info("MQTT %s: no pending requests", cmd)
        return
    try:
        if cmd == "approve_oldest":
            ops.approve_request(r["id"], None, ops.default_due_date(), via="mqtt")
        elif cmd == "decline_oldest":
            ops.decline_request(r["id"], None, via="mqtt")
        else:
            log.info("Unknown MQTT command %r", payload)
    except Exception as e:
        log.info("MQTT %s skipped: %s", cmd, getattr(e, "detail", e))
    events.touch()


def start() -> None:
    """Connect in the background; paho handles reconnects."""
    global _client
    with _lock:
        if _client is not None:
            return
        cfg = broker_config()
        if not cfg:
            state.update(status="off", error=None)
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            state.update(status="error", error="paho-mqtt is not installed")
            return
        base = base_topic()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"home-library-{_instance_id()}")
        if cfg.get("username"):
            client.username_pw_set(cfg["username"], cfg.get("password"))
        if cfg.get("ssl"):
            client.tls_set()
        client.will_set(f"{base}/status", "offline", qos=1, retain=True)
        client.reconnect_delay_set(min_delay=2, max_delay=60)

        def on_connect(c, _userdata, _flags, reason, _props):
            if reason.is_failure:
                state.update(status="error", error=f"broker refused the connection ({reason})")
                return
            state.update(status="connected", error=None)
            log.info("Connected to MQTT broker %s:%s", cfg["host"], cfg["port"])
            c.subscribe(f"{base}/command", qos=1)
            c.subscribe(f"{DISCOVERY_PREFIX}/status", qos=1)   # Home Assistant's birth message
            c.publish(f"{base}/status", "online", qos=1, retain=True)
            publish_discovery()
            if _last_state:
                publish_state(_last_state)
            from . import events
            events.touch()

        def on_disconnect(_c, _userdata, _flags, reason, _props):
            if state["status"] == "connected":
                log.warning("MQTT disconnected (%s), reconnecting", reason)
            state.update(status="connecting", error=None if not reason.is_failure else str(reason))

        def on_message(_c, _userdata, msg):
            payload = msg.payload.decode("utf-8", "replace")
            if msg.topic == f"{base}/command":
                threading.Thread(target=_on_command, args=(payload,), daemon=True).start()
            elif msg.topic == f"{DISCOVERY_PREFIX}/status" and payload == "online":
                # Home Assistant restarted: send discovery again so nothing goes missing.
                publish_discovery()
                if _last_state:
                    publish_state(_last_state)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        state.update(status="connecting", broker=f"{cfg['host']}:{cfg['port']}", error=None)
        try:
            client.connect_async(cfg["host"], cfg["port"], keepalive=60)
            client.loop_start()
        except Exception as e:
            state.update(status="error", error=str(e))
            return
        _client = client


def refresh_discovery() -> None:
    """Call after the library is renamed so the device name follows."""
    publish_discovery()


def stop() -> None:
    global _client
    with _lock:
        if _client is None:
            return
        try:
            _client.publish(f"{base_topic()}/status", "offline", qos=1, retain=True).wait_for_publish(2)
        except Exception:
            pass
        _client.loop_stop()
        _client.disconnect()
        _client = None
        state.update(status="off")
