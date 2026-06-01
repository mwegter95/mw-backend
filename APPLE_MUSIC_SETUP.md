# Apple Music Tools — setup

Mounted at `/apple` on mw-backend (see `apple_music_blueprint.py`). Mirrors the
Extractor + Builder pieces of the Spotify Super User Tools.

## What works without any setup

**Extractor** (`/apple/extractor`) — needs **no credentials**. It scrapes the
public Apple Music playlist share page and parses the embedded
`serialized-server-data` JSON. Works for Apple-curated playlists and personal
shared (`pl.u-…`) links. (Apple renders ~100 tracks into the initial page; very
long playlists may be capped at that initial chunk.)

## What needs a MusicKit key

**Builder** (`/apple/builder`) — catalog search and playlist creation are gated
behind an Apple developer token. Until the key is configured, the builder shows
a "not configured yet" notice and the Extractor keeps working.

Apple Music has **no server-side OAuth** like Spotify. The split is:
- **Catalog search** uses a *developer token* (ES256 JWT we sign server-side
  from the MusicKit `.p8` key).
- **Creating the playlist** uses a *Music User Token* that **MusicKit JS** gets
  in the browser after the visitor signs in with their Apple ID (they need an
  active Apple Music subscription). The frontend sends that token to
  `/apple/create-playlist` in the `Music-User-Token` header.

### Getting the credentials (one-time, requires a paid Apple Developer account)

1. <https://developer.apple.com/account> → **Certificates, Identifiers & Profiles**.
2. **Identifiers** → add a **Media ID** (MusicKit identifier).
3. **Keys** → create a key with **MusicKit** enabled. Download the
   `AuthKey_XXXXXXXXXX.p8` (you can only download it once). Note the **Key ID**.
4. Your **Team ID** is shown at the top-right of the developer account / in
   Membership details.

### Add to `mw-backend/.env`

```ini
APPLE_MUSIC_TEAM_ID=ABCDE12345
APPLE_MUSIC_KEY_ID=FGHIJ67890
# Either point at the .p8 file…
APPLE_MUSIC_PRIVATE_KEY_PATH=/secure/path/AuthKey_FGHIJ67890.p8
# …or inline the PEM (escape newlines as \n):
# APPLE_MUSIC_PRIVATE_KEY=-----BEGIN PRIVATE KEY-----\nMIG...\n-----END PRIVATE KEY-----
```

Restart the server. `GET /apple/dev-token` should return `{"configured": true, …}`
and the Builder will enable search + the "Connect Apple Music" button.

Notes:
- The Builder must be served over HTTPS for MusicKit JS (production
  `api.michaelwegter.com` already is).
- `PyJWT` + `cryptography` (already in `requirements.txt`) sign the ES256 token.
- The developer token is cached in-process for 12h and re-minted on demand.
