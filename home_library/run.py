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
    except (OSError, ValueError) as e:
        print(f"Could not read add-on options: {e}")

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8099")),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
