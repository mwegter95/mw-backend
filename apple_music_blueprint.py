"""
Apple Music Tools — Flask Blueprint
Mounts under /apple on the mw-backend server.

Mirrors the Extractor + Builder pieces of the Spotify Super User Tools, but the
Apple Music API works differently, so the auth model differs:

  - Playlist Extractor  (NO auth — scrapes the public share page)
        Works for Apple-curated playlists AND personal shared (pl.u-…) links.
        Pulls title/artist/duration straight from the page's embedded JSON.

  - Playlist Builder    (Apple developer token + MusicKit JS user token)
        Catalog search needs a developer token (ES256 JWT signed with a
        MusicKit .p8 key). Creating the playlist needs a Music User Token that
        MusicKit JS obtains in the browser (user signs in with Apple ID and
        needs an active Apple Music subscription).

The Extractor needs no credentials at all. The Builder degrades gracefully:
if the MusicKit key isn't configured, /apple/dev-token reports it and the
builder UI shows a "not configured yet" state.

Optional .env keys (add to mw-backend .env to enable the Builder):
  APPLE_MUSIC_TEAM_ID        Apple Developer Team ID (10 chars)
  APPLE_MUSIC_KEY_ID         MusicKit Key ID (10 chars)
  APPLE_MUSIC_PRIVATE_KEY    Contents of the AuthKey_XXXX.p8 file (PEM, with
                             literal \\n or real newlines), OR…
  APPLE_MUSIC_PRIVATE_KEY_PATH   …a filesystem path to the .p8 file instead.
"""

import os
import re
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor

import jwt
from flask import (
    Blueprint, render_template, request, jsonify, redirect, url_for,
    after_this_request
)

# ─── Blueprint setup ──────────────────────────────────────────────────────────

apple_bp = Blueprint(
    "apple",
    __name__,
    url_prefix="/apple",
    template_folder="templates/apple",
)

# Apple Music API base + the JS user-agent so share pages serve full HTML.
APPLE_API_BASE = "https://api.music.apple.com"
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

# Developer tokens are valid up to 6 months; we mint a 12h token and cache it.
_DEV_TOKEN_TTL = 12 * 60 * 60
_dev_token_cache = {"token": None, "exp": 0}


# ─── Apple developer-token credentials (read at request time) ─────────────────

def _team_id():
    return os.getenv("APPLE_MUSIC_TEAM_ID", "").strip()


def _key_id():
    return os.getenv("APPLE_MUSIC_KEY_ID", "").strip()


def _private_key():
    """Return the MusicKit .p8 private key PEM, from either an inline env var
    (newlines may be escaped as \\n) or a file path. Returns '' if unset."""
    inline = os.getenv("APPLE_MUSIC_PRIVATE_KEY", "")
    if inline.strip():
        return inline.replace("\\n", "\n").strip()
    path = os.getenv("APPLE_MUSIC_PRIVATE_KEY_PATH", "").strip()
    if path and Path(path).exists():
        return Path(path).read_text().strip()
    return ""


def apple_music_configured():
    """True only if all three pieces needed to sign a developer token exist."""
    return bool(_team_id() and _key_id() and _private_key())


def get_developer_token():
    """Mint (and cache) an ES256 developer token. Returns None if unconfigured."""
    if not apple_music_configured():
        return None
    now = int(time.time())
    if _dev_token_cache["token"] and _dev_token_cache["exp"] - 60 > now:
        return _dev_token_cache["token"]
    token = jwt.encode(
        {"iss": _team_id(), "iat": now, "exp": now + _DEV_TOKEN_TTL},
        _private_key(),
        algorithm="ES256",
        headers={"kid": _key_id()},
    )
    _dev_token_cache.update(token=token, exp=now + _DEV_TOKEN_TTL)
    return token


# ─── HTTP helpers (stdlib only — no extra deps) ───────────────────────────────

def _http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _api_request(method, path, dev_token, user_token=None, body=None, timeout=20):
    """Call the Apple Music API. Returns parsed JSON. Raises on HTTP error."""
    headers = {"Authorization": f"Bearer {dev_token}"}
    if user_token:
        headers["Music-User-Token"] = user_token
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{APPLE_API_BASE}{path}", data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _add_frame_headers(response):
    """Allow this page to be iframed from michaelwegter.com."""
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors 'self' https://michaelwegter.com https://www.michaelwegter.com"
    )
    response.headers.pop("X-Frame-Options", None)
    return response


# ─── Extractor: scrape a public Apple Music playlist page ─────────────────────

