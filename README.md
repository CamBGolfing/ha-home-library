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
- **Share with family and friends.** Family signs in through Home Assistant. Friends get invite links to library-only accounts, browse what you have, and ask to borrow.
- **Your name and logo.** Call it whatever you like and upload your own logo; it's used everywhere, including the home-screen icon.
- **Installable.** Add it to your phone's home screen and it opens like an app.

## Install on Home Assistant

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**, open the **⋮** menu, choose **Repositories**, and add:
   ```
   https://github.com/CamBGolfing/ha-home-library
   ```
2. Find **Home Library** in the store, install it, and start it. Turn on **Show in sidebar**.
3. Open **Library** from the sidebar. The first person to open it becomes the library's owner, so do this yourself right after installing.

The add-on builds locally on first install, which takes a few minutes on a Pi.

### Camera access and HTTPS

Browsers only allow camera access on secure (HTTPS) pages. If you reach Home Assistant over HTTPS (Nabu Casa, Let's Encrypt, or a reverse proxy), scanning works from the sidebar. Over plain `http://homeassistant.local:8123`, the camera is blocked by the browser; you can still type ISBNs or use a USB/Bluetooth barcode scanner (they act like a keyboard, just scan into the box on the Scan page).

### Scanning tips

- Hold the barcode about 6 inches (15 cm) from the camera. It doesn't need to fill the box. Newer Pro iPhones can't focus any closer with the main lens, so too close is the most common reason a scan doesn't catch.
- If your phone shows a camera list on the Scan page, try the other back cameras. On iPhone, "Back Triple Camera" or "Back Dual Wide Camera" can switch to the close-up lens.
- **Zoom 2x** (when shown) lets you hold the phone farther back while keeping the barcode big.
- **Take a photo** always works: it opens the phone's own camera, which focuses up close, and reads the barcode from the picture.

If the camera is blocked inside the Home Assistant panel (some browsers and the companion app don't pass camera permission into embedded pages), the Scan page offers an **Open in its own tab** button.

### Options

| Option | What it does |
| --- | --- |
| `ha_user_role` | What Home Assistant users can do the first time they open the library: `editor` (add, edit and lend; the default) or `viewer` (browse and ask to borrow). Change anyone later on the Account page. |
| `google_books_api_key` | Optional. Lookups work without it; a key raises Google's daily request limit. |
| `log_level` | `debug`, `info`, `warning` or `error`. |

## Sharing with family and friends

There are three levels of access:

| Level | Can |
| --- | --- |
| **Owner** | Everything, including inviting people and changing access |
| **Family** | Scan, add, edit and lend books |
| **Friend** | Browse and search, ask to borrow, see what they've borrowed. They see that a book is checked out, but not who has it, and don't see your private notes. |

### Family (people with a Home Assistant login)

1. In Home Assistant, go to **Settings → People → Add person**, turn on **Allow person to login**, and leave **Administrator** off.
2. They open **Library** from the sidebar and are signed in automatically as Family.

Library only trusts Home Assistant's sign-in for requests that come through Home Assistant itself, so this can't be faked from outside.

### Friends (no Home Assistant access)

Friends use the library's own address and a library account. They never see or touch Home Assistant.

**1. Give the library its own address.** The easiest way is a Cloudflare Tunnel, which needs a domain on Cloudflare and keeps everything on HTTPS without opening ports on your router.

- On the Home Library add-on's **Configuration** tab, under **Network**, set port `8099` to `8099` and restart the add-on.
- Install the [Cloudflared add-on](https://github.com/brenner-tobias/addon-cloudflared). To share *only* the library and not Home Assistant, leave `external_hostname` empty and add:
  ```yaml
  additional_hosts:
    - hostname: library.yourdomain.com
      service: http://<your-home-assistant-ip>:8099
  ```
- Start Cloudflared and follow its log to link it to your Cloudflare account.

Tailscale also works if your friends are on your tailnet (share the device with them), or use Tailscale Funnel for a public HTTPS address.

**2. Tell the library its address.** In Library, open **Account** and enter it under **Library address for friends**, e.g. `https://library.yourdomain.com`.

**3. Invite people.** On the same page, choose what they can do and click **Make invite link**. Send them the link. It works once and expires after 7 days. They pick a username and password and land in the library.

On the Account page you can also change anyone's access, turn someone's access off, remove them, or make a password reset link.

### Name and logo

As the owner, open **Account → Library name and logo**. Type a name (and optionally a shorter one for phone home screens), choose a picture, and pick **Fill square** (crops to fit, good for photos) or **Fit inside** (keeps the whole logo with a background color, good for logos). The preview shows how the home-screen icon will look. Click **Save name and logo**.

Phones that already added the library to their home screen keep the old icon until they remove it and add it again. The Home Assistant sidebar label ("Library") is set by the add-on and doesn't change.

### Security notes

- Sign-in attempts are rate limited, passwords are stored with scrypt, and sessions last 30 days.
- Only expose the direct port over HTTPS (a tunnel or reverse proxy). Don't forward port 8099 on your router as plain HTTP.
- Turning someone's access off signs them out right away.

## Install on a Raspberry Pi with Docker

```bash
git clone https://github.com/CamBGolfing/ha-home-library home-library
cd home-library
docker compose up -d --build
```

Open `http://<pi-address>:8099` and create the owner account. Then invite people from the **Account** page. Data (database and covers) lives in `./data`.

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
- `GET /api/stats` counts for dashboards

Every endpoint needs a signed-in session, and writes need an `X-Library-Request: 1` header (protection against cross-site requests).
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
    db.py                SQLite schema and migrations
    auth.py              Accounts, sessions, roles, Home Assistant sign-in
    lookup.py            Open Library + Google Books lookups, ISBN helpers
    static/              Front end (plain JS, no build step)
tests/                   API tests (network stubbed)
docker-compose.yml       Standalone install
```

Barcode scanning uses the browser's built-in barcode detector where one exists (Chrome on Android), and otherwise [zxing-cpp](https://github.com/zxing-cpp/zxing-cpp) compiled to WebAssembly via [zxing-wasm](https://github.com/Sec-ant/zxing-wasm) and the [barcode-detector](https://github.com/Sec-ant/barcode-detector) ponyfill (MIT; zxing-cpp is Apache 2.0). Both are vendored so the app works offline and doesn't depend on a CDN.

Book data comes from [Open Library](https://openlibrary.org/developers/api) and [Google Books](https://developers.google.com/books).
