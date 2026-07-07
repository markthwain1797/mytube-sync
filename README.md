# MyTube Sync

> **Personal project, shared as-is.**
> I built this for myself and my family. I'm sharing it in case it's useful to others,
> but I can't guarantee it works in every setup, and I'm not planning to build features
> on request. If something doesn't work for you, feel free to contribute.

A self-hosted YouTube companion: subscriptions, playlists, and watch history that live on
your own server instead of Google's. Consists of a small FastAPI backend and a Firefox
extension (desktop + Android).

---

## Repository layout

```
app/                        Python package — the FastAPI backend
  __init__.py
  main.py                   API routes
  database.py               SQLAlchemy / SQLite setup
  models.py                 ORM models
frontend/
  index.html                Web UI — served by the backend at /
requirements.txt
mytube-extension/           Firefox WebExtension (desktop + Android)
mytube_sync-1.0.0.xpi       Pre-signed build — install this directly, no AMO account needed
mytube-sync.service.example Systemd unit template
```

---

## Backend setup

**Requirements:** Python 3.10+, a Linux server, an HTTPS reverse proxy (nginx, Caddy, apache, etc.).

### 1. Create a system user (optional but recommended)

```bash
sudo useradd -r -m -d /home/mytube -s /sbin/nologin mytube
```

### 2. Clone / copy the files into place

```
/home/mytube/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── database.py
│   └── models.py
├── frontend/
│   └── index.html
└── requirements.txt
```

### 3. Create a virtual environment and install dependencies

```bash
cd /home/mytube
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Test that it starts

```bash
cd /home/mytube
venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 6060
```

You should see uvicorn's startup output. Visit `http://127.0.0.1:6060/` — it should
serve the web UI. Stop it with Ctrl+C once confirmed.

### 5. Install as a systemd service

Copy the example unit file and edit it:

```bash
sudo cp mytube-sync.service.example /etc/systemd/system/mytube-sync.service
sudo nano /etc/systemd/system/mytube-sync.service
```

The working directory must be the **parent** of `app/` (i.e. `/home/mytube`, not
`/home/mytube/app`), and uvicorn is invoked as `app.main:app`:

```ini
[Service]
User=mytube
Group=mytube
WorkingDirectory=/home/mytube
ExecStart=/home/mytube/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 6060
```

Then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mytube-sync
sudo systemctl status mytube-sync
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `MYTUBE_DB_PATH` | `<WorkingDirectory>/data/mytube.db` | Path to the SQLite database file |
| `MYTUBE_ROOT_PATH` | _(empty)_ | Path prefix if serving behind a reverse proxy subdirectory, e.g. `/mytube-sync` |

Set these in the `[Service]` block of the unit file:
```ini
Environment=MYTUBE_DB_PATH=/home/mytube/data/mytube.db
Environment=MYTUBE_ROOT_PATH=/mytube-sync
```

### 6. Reverse proxy

The extension runs on HTTPS (youtube.com), so the backend also needs HTTPS — browsers
block cross-origin requests from HTTPS pages to plain HTTP backends.

Point your reverse proxy at `127.0.0.1:6060`. Example nginx config:

```nginx
location /mytube-sync/ {
    proxy_pass http://127.0.0.1:6060/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

Example apache config:

```apache
ProxyPass /mytube-sync http://127.0.0.1:6060
ProxyPassReverse /mytube-sync http://127.0.0.1:6060

<Location /mytube-sync>
    Require all granted
    Header always set Access-Control-Allow-Headers "X-User-Id, Content-Type, Authorization"
    Header always set Access-Control-Allow-Methods "GET, POST, PUT, DELETE, OPTIONS"
</Location>
```

If you use a path prefix like `/mytube-sync`, set `MYTUBE_ROOT_PATH=/mytube-sync` in the
service environment.

### 7. Create a user account

There's no signup UI — create accounts via the API directly:

```bash
curl -X POST https://your-domain.example.com/mytube-sync/users \
  -H "Content-Type: application/json" \
  -d '{"name": "yourname"}'
```

The response includes a `token`. Save it — this is what you'll paste into the extension
popup and the web UI. Each user has their own token; data is fully separated per user.

---

## Extension setup

### Install on Firefox desktop (temporary, for testing)

1. Open `about:debugging#/runtime/this-firefox`
2. Click "Load Temporary Add-on"
3. Select `mytube-extension/manifest.json`

The extension disappears when Firefox restarts — fine for quick testing, but you'll want
a signed install for daily use (see below).

### Install permanently — desktop

A signed build is included in this repo as `mytube_sync-1.0.0.xpi`.

1. In Firefox, go to `about:addons`
2. Gear icon → "Install Add-on From File"
3. Select `mytube_sync-1.0.0.xpi`

If you've modified the code and need a new signed build, see [Sign it yourself](#sign-it-yourself) below.

### Install permanently — Android

Firefox for Android only installs extensions from Mozilla's servers — it does **not** allow
sideloading `.xpi` files from local storage. There are a few ways around this:

**Option A — Firefox Nightly (easiest for personal use)**

Firefox Nightly has a developer mode that allows installing any signed `.xpi` directly:

