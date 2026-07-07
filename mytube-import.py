#!/usr/bin/env python3
"""
mytube-import.py — Import YouTube Takeout data into a MyTube Sync backend.

Usage:
    python3 mytube-import.py

The script will prompt for your backend URL, API token, and the path to your
Google Takeout export folder, then let you choose what to import.

Supported Takeout structures (any account language):

    <takeout-root>/
      [Takeout/]                              ← optional outer wrapper
        YouTube [und/and] YouTube Music/      ← or already the YT folder itself
          <subs-folder>/
              <anything>.csv                  ← file with Kanal-URL / Channel Url column
          <playlists-folder>/
              <name>-Video.csv  OR  <name>.csv   ← per-playlist video files
          <history-folder>/
              <anything>.html   OR  <anything>.json

Discovery is content-based, not name-based, so it works regardless of the
account language Google used when generating the export.
"""

import os
import sys
import csv
import json
import re
import html.parser
import urllib.request
import urllib.error


# ---------------------------------------------------------------------------
# HTTP helpers (no third-party deps — stdlib only)
# ---------------------------------------------------------------------------

def api_request(base_url, token, method, path, body=None):
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="ignore")
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {body_text[:200]}")


def verify_connection(base_url, token):
    return api_request(base_url, token, "GET", "/current_user")


# ---------------------------------------------------------------------------
# Takeout structure discovery (locale-agnostic)
# ---------------------------------------------------------------------------

def _is_yt_folder(path):
    """Heuristic: does this directory look like the YouTube Takeout root?"""
    if not os.path.isdir(path):
        return False
    entries = set(os.listdir(path))
    # Must contain at least one of the characteristic sub-folders.
    # We check by sniffing content rather than names (see find_* helpers below).
    for entry in entries:
        sub = os.path.join(path, entry)
        if not os.path.isdir(sub):
            continue
        # If any sub-folder contains a CSV with a channel-URL column → subs folder present
        if _find_subs_csv(sub) or _find_playlists_folder(sub) or _find_history_file(sub):
            return True
    return False


def find_yt_folder(takeout_root):
    """
    Walk up to 3 levels deep to find the YouTube Takeout root folder.
    Handles: bare export, Takeout/ wrapper, or pointing directly at the YT folder.
    """
    # Level 0: the path itself
    if _is_yt_folder(takeout_root):
        return takeout_root

    # Level 1: direct children (e.g. "YouTube and YouTube Music/")
    try:
        children = os.listdir(takeout_root)
    except OSError:
        return None

    for name in children:
        child = os.path.join(takeout_root, name)
        if not os.path.isdir(child):
            continue
        if _is_yt_folder(child):
            return child
        # Level 2: e.g. Takeout/ → "YouTube and YouTube Music/"
        try:
            for sub in os.listdir(child):
                sub_path = os.path.join(child, sub)
                if os.path.isdir(sub_path) and _is_yt_folder(sub_path):
                    return sub_path
        except OSError:
            continue

    return None


