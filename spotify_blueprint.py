"""
Spotify Super User Tools — Flask Blueprint
Mounts under /spotify on the mw-backend server.

Features available on the web:
  - Playlist Extractor  (no auth)
  - Playlist Builder    (Spotify OAuth)
  - Cleanify            (Spotify OAuth)

Features NOT available on the web (local tool only):
  - Explicit Marker     (requires server-local MP3 files)
  - Clean Marker        (requires server-local MP3 files)

Required .env keys (add to mw-backend .env):
  SPOTIFY_CLIENT_ID
  SPOTIFY_CLIENT_SECRET
  SPOTIFY_REDIRECT_URI   (defaults to https://api.michaelwegter.com/spotify/callback)
"""

import os
import re
import uuid
from pathlib import Path
from datetime import datetime
from difflib import SequenceMatcher

from flask import (
    Blueprint, render_template, request, jsonify,
    session, redirect, url_for, after_this_request
)
from spotipy import Spotify
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

# ─── Blueprint setup ──────────────────────────────────────────────────────────

# template_folder is relative to this file
spotify_bp = Blueprint(
    "spotify",
    __name__,
    url_prefix="/spotify",
    template_folder="templates/spotify",
)

# Cache directory lives inside mw-backend/data/
CACHE_DIR = Path(__file__).parent / "data" / "spotify_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ─── Spotify credentials (read at request time so .env reload works) ─────────

def _creds():
    return os.getenv("SPOTIFY_CLIENT_ID"), os.getenv("SPOTIFY_CLIENT_SECRET")

def _redirect_uri():
    return os.getenv(
        "SPOTIFY_REDIRECT_URI",
        "https://api.michaelwegter.com/spotify/callback"
    )

SCOPE = "playlist-modify-public playlist-modify-private"

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _add_frame_headers(response):
    """Allow this page to be iframed from michaelwegter.com."""
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors 'self' https://michaelwegter.com https://www.michaelwegter.com"
    )
    response.headers.pop("X-Frame-Options", None)
    return response


def get_client_credentials_spotify():
    client_id, client_secret = _creds()
    auth_manager = SpotifyClientCredentials(
        client_id=client_id,
        client_secret=client_secret
    )
    return Spotify(auth_manager=auth_manager)


def get_oauth_manager():
    if "spotify_uuid" not in session:
        session["spotify_uuid"] = str(uuid.uuid4())
    cache_path = str(CACHE_DIR / session["spotify_uuid"])
    client_id, client_secret = _creds()
    return SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=_redirect_uri(),
        scope=SCOPE,
        cache_path=cache_path,
        show_dialog=True,
    )


def get_authenticated_spotify():
    """Returns a Spotify client if the user has a valid OAuth token, else None."""
    try:
        oauth = get_oauth_manager()
        token_info = oauth.get_cached_token()
        if not token_info:
            return None
        if oauth.is_token_expired(token_info):
            token_info = oauth.refresh_access_token(token_info["refresh_token"])
            if not token_info:
                return None
        return Spotify(auth=token_info["access_token"])
    except Exception:
        return None


def extract_playlist_id(playlist_url):
    if "playlist/" in playlist_url:
        return playlist_url.split("playlist/")[1].split("?")[0]
    return playlist_url


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


def search_and_rank(sp, song_name, artist_name):
    queries = [
        f'track:"{song_name}" artist:"{artist_name}"',
        f"{song_name} {artist_name}",
        f'track:"{song_name}" {artist_name}',
    ]
    seen_ids = set()
    all_results = []
    for query in queries:
        try:
            results = sp.search(q=query, type="track", limit=10)
            for track in results["tracks"]["items"]:
                if track["id"] not in seen_ids:
                    seen_ids.add(track["id"])
                    track_artists = ", ".join([a["name"] for a in track["artists"]])
                    name_sim = similarity(song_name, track["name"])
                    artist_sim = similarity(artist_name, track_artists)
                    individual_sims = [similarity(artist_name, a["name"]) for a in track["artists"]]
                    best_artist_sim = max([artist_sim] + individual_sims)
                    score = (name_sim * 0.45) + (best_artist_sim * 0.45) + (track["popularity"] / 100 * 0.10)
                    all_results.append({
                        "id": track["id"],
                        "uri": track["uri"],
                        "name": track["name"],
                        "artists": track_artists,
                        "album": track["album"]["name"],
                        "album_art": track["album"]["images"][-1]["url"] if track["album"]["images"] else "",
                        "preview_url": track.get("preview_url", ""),
                        "popularity": track["popularity"],
                        "score": round(score, 3),
                        "external_url": track["external_urls"].get("spotify", ""),
                    })
        except Exception:
            continue
    all_results.sort(key=lambda x: x["score"], reverse=True)
    return all_results[:5]


