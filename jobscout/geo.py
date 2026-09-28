"""Geography: offline gazetteer, Census geocoder, haversine, optional ORS drive times.

The gazetteer (jobscout/data/places.json) is built from the Census 2024 Gazetteer
national files for places and county subdivisions, restricted to MN, WI, IA, ND, SD:

    python -m jobscout.geo build 2024_Gaz_place_national.txt 2024_Gaz_cousubs_national.txt

Keys are "<name>|<ST>": the name lowercased with one trailing LSAD word removed
("Maplewood city" → "maplewood", "Birchwood Village city" → "birchwood village"),
periods dropped and "saint " folded to "st " so "Saint Paul"/"St. Paul" both hit "st paul|MN".
"""
import json
import logging
import math
import re
import sys
from functools import lru_cache
from urllib.parse import urlencode

from . import config, db
from .http import FetchError, get_fetcher

log = logging.getLogger("jobscout")

GAZETTEER_STATES = ("MN", "WI", "IA", "ND", "SD")
PLACES_FILE = config.PACKAGE_DATA_DIR / "places.json"
_LSAD_SUFFIXES = (" city", " village", " town", " township", " cdp", " borough", " ut",
                  " unorganized territory", " (balance)")
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
ORS_MATRIX_URL = "https://api.openrouteservice.org/v2/matrix/driving-car"


def normalize_place_name(name) -> str:
    n = (name or "").strip().lower().replace(".", "")
    n = re.sub(r"\s+", " ", n)
    n = re.sub(r"^saint ", "st ", n)
    n = re.sub(r"^ste ", "ste ", n)
    return n


def _strip_lsad(name) -> str:
    low = name.lower()
    for suffix in _LSAD_SUFFIXES:
        if low.endswith(suffix) and len(low) > len(suffix):
            return name[: -len(suffix)]
    return name


@lru_cache(maxsize=1)
def _places():
    try:
        return json.loads(PLACES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("geo: gazetteer %s missing or unreadable", PLACES_FILE)
        return {}


def find_city(city, state=None):
    """(lat, lng, state) for a city in the regional gazetteer; tries MN, WI, IA, ND, SD when state is None."""
    if not city:
        return None
    name = normalize_place_name(city)
    states = [state.upper()] if state else list(GAZETTEER_STATES)
    for st in states:
        hit = _places().get(f"{name}|{st}")
        if hit:
            return hit[0], hit[1], st
    return None


def lookup_city(city, state=None):
    """(lat, lng) for a city in the regional gazetteer, or None."""
    hit = find_city(city, state)
    return (hit[0], hit[1]) if hit else None


def haversine_miles(lat1, lng1, lat2, lng2) -> float:
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def miles_between(a_lat, a_lng, b_lat, b_lng):
    if None in (a_lat, a_lng, b_lat, b_lng):
        return None
    return round(haversine_miles(a_lat, a_lng, b_lat, b_lng), 1)


def census_geocode(address, fetcher=None):
    """Street address → (lat, lng) via the Census one-line geocoder, or None."""
    if not address:
        return None
    url = CENSUS_URL + "?" + urlencode({"address": address, "benchmark": "Public_AR_Current", "format": "json"})
    try:
        data = (fetcher or get_fetcher()).get_json(url)
    except (FetchError, ValueError) as exc:
        log.info("geo: census geocoder failed for %r: %s", address, exc)
        return None
    matches = (data.get("result") or {}).get("addressMatches") or []
    if not matches:
        return None
    coords = matches[0].get("coordinates") or {}
    if coords.get("y") is None:
        return None
    return float(coords["y"]), float(coords["x"])


def geocode_address(address, fetcher=None):
    """(lat, lng, precision) for a free-form address: Census first, then "City, ST" gazetteer lookup."""
    hit = census_geocode(address, fetcher)
    if hit:
        return hit[0], hit[1], "address"
    from .normalize import parse_location
    loc = parse_location(address)
    city_hit = lookup_city(loc.city, loc.state) if loc.city else None
    if city_hit:
        return city_hit[0], city_hit[1], "city"
    return None


# ── drive times (optional) ──────────────────────────────────────────────────

def _coord_key(lat, lng):
    return round(lat, 4), round(lng, 4)


def cached_minutes(conn, profile_id):
    """{(lat, lng): minutes} for a profile from the distances cache."""
    rows = conn.execute("SELECT lat, lng, minutes FROM distances WHERE profile_id=?", (profile_id,))
    return {(r["lat"], r["lng"]): r["minutes"] for r in rows}


def refresh_drive_minutes(conn, profile, coords, fetcher=None, batch=500):
    """Fill `distances` with ORS driving minutes for coordinates not yet cached.
    No-op unless ORS_API_KEY is set; failures are logged and skipped."""
    key = config.ors_api_key()
    if not key or profile.get("home_lat") is None:
        return 0
    have = cached_minutes(conn, profile["id"])
    todo = sorted({_coord_key(la, ln) for la, ln in coords if la is not None} - set(have))
    fetcher = fetcher or get_fetcher()
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        body = {"locations": [[profile["home_lng"], profile["home_lat"]]] + [[ln, la] for la, ln in chunk],
                "sources": [0], "destinations": list(range(1, len(chunk) + 1)), "metrics": ["duration"]}
        try:
            data = fetcher.post_json(ORS_MATRIX_URL, body, headers={"Authorization": key})
        except (FetchError, ValueError) as exc:
            log.warning("geo: ORS matrix failed: %s", exc)
            return done
        durations = (data.get("durations") or [[]])[0]
        now = db.now_iso()
        for (la, ln), seconds in zip(chunk, durations):
            miles = miles_between(profile["home_lat"], profile["home_lng"], la, ln)
            minutes = round(seconds / 60, 1) if seconds is not None else None
            conn.execute("INSERT OR REPLACE INTO distances(profile_id, lat, lng, miles, minutes, computed_at) "
                         "VALUES(?,?,?,?,?,?)", (profile["id"], la, ln, miles, minutes, now))
            done += 1
        conn.commit()
    return done


# ── gazetteer build ─────────────────────────────────────────────────────────

def _read_gazetteer(path):
    with open(path, encoding="latin-1") as fh:
        header = [h.strip() for h in fh.readline().split("\t")]
        for line in fh:
            row = dict(zip(header, (c.strip() for c in line.split("\t"))))
            if row.get("USPS") in GAZETTEER_STATES:
                yield row


def build_places(place_file, cousub_file, out_path=PLACES_FILE):
    """Write the compact regional gazetteer. Places win over county subdivisions of the same name."""
    places = {}
    for path in (place_file, cousub_file):
        for row in _read_gazetteer(path):
            name = normalize_place_name(_strip_lsad(row["NAME"]))
            key = f"{name}|{row['USPS']}"
            if key not in places:
                places[key] = [round(float(row["INTPTLAT"]), 4), round(float(row["INTPTLONG"]), 4)]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(places, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    _places.cache_clear()
    return len(places)


if __name__ == "__main__":  # python -m jobscout.geo build PLACE_FILE COUSUB_FILE
    if len(sys.argv) == 4 and sys.argv[1] == "build":
        print(f"wrote {build_places(sys.argv[2], sys.argv[3])} entries to {PLACES_FILE}")
    else:
        print(__doc__)
