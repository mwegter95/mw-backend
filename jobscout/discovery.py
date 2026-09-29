"""Company discovery (contract §9.3): the app finds local employers itself.

Query plan = (query words for each discover industry + each free-text keyword) × towns within the
profile radius, spread so every term gets several towns, keywords first, capped at max_queries.
Sources, all fail-soft:
  maps    Google Maps via Playwright (clientfinder._search_google_maps)
  search  DuckDuckGo + Bing + Yellow Pages via Playwright (clientfinder helpers)
  osm     OpenStreetMap Overpass: named features with a website inside the radius
  places  Google Places API (New) Text Search, only when GOOGLE_PLACES_API_KEY is set
Candidates are deduped by normalised domain and filtered (aggregators, national brands, junk, .gov);
new ones become companies (source 'discovery', status 'pending'), then get pipeline steps 1–2 at once
and steps 3–4 queued.
"""
import asyncio
import importlib.util
import logging
import math
import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config, db, geo, runs, taxonomy
from .http import APP_UA, FetchError, get_fetcher

log = logging.getLogger("jobscout")

DEFAULT_MAX_QUERIES = 60
DEFAULT_SOURCES = ["maps", "search", "osm"]
PER_QUERY_SOURCES = ("maps", "search", "places")
OVERPASS_URLS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_FIELDS = ("places.displayName,places.websiteUri,places.formattedAddress,places.location,"
                 "places.primaryType,places.types,nextPageToken")

# Twin Cities metro + western Wisconsin within ~45 mi of Birchwood Village.
DEFAULT_TOWNS = [
    ("Stillwater", "MN"), ("Oakdale", "MN"), ("Woodbury", "MN"), ("Maplewood", "MN"), ("White Bear Lake", "MN"),
    ("Vadnais Heights", "MN"), ("Shoreview", "MN"), ("Roseville", "MN"), ("St. Paul", "MN"), ("Mahtomedi", "MN"),
    ("Lake Elmo", "MN"), ("Hugo", "MN"), ("Forest Lake", "MN"), ("Lino Lakes", "MN"), ("Blaine", "MN"),
    ("Arden Hills", "MN"), ("New Brighton", "MN"), ("Little Canada", "MN"), ("North St. Paul", "MN"),
    ("Cottage Grove", "MN"), ("Hastings", "MN"), ("Inver Grove Heights", "MN"), ("Eagan", "MN"),
    ("South St. Paul", "MN"), ("Mendota Heights", "MN"), ("Burnsville", "MN"), ("Lakeville", "MN"),
    ("Bloomington", "MN"), ("Edina", "MN"), ("Minneapolis", "MN"), ("St. Louis Park", "MN"), ("Plymouth", "MN"),
    ("Minnetonka", "MN"), ("Eden Prairie", "MN"), ("Brooklyn Park", "MN"), ("Maple Grove", "MN"),
    ("Coon Rapids", "MN"), ("Anoka", "MN"), ("Fridley", "MN"), ("Chaska", "MN"), ("Shakopee", "MN"),
    ("Bayport", "MN"), ("Hudson", "WI"), ("River Falls", "WI"), ("New Richmond", "WI"), ("Somerset", "WI"),
]