def _extract_storefront(playlist_url):
    """Pull the 2-letter storefront out of a music.apple.com URL (default us)."""
    m = re.search(r"music\.apple\.com/([a-z]{2})/", playlist_url)
    return m.group(1) if m else "us"


def _scrape_playlist(playlist_url):
    """Fetch a public Apple Music playlist page and parse its embedded JSON.

    Returns (playlist_name, [ "Title - Artist", … ]).  Works for both
    Apple-curated playlists and personal shared (pl.u-…) links because the
    rendered share page embeds a `serialized-server-data` JSON blob with the
    full (initial) track listing.
    """
    html = _http_get(playlist_url, headers={"User-Agent": _BROWSER_UA})

    playlist_name = "Apple Music Playlist"
    m_title = re.search(
        r'<meta property="og:title" content="([^"]+)"', html
    )
    if m_title:
        # og:title is usually "<Playlist Name> on Apple Music" (or a " - Apple Music"
        # / curator suffix on some pages). Strip whichever trailing form is present.
        name = m_title.group(1).strip()
        name = re.sub(r"\s+on Apple Music$", "", name)
        name = re.split(r"\s+[-–—]\s+Apple Music$", name)[0].strip()
        playlist_name = name or playlist_name

    tracks = []
    m = re.search(
        r'<script type="application/json" id="serialized-server-data">(.*?)</script>',
        html, re.S
    )
    if m:
        try:
            data = json.loads(m.group(1))
            tracks = _walk_for_tracks(data)
        except (ValueError, json.JSONDecodeError):
            tracks = []

    return playlist_name, tracks