def find_clean_version(original_track, candidates):
    clean_candidates = [c for c in candidates if not c.get("explicit")]
    if not clean_candidates:
        return None
    original_duration = original_track["duration_ms"]
    for candidate in clean_candidates:
        if abs(candidate["duration_ms"] - original_duration) < 3000:
            return candidate
    keywords = ["clean", "radio edit", "edited", "clean version"]
    for candidate in clean_candidates:
        name_album = f"{candidate['name']} {candidate['album']['name']}".lower()
        if any(keyword in name_album for keyword in keywords):
            return candidate
    clean_candidates.sort(key=lambda x: x.get("popularity", 0), reverse=True)
    return clean_candidates[0]


# ─── Page routes ──────────────────────────────────────────────────────────────

@spotify_bp.route("/")
def index():
    return redirect(url_for("spotify.extractor"))


@spotify_bp.route("/extractor")
def extractor():
    @after_this_request
    def add_headers(resp):
        return _add_frame_headers(resp)
    return render_template("extractor.html")


@spotify_bp.route("/builder")
def builder():
    @after_this_request
    def add_headers(resp):
        return _add_frame_headers(resp)
    authenticated = get_authenticated_spotify() is not None
    return render_template("builder.html", authenticated=authenticated)


@spotify_bp.route("/cleanify")
def cleanify():
    @after_this_request
    def add_headers(resp):
        return _add_frame_headers(resp)
    authenticated = get_authenticated_spotify() is not None
    return render_template("cleanify.html", authenticated=authenticated)


@spotify_bp.route("/marker")
def marker():
    @after_this_request
    def add_headers(resp):
        return _add_frame_headers(resp)
    return render_template("marker.html")


@spotify_bp.route("/clean-marker")
def clean_marker():
    @after_this_request
    def add_headers(resp):
        return _add_frame_headers(resp)
    return render_template("clean_marker.html")


# ─── Auth routes ──────────────────────────────────────────────────────────────

@spotify_bp.route("/login")
def login():
    if "spotify_uuid" not in session:
        session["spotify_uuid"] = str(uuid.uuid4())
    oauth = get_oauth_manager()
    auth_url = oauth.get_authorize_url()
    return redirect(auth_url)


@spotify_bp.route("/callback")
def callback():
    oauth = get_oauth_manager()
    code = request.args.get("code")
    if code:
        try:
            oauth.get_access_token(code)
        except Exception:
            pass
    # Render a close-popup page; if opened as popup this closes the window,
    # otherwise redirect to builder.
    return render_template("callback_done.html")


@spotify_bp.route("/logout")
def logout():
    cache_path = CACHE_DIR / session.get("spotify_uuid", "default")
    if cache_path.exists():
        cache_path.unlink()
    session.pop("spotify_uuid", None)
    return redirect(url_for("spotify.builder"))


# ─── API routes ───────────────────────────────────────────────────────────────