# Searchable business phrases per taxonomy slug.
INDUSTRY_QUERY_WORDS = {
    "mfg_plastics_packaging": ["plastic injection molding company", "plastics manufacturer", "packaging manufacturer",
                               "thermoforming company"],
    "mfg_building_materials": ["building materials manufacturer", "window and door manufacturer",
                               "precast concrete manufacturer", "cabinet manufacturer"],
    "mfg_chemicals_coatings": ["chemical manufacturer", "adhesives manufacturer", "coatings manufacturer"],
    "mfg_industrial_equipment": ["industrial equipment manufacturer", "machinery manufacturer",
                                 "automation equipment company"],
    "mfg_electrical_electronics": ["electronics manufacturer", "electrical products manufacturer",
                                   "circuit board manufacturer"],
    "mfg_medical_devices": ["medical device manufacturer", "medical device company"],
    "mfg_food_beverage": ["food manufacturer", "food processing company", "beverage company"],
    "mfg_consumer_products": ["consumer products company", "outdoor products manufacturer", "sporting goods manufacturer"],
    "mfg_metals_fabrication": ["metal fabrication company", "precision machining company", "metal stamping company"],
    "mfg_other": ["manufacturing company", "contract manufacturer"],
    "construction_real_estate": ["general contractor", "construction company headquarters", "commercial real estate company"],
    "distribution_logistics": ["wholesale distributor", "logistics company", "industrial distributor"],
    "retail_ecommerce": ["ecommerce company", "retail company headquarters"],
    "agriculture": ["agribusiness company", "agricultural products company"],
    "tech_software": ["software company", "technology company headquarters"],
    "it_services": ["IT services company", "IT consulting firm"],
    "professional_services": ["consulting firm", "engineering firm", "architecture firm"],
    "agency_marketing": ["marketing agency", "advertising agency", "public relations firm"],
    "financial_insurance": ["insurance company headquarters", "financial services company", "credit union"],
    "healthcare": ["health system", "healthcare company headquarters"],
    "education": ["college", "university", "private school"],
    "nonprofit": ["nonprofit organization headquarters", "trade association"],
    "government": ["county government", "city government offices"],
    "energy_utilities": ["energy company", "electric utility", "solar company"],
    "hospitality_media": ["media company", "hospitality company headquarters", "publishing company"],
    "other": ["company headquarters"],
}

# OSM tag values that hint at an industry (used as the categoriser's prior).
_OSM_INDUSTRY = [
    (re.compile(r"plastic|packag", re.I), "mfg_plastics_packaging"),
    (re.compile(r"concrete|cement|building_materials|brick|glass|lumber|sawmill|window|cabinet", re.I), "mfg_building_materials"),
    (re.compile(r"chemical|paint|coating", re.I), "mfg_chemicals_coatings"),
    (re.compile(r"electronic|electrical", re.I), "mfg_electrical_electronics"),
    (re.compile(r"metal|steel|weld|machin|foundry", re.I), "mfg_metals_fabrication"),
    (re.compile(r"food|brewery|bakery|dairy|beverage|winery|distillery", re.I), "mfg_food_beverage"),
]


# ── plan ────────────────────────────────────────────────────────────────────

def towns_within(home_lat, home_lng, radius_miles, towns=DEFAULT_TOWNS):
    """Default towns resolvable in the gazetteer and within the radius, nearest first."""
    out = []
    for name, st in towns:
        hit = geo.lookup_city(name, st)
        if not hit:
            continue
        miles = geo.haversine_miles(home_lat, home_lng, hit[0], hit[1])
        if miles <= radius_miles:
            out.append({"name": name, "state": st, "miles": round(miles, 1)})
    return sorted(out, key=lambda t: t["miles"])


_BUSINESS_WORD = re.compile(r"\b(compan(y|ies)|manufactur\w*|makers?|suppliers?|producers?|factory|plants?|firms?|"
                            r"agenc(y|ies)|contractors?|distributors?|fabricators?|shops?|brewer(y|ies)|mills?)\b", re.I)


def business_phrase(keyword):
    """A bare process or product ("commercial printing") searches as articles about the word; asking for a
    company ("commercial printing company") returns the businesses that do it."""
    keyword = keyword.strip()
    return keyword if _BUSINESS_WORD.search(keyword) else f"{keyword} company"


def broad_terms():
    """No industries or keywords chosen: one phrase for every industry, so a search covers all kinds of local
    employers evenly (OpenStreetMap already sweeps the whole radius regardless of industry)."""
    return [(INDUSTRY_QUERY_WORDS[slug][0], slug) for slug in taxonomy.INDUSTRY_IDS if slug in INDUSTRY_QUERY_WORDS]


def query_terms(industries, keywords):
    """[(phrase, industry slug | None)] — free-text keywords first, then industry phrases; everything
    (broad_terms) when neither is given."""
    terms = [(business_phrase(k), None) for k in keywords or [] if k and k.strip()]
    for slug in industries or []:
        terms += [(w, slug) for w in INDUSTRY_QUERY_WORDS.get(slug, [])]
    if not terms:
        terms = broad_terms()
    seen, out = set(), []
    for phrase, slug in terms:
        if phrase.lower() not in seen:
            seen.add(phrase.lower())
            out.append((phrase, slug))
    return out