def _walk_for_tracks(node, out=None):
    """Depth-first walk collecting song rows (title + artistName + trackNumber)
    in document order. Apple's serialized blob nests them under section lists."""
    if out is None:
        out = []
    if isinstance(node, dict):
        if "title" in node and "artistName" in node and "trackNumber" in node:
            title = (node.get("title") or "").strip()
            artist = (node.get("artistName") or "").strip()
            if title:
                out.append(f"{title} - {artist}" if artist else title)
        else:
            for v in node.values():
                _walk_for_tracks(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_for_tracks(v, out)
    return out


# ─── Builder: catalog search + ranking (mirrors the Spotify scorer) ───────────

def parse_song_list(text):
    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    songs = []
    for line in lines:
        parts = re.split(r"\s*[—–\-]\s*", line, maxsplit=1)
        if len(parts) == 2:
            song_name = parts[0].strip()
            artist_raw = parts[1].strip()
            artist_clean = re.sub(r"\s*\(.*?\)\s*$", "", artist_raw).strip()
            note = ""
            note_match = re.search(r"\((.+?)\)\s*$", artist_raw)
            if note_match:
                note = note_match.group(1)
            songs.append({
                "song": song_name,
                "artist": artist_clean,
                "note": note,
                "original": line,
            })
        else:
            songs.append({"song": line, "artist": "", "note": "", "original": line})
    return songs


def similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _artwork_url(artwork, size=80):
    """Apple artwork URLs are templates with {w}/{h}/{f} placeholders."""
    url = (artwork or {}).get("url", "")
    if not url:
        return ""
    return (url.replace("{w}", str(size)).replace("{h}", str(size))
               .replace("{f}", "jpg"))


def search_and_rank(dev_token, storefront, song_name, artist_name):
    term = f"{song_name} {artist_name}".strip()
    qs = urllib.parse.urlencode({"term": term, "types": "songs", "limit": 10})
    try:
        res = _api_request(
            "GET", f"/v1/catalog/{storefront}/search?{qs}", dev_token
        )
    except Exception:
        return []
    items = (((res.get("results") or {}).get("songs") or {}).get("data")) or []
    ranked = []
    for track in items:
        attrs = track.get("attributes") or {}
        track_name = attrs.get("name", "")
        track_artist = attrs.get("artistName", "")
        name_sim = similarity(song_name, track_name)
        artist_sim = similarity(artist_name, track_artist) if artist_name else 0.5
        score = (name_sim * 0.55) + (artist_sim * 0.45)
        ranked.append({
            "id": track.get("id"),
            "name": track_name,
            "artists": track_artist,
            "album": attrs.get("albumName", ""),
            "album_art": _artwork_url(attrs.get("artwork")),
            "score": round(score, 3),
            "external_url": attrs.get("url", ""),
        })
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked[:5]


# ─── Page routes ──────────────────────────────────────────────────────────────

@apple_bp.route("/")
def index():
    @after_this_request
    def add_headers(resp):
        return _add_frame_headers(resp)
    return render_template("extractor.html", configured=apple_music_configured())


@apple_bp.route("/extractor")
def extractor():
    @after_this_request
    def add_headers(resp):
        return _add_frame_headers(resp)
    return render_template("extractor.html", configured=apple_music_configured())


@apple_bp.route("/builder")
def builder():
    # The Builder is hidden until a MusicKit key is configured; send visitors
    # back to the Extractor so the option doesn't surface at all until then.
    if not apple_music_configured():
        return redirect(url_for("apple.extractor"))

    @after_this_request
    def add_headers(resp):
        return _add_frame_headers(resp)
    return render_template("builder.html", configured=True)


# ─── API routes ───────────────────────────────────────────────────────────────

@apple_bp.route("/dev-token")
def dev_token():
    """Hand MusicKit JS the developer token. Reports configured=false when the
    MusicKit key isn't set so the builder can show a graceful notice."""
    token = get_developer_token()
    if not token:
        return jsonify({"configured": False, "token": None})
    return jsonify({"configured": True, "token": token})


@apple_bp.route("/extract", methods=["POST"])
def extract():
    data = request.get_json(silent=True) or {}
    playlist_url = data.get("playlist_url", "").strip()
    if not playlist_url:
        return jsonify({"success": False, "error": "Please provide a playlist URL"})
    if "music.apple.com" not in playlist_url:
        return jsonify({"success": False, "error": "Please provide an Apple Music playlist link"})
    try:
        playlist_name, tracks = _scrape_playlist(playlist_url)
        if not tracks:
            return jsonify({
                "success": False,
                "error": "Couldn't read any tracks from that page. Make sure the "
                         "playlist link is public and shareable.",
            })
        return jsonify({
            "success": True,
            "playlist_name": playlist_name,
            "tracks": tracks,
            "count": len(tracks),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


def _search_one(args):
    """Search the Apple Music catalog for a single song entry."""
    dev_token, storefront, entry = args
    matches = search_and_rank(dev_token, storefront, entry["song"], entry["artist"])
    return {
        "query": entry,
        "matches": matches,
        "selected": matches[0]["id"] if matches else None,
    }


@apple_bp.route("/search-songs", methods=["POST"])
def search_songs():
    dev_token = get_developer_token()
    if not dev_token:
        return jsonify({
            "success": False,
            "error": "Apple Music API is not configured on the server yet.",
        })
    data = request.get_json(silent=True) or {}
    song_list_text = data.get("song_list", "").strip()
    storefront = (data.get("storefront") or "us").strip().lower()
    if not song_list_text:
        return jsonify({"success": False, "error": "Please provide a song list"})
    try:
        parsed = parse_song_list(song_list_text)
        workers = min(len(parsed), 10)
        jobs = [(dev_token, storefront, e) for e in parsed]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(_search_one, jobs))
        return jsonify({"success": True, "results": results})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@apple_bp.route("/create-playlist", methods=["POST"])
def create_playlist():
    dev_token = get_developer_token()
    if not dev_token:
        return jsonify({
            "success": False,
            "error": "Apple Music API is not configured on the server yet.",
        })
    user_token = (
        request.headers.get("Music-User-Token", "").strip()
        or (request.get_json(silent=True) or {}).get("music_user_token", "").strip()
    )
    if not user_token:
        return jsonify({"success": False, "error": "Not connected to Apple Music. Please connect first."})
    data = request.get_json(silent=True) or {}
    playlist_name = data.get("playlist_name", "My Playlist")
    track_ids = data.get("track_ids", [])
    if not track_ids:
        return jsonify({"success": False, "error": "No tracks selected"})
    try:
        body = {
            "attributes": {
                "name": playlist_name,
                "description": f"Created with Apple Music Tools on {datetime.now().strftime('%Y-%m-%d')}",
            },
            "relationships": {
                "tracks": {
                    "data": [{"id": tid, "type": "songs"} for tid in track_ids]
                }
            },
        }
        res = _api_request(
            "POST", "/v1/me/library/playlists", dev_token,
            user_token=user_token, body=body
        )
        created = (res.get("data") or [{}])[0]
        playlist_id = created.get("id", "")
        return jsonify({
            "success": True,
            "playlist_name": playlist_name,
            "playlist_id": playlist_id,
            "playlist_url": f"https://music.apple.com/library/playlist/{playlist_id}" if playlist_id else "",
            "track_count": len(track_ids),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
