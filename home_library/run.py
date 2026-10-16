"""Entry point for Docker, the Home Assistant add-on, and bare-metal installs.

On Home Assistant, add-on options arrive in /data/options.json and are mapped to
environment variables before the server starts.
"""
import json
import os
from pathlib import Path

import uvicorn

options_file = Path(os.environ.get("LIBRARY_DATA_DIR", "/data")) / "options.json"
if options_file.exists():
    try:
        opts = json.loads(options_file.read_text())
        if opts.get("google_books_api_key"):
            os.environ.setdefault("GOOGLE_BOOKS_API_KEY", opts["google_books_api_key"])
        if opts.get("log_level"):
            os.environ.setdefault("LOG_LEVEL", opts["log_level"])
        if opts.get("ha_user_role"):
            os.environ.setdefault("HA_USER_ROLE", opts["ha_user_role"])
        if opts.get("notify_services"):
            os.environ.setdefault("NOTIFY_SERVICES", ",".join(opts["notify_services"]))
        for key in ("notify_on_request", "notify_on_overdue", "notify_on_return", "mqtt_enabled"):
            if key in opts:
                os.environ.setdefault(key.upper(), "true" if opts[key] else "false")
        if "overdue_check_hour" in opts:
            os.environ.setdefault("OVERDUE_CHECK_HOUR", str(opts["overdue_check_hour"]))
        if "loan_days" in opts:
            os.environ.setdefault("LOAN_DAYS", str(opts["loan_days"]))
    except (OSError, ValueError) as e:
        print(f"Could not read add-on options: {e}")

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8099")),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        # Keep the real peer address: Home Assistant sign-in is trusted only for
        # connections that actually come from the Supervisor, so forwarded headers
        # must never be allowed to rewrite it.
        proxy_headers=False,
    )