def _town_list(names):
    """["Oakdale, MN", "Hudson WI", "Stillwater"] → [(name, ST)] (state defaults to MN)."""
    out = []
    for raw in names or []:
        m = re.match(r"^\s*(.+?)[,\s]+([A-Za-z]{2})\s*$", raw)
        out.append((m.group(1).strip(), m.group(2).upper()) if m else (raw.strip(), "MN"))
    return out


def build_plan(industries, keywords, sources, max_queries, home_lat, home_lng, radius_miles, towns=None):
    """Preview/plan: {"towns":[…], "queries":[{source, query, town, industry}], "total"}.

    Terms take turns: round r gives term i the town at index (r·len(terms)+i) mod n, so every term
    is searched in several different towns before any budget runs out. `towns` overrides the default
    town list (still limited to the radius).
    """
    sources = [s for s in (sources or DEFAULT_SOURCES) if s in ("maps", "search", "osm", "places")]
    if "places" in sources and not config.google_places_api_key():
        sources.remove("places")
    towns = towns_within(home_lat, home_lng, radius_miles, _town_list(towns) if towns else DEFAULT_TOWNS)
    terms = query_terms(industries, keywords)
    per_query = [s for s in sources if s in PER_QUERY_SOURCES]
    queries = []
    budget = max(1, int(max_queries or DEFAULT_MAX_QUERIES))
    if "osm" in sources:
        queries.append({"source": "osm", "query": "OpenStreetMap businesses",
                        "town": f"within {radius_miles} mi of home", "industry": None})
    if towns and terms and per_query:
        pairs_needed = len(towns) * len(terms)
        for k in range(pairs_needed):
            if len(queries) >= budget:
                break
            r, i = divmod(k, len(terms))
            phrase, slug = terms[i]
            town = towns[(r * len(terms) + i) % len(towns)]
            for source in per_query:
                if len(queries) < budget:
                    queries.append({"source": source, "query": phrase, "town": f"{town['name']}, {town['state']}",
                                    "industry": slug})
    return {"towns": towns, "queries": queries[:budget], "total": len(queries[:budget])}


def profile_defaults(profile):
    """Discovery defaults for a profile: its hunt settings, else the industries it wants, else everything."""
    p = profile or {}
    return {
        "industries": p.get("discover_industries") or p.get("industries_want") or [],
        "keywords": p.get("discover_keywords") or [],
        "sources": p.get("discover_sources") or DEFAULT_SOURCES,
        "max_queries": DEFAULT_MAX_QUERIES,
        "home_lat": p.get("home_lat") or config.HOME_LAT,
        "home_lng": p.get("home_lng") or config.HOME_LNG,
        "radius_miles": p.get("radius_miles") or 35,
    }


def resolve_options(profile, options=None):
    opts = profile_defaults(profile)
    opts.update({k: v for k, v in (options or {}).items() if v not in (None, [], "")})
    return opts


def plan_for(profile, options=None):
    opts = resolve_options(profile, options)
    return build_plan(opts["industries"], opts["keywords"], opts["sources"], opts["max_queries"],
                      opts["home_lat"], opts["home_lng"], opts["radius_miles"], opts.get("towns"))


# ── candidate filtering / recording ─────────────────────────────────────────

def _cf():
    try:
        import clientfinder_blueprint as cf  # mw-backend root module
        return cf
    except Exception:  # noqa: BLE001 — discovery still works for osm/places without it
        return None


_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")


def normalize_domain(url):
    """"https://www.Acme.com/about?x" → "acme.com"; None for junk."""
    if not url:
        return None
    d = re.sub(r"^[a-z]+://", "", str(url).strip().lower())
    d = re.split(r"[/?#]", d)[0].split(":")[0].removeprefix("www.").strip(".")
    return d if _DOMAIN_RE.match(d) else None


def filter_reason(domain):
    """Why a candidate domain is dropped (None = keep)."""
    if not domain:
        return "invalid"
    if domain.endswith((".gov", ".mil")) or re.search(r"\.[a-z]{2}\.us$", domain):  # .mn.us etc. = localities
        return "government"
    cf = _cf()
    if cf:
        if not cf._looks_like_real_domain(domain):
            return "invalid"
        if cf._is_aggregator(domain):
            return "aggregator"
        if cf._is_national_brand(domain):
            return "national_brand"
    return None


