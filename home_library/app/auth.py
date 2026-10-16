"""Who is making this request, and what are they allowed to do.

Two ways in:

* Home Assistant ingress (the sidebar panel). The Supervisor has already
  authenticated the person and passes their HA user in X-Remote-User-* headers,
  stripping any copies the browser tried to send. We only believe those headers
  when the TCP connection itself comes from the Supervisor's ingress address and
  we are actually running as an add-on.

* Everything else (the direct port, a Cloudflare tunnel, plain Docker). The person
  signs in with a library account and gets a session cookie. Library accounts are
  created from invite links, so friends never need a Home Assistant login.
"""
import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request

from . import db

ROLES = ("viewer", "editor", "owner")
RANK = {r: i for i, r in enumerate(ROLES)}

SESSION_COOKIE = "hl_session"
SESSION_DAYS = 30
INVITE_DAYS = 7
CSRF_HEADER = "x-library-request"

# The Supervisor proxies ingress traffic from this fixed address on the hassio network.
INGRESS_IP = os.environ.get("INGRESS_IP", "172.30.32.2")
IS_ADDON = bool(os.environ.get("SUPERVISOR_TOKEN")) or os.environ.get("LIBRARY_TRUST_INGRESS") == "1"
HA_USER_ROLE = os.environ.get("HA_USER_ROLE", "editor")
if HA_USER_ROLE not in ROLES:
    HA_USER_ROLE = "editor"


# ---------------------------------------------------------------- passwords

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pw.encode(), salt=salt, dklen=32, **_SCRYPT)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${b64(salt)}${b64(digest)}"


def verify_password(pw: str, stored: str | None) -> bool:
    if not stored:
        # Burn similar time so missing accounts aren't distinguishable by timing.
        hashlib.scrypt(pw.encode(), salt=b"0" * 16, dklen=32, **_SCRYPT)
        return False
    try:
        _, n, r, p, salt, digest = stored.split("$")
        check = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), dklen=32, n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(check, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def check_password_rules(pw: str) -> None:
    if len(pw) < 8:
        raise HTTPException(400, "Use at least 8 characters for the password")
    if len(pw) > 200:
        raise HTTPException(400, "That password is too long")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------- login throttling

_failures: dict[str, deque] = defaultdict(deque)
FAIL_WINDOW = 15 * 60
FAIL_LIMIT = 10


def throttle_check(key: str) -> None:
    q = _failures[key]
    now = time.monotonic()
    while q and now - q[0] > FAIL_WINDOW:
        q.popleft()
    if len(q) >= FAIL_LIMIT:
        raise HTTPException(429, "Too many sign-in attempts. Wait a few minutes and try again.")


def throttle_fail(key: str) -> None:
    _failures[key].append(time.monotonic())


def throttle_clear(key: str) -> None:
    _failures.pop(key, None)


# ---------------------------------------------------------------- identity

def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def is_ingress(request: Request) -> bool:
    return IS_ADDON and client_ip(request) == INGRESS_IP


def public_user(u: dict | None) -> dict | None:
    if not u:
        return None
    return {k: u[k] for k in ("id", "username", "display_name", "role", "disabled", "created_at", "last_seen")} | {
        "has_password": bool(u.get("password_hash")),
        "ha_linked": bool(u.get("ha_user_id")),
        "theme": u.get("theme") or "",
    }


def _ha_user(conn, request: Request) -> dict:
    ha_id = request.headers.get("x-remote-user-id") or "__homeassistant__"
    name = (request.headers.get("x-remote-user-display-name")
            or request.headers.get("x-remote-user-name") or "Home Assistant").strip()
    u = db.row(conn.execute("SELECT * FROM users WHERE ha_user_id = ?", (ha_id,)))
    if u is None:
        # The first person to open the library becomes its owner. On a fresh add-on
        # install that's whoever installed it, from the HA sidebar.
        has_owner = conn.execute("SELECT 1 FROM users WHERE role = 'owner' AND disabled = 0").fetchone()
        role = HA_USER_ROLE if has_owner else "owner"
        cur = conn.execute("INSERT INTO users(display_name, role, ha_user_id) VALUES (?, ?, ?)", (name, role, ha_id))
        u = db.row(conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)))
    elif name and name != u["display_name"] and not u["username"]:
        conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (name, u["id"]))
        u["display_name"] = name
    return u


def resolve(request: Request) -> dict | None:
    """Return the current user (dict with 'via'), or None if signed out."""
    cached = getattr(request.state, "user", "unset")
    if cached != "unset":
        return cached
    user = None
    with db.connect() as conn:
        if is_ingress(request):
            user = _ha_user(conn, request)
            user["via"] = "homeassistant"
        else:
            token = request.cookies.get(SESSION_COOKIE)
            if token:
                user = db.row(conn.execute(
                    "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                    "WHERE s.token_hash = ? AND s.expires_at > datetime('now')", (token_hash(token),)))
                if user:
                    user["via"] = "password"
        if user and user["disabled"]:
            user = None
        if user and (not user["last_seen"] or user["last_seen"][:13] != time.strftime("%Y-%m-%d %H", time.gmtime())):
            conn.execute("UPDATE users SET last_seen = datetime('now') WHERE id = ?", (user["id"],))
    request.state.user = user
    return user


def setup_needed() -> bool:
    """Standalone installs start with no accounts; the first visitor creates the owner.

    On Home Assistant the owner comes from the sidebar instead, so this never opens
    a door on the direct port.
    """
    if IS_ADDON:
        return False
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def create_session(conn, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?, ?, datetime('now', ?))",
        (token_hash(token), user_id, f"+{SESSION_DAYS} days"))
    return token


def set_session_cookie(response, request: Request, token: str) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=secure, path="/")


# ---------------------------------------------------------------- dependencies

def current_user(request: Request) -> dict:
    user = resolve(request)
    if not user:
        raise HTTPException(401, "Sign in to continue")
    return user


def require(min_role: str):
    def dep(user: dict = Depends(current_user)) -> dict:
        if RANK.get(user["role"], -1) < RANK[min_role]:
            raise HTTPException(403, "Your account can't do that. Ask the library owner for access.")
        return user
    return dep


viewer = require("viewer")
editor = require("editor")
owner = require("owner")


def can(user: dict | None, role: str) -> bool:
    return bool(user) and RANK.get(user["role"], -1) >= RANK[role]