def _csv_columns(path):
    """Return the header columns of a CSV file, or [] on failure."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, [])
        return [c.strip() for c in header]
    except Exception:
        return []


# --- Subscriptions ---

_SUBS_URL_COLS  = {"Channel Url", "Channel URL", "channel_url", "Kanal-URL"}
_SUBS_ID_COLS   = {"Channel Id",  "channel_id",  "Kanal-ID"}
_SUBS_NAME_COLS = {"Channel Title","channel_title","Kanaltitel", "Kanalname"}

def _find_subs_csv(folder):
    """
    Find the subscriptions CSV inside `folder` by looking for a file whose
    header contains a channel-URL or channel-ID column. Returns path or None.
    """
    if not os.path.isdir(folder):
        return None
    for fname in os.listdir(folder):
        if not fname.lower().endswith(".csv"):
            continue
        fpath = os.path.join(folder, fname)
        cols = set(_csv_columns(fpath))
        if cols & _SUBS_URL_COLS or cols & _SUBS_ID_COLS:
            return fpath
    return None


def find_subs_csv(yt_folder):
    """Search all immediate sub-folders of yt_folder for the subs CSV."""
    for entry in os.listdir(yt_folder):
        candidate = _find_subs_csv(os.path.join(yt_folder, entry))
        if candidate:
            return candidate
    return None


# --- Playlists ---

def _find_playlists_folder(folder):
    """
    A playlists folder contains at least one *-Video.csv or a CSV with a
    Video-ID / Video Id column. Returns the folder path or None.
    """
    if not os.path.isdir(folder):
        return None
    for fname in os.listdir(folder):
        if not fname.lower().endswith(".csv"):
            continue
        fpath = os.path.join(folder, fname)
        # Quick name check first (fast)
        if re.search(r"-video\.csv$", fname, re.IGNORECASE):
            return folder
        # Column check (slower but locale-agnostic)
        cols = set(_csv_columns(fpath))
        if cols & {"Video-ID", "Video Id", "video_id", "Video ID", "Video URL"}:
            return folder
    return None


def find_playlists_folder(yt_folder):
    for entry in os.listdir(yt_folder):
        result = _find_playlists_folder(os.path.join(yt_folder, entry))
        if result:
            return result
    return None


# --- History ---

def _find_history_file(folder):
    """
    Find the watch-history file (JSON or HTML) inside `folder`.
    Returns (path, "json"|"html") or None.
    """
    if not os.path.isdir(folder):
        return None
    for fname in os.listdir(folder):
        fpath = os.path.join(folder, fname)
        if not os.path.isfile(fpath):
            continue
        low = fname.lower()
        if low.endswith(".json"):
            # Quick sanity: starts with [ (array of history objects)
            try:
                with open(fpath, "rb") as f:
                    peek = f.read(16).lstrip()
                if peek.startswith(b"["):
                    return (fpath, "json")
            except OSError:
                pass
        elif low.endswith(".html"):
            # Quick sanity: contains a youtube watch URL
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    snippet = f.read(2048)
                if "youtube.com/watch" in snippet:
                    return (fpath, "html")
            except OSError:
                pass
    return None


def find_history_file(yt_folder):
    for entry in os.listdir(yt_folder):
        result = _find_history_file(os.path.join(yt_folder, entry))
        if result:
            return result
    return None


# ---------------------------------------------------------------------------
# Progress display
# ---------------------------------------------------------------------------

def progress(current, total, label=""):
    bar_width = 30
    filled = int(bar_width * current / total) if total else 0
    bar = "█" * filled + "░" * (bar_width - filled)
    pct = int(100 * current / total) if total else 0
    print(f"\r  [{bar}] {pct:3d}%  {current}/{total}  {label:<40}", end="", flush=True)

def progress_done():
    print()


# ---------------------------------------------------------------------------
# UC channel ID → @handle resolution
# ---------------------------------------------------------------------------

def resolve_uc_to_handle(uc_id):
    """
    Attempt to resolve a raw UCxxxxx channel ID to an @handle by fetching
    the channel's page and reading the canonical URL. Returns "@handle" or None.
    """
    url = f"https://www.youtube.com/channel/{uc_id}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; MyTubeImport/1.0)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html_text = resp.read().decode("utf-8", errors="ignore")

        m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/(@[a-zA-Z0-9_.-]+)"', html_text)
        if m:
            return m.group(1)

        m = re.search(r'"canonicalBaseUrl"\s*:\s*"(/(@[a-zA-Z0-9_.-]+))"', html_text)
        if m:
            return m.group(2)

        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Import: Subscriptions
# ---------------------------------------------------------------------------

def import_subscriptions(yt_folder, base_url, token):
    path = find_subs_csv(yt_folder)
    if not path:
        print("  ✗ Could not find subscriptions CSV (looked for a file with a channel URL column).")
        return

    print(f"  Reading: {os.path.relpath(path, yt_folder)}")

    handles = []      # resolved @handles ready to import
    to_resolve = []   # (uc_id, channel_title) pairs needing resolution

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Accept any known column name for channel URL
            channel_url = next(
                (row[c].strip() for c in _SUBS_URL_COLS if c in row and row[c].strip()),
                ""
            )
            title = next(
                (row[c].strip() for c in _SUBS_NAME_COLS if c in row and row[c].strip()),
                ""
            )

            if not channel_url:
                continue

            handle_match = re.search(r"/@([a-zA-Z0-9_.-]+)", channel_url)
            if handle_match:
                handles.append("@" + handle_match.group(1))
                continue

            uc_match = re.search(r"/channel/(UC[a-zA-Z0-9_-]+)", channel_url)
            if uc_match:
                to_resolve.append((uc_match.group(1), title))
                continue

            # Fallback: dedicated channel-ID column
            uc_id = next(
                (row[c].strip() for c in _SUBS_ID_COLS if c in row and row[c].strip()),
                ""
            )
            if uc_id.startswith("UC"):
                to_resolve.append((uc_id, title))

    print(f"  Found {len(handles)} channels with @handle, "
          f"{len(to_resolve)} needing UC→handle resolution.")

    dropped = 0
    if to_resolve:
        print(f"  Resolving {len(to_resolve)} UC IDs (one web request each)...")
        for i, (uc_id, title) in enumerate(to_resolve):
            progress(i + 1, len(to_resolve), (title or uc_id)[:40])
            handle = resolve_uc_to_handle(uc_id)
            if handle:
                handles.append(handle)
            else:
                dropped += 1
        progress_done()
        print(f"  Resolved: {len(to_resolve) - dropped}  Dropped (no @handle found): {dropped}")

    if not handles:
        print("  No subscribable channels found after resolution.")
        return

    print(f"  Importing {len(handles)} subscriptions...")
    added = skipped = errors = 0

    for i, channel_id in enumerate(handles):
        progress(i + 1, len(handles), channel_id[:40])
        try:
            result = api_request(base_url, token, "POST", "/subscriptions",
                                 {"channel_id": channel_id})
            if isinstance(result, dict) and result.get("success"):
                added += 1
            else:
                skipped += 1
        except RuntimeError as e:
            if "409" in str(e) or "already" in str(e).lower():
                skipped += 1
            else:
                errors += 1

    progress_done()
    print(f"  ✓ Added: {added}  Skipped (already exist): {skipped}  Errors: {errors}"
          + (f"  Dropped (unresolvable): {dropped}" if dropped else ""))


# ---------------------------------------------------------------------------
# Import: Playlists
# ---------------------------------------------------------------------------

# Filename stem patterns (lowercased, stripped of the trailing "-video" suffix
# that German Takeout appends) that map to MyTube system playlists.
# We match against the stem both with and without a trailing "-video" part.
_SYSTEM_PLAYLIST_STEMS = {
    # English Takeout stems (after stripping -Videos? suffix)
    "watch later":    "Watch Later",
    # German Takeout — Watch Later keeps its English internal filename
    "später ansehen": "Watch Later",
}

def _playlist_system_name(filename_stem):
    """
    Map a Takeout playlist CSV filename stem to a MyTube system playlist name,
    or return None for regular playlists.

    Takeout appends '-Video' or '-Videos' to every playlist CSV filename in
    some locales/versions. We strip that suffix before looking up the stem.
    """
    # Strip trailing -Videos or -Video (case-insensitive)
    stem = re.sub(r"-Videos?$", "", filename_stem, flags=re.IGNORECASE).strip()
    return _SYSTEM_PLAYLIST_STEMS.get(stem.lower())


def _read_playlist_video_ids(csv_path):
    """
    Read video IDs from a Takeout playlist CSV.

    Both English and German formats have a video-ID column, but with different
    header names and sometimes preamble lines before the real header.
    We scan for the first row containing a recognisable ID column name.
    """
    _ID_COLS = {"Video-ID", "Video Id", "video_id", "Video ID"}
    _URL_COLS = {"Video URL"}

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        lines = f.readlines()

    # Find the header row
    header_idx = None
    for idx, line in enumerate(lines):
        for col in _ID_COLS | _URL_COLS:
            if col in line:
                header_idx = idx
                break
        if header_idx is not None:
            break

    if header_idx is None:
        return []

    video_ids = []
    reader = csv.DictReader(lines[header_idx:])
    for row in reader:
        vid = next(
            (row[c].strip() for c in _ID_COLS | _URL_COLS if c in row and row[c].strip()),
            ""
        )
        # If it looks like a URL, extract the ID
        url_match = re.search(r"[?&]v=([a-zA-Z0-9_-]{11})", vid)
        if url_match:
            vid = url_match.group(1)
        if vid and len(vid) == 11 and re.match(r"^[a-zA-Z0-9_-]{11}$", vid):
            video_ids.append(vid)

    return video_ids


def import_playlists(yt_folder, base_url, token):
    pl_dir = find_playlists_folder(yt_folder)
    if not pl_dir:
        print("  ✗ Could not find playlists folder (looked for CSVs with a Video-ID column).")
        return

    print(f"  Reading from: {os.path.relpath(pl_dir, yt_folder)}/")
    print()

    _META_COLS = {"Playlist-ID", "Playlist-Titel", "Playlist Title", "Playlist Id"}

    csv_files = []
    for fname in sorted(os.listdir(pl_dir)):
        if not fname.lower().endswith(".csv"):
            continue
        fpath = os.path.join(pl_dir, fname)
        cols = set(_csv_columns(fpath))
        if cols & _META_COLS and not cols & {"Video-ID", "Video Id", "video_id"}:
            print(f"  (Skipping metadata file: {fname})")
            continue
        csv_files.append(fname)

    if not csv_files:
        print("  No playlist video CSVs found.")
        return

    existing = api_request(base_url, token, "GET", "/playlists")
    existing_by_name = {p["name"]: p["id"] for p in existing}

    total_added = total_skipped = total_errors = 0

    for csv_file in csv_files:
        stem = os.path.splitext(csv_file)[0]
        system_name = _playlist_system_name(stem)
        playlist_name = system_name or re.sub(r"-Videos?$", "", stem, flags=re.IGNORECASE).strip()

        print(f"\n  Playlist: {playlist_name}")

        video_ids = _read_playlist_video_ids(os.path.join(pl_dir, csv_file))
        if not video_ids:
            print("    No videos found.")
            continue

        print(f"    {len(video_ids)} videos.")

        # Get or create the playlist
        if playlist_name in existing_by_name:
            playlist_id = existing_by_name[playlist_name]
        else:
            result = api_request(base_url, token, "POST", "/playlists", {"name": playlist_name})
            playlist_id = result["data"]["id"]
            existing_by_name[playlist_name] = playlist_id

        added = skipped = errors = 0
        for i, vid in enumerate(video_ids):
            progress(i + 1, len(video_ids), vid)
            try:
                r = api_request(base_url, token, "POST",
                                f"/playlists/{playlist_id}/add",
                                {"video_id": vid})
                if isinstance(r, dict) and r.get("success"):
                    added += 1
                else:
                    skipped += 1
            except RuntimeError as e:
                if "409" in str(e) or "already" in str(e).lower():
                    skipped += 1
                else:
                    errors += 1

        progress_done()
        print(f"    ✓ Added: {added}  Skipped: {skipped}  Errors: {errors}")
        total_added   += added
        total_skipped += skipped
        total_errors  += errors

    print(f"\n  Total — Added: {total_added}  Skipped: {total_skipped}  Errors: {total_errors}")


# ---------------------------------------------------------------------------
# Import: Watch History
# ---------------------------------------------------------------------------

def extract_video_id(url):
    if not url:
        return None
    m = re.search(r"[?&]v=([a-zA-Z0-9_-]{11})", url)
    return m.group(1) if m else None


class _HistoryHtmlParser(html.parser.HTMLParser):
    """Extract video IDs from YouTube's watch-history HTML export."""
    def __init__(self):
        super().__init__()
        self.video_ids = []
        self._seen = set()

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        for attr, value in attrs:
            if attr == "href" and value:
                vid = extract_video_id(value)
                if vid and vid not in self._seen:
                    self._seen.add(vid)
                    self.video_ids.append(vid)