def _city_state(address):
    m = re.search(r"([A-Z][A-Za-z.' -]+),\s*([A-Z]{2})\b(?:\s*\d{5})?", address or "")
    return (m.group(1).strip(), m.group(2)) if m else (None, None)


def record_candidates(conn, candidates, stats, seen):
    """Insert new companies / annotate known ones. Returns new company ids."""
    now = db.now_iso()
    new_ids = []
    for cand in candidates:
        stats["candidates"] += 1
        stats["by_source"][cand["source"]] = stats["by_source"].get(cand["source"], 0) + 1
        domain = normalize_domain(cand.get("website"))
        if filter_reason(domain):
            stats["filtered"] += 1
            continue
        if domain in seen:
            continue
        seen.add(domain)
        via = f'{cand["source"]}: "{cand["query"]}" {cand.get("town") or ""}'.strip()
        row = conn.execute("SELECT id, facts FROM companies WHERE domain=?", (domain,)).fetchone()
        if row:
            facts = db.loads(row["facts"], {})
            facts["discovered_via_all"] = list(dict.fromkeys((facts.get("discovered_via_all") or []) + [via]))[-10:]
            db.update(conn, "companies", "id", row["id"], {"facts": db.dumps(facts)})
            stats["known"] += 1
            continue
        city, state = cand.get("city"), cand.get("state")
        if cand.get("address") and not state:
            parsed_city, parsed_state = _city_state(cand["address"])
            city, state = (parsed_city, parsed_state) if parsed_city else (city, state)
        lat, lng = cand.get("lat"), cand.get("lng")
        facts = {"_query_industry": cand.get("industry"), "discovery": {k: cand.get(k) for k in
                                                                         ("name", "address", "category", "source")}}
        cols = {
            "name": (cand.get("name") or domain)[:120], "domain": domain, "homepage_url": f"https://{domain}",
            "hq_address": cand.get("address"), "hq_city": city, "hq_state": state, "lat": lat, "lng": lng,
            "geo_precision": "address" if lat is not None else None, "source": "discovery", "source_detail": via,
            "discovered_via": via, "discovered_at": now, "status": "pending", "enrich_status": "pending",
            "employee_band": "unknown", "facts": db.dumps(facts), "created_at": now, "updated_at": now,
        }
        if lat is None and city:
            hit = geo.find_city(city, state)
            if hit:
                cols.update(lat=hit[0], lng=hit[1], geo_precision="city", hq_state=state or hit[2])
        cur = conn.execute(f"INSERT INTO companies({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                           tuple(cols.values()))
        new_ids.append(cur.lastrowid)
        stats["new_companies"] += 1
    conn.commit()
    return new_ids


# ── sources ─────────────────────────────────────────────────────────────────

# OSM office/craft kinds that are almost always one-person or storefront outfits (a realtor, an
# insurance agent's office, a locksmith). Dropping them keeps a metro-wide OSM sweep to employers worth
# categorizing; anything skipped can still be added by URL.
_OSM_SKIP_CATEGORIES = {
    "estate_agent", "insurance", "tax_advisor", "notary", "government", "political_party", "religion",
    "therapist", "physician", "dentist", "coworking", "travel_agent", "employment_agency",
    "financial_advisor", "photographer", "hairdresser", "tailor", "dressmaker", "locksmith", "key_cutter",
    "shoemaker", "jeweller", "beekeeper", "handicraft", "sculptor", "clockmaker", "watchmaker",
}


def overpass_query(lat, lng, radius_m):
    """One bounding box and one key-regex filter per website tag. A large `around:` union of many
    selectors times out (HTTP 504) on the public Overpass servers; this form returns the whole metro
    in seconds. osm_discover trims the box back to the circle."""
    dlat = radius_m / 111_320
    dlng = radius_m / (111_320 * max(0.1, math.cos(math.radians(lat))))
    bbox = f"{lat - dlat:.4f},{lng - dlng:.4f},{lat + dlat:.4f},{lng + dlng:.4f}"
    kinds = '[~"^(office|industrial|craft|man_made)$"~"."]'
    return (f"[out:json][timeout:60][bbox:{bbox}];"
            f'(nwr["website"]{kinds};nwr["contact:website"]{kinds};);out center tags;')


def parse_osm(data):
    """Overpass JSON → candidates."""
    out = []
    for el in (data or {}).get("elements") or []:
        tags = el.get("tags") or {}
        website = tags.get("website") or tags.get("contact:website")
        if not tags.get("name") or not website:
            continue
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lng = el.get("lon") or (el.get("center") or {}).get("lon")
        hint = " ".join(str(tags.get(k, "")) for k in ("industrial", "craft", "product", "office", "description"))
        industry = next((slug for rx, slug in _OSM_INDUSTRY if rx.search(hint)), None)
        street = " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x)
        out.append({"name": tags["name"], "website": website, "city": tags.get("addr:city"),
                    "state": tags.get("addr:state"), "address": street or None, "lat": lat, "lng": lng,
                    "category": tags.get("industrial") or tags.get("craft") or tags.get("office"),
                    "source": "osm", "industry": industry})
    return out


def osm_discover(lat, lng, radius_miles, fetcher=None):
    """Named OSM features with a website within the radius; [] when Overpass is unreachable."""
    fetcher = fetcher or get_fetcher()
    query = overpass_query(lat, lng, radius_miles * 1609.34)
    for url in OVERPASS_URLS:
        try:
            resp = fetcher.request("POST", url, kind="api", data={"data": query}, retries=1, timeout=100,
                                   headers={"User-Agent": APP_UA, "Accept": "application/json"})
            cands = [c for c in parse_osm(resp.json())
                     if c["lat"] is None or geo.haversine_miles(lat, lng, c["lat"], c["lng"]) <= radius_miles]
            kept = [c for c in cands if c.get("category") not in _OSM_SKIP_CATEGORIES]
            log.info("discovery: osm %d in radius, %d after skipping storefront/solo categories", len(cands), len(kept))
            return kept
        except (FetchError, ValueError) as exc:
            log.warning("discovery: overpass %s failed: %s", url, exc)
    return []


def parse_places(data, query):
    out = []
    for p in (data or {}).get("places") or []:
        loc = p.get("location") or {}
        city, state = _city_state(p.get("formattedAddress"))
        out.append({"name": (p.get("displayName") or {}).get("text"), "website": p.get("websiteUri"),
                    "address": p.get("formattedAddress"), "city": city, "state": state,
                    "lat": loc.get("latitude"), "lng": loc.get("longitude"), "category": p.get("primaryType"),
                    "source": "places", "query": query["query"], "town": query["town"], "industry": query["industry"]})
    return out


def places_search(query, home_lat, home_lng, fetcher=None, pages=3):
    key = config.google_places_api_key()
    if not key:
        return []
    fetcher = fetcher or get_fetcher()
    body = {"textQuery": f"{query['query']} near {query['town']}", "pageSize": 20,
            "locationBias": {"circle": {"center": {"latitude": home_lat, "longitude": home_lng}, "radius": 50000}}}
    out = []
    for _ in range(pages):
        try:
            data = fetcher.post_json(PLACES_URL, body, headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": PLACES_FIELDS})
        except (FetchError, ValueError) as exc:
            log.warning("discovery: places failed for %r: %s", query["query"], exc)
            break
        out += parse_places(data, query)
        if not data.get("nextPageToken"):
            break
        body["pageToken"] = data["nextPageToken"]
    return out


async def _launch_async(pw, launch_args):
    channel = config.browser_channel()
    if channel and channel != "chromium":
        try:
            return await pw.chromium.launch(channel=channel, headless=True, args=launch_args)
        except Exception:  # noqa: BLE001
            pass
    return await pw.chromium.launch(headless=True, args=launch_args)


# Google Maps result cards already carry what discovery needs — the place link (name + coordinates),
# a "Website" button, and a text line with category · street · phone — so read them straight off the
# results feed instead of clicking into every place.
_MAPS_CARDS_JS = r"""
() => {
  const feed = document.querySelector('div[role="feed"]');
  const out = [];
  if (!feed) {
    const h1 = document.querySelector('h1');
    const site = document.querySelector('a[data-item-id="authority"]');
    const addr = document.querySelector('button[data-item-id="address"]');
    if (h1 && site) out.push({name: h1.innerText.trim(), website: site.href, href: location.href,
                              text: addr ? addr.innerText : ''});
    return out;
  }
  for (const a of feed.querySelectorAll('a[href*="/maps/place/"]')) {
    let card = a.parentElement;
    for (let i = 0; i < 4 && card && !card.querySelector('a[data-value="Website"]'); i++) card = card.parentElement;
    const site = card && card.querySelector('a[data-value="Website"]');
    out.push({name: a.getAttribute('aria-label') || '', website: site ? site.href : '', href: a.href,
              text: card ? card.innerText.replace(/\s+/g, ' ') : ''});
  }
  return out;
}"""
_MAPS_COORDS = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
_MAPS_PHONE = re.compile(r"\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}")
_MAPS_RATING = re.compile(r"^(\d\.\d\s*\([\d,]+\)|No reviews)\s*")


def parse_maps_card(card):
    """{name, website, href, text} from the Maps results feed → candidate dict (or None without a website)."""
    name, website = (card.get("name") or "").strip(), (card.get("website") or "").strip()
    if not name or not website:
        return None
    text = card.get("text") or ""
    rest = text.split(name, 2)[-1].strip() if name in text else text  # the card repeats the name
    rest = _MAPS_RATING.sub("", rest)
    parts = [p.strip() for p in re.split(r"\s+·\s+|\s*\ue934\s*", rest) if p.strip()]
    category = parts[0] if parts and not re.match(r"\d", parts[0]) else None
    street = next((re.split(r"\s+(Open|Closed|Closes|Opens)\b", p)[0] for p in parts if re.match(r"\d+\s+\w", p)), None)
    coords = _MAPS_COORDS.search(card.get("href") or "")
    phone = _MAPS_PHONE.search(text)
    return {"name": name, "website": website, "address": street, "category": category,
            "phone": phone.group(0) if phone else None,
            "lat": float(coords.group(1)) if coords else None, "lng": float(coords.group(2)) if coords else None}


async def _maps_search(ctx, query, max_results=40):
    """Google Maps results for "<query> near <town>" → candidates (name, website, street, coords)."""
    from urllib.parse import quote
    page = await ctx.new_page()
    try:
        await page.goto(f"https://www.google.com/maps/search/{quote(query)}?hl=en&gl=us",
                        wait_until="domcontentloaded", timeout=25000)
        for sel in ('button[aria-label*="Accept all" i]', 'form[action*="consent"] button'):
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                await page.wait_for_timeout(1200)
                break
        try:
            await page.wait_for_selector('div[role="feed"], h1', timeout=15000)
        except Exception:
            return []
        seen = 0
        for _ in range(5):  # scroll the feed until no new cards load
            feed = await page.query_selector('div[role="feed"]')
            if not feed:
                break
            count = len(await page.query_selector_all('div[role="feed"] a[href*="/maps/place/"]'))
            if count >= max_results or count == seen:
                break
            seen = count
            await feed.evaluate("el => el.scrollBy(0, el.scrollHeight)")
            await page.wait_for_timeout(1200)
        cards = await page.evaluate(_MAPS_CARDS_JS)
        return [c for c in (parse_maps_card(card) for card in cards[:max_results]) if c]
    finally:
        await page.close()


async def _browser_queries(queries, on_result):
    from playwright.async_api import async_playwright
    cf = _cf()
    async with async_playwright() as pw:
        browser = await _launch_async(pw, cf._LAUNCH_ARGS)
        try:
            for q in queries:
                town = q["town"].split(",")[0]
                ctx = await browser.new_context(**cf._BROWSER_CTX_OPTS)
                try:
                    if q["source"] == "maps":
                        calls = [_maps_search(ctx, f"{q['query']} near {q['town']}")]
                    else:
                        text = f"{q['query']} {q['town']}"
                        calls = [cf._search_duckduckgo(ctx, text, town), cf._search_bing(ctx, text, town),
                                 cf._search_yellow_pages(ctx, q["query"], town)]
                    results = await asyncio.gather(*calls, return_exceptions=True)
                finally:
                    await ctx.close()
                found = []
                for item in results:
                    if isinstance(item, Exception):
                        log.warning("discovery: %s search failed for %r: %s", q["source"], q["query"], item)
                    else:  # clientfinder helpers return (results, diag); _maps_search returns results
                        found += (item[0] if isinstance(item, tuple) else item) or []
                # The query town is not evidence of where a result is; only a listed address or coordinates are.
                on_result(q, [{"name": b.get("name"), "website": b.get("website"), "address": b.get("address") or None,
                               "category": b.get("category"), "lat": b.get("lat"), "lng": b.get("lng"),
                               "source": q["source"], "query": q["query"], "town": q["town"],
                               "industry": q["industry"]} for b in found])
                await asyncio.sleep(random.uniform(2.0, 5.0))  # pace searches like a person would
        finally:
            await browser.close()


def browser_search(queries, on_result):
    """Run maps/search queries in one Playwright browser. Returns an error string or None."""
    if not queries:
        return None
    cf = _cf()
    if cf is None:
        return "clientfinder helpers unavailable"
    if importlib.util.find_spec("playwright") is None:
        return "Playwright not installed"
    try:
        asyncio.run(_browser_queries(queries, on_result))
    except Exception as exc:  # noqa: BLE001 — browser missing/crashed → fail soft
        return f"{type(exc).__name__}: {str(exc)[:160]}"
    return None


# ── run ─────────────────────────────────────────────────────────────────────

def run_discovery(run, options=None, profile=None, fetcher=None, start_pipeline=True):
    """Discover run: plan → sources → record candidates → steps 1–2 for new companies → queue 3–4."""
    from . import pipeline
    opts = resolve_options(profile, options)
    plan = plan_for(profile, options)
    stats = {"queries": 0, "candidates": 0, "new_companies": 0, "known": 0, "filtered": 0, "by_source": {}}
    run.log(f"discovery plan: {plan['total']} queries over {len(plan['towns'])} towns")
    seen, new_ids, total = set(), [], plan["total"]

    def record(query, candidates):
        stats["queries"] += 1
        for cand in candidates:
            cand.setdefault("query", query["query"])
            cand.setdefault("town", query["town"])
        with db.session() as conn:
            ids = record_candidates(conn, candidates, stats, seen)
        new_ids.extend(ids)
        run.log(f"{query['source']}: \"{query['query']}\" {query['town']} → {len(candidates)} candidates, {len(ids)} new")
        run.progress(stats["queries"], total, "discover")

    by_source = {s: [q for q in plan["queries"] if q["source"] == s] for s in ("osm", "places", "maps", "search")}
    if by_source["osm"]:
        record(by_source["osm"][0], osm_discover(opts["home_lat"], opts["home_lng"], opts["radius_miles"], fetcher))
    for q in by_source["places"]:
        record(q, places_search(q, opts["home_lat"], opts["home_lng"], fetcher))
    browser_qs = by_source["maps"] + by_source["search"]
    error = browser_search(browser_qs, record)
    if error:
        run.log(f"maps/search skipped: {error}")
    stats["by_source"] = {s: stats["by_source"].get(s, 0) for s in {q["source"] for q in plan["queries"]}}

    if new_ids:
        run.log(f"categorizing {len(new_ids)} new companies (facts + heuristics; AI enrichment queued)")
        run.progress(0, len(new_ids), "categorize")
        with ThreadPoolExecutor(max_workers=pipeline.MAX_WORKERS) as pool:
            futures = [pool.submit(_prepare, cid, fetcher, run) for cid in new_ids]
            for done, fut in enumerate(as_completed(futures), 1):
                fut.result()
                run.progress(done, len(new_ids), "categorize")
        if start_pipeline:
            try:
                follow = runs.start("pipeline", pipeline.run_pipeline, company_ids=new_ids, fetcher=fetcher)
                run.log(f"careers/ATS detection + job sweep continues in pipeline run {follow.id}")
            except runs.AlreadyRunning as exc:
                run.log(f"pipeline run {exc.run_id} already running; new companies will be checked by the next Find matches")
    run.log(f"discovery done: {stats['new_companies']} new, {stats['known']} known, {stats['filtered']} filtered")
    return stats


def import_seeds(source, source_detail=None):
    """Bulk-import companies from a seed JSON file/list (CLI utility; nothing ships pre-seeded).
    Upserts by normalised domain, only filling empty fields so user/AI edits are never clobbered."""
    import json
    from pathlib import Path
    from . import ats, enrich
    if isinstance(source, (str, Path)):
        source_detail = source_detail or Path(source).name
        items = json.loads(Path(source).read_text(encoding="utf-8"))
    else:
        items = source
    stats = {"inserted": 0, "updated": 0, "skipped": 0}
    now = db.now_iso()
    with db.session() as conn:
        for item in items if isinstance(items, list) else []:
            domain = normalize_domain(item.get("domain") or item.get("homepage_url")) if isinstance(item, dict) else None
            if not domain or not item.get("name"):
                stats["skipped"] += 1
                continue
            well_known = item.get("well_known")
            fields = {
                "name": str(item["name"])[:120], "homepage_url": item.get("homepage_url") or f"https://{domain}",
                "careers_url": item.get("careers_url") or None, "hq_city": item.get("hq_city"),
                "hq_state": item.get("hq_state"),
                "industry": taxonomy.coerce(item.get("industry"), taxonomy.INDUSTRY_IDS, "other") if item.get("industry") else None,
                "sub_industry": item.get("sub_industry"),
                "products": db.dumps([str(p) for p in item.get("products") or [] if p][:8]),
                "summary": item.get("summary"),
                "ownership": taxonomy.coerce(item.get("ownership"), taxonomy.OWNERSHIP, "unknown"),
                "employee_band": taxonomy.coerce(item.get("employee_band"), taxonomy.EMPLOYEE_BANDS, "unknown"),
                "founded_year": item.get("founded_year") if isinstance(item.get("founded_year"), int) else None,
                "well_known": int(bool(well_known)) if isinstance(well_known, bool) else None,
            }
            detected = ats.detect_from_url(fields["careers_url"]) if fields["careers_url"] else None
            if detected:
                fields.update({k: detected[k] for k in ("ats_type", "ats_host", "ats_key", "ats_site")})
            hit = geo.find_city(fields["hq_city"], fields["hq_state"]) if fields["hq_city"] else None
            if hit:
                fields.update(lat=hit[0], lng=hit[1], geo_precision="city")
            row = db.row_dict(conn.execute("SELECT * FROM companies WHERE domain=?", (domain,)).fetchone())
            if row:
                updates = {k: v for k, v in fields.items()
                           if v not in (None, "", "[]", "unknown") and row.get(k) in (None, "", "[]", "unknown")}
                if row.get("enrich_source") == "heuristic" and fields["industry"]:
                    updates["industry"] = fields["industry"]
                if updates:
                    updates["updated_at"] = now
                    db.update(conn, "companies", "id", row["id"], updates)
                    stats["updated"] += 1
                continue
            complete = all(fields.get(k) not in (None, "unknown") for k in ("industry", "summary", "employee_band",
                                                                           "ownership", "well_known"))
            facts = {"seed_notes": item.get("notes"), "seed_sources": item.get("sources") or []}
            fields.update(domain=domain, source="seed", source_detail=source_detail, status="pending",
                          enrich_source="seed" if (fields["industry"] or fields["summary"]) else None,
                          enrich_status="done" if complete else "pending", entity_type="company",
                          local_presence="hq" if (fields["hq_state"] or "").upper() in config.LOCAL_STATES else "unknown",
                          facts=db.dumps(facts), created_at=now, updated_at=now)
            fields.update(enrich.gem_fields(conn, fields))
            conn.execute(f"INSERT INTO companies({','.join(fields)}) VALUES({','.join('?' * len(fields))})",
                         tuple(fields.values()))
            stats["inserted"] += 1
    return stats


def _prepare(company_id, fetcher, run):
    from . import pipeline
    with db.session() as conn:
        company = db.row_dict(conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone())
        if company:
            try:
                pipeline.prepare_company(conn, company, fetcher, run)
            except Exception as exc:  # noqa: BLE001
                run.log(f"{company['name']}: categorize failed: {exc}")