@spotify_bp.route("/extract", methods=["POST"])
def extract():
    data = request.get_json(silent=True) or {}
    playlist_url = data.get("playlist_url", "").strip()
    if not playlist_url:
        return jsonify({"success": False, "error": "Please provide a playlist URL"})
    client_id, client_secret = _creds()
    if not client_id or not client_secret:
        return jsonify({
            "success": False,
            "error": "Spotify API credentials are not configured on the server.",
        })
    try:
        sp = get_client_credentials_spotify()
        playlist_id = extract_playlist_id(playlist_url)
        playlist_info = sp.playlist(playlist_id)
        playlist_name = playlist_info["name"]
        tracks = []
        results = sp.playlist_tracks(playlist_id)
        while results:
            for item in results["items"]:
                track = item["track"]
                if track:
                    artists = ", ".join([a["name"] for a in track["artists"]])
                    tracks.append(f"{track['name']} - {artists}")
            results = sp.next(results) if results["next"] else None
        return jsonify({
            "success": True,
            "playlist_name": playlist_name,
            "tracks": tracks,
            "count": len(tracks),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@spotify_bp.route("/search-songs", methods=["POST"])
def search_songs():
    data = request.get_json(silent=True) or {}
    song_list_text = data.get("song_list", "").strip()
    if not song_list_text:
        return jsonify({"success": False, "error": "Please provide a song list"})
    try:
        parsed = parse_song_list(song_list_text)
        sp = get_client_credentials_spotify()
        results = []
        for entry in parsed:
            matches = search_and_rank(sp, entry["song"], entry["artist"])
            results.append({
                "query": entry,
                "matches": matches,
                "selected": matches[0]["uri"] if matches else None,
            })
        return jsonify({"success": True, "results": results})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@spotify_bp.route("/create-playlist", methods=["POST"])
def create_playlist():
    sp = get_authenticated_spotify()
    if not sp:
        return jsonify({"success": False, "error": "Not connected to Spotify. Please connect first."})
    data = request.get_json(silent=True) or {}
    playlist_name = data.get("playlist_name", "My Playlist")
    track_uris = data.get("track_uris", [])
    if not track_uris:
        return jsonify({"success": False, "error": "No tracks selected"})
    try:
        user = sp.current_user()
        playlist = sp.user_playlist_create(
            user=user["id"],
            name=playlist_name,
            public=True,
            description=f"Created with Spotify Super User Tools on {datetime.now().strftime('%Y-%m-%d')}",
        )
        for i in range(0, len(track_uris), 100):
            sp.playlist_add_items(playlist["id"], track_uris[i:i + 100])
        return jsonify({
            "success": True,
            "playlist_url": playlist["external_urls"]["spotify"],
            "playlist_name": playlist_name,
            "track_count": len(track_uris),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@spotify_bp.route("/cleanify-playlist", methods=["POST"])
def cleanify_playlist():
    sp = get_authenticated_spotify()
    if not sp:
        return jsonify({"success": False, "error": "Not connected to Spotify. Please connect first."})
    data = request.get_json(silent=True) or {}
    playlist_url = data.get("playlist_url", "").strip()
    output_mode = data.get("output_mode", "all")
    if not playlist_url:
        return jsonify({"success": False, "error": "Please provide a playlist URL or ID"})
    try:
        playlist_id = extract_playlist_id(playlist_url)
        playlist_info = sp.playlist(playlist_id)
        original_name = playlist_info["name"]

        tracks = []
        results = sp.playlist_tracks(playlist_id)
        while results:
            for item in results["items"]:
                if item["track"]:
                    tracks.append(item["track"])
            results = sp.next(results) if results["next"] else None

        new_uris = []
        swapped = []
        not_found = []
        already_clean = []

        for track in tracks:
            uri = track["uri"]
            was_explicit = track.get("explicit")
            if was_explicit:
                query = f"track:{track['name']} artist:{track['artists'][0]['name']}"
                search_results = sp.search(q=query, type="track", limit=10)
                clean_version = find_clean_version(track, search_results["tracks"]["items"])
                if clean_version:
                    uri = clean_version["uri"]
                    swapped.append({
                        "original": f"{track['name']} - {track['artists'][0]['name']}",
                        "replacement": f"{clean_version['name']} - {clean_version['artists'][0]['name']}",
                        "uri": uri,
                    })
                else:
                    not_found.append(f"{track['name']} - {track['artists'][0]['name']}")
            else:
                already_clean.append(f"{track['name']} - {track['artists'][0]['name']}")

            if output_mode == "all":
                new_uris.append(uri)
            elif output_mode == "cleaned_only" and was_explicit and uri != track["uri"]:
                new_uris.append(uri)

        if not new_uris:
            return jsonify({"success": False, "error": "No tracks to add to the new playlist."})

        user = sp.current_user()
        new_name = f"{original_name} (Clean)"
        new_playlist = sp.user_playlist_create(
            user=user["id"],
            name=new_name,
            public=True,
            description=f"Clean version created with Spotify Super User Tools on {datetime.now().strftime('%Y-%m-%d')}",
        )
        for i in range(0, len(new_uris), 100):
            sp.playlist_add_items(new_playlist["id"], new_uris[i:i + 100])

        return jsonify({
            "success": True,
            "playlist_name": new_name,
            "playlist_url": new_playlist["external_urls"]["spotify"],
            "swapped": swapped,
            "not_found": not_found,
            "already_clean_count": len(already_clean),
            "output_mode": output_mode,
            "total_in_new_playlist": len(new_uris),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