def _parse_history_json(path):
    """Parse English-style watch-history.json."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    seen = set()
    ids = []
    for item in raw:
        if item.get("header") != "YouTube":
            continue
        vid = extract_video_id(item.get("titleUrl", ""))
        if vid and vid not in seen:
            seen.add(vid)
            ids.append(vid)
    return ids


def _parse_history_html(path):
    """Parse German-style Wiedergabeverlauf.html."""
    parser = _HistoryHtmlParser()
    with open(path, encoding="utf-8", errors="ignore") as f:
        # Stream in chunks to handle large files without loading all into RAM
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            parser.feed(chunk)
    return parser.video_ids


def import_history(yt_folder, base_url, token):
    result = find_history_file(yt_folder)
    if not result:
        print("  ✗ Could not find watch history file (looked for .json or .html with YouTube watch URLs).")
        return

    path, fmt = result
    print(f"  Reading: {os.path.relpath(path, yt_folder)} ({fmt.upper()})", flush=True)

    if fmt == "json":
        entries = _parse_history_json(path)
    else:
        entries = _parse_history_html(path)

    if not entries:
        print("  No watch history entries found.")
        return

    total = len(entries)
    print(f"  Found {total} unique videos in watch history.")
    print("  Importing... (safe to Ctrl+C and re-run — duplicates are skipped)")

    added = skipped = errors = 0
    for i, vid in enumerate(entries):
        progress(i + 1, total, f"{added} added, {skipped} skipped")
        try:
            r = api_request(base_url, token, "POST", "/history/update", {
                "video_id": vid,
                "progress_seconds": 0,
                "completed": True,
            })
            if isinstance(r, dict) and r.get("success"):
                added += 1
            else:
                skipped += 1
        except RuntimeError as e:
            if "409" in str(e) or "already" in str(e).lower():
                skipped += 1
            else:
                errors += 1

    progress_done()
    print(f"  ✓ Added: {added}  Skipped (already exist): {skipped}  Errors: {errors}")
    print("  Note: Takeout history has no progress data — all entries imported as 'completed'.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def prompt(label, default=None, secret=False):
    if default:
        label = f"{label} [{default}]"
    label += ": "
    if secret:
        import getpass
        value = getpass.getpass(label)
    else:
        value = input(label).strip()
    return value or default or ""


def main():
    print("=" * 60)
    print("  MyTube Sync — YouTube Takeout Importer")
    print("=" * 60)
    print()

    base_url = prompt("Backend URL (e.g. https://your-domain.example.com/mytube-sync)")
    if not base_url:
        print("Backend URL is required.")
        sys.exit(1)

    token = prompt("API token", secret=True)
    if not token:
        print("Token is required.")
        sys.exit(1)

    print("\nVerifying connection...", end=" ", flush=True)
    try:
        user = verify_connection(base_url, token)
        print(f"✓ Connected as '{user['name']}'")
    except Exception as e:
        print(f"\n✗ Connection failed: {e}")
        sys.exit(1)

    print()
    takeout_root = prompt("Path to your Takeout export folder")
    if not os.path.isdir(takeout_root):
        print(f"✗ Directory not found: {takeout_root}")
        sys.exit(1)

    print("  Scanning export structure...", end=" ", flush=True)
    yt_folder = find_yt_folder(takeout_root)
    if not yt_folder:
        print()
        print(f"✗ Could not find a YouTube Takeout export inside: {takeout_root}")
        print("  Make sure you're pointing at the root of the unzipped Takeout archive.")
        sys.exit(1)

    print(f"✓")
    print(f"  YouTube export folder: {yt_folder}")

    print()
    print("What would you like to import?")
    print("  [s] Subscriptions")
    print("  [p] Playlists")
    print("  [h] Watch history")
    print("  [a] All of the above")
    print()
    choice = input("Choice (s/p/h/a): ").strip().lower()

    do_subs      = choice in ("s", "a")
    do_playlists = choice in ("p", "a")
    do_history   = choice in ("h", "a")

    if not (do_subs or do_playlists or do_history):
        print("Nothing selected, exiting.")
        sys.exit(0)

    print()

    if do_subs:
        print("── Subscriptions " + "─" * 42)
        import_subscriptions(yt_folder, base_url, token)
        print()

    if do_playlists:
        print("── Playlists " + "─" * 46)
        import_playlists(yt_folder, base_url, token)
        print()

    if do_history:
        print("── Watch History " + "─" * 42)
        import_history(yt_folder, base_url, token)
        print()

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nAborted.")
        sys.exit(0)
