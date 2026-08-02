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

        My Activity/                          ← separate top-level Takeout category
          YouTube/
              <anything>.html                 ← liked/disliked video entries

Discovery is content-based, not name-based, so it works regardless of the
account language Google used when generating the export.

Note on Liked Videos: Takeout's "YouTube and YouTube Music" export does not
include a Liked Videos list. That data is only available via the separate
"My Activity" Takeout category (select "My Activity" → filter to YouTube),
which logs it as a rated-video activity entry rather than a playlist.
"""

import os
import sys
import csv
import json
import re
import html.parser
import urllib.request
import urllib.error
from datetime import datetime, timedelta


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

def _looks_like_my_activity_folder(name):
    """
    Does this folder name look like Takeout's "My Activity" category
    (English "My Activity", German "Meine Aktivitäten")? Used to keep it
    from being confused with the main "YouTube and YouTube Music" export -
    its YouTube subfolder contains an HTML file with real youtube.com/watch
    links too, which would otherwise satisfy _find_history_file's content
    check and get misidentified as the watch-history folder.
    """
    low = name.lower()
    return "my activity" in low or "aktivität" in low or "aktivitat" in low


def _is_yt_folder(path):
    """Heuristic: does this directory look like the YouTube Takeout root?"""
    if not os.path.isdir(path):
        return False
    entries = set(os.listdir(path))
    # Must contain at least one of the characteristic sub-folders.
    # Checked by sniffing content rather than names (see find_* helpers below).
    for entry in entries:
        if _looks_like_my_activity_folder(entry):
            continue
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
        if _looks_like_my_activity_folder(name):
            continue
        child = os.path.join(takeout_root, name)
        if not os.path.isdir(child):
            continue
        if _is_yt_folder(child):
            return child
        # Level 2: e.g. Takeout/ → "YouTube and YouTube Music/"
        try:
            for sub in os.listdir(child):
                if _looks_like_my_activity_folder(sub):
                    continue
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
            # Quick sanity: contains a youtube watch URL. Takeout's HTML
            # exports embed a large chunk of boilerplate CSS/JS (~140KB in
            # practice) before any real content, so this has to peek well
            # past that rather than just the first couple KB.
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    snippet = f.read(400_000)
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
# Matched against the stem both with and without a trailing "-video" part.
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
    some locales/versions. That suffix is stripped before looking up the stem.
    """
    # Strip trailing -Videos or -Video (case-insensitive)
    stem = re.sub(r"-Videos?$", "", filename_stem, flags=re.IGNORECASE).strip()
    return _SYSTEM_PLAYLIST_STEMS.get(stem.lower())