1. Install [Firefox Nightly](https://play.google.com/store/apps/details?id=org.mozilla.fenix) from the Play Store
2. Open Nightly → Settings → About Firefox Nightly → tap the Firefox logo **5 times**
   until "Debug menu enabled" appears
3. Go back to Settings → Install Extension from File
4. Transfer `mytube_sync-1.0.0.xpi` to the phone (USB, cloud storage, email attachment)
   and select it

This only works in Nightly, not regular Firefox for Android.

**Option B — Custom AMO collection (regular Firefox for Android)**

Regular Firefox for Android can be pointed at a custom AMO collection. This requires the
extension to be hosted on AMO (even as an unlisted/private add-on):

1. Upload the extension to AMO as an unlisted add-on (see [Sign it yourself](#sign-it-yourself))
2. Note your **numeric AMO user ID** — visible in your profile URL:
   `addons.mozilla.org/en-US/firefox/user/12345678/`
3. Create a **Collection** on AMO (My Collections → Create a Collection), give it any
   name and slug, and add this extension to it
4. In Firefox for Android: Settings → About Firefox → tap the logo 5 times →
   Custom Add-on Collection → enter your user ID and collection slug
5. Firefox restarts showing your collection under Add-ons — install from there

---

### Sign it yourself

If you've modified the code and need a new signed build:

1. Create a free account at [addons.mozilla.org](https://addons.mozilla.org)
2. Zip the **contents** of `mytube-extension/` — `manifest.json` must be at the zip root:
   ```bash
   cd mytube-extension && zip -r ../mytube-extension.zip .
   ```
3. [addons.mozilla.org/developers](https://addons.mozilla.org/developers) → Submit a New
   Add-on → **"On your own"** (unlisted — not published publicly)
4. Upload the zip — automated signing takes a few minutes, no human review
5. Download the signed `.xpi`

> **Keep the same extension ID.** The UUID in `manifest.json`'s
> `browser_specific_settings.gecko.id` must stay the same across re-submissions —
> changing it makes Firefox treat the new build as a completely different extension
> and wipes stored settings. Only generate a new one if you want a clean break:
> ```bash
> python3 -c "import uuid; print('{' + str(uuid.uuid4()) + '}')"
> ```

---

## Web UI

Once the backend is running, opening its URL in a browser (e.g.
`https://your-domain.example.com/mytube-sync/`) serves a built-in management interface.
Enter your API token and press Enter to connect — no separate install needed.

Three fixed columns show Playlists, Watch History, and Subscriptions simultaneously.
A search bar at the top accepts a YouTube video URL or `@channel` handle and shows
inline actions (subscribe, like, watch later, mark watched). Data auto-refreshes every
60 seconds; the subscription feed loads in the background so the rest of the UI appears
immediately.

---

## Importing from YouTube (Google Takeout)

`mytube-import.py` imports your existing YouTube data from a Google Takeout export.

### Get your Takeout export

1. Go to [takeout.google.com](https://takeout.google.com)
2. Deselect all, then select only **YouTube and YouTube Music**
3. Under its options, make sure **subscriptions**, **playlists**, and **history** are
   included
4. Export, download, and unzip the archive

### Run the importer

No dependencies beyond Python's standard library:

```bash
python3 mytube-import.py
```

It will prompt for your backend URL, API token, and the path to the unzipped export
folder, then ask which data types to import (subscriptions / playlists / watch history /
all). Duplicates are silently skipped — safe to Ctrl+C and re-run.

The importer works regardless of your Google account's language — it locates the
relevant files by inspecting their contents rather than assuming fixed folder/file names,
so German, English, and other Takeout exports are all handled the same way.

**Notes:**
- **Subscriptions:** Takeout exports channel handles (`@ChannelName`) where available.
  Channels that only have a raw `UCxxxxx` ID are resolved to their `@handle` via a
  web request to YouTube. If resolution fails, the channel is dropped — no raw IDs are
  stored. A dropped count is shown at the end.
- **Playlists:** Imported as-is. "Watch Later" is mapped to the system playlist of the
  same name. Everything else (including "Favorites") becomes a regular playlist.
  Note that YouTube Takeout does **not** export your liked videos — the Liked Videos
  system playlist will remain empty after import; this is a Google limitation.
- **Watch history:** Takeout has no progress data. All history entries are imported as
  "completed". For large histories (10k+ entries) this will take a while; the progress
  bar updates continuously and it's safe to interrupt and re-run.

---

## Known limitations / rough edges

- **Subscription feed speed**: the backend fetches YouTube RSS feeds live on every request
  to `/subscriptions/feed`. With many subscriptions this can be slow. No caching or
  background refresh is implemented.
- **No user management UI**: creating and deleting users is done via direct API calls.
- **Broad host permission**: the extension requests `<all_urls>` because the backend URL
  is entered at runtime and can't be known at build time. Firefox shows this as "Access
  your data for all websites" during install — this is expected.
- **Channel subscriptions store `@handle` only**: if you visit a channel via its raw
  `youtube.com/channel/UCxxxxx` URL and the page hasn't finished loading the canonical
  handle yet, the subscribe button will be temporarily disabled rather than risking storing
  the raw ID. Navigating to the channel's `/@handle` URL always works.
- **No dark/light mode toggle**: the sidebar uses a fixed dark theme.

---

## License

MIT — do what you want with it, no warranty implied.
