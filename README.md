# Home Library

A small self-hosted catalog for the books in your house. Scan a barcode with your phone, and the book shows up with its cover, author and description pulled from Open Library and Google Books. Track which shelf it lives on, tag it, and keep tabs on who borrowed what.

Runs as a Home Assistant add-on (with a sidebar panel) or as a plain Docker container on a Raspberry Pi.

## What it does

- **Scan with your phone camera.** Point at the barcode on the back cover. Two modes: *Ask first* shows the match before adding; *Add instantly* is for blasting through a whole bookcase.
- **Lookup from public data.** Open Library first, Google Books to fill gaps (descriptions, missing covers). Covers are downloaded and stored locally so the library works fast and offline once cataloged.
- **Duplicate check.** Scanning a book you already own tells you which shelf it's on, and offers to add another copy. Handy at a used bookstore.
- **Shelves and rooms.** Pick a default shelf before a scanning session and every book lands there.
- **Tags, series, wishlist.** Tag books however you like (Kids, Signed, Favorites), record series order, and keep a wishlist separate from what you own.
- **Loans.** Lend a book to someone with an optional due date; overdue loans are flagged in the nav and library.
- **No barcode?** Search by title or author, type an ISBN, or enter it by hand. Snap a photo to use as the cover.
- **Export.** Download everything as CSV. On Home Assistant, the library is included in normal HA backups.
- **Installable.** Add it to your phone's home screen and it opens like an app.

## Install on Home Assistant

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**, open the **⋮** menu, choose **Repositories**, and add:
   ```
   https://github.com/CamBGolfing/ha-home-library
   ```
2. Find **Home Library** in the store, install it, and start it. Turn on **Show in sidebar**.
3. Open **Library** from the sidebar.

The add-on builds locally on first install, which takes a few minutes on a Pi.

### Camera access and HTTPS

Browsers only allow camera access on secure (HTTPS) pages. If you reach Home Assistant over HTTPS (Nabu Casa, Let's Encrypt, or a reverse proxy), scanning works from the sidebar. Over plain `http://homeassistant.local:8123`, the camera is blocked by the browser; you can still type ISBNs or use a USB/Bluetooth barcode scanner (they act like a keyboard, just scan into the box on the Scan page).

If the camera is blocked inside the Home Assistant panel (some browsers and the companion app don't pass camera permission into embedded pages), the Scan page offers an **Open in its own tab** button.

### Options

| Option | What it does |
| --- | --- |
| `google_books_api_key` | Optional. Lookups work without it; a key raises Google's daily request limit. |
| `log_level` | `debug`, `info`, `warning` or `error`. |

The add-on also has an optional direct port (8099). It's off by default. The app has no login of its own, so only turn that on for a trusted network.

## Install on a Raspberry Pi with Docker

```bash
git clone https://github.com/CamBGolfing/ha-home-library home-library
cd home-library
docker compose up -d --build
```

Open `http://<pi-address>:8099`. Data (database and covers) lives in `./data`.

For phone scanning you'll want HTTPS in front of it, e.g. Caddy, Nginx Proxy Manager, Traefik, or a Tailscale HTTPS cert (`tailscale serve`).

## Run without Docker

```bash
cd home_library
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
LIBRARY_DATA_DIR=./data python run.py
```

Example systemd unit (`/etc/systemd/system/home-library.service`):

```ini
[Unit]
Description=Home Library
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/home-library/home_library
Environment=LIBRARY_DATA_DIR=/home/pi/home-library/data
ExecStart=/home/pi/home-library/home_library/.venv/bin/python run.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `LIBRARY_DATA_DIR` | `/data` | Where the SQLite database and covers are stored |
| `PORT` | `8099` | HTTP port |
| `GOOGLE_BOOKS_API_KEY` | | Optional Google Books key |
| `LOG_LEVEL` | `info` | Logging level |

## Backups

Everything is in the data folder: `library.db` (SQLite) and `covers/`. Copy that folder and you have a full backup. Home Assistant backups include it automatically.

## API

The app is a JSON API with a web front end. Interactive docs are at `/api/docs`. A few useful endpoints:

- `GET /api/lookup/{isbn}` look up a barcode, also reports if you already own it
- `POST /api/quick-add` `{"code": "978...", "shelf_id": 1, "tags": ["Kids"]}` scan-and-add in one call
- `GET /api/books?q=tolkien&shelf=2&tag=3&status=owned&loaned=true`
- `GET /api/stats` counts for dashboards (works nicely with a Home Assistant REST sensor)
- `GET /api/export.csv`

## Development

```bash
pip install -r requirements-dev.txt
pytest
cd home_library && LIBRARY_DATA_DIR=../data uvicorn app.main:app --reload --port 8099
```

Project layout:

```
repository.yaml          Home Assistant add-on repository manifest
home_library/            The add-on (also the Docker build context)
  config.yaml            Add-on config: ingress, options, arches
  Dockerfile
  run.py                 Entry point, reads HA options
  app/
    main.py              FastAPI routes
    db.py                SQLite schema
    lookup.py            Open Library + Google Books lookups, ISBN helpers
    static/              Front end (plain JS, no build step)
tests/                   API tests (network stubbed)
docker-compose.yml       Standalone install
```

Barcode scanning uses [html5-qrcode](https://github.com/mebjas/html5-qrcode) (Apache 2.0), vendored so the app doesn't depend on a CDN. It uses the browser's native barcode detector when available.

Book data comes from [Open Library](https://openlibrary.org/developers/api) and [Google Books](https://developers.google.com/books).