def _read_playlist_video_ids(csv_path):
    """
    Read video IDs from a Takeout playlist CSV.

    Both English and German formats have a video-ID column, but with different
    header names and sometimes preamble lines before the real header.
    This scans for the first row containing a recognisable ID column name.
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


# --- Timestamp parsing ---

# Central European timezone abbreviations, as used in German Takeout
# exports ("24.07.2026, 13:47:51 MESZ"). Mapped to their UTC offset in
# hours so the parsed datetime can be converted to real UTC.
_TZ_OFFSETS_HOURS = {
    "MEZ": 1, "MESZ": 2,    # German: Mitteleuropäische (Sommer)zeit
    "CET": 1, "CEST": 2,    # English equivalents, in case they appear
}


def parse_takeout_timestamp(text):
    """
    Parse a Takeout activity timestamp into a naive UTC datetime, or None
    if the format isn't recognised.

    The German numeric format ("DD.MM.YYYY, HH:MM:SS TZ") is confirmed
    against real exports. The English format is best-effort: Google's
    English exports use a different layout, but I don't have a confirmed
    real sample to verify against, so unrecognised text just falls back to
    None rather than raising - callers should treat that as "unknown time"
    and keep working rather than fail the whole import.
    """
    text = (text or "").strip()
    if not text:
        return None

    # German numeric format: "24.07.2026, 13:47:51 MESZ"
    m = re.match(
        r"^(\d{1,2})\.(\d{1,2})\.(\d{4}),\s*(\d{1,2}):(\d{2}):(\d{2})\s*(\S+)?$",
        text
    )
    if m:
        day, month, year, hour, minute, second, tz = m.groups()
        try:
            dt = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
        except ValueError:
            return None
        offset = _TZ_OFFSETS_HOURS.get((tz or "").upper())
        if offset is not None:
            dt = dt - timedelta(hours=offset)
        return dt

    # Best-effort English formats — unverified against a real export.
    cleaned = re.sub(r"\s+[A-Za-z]{2,5}([+-]\d{1,2}(:\d{2})?)?$", "", text).strip()
    for fmt in (
        "%b %d, %Y, %I:%M:%S %p",   # "Jul 24, 2026, 1:47:51 PM"
        "%B %d, %Y, %I:%M:%S %p",   # "July 24, 2026, 1:47:51 PM"
        "%d %b %Y, %H:%M:%S",       # "24 Jul 2026, 13:47:51"
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue

    return None


# --- Shared activity-entry extraction (used by watch history AND likes) ---

# Matches the data-bearing content-cell of one Takeout activity entry (the
# cell holding the title link, channel link, and timestamp), stopping right
# before the adjacent (always-empty) "text-right" cell that follows it.
_CONTENT_CELL_RE = re.compile(
    r'content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">(.*?)'
    r'</div><div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 mdl-typography--text-right">',
    re.DOTALL
)
_WATCH_LINK_RE = re.compile(
    r'<a href="https://www\.youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})"[^>]*>.*?</a>',
    re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")


def iter_activity_entries(html_text):
    """
    Yield {"video_id", "prefix", "suffix", "timestamp_text"} for every
    Takeout activity entry referencing a YouTube watch link, regardless of
    activity type (watched / liked / disliked / etc. — callers filter by
    `prefix`/`suffix`).

    `prefix` is the plain-text content before the link (e.g. German's
    'Mit "Mag ich" bewertet: ' for a like) and `suffix` is the plain text
    right after it (e.g. German's 'angesehen' for a watch). Works against
    the single-file MyActivity / Wiedergabeverlauf HTML export.
    """
    for cell in _CONTENT_CELL_RE.findall(html_text):
        link_match = _WATCH_LINK_RE.search(cell)
        if not link_match:
            continue

        video_id = link_match.group(1)
        prefix_html = cell[:link_match.start()]
        rest = cell[link_match.end():]

        suffix_match = re.match(r"(.*?)<br>", rest, re.DOTALL)
        suffix_html = suffix_match.group(1) if suffix_match else ""

        lines = [l for l in cell.split("<br>") if l.strip()]
        timestamp_text = html.unescape(_TAG_RE.sub("", lines[-1])).strip() if lines else ""

        yield {
            "video_id": video_id,
            "prefix": html.unescape(_TAG_RE.sub("", prefix_html)).strip(),
            "suffix": html.unescape(_TAG_RE.sub("", suffix_html)).strip(),
            "timestamp_text": timestamp_text,
        }


def _read_html_text(path):
    """Read a (potentially large) Takeout HTML export fully into memory."""
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()


def _parse_history_json(path):
    """Parse English-style watch-history.json, including real timestamps."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    seen = set()
    entries = []
    for item in raw:
        if item.get("header") != "YouTube":
            continue
        vid = extract_video_id(item.get("titleUrl", ""))
        if not vid or vid in seen:
            continue
        seen.add(vid)
        when = None
        time_str = item.get("time")
        if time_str:
            try:
                # Takeout JSON timestamps are ISO 8601, e.g.
                # "2026-07-24T13:47:51.000Z". datetime.fromisoformat doesn't
                # accept the trailing "Z" on Python < 3.11, so normalise it.
                when = datetime.fromisoformat(time_str.replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                when = None
        entries.append({"video_id": vid, "watched_at": when})
    return entries


def _parse_history_html(path):
    """Parse watch-history HTML (English or German), including real timestamps."""
    html_text = _read_html_text(path)
    seen = set()
    entries = []
    for entry in iter_activity_entries(html_text):
        vid = entry["video_id"]
        if vid in seen:
            continue
        seen.add(vid)
        entries.append({
            "video_id": vid,
            "watched_at": parse_takeout_timestamp(entry["timestamp_text"]),
        })
    return entries


# Suffix markers (the text right after the video link) that identify a
# "watched" entry in a My Activity export, as opposed to a like/dislike/
# subscribe/comment-reply entry mixed into the same file.
_WATCHED_SUFFIX_MARKERS = [
    "angesehen",                # German, confirmed
    "als angesehen markiert",   # German "marked as watched" variant, confirmed present
    "watched",                  # English, best-effort / unverified
]


def _parse_my_activity_watched(path):
    """
    Parse a My Activity HTML export for watched-video entries, to
    supplement (not replace) the dedicated watch-history export - My
    Activity's retention window doesn't fully overlap with it in either
    direction, so combining both gives more complete coverage than either
    alone.
    """
    html_text = _read_html_text(path)
    seen = set()
    entries = []
    for entry in iter_activity_entries(html_text):
        suffix = entry["suffix"].lower()
        if not any(m in suffix for m in _WATCHED_SUFFIX_MARKERS):
            continue
        vid = entry["video_id"]
        if vid in seen:
            continue
        seen.add(vid)
        entries.append({
            "video_id": vid,
            "watched_at": parse_takeout_timestamp(entry["timestamp_text"]),
        })
    return entries


def _merge_history_entries(*entry_lists):
    """
    Merge watch-history entries from multiple sources, deduping by
    video_id. When a video appears in more than one source, the entry with
    the more recent (or only) known timestamp wins - a raw list dedupe
    would arbitrarily pick whichever source happened to be scanned first.
    """
    by_id = {}
    for entries in entry_lists:
        for entry in entries:
            vid = entry["video_id"]
            existing = by_id.get(vid)
            if existing is None:
                by_id[vid] = entry
                continue
            new_ts = entry["watched_at"]
            old_ts = existing["watched_at"]
            if new_ts is not None and (old_ts is None or new_ts > old_ts):
                by_id[vid] = entry
    return list(by_id.values())


def import_history(yt_folder, takeout_root, base_url, token):
    result = find_history_file(yt_folder)
    dedicated_entries = []
    if result:
        path, fmt = result
        print(f"  Reading: {os.path.relpath(path, yt_folder)} ({fmt.upper()})", flush=True)
        dedicated_entries = _parse_history_json(path) if fmt == "json" else _parse_history_html(path)
    else:
        print("  (No dedicated watch-history file found in the main export.)")

    activity_path = find_my_activity_file(takeout_root)
    activity_entries = []
    if activity_path:
        print(f"  Reading: {os.path.relpath(activity_path, takeout_root)} (My Activity)", flush=True)
        activity_entries = _parse_my_activity_watched(activity_path)

    if not dedicated_entries and not activity_entries:
        print("  ✗ Could not find watch history in either the main Takeout export or a")
        print("    'My Activity' export (looked for .json/.html with YouTube watch URLs).")
        return

    entries = _merge_history_entries(dedicated_entries, activity_entries)

    # Sort oldest → newest by real watch date, so the backend's history
    # list ends up in true chronological order rather than whatever order
    # Takeout happened to list entries in (newest-first, by ID). Entries
    # with no parseable timestamp sort to the front rather than being
    # dropped or reordering everything else.
    entries.sort(key=lambda e: e["watched_at"] or datetime.min)
    undated = sum(1 for e in entries if e["watched_at"] is None)

    total = len(entries)
    print(f"  Found {total} unique videos "
          f"({len(dedicated_entries)} from watch history, {len(activity_entries)} from My Activity, "
          f"{total} after merging duplicates).")
    if undated:
        print(f"  Note: {undated} entries had no parseable timestamp and were placed first.")
    print("  Importing... (safe to Ctrl+C and re-run — duplicates are skipped)")

    added = skipped = errors = 0
    for i, entry in enumerate(entries):
        vid = entry["video_id"]
        progress(i + 1, total, f"{added} added, {skipped} skipped")
        body = {
            "video_id": vid,
            "progress_seconds": 0,
            "completed": True,
        }
        if entry["watched_at"] is not None:
            body["watched_at"] = entry["watched_at"].isoformat()
        try:
            r = api_request(base_url, token, "POST", "/history/update", body)
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
    if undated == total:
        print("  Note: no timestamps could be parsed — all entries imported as 'completed' with no watch date.")


# ---------------------------------------------------------------------------
# Import: Liked Videos (via the separate "My Activity" Takeout export)
# ---------------------------------------------------------------------------
#
# Takeout's "YouTube and YouTube Music" export does not include a Liked
# Videos list. That data does exist in Google's records, though: it's in
# the separate "My Activity" Takeout category, filtered to the YouTube
# product, as "Mit \"Mag ich\" bewertet: <video>" (German) / "Liked video
# <video>" (English) entries alongside watched/subscribed/etc. activity.
#
# The German phrasing is confirmed against a real export. The English
# phrasing is a best-effort guess based on Google's documented title
# pattern for this export ("Watched...", "Subscribed to...") — if it
# doesn't match your export, please open an issue with the real wording
# so it can be added.

_LIKE_PREFIX_MARKERS = [
    'mit "mag ich" bewertet',   # German, confirmed
    "liked video",              # English, best-effort / unverified
]
_DISLIKE_PREFIX_MARKERS = [
    'mit "mag ich nicht" bewertet',  # German, confirmed
    "disliked video",               # English, best-effort / unverified
]


def _classify_rating(prefix):
    low = prefix.lower()
    if any(m in low for m in _DISLIKE_PREFIX_MARKERS):
        return "dislike"
    if any(m in low for m in _LIKE_PREFIX_MARKERS):
        return "like"
    return None


def find_my_activity_file(takeout_root):
    """
    Locate the "My Activity" → YouTube export - a separate Takeout category
    from "YouTube and YouTube Music", typically at
    <root>/My Activity/YouTube/MyActivity.html.

    Starts from folders that look like "My Activity" / "Meine Aktivitäten"
    to avoid scanning an entire Takeout export (which can include large
    media files), then content-sniffs for the rating-entry marker rather
    than relying on an exact filename.
    """
    search_roots = []
    for dirpath, dirnames, _ in os.walk(takeout_root):
        depth = dirpath[len(takeout_root):].count(os.sep)
        if depth >= 3:
            dirnames[:] = []
            continue
        for d in dirnames:
            if _looks_like_my_activity_folder(d):
                search_roots.append(os.path.join(dirpath, d))

    for root in search_roots or [takeout_root]:
        for dirpath, _, filenames in os.walk(root):
            for fname in filenames:
                if not fname.lower().endswith((".html", ".htm")):
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    with open(fpath, "rb") as f:
                        head = f.read(400_000)
                        if b"youtube.com/watch" not in head:
                            continue
                        # Rating entries can be sparse relative to watch
                        # entries in a large activity export, so the
                        # marker itself may sit well past any reasonable
                        # peek size - read the rest of the file too.
                        rest = f.read()
                except OSError:
                    continue
                full_lower = (head + rest).lower()
                if b"bewertet" in full_lower or b"liked video" in full_lower:
                    return fpath
    return None


def _parse_my_activity_likes(path):
    """
    Parse a My Activity HTML export for Liked-video entries. Returns a list
    of {"video_id", "added_at"} dicts, most-recent-like kept if a video was
    liked more than once. Dislikes are detected but not imported anywhere
    (there's no "Disliked Videos" concept in MyTube Sync) - only counted for
    the summary printed to the user.
    """
    html_text = _read_html_text(path)
    seen = set()
    likes = []
    dislike_count = 0
    for entry in iter_activity_entries(html_text):
        rating = _classify_rating(entry["prefix"])
        if rating is None:
            continue
        vid = entry["video_id"]
        if rating == "dislike":
            dislike_count += 1
            continue
        if vid in seen:
            continue
        seen.add(vid)
        likes.append({
            "video_id": vid,
            "added_at": parse_takeout_timestamp(entry["timestamp_text"]),
        })
    return likes, dislike_count


def import_likes(takeout_root, base_url, token):
    path = find_my_activity_file(takeout_root)
    if not path:
        print("  ✗ Could not find a 'My Activity' YouTube export (looked for a folder named")
        print("    something like 'My Activity' containing an HTML file with rated-video entries).")
        print("    This is a separate Takeout export from the main YouTube one - see README.")
        return

    print(f"  Reading: {os.path.relpath(path, takeout_root)}", flush=True)
    likes, dislike_count = _parse_my_activity_likes(path)

    if not likes:
        print("  No liked-video entries found.")
        return

    # Oldest → newest, so the playlist ends up in the same chronological
    # order the rest of the app already uses for playlists (position
    # ascending = oldest added first).
    likes.sort(key=lambda e: e["added_at"] or datetime.min)
    undated = sum(1 for e in likes if e["added_at"] is None)

    print(f"  Found {len(likes)} liked videos"
          + (f" and {dislike_count} disliked videos (not imported)" if dislike_count else "")
          + ".")
    if undated:
        print(f"  Note: {undated} entries had no parseable timestamp and were placed first.")

    existing = api_request(base_url, token, "GET", "/playlists")
    liked_playlist = next((p for p in existing if p["name"] == "Liked Videos"), None)
    if liked_playlist:
        playlist_id = liked_playlist["id"]
    else:
        result = api_request(base_url, token, "POST", "/playlists", {"name": "Liked Videos"})
        playlist_id = result["data"]["id"]

    total = len(likes)
    added = skipped = errors = 0
    for i, entry in enumerate(likes):
        vid = entry["video_id"]
        progress(i + 1, total, f"{added} added, {skipped} skipped")
        body = {"video_id": vid}
        if entry["added_at"] is not None:
            body["added_at"] = entry["added_at"].isoformat()
        try:
            r = api_request(base_url, token, "POST", f"/playlists/{playlist_id}/add", body)
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
    print("  [l] Liked videos (via a separate 'My Activity' Takeout export - see README)")
    print("  [a] All of the above")
    print()
    choice = input("Choice (s/p/h/l/a): ").strip().lower()

    do_subs      = choice in ("s", "a")
    do_playlists = choice in ("p", "a")
    do_history   = choice in ("h", "a")
    do_likes     = choice in ("l", "a")

    if not (do_subs or do_playlists or do_history or do_likes):
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
        import_history(yt_folder, takeout_root, base_url, token)
        print()

    if do_likes:
        print("── Liked Videos " + "─" * 43)
        import_likes(takeout_root, base_url, token)
        print()

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nAborted.")
        sys.exit(0)
