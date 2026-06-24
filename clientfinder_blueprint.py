"""Client Finder 1.0 — Flask Blueprint. Mounts under /clientfinder."""
import json
import sqlite3
import csv
import io
import os
import re
import uuid
import asyncio
import threading
import queue
import time
from pathlib import Path
from datetime import datetime, timezone

from urllib.parse import quote as _urlencode
from flask import Blueprint, request, jsonify, Response, send_from_directory

clientfinder_bp = Blueprint("clientfinder", __name__, url_prefix="/clientfinder")

_DB = Path(__file__).parent / "data" / "clientfinder.db"
_SCREENSHOTS_DIR = Path(__file__).parent / "data" / "cf_screenshots"
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def _get_conn():
    conn = sqlite3.connect(str(_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


_SEED_VERSION = "2"  # bump to re-migrate on next Flask restart

def init_clientfinder_db():
    """Seed if empty; migrate seed-record statuses/notes when version bumps."""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            industry TEXT,
            city TEXT,
            state TEXT DEFAULT 'MN',
            employee_count INTEGER,
            website TEXT,
            screenshot_url TEXT,
            score_modernity REAL,
            score_mobile REAL,
            score_function REAL,
            composite_score REAL,
            outdated_stack INTEGER DEFAULT 0,
            stack_flags TEXT DEFAULT '[]',
            dm_name TEXT,
            dm_title TEXT,
            dm_seniority TEXT,
            dm_source TEXT DEFAULT 'Apollo',
            dm_linkedin TEXT,
            email TEXT,
            phone TEXT,
            contact_form_url TEXT,
            outreach_status TEXT DEFAULT 'New',
            notes TEXT DEFAULT '',
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    if count == 0:
        for lead in _SEED_LEADS:
            conn.execute("""
                INSERT INTO leads (company_name,industry,city,state,employee_count,website,
                    screenshot_url,score_modernity,score_mobile,score_function,composite_score,
                    outdated_stack,stack_flags,dm_name,dm_title,dm_seniority,dm_linkedin,
                    email,phone,contact_form_url,outreach_status,notes,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                lead['company_name'], lead['industry'], lead['city'], 'MN',
                lead['employee_count'], lead['website'], lead.get('screenshot_url', ''),
                lead['score_modernity'], lead['score_mobile'], lead['score_function'],
                lead['composite_score'], 1 if lead['outdated_stack'] else 0,
                json.dumps(lead.get('stack_flags', [])),
                lead.get('dm_name', ''), lead.get('dm_title', ''), lead.get('dm_seniority', ''),
                lead.get('dm_linkedin', ''), lead.get('email'), lead.get('phone'),
                lead.get('contact_form_url'),
                'New', '',  # always seed with New status and empty notes
                lead.get('created_at', _now()), _now()
            ))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('seed_version', ?)", (_SEED_VERSION,))
        conn.commit()
    else:
        # One-time migration: reset seed records to clean state
        ver = conn.execute("SELECT value FROM meta WHERE key='seed_version'").fetchone()
        if ver is None or ver[0] != _SEED_VERSION:
            conn.execute(
                "UPDATE leads SET outreach_status='New', notes='' WHERE id <= 50"
            )
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('seed_version', ?)", (_SEED_VERSION,))
            conn.commit()
    conn.close()


# ─── Routes ────────────────────────────────────────────────────────────────────

@clientfinder_bp.route("/leads", methods=["GET"])
def get_leads():
    conn = _get_conn()
    q = "SELECT * FROM leads WHERE 1=1"
    params = []
    if industry := request.args.get('industry'):
        q += " AND industry=?"; params.append(industry)
    if status := request.args.get('status'):
        q += " AND outreach_status=?"; params.append(status)
    if region := request.args.get('region'):
        tc = ('Minneapolis','St. Paul','Bloomington','Edina','Eden Prairie','Plymouth',
              'Minnetonka','Eagan','Roseville','St. Louis Park','Burnsville','Golden Valley',
              'Woodbury','Brooklyn Park')
        placeholders = ','.join('?' * len(tc))
        if region == 'twin_cities':
            q += f" AND city IN ({placeholders})"; params.extend(tc)
        elif region == 'greater_mn':
            q += f" AND city NOT IN ({placeholders})"; params.extend(tc)
    q += " ORDER BY created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    leads = []
    for r in rows:
        d = dict(r)
        d['stack_flags'] = json.loads(d.get('stack_flags') or '[]')
        d['outdated_stack'] = bool(d['outdated_stack'])
        leads.append(d)
    return jsonify({"leads": leads})


@clientfinder_bp.route("/leads", methods=["POST"])
def add_lead():
    data = request.get_json(silent=True) or {}
    items = data if isinstance(data, list) else [data]
    conn = _get_conn()
    ids = []
    for item in items:
        cur = conn.execute("""
            INSERT INTO leads (company_name,industry,city,employee_count,website,
                screenshot_url,score_modernity,score_mobile,score_function,composite_score,
                outdated_stack,stack_flags,dm_name,dm_title,dm_seniority,email,phone,
                contact_form_url,outreach_status,notes,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            item.get('company_name', ''), item.get('industry', ''), item.get('city', ''),
            item.get('employee_count'), item.get('website', ''), item.get('screenshot_url', ''),
            item.get('score_modernity', 5), item.get('score_mobile', 5), item.get('score_function', 5),
            item.get('composite_score', 5), 1 if item.get('outdated_stack') else 0,
            json.dumps(item.get('stack_flags', [])), item.get('dm_name', ''), item.get('dm_title', ''),
            item.get('dm_seniority', ''), item.get('email'), item.get('phone'),
            item.get('contact_form_url'), item.get('outreach_status', 'New'), item.get('notes', ''),
            _now(), _now()
        ))
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "ids": ids}), 201


@clientfinder_bp.route("/leads/<int:lead_id>", methods=["PATCH"])
def update_lead(lead_id):
    data = request.get_json(silent=True) or {}
    allowed = {'outreach_status', 'notes', 'score_modernity', 'score_mobile', 'score_function',
               'composite_score', 'outdated_stack', 'stack_flags', 'dm_name', 'dm_title',
               'dm_seniority', 'email', 'phone', 'contact_form_url', 'screenshot_url',
               'website', 'industry', 'city', 'employee_count'}
    sets, params = [], []
    for k, v in data.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(
                json.dumps(v) if k == 'stack_flags'
                else (1 if v is True else (0 if v is False else v))
            )
    if not sets:
        return jsonify({"ok": False, "error": "no valid fields"}), 400
    sets.append("updated_at=?")
    params.append(_now())
    params.append(lead_id)
    conn = _get_conn()
    conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@clientfinder_bp.route("/leads/<int:lead_id>", methods=["DELETE"])
def delete_lead(lead_id):
    conn = _get_conn()
    conn.execute("DELETE FROM leads WHERE id=?", (lead_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@clientfinder_bp.route("/leads/export", methods=["GET"])
def export_leads():
    conn = _get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM leads ORDER BY created_at DESC").fetchall()]
    conn.close()
    out = io.StringIO()
    if rows:
        w = csv.DictWriter(out, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    return Response(
        out.getvalue(),
        mimetype='text/csv',
        headers={"Content-Disposition": "attachment; filename=client-finder-leads.csv"}
    )


@clientfinder_bp.route("/leads/stats", methods=["GET"])
def lead_stats():
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    by_status = {r[0]: r[1] for r in conn.execute(
        "SELECT outreach_status, COUNT(*) FROM leads GROUP BY outreach_status").fetchall()}
    avg_score = conn.execute("SELECT AVG(composite_score) FROM leads").fetchone()[0]
    outdated_count = conn.execute("SELECT COUNT(*) FROM leads WHERE outdated_stack=1").fetchone()[0]
    by_industry = {r[0]: r[1] for r in conn.execute(
        "SELECT industry, COUNT(*) FROM leads GROUP BY industry").fetchall()}
    conn.close()
    return jsonify({
        "total": total,
        "by_status": by_status,
        "avg_composite_score": round(avg_score or 0, 1),
        "outdated_stack_count": outdated_count,
        "outdated_stack_pct": round((outdated_count / total * 100) if total else 0, 1),
        "by_industry": by_industry,
    })


# ─── Discovery endpoints ───────────────────────────────────────────────────────

_ENRICH_TITLES_BY_INDUSTRY = {
    "Entertainment": ["Owner", "General Manager", "Venue Director", "Franchise Owner"],
    "Professional Services": ["Managing Partner", "Owner", "Principal", "Founder", "CEO"],
    "Home & Commercial Services": ["Owner", "President", "Operations Manager", "CEO"],
    "Healthcare & Wellness": ["Owner", "Clinical Director", "Practice Manager", "CEO"],
    "Retail & Hospitality": ["Owner", "General Manager", "Co-Founder", "CEO"],
    "Manufacturing & Logistics": ["Owner", "President", "Operations Director", "CEO"],
}
_FIRST_NAMES = ["Alex","Jordan","Taylor","Morgan","Casey","Jamie","Riley","Dana","Cameron","Avery","Blake","Quinn","Reese","Sydney","Drew","Chris","Sam","Pat","Lee","Robin"]
_LAST_INITS = ["A","B","C","D","E","F","G","H","J","K","L","M","N","O","P","R","S","T","W"]

def _mock_enrich(company_name, industry, city):
    import random
    rng = random.Random(company_name)  # deterministic per company
    fn = rng.choice(_FIRST_NAMES)
    li = rng.choice(_LAST_INITS)
    titles = _ENRICH_TITLES_BY_INDUSTRY.get(industry, ["Owner", "CEO", "Manager"])
    title = rng.choice(titles)
    seniority = "C-Suite" if title in ("Owner","CEO","Co-Founder","Founder","President","Managing Partner","Principal") else "Director"
    area = "612" if city in ("Minneapolis","St. Paul","Golden Valley","St. Louis Park") else \
           "952" if city in ("Bloomington","Edina","Eden Prairie","Plymouth","Minnetonka","Burnsville","Eagan") else \
           "651" if city == "Roseville" else \
           "218" if city in ("Duluth","Brainerd","Moorhead") else \
           "507" if city in ("Rochester","Mankato","Winona") else "320"
    phone = f"({area}) 555-{rng.randint(1000,9999)}"
    domain = re.sub(r'[^a-z0-9]', '', company_name.lower())[:12] + ".com"
    email = f"info@{domain}" if rng.random() > 0.3 else None
    return {"dm_name": f"{fn} {li}.", "dm_title": title, "dm_seniority": seniority,
            "dm_source": "Apollo", "dm_linkedin": "", "email": email, "phone": phone,
            "contact_form_url": f"{domain}/contact" if rng.random() > 0.5 else None}


@clientfinder_bp.route("/screenshot/<path:filename>", methods=["GET"])
def serve_screenshot(filename):
    return send_from_directory(str(_SCREENSHOTS_DIR), filename)


def _clean_url(url):
    """Strip scheme/www and trailing slash, return bare domain+path."""
    if not url:
        return ""
    return re.sub(r'^https?://(www\.)?', '', str(url)).rstrip('/')

def _extract_domain(url):
    """Return lowercased domain only — used for deduplication."""
    return _clean_url(url).split('/')[0].lower()

_SKIP_DOMAINS = {
    "yelp.com","yellowpages.com","facebook.com","wikipedia.org",
    "instagram.com","twitter.com","x.com","linkedin.com","google.com",
    "angi.com","thumbtack.com","bbb.org","nextdoor.com","tripadvisor.com",
    "homeadvisor.com","angieslist.com","houzz.com","bark.com",
    "youtube.com","reddit.com","amazon.com","ebay.com","etsy.com","pinterest.com",
    "mapquest.com","indeed.com","glassdoor.com","ziprecruiter.com","craigslist.org",
    "tiktok.com","apple.com","yellowpages.ca","manta.com","chamberofcommerce.com",
    "expedia.com","booking.com","groupon.com","opentable.com","doordash.com",
    "ubereats.com","grubhub.com","zomato.com","foursquare.com","wikiwand.com",
    "businessyab.com","cylex.us.com","superpages.com","local.com","citysearch.com",
    "mapcarta.com","loc8nearme.com","dnb.com","zoominfo.com","crunchbase.com",
}

def _is_aggregator(domain):
    return any(domain.endswith(d) for d in _SKIP_DOMAINS)


_DOMAIN_RE = re.compile(r'^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$')

def _looks_like_real_domain(domain):
    """Reject IPs, schemes, junk and malformed hosts before we spend time loading them."""
    if not domain or '/' in domain or ' ' in domain:
        return False
    if re.match(r'^\d{1,3}(\.\d{1,3}){3}$', domain):  # bare IP
        return False
    if domain.endswith('.gov') or domain.endswith('.gov/'):
        return False  # municipal pages aren't sellable web leads
    return bool(_DOMAIN_RE.match(domain))


def _decode_ddg_href(href):
    """DuckDuckGo wraps outbound links as /l/?uddg=<encoded>. Unwrap to the real URL."""
    from urllib.parse import unquote, urlparse, parse_qs
    if not href:
        return ""
    if "duckduckgo.com/l/" in href or href.startswith("/l/"):
        try:
            qs = parse_qs(urlparse(href).query)
            if "uddg" in qs:
                return unquote(qs["uddg"][0])
        except Exception:
            return ""
    return href


def _clean_name(title):
    """Trim a SERP title down to a business-like name (drop trailing taglines)."""
    if not title:
        return ""
    # Cut at common separators that precede taglines / location suffixes
    for sep in (" | ", " – ", " — ", " - ", " : "):
        if sep in title:
            title = title.split(sep)[0]
            break
    return title.strip()[:80]


async def _search_duckduckgo(ctx, query, city):
    """DuckDuckGo HTML (no JS required) — finds business websites not in directories."""
    page = await ctx.new_page()
    results = []
    try:
        safe_q = _urlencode(
            query + " -site:yelp.com -site:yellowpages.com -site:facebook.com -site:linkedin.com"
        )
        # POST endpoint is far more reliable than the GET html page under automation
        await page.goto(f"https://html.duckduckgo.com/html/?q={safe_q}",
                        wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(700)

        anchors = await page.query_selector_all("a.result__a")
        if not anchors:
            anchors = await page.query_selector_all(".result__title a")
        for a in anchors[:14]:
            try:
                title = (await a.inner_text()).strip()
                href  = _decode_ddg_href(await a.get_attribute("href") or "")
                domain = _extract_domain(href)
                if not domain or _is_aggregator(domain):
                    continue
                results.append({
                    "name": _clean_name(title), "website": domain,
                    "city": city, "address": "", "source": "DuckDuckGo",
                })
                if len(results) >= 8:
                    break
            except Exception:
                continue
    except Exception:
        pass
    finally:
        await page.close()
    return results


async def _search_bing(ctx, query, city):
    """Bing HTML — reliable fallback search source under headless automation."""
    page = await ctx.new_page()
    results = []
    try:
        safe_q = _urlencode(
            query + " -site:yelp.com -site:yellowpages.com -site:facebook.com -site:linkedin.com"
        )
        await page.goto(f"https://www.bing.com/search?q={safe_q}&setlang=en-us&cc=us",
                        wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(700)

        for item in (await page.query_selector_all("li.b_algo"))[:14]:
            try:
                a = await item.query_selector("h2 a")
                if not a:
                    continue
                title = (await a.inner_text()).strip()
                href  = (await a.get_attribute("href") or "").strip()
                domain = _extract_domain(href)
                if not domain or _is_aggregator(domain):
                    continue
                results.append({
                    "name": _clean_name(title), "website": domain,
                    "city": city, "address": "", "source": "Bing",
                })
                if len(results) >= 8:
                    break
            except Exception:
                continue
    except Exception:
        pass
    finally:
        await page.close()
    return results


async def _search_google_maps(ctx, query, city):
    """Google Maps — click each card to retrieve the website link from the detail panel."""
    page = await ctx.new_page()
    results = []
    try:
        await page.goto(
            f"https://www.google.com/maps/search/{_urlencode(query)}",
            wait_until="domcontentloaded", timeout=20000,
        )
        await page.wait_for_timeout(2500)

        # Result cards are role=article or .Nv2PK
        cards = await page.query_selector_all('[role="article"]')
        if not cards:
            cards = await page.query_selector_all(".Nv2PK")

        for card in cards[:5]:
            try:
                # Grab name from card before clicking
                heading = await card.query_selector('[role="heading"]')
                name = (await heading.inner_text()).strip() if heading else ""
                if not name:
                    continue

                await card.click()
                await page.wait_for_timeout(1800)

                # Website link in detail panel — try several stable selectors
                website = ""
                for sel in [
                    'a[data-item-id="authority"]',
                    'a[aria-label*="website" i]',
                    'a[href^="http"]:not([href*="google"])[aria-label]',
                ]:
                    el = await page.query_selector(sel)
                    if el:
                        href = (await el.get_attribute("href") or "").strip()
                        if href and "google" not in href:
                            website = _clean_url(href)
                            break

                # Address
                address = ""
                for sel in ['[data-item-id="address"] .rogA2c',
                            'button[data-item-id="address"]']:
                    el = await page.query_selector(sel)
                    if el:
                        address = (await el.inner_text()).strip()
                        break

                # Phone
                phone = ""
                for sel in ['[data-item-id^="phone"] .rogA2c',
                            'button[aria-label*="phone" i]']:
                    el = await page.query_selector(sel)
                    if el:
                        phone = (await el.inner_text()).strip()
                        break

                # Category
                cat = ""
                cat_el = await page.query_selector(".DkEaL")
                if cat_el:
                    cat = (await cat_el.inner_text()).strip()

                results.append({
                    "name": name, "website": _extract_domain(website),
                    "city": city, "address": address,
                    "phone": phone, "category": cat,
                    "source": "Google Maps",
                })

                # Return to list
                back = await page.query_selector('button[aria-label="Back"]')
                if back:
                    await back.click()
                    await page.wait_for_timeout(800)
            except Exception:
                continue
    except Exception:
        pass
    finally:
        await page.close()
    return results


async def _search_yellow_pages(ctx, industry, city):
    """Yellow Pages search — website link is in-card, no click-through needed."""
    page = await ctx.new_page()
    results = []
    try:
        city_yp = city.replace(" ", "+")
        url = (
            f"https://www.yellowpages.com/search"
            f"?search_terms={_urlencode(industry)}"
            f"&geo_location_terms={city_yp}%2C+MN"
        )
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(1000)

        for listing in (await page.query_selector_all(".result"))[:8]:
            try:
                name_el = await listing.query_selector(".business-name span")
                if not name_el:
                    name_el = await listing.query_selector(".business-name")
                name = (await name_el.inner_text()).strip() if name_el else ""
                if not name:
                    continue

                website = ""
                site_el = await listing.query_selector("a.track-visit-website")
                if site_el:
                    href = await site_el.get_attribute("href") or ""
                    website = _extract_domain(href)

                phone = ""
                phone_el = await listing.query_selector(".phones .phone")
                if phone_el:
                    phone = (await phone_el.inner_text()).strip()

                address = ""
                addr_el = await listing.query_selector(".adr")
                if addr_el:
                    address = (await addr_el.inner_text()).strip()

                results.append({
                    "name": name, "website": website,
                    "city": city, "address": address,
                    "phone": phone, "source": "Yellow Pages",
                })
                if len(results) >= 6:
                    break
            except Exception:
                continue
    except Exception:
        pass
    finally:
        await page.close()
    return results


async def _multi_source_search(browser, query, industry, city, want=12):
    """Run search sources concurrently in isolated contexts, dedupe by domain."""
    _ctx_opts = dict(
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US",
    )
    contexts = [await browser.new_context(**_ctx_opts) for _ in range(4)]
    try:
        raw = await asyncio.gather(
            _search_duckduckgo(contexts[0], query, city),
            _search_bing(contexts[1], query, city),
            _search_google_maps(contexts[2], query, city),
            _search_yellow_pages(contexts[3], industry, city),
            return_exceptions=True,
        )
    finally:
        for c in contexts:
            try:
                await c.close()
            except Exception:
                pass

    seen, businesses = set(), []
    for batch in raw:
        if isinstance(batch, Exception) or not batch:
            continue
        for biz in batch:
            domain = _extract_domain(biz.get("website", ""))
            key = domain or (biz.get("name", "") or "").lower()
            if key and key not in seen:
                seen.add(key)
                biz["industry"] = industry
                businesses.append(biz)
    return businesses[:want]


# ─── Query planning ────────────────────────────────────────────────────────────
# Concrete, searchable business types per industry — never the literal "local business".
_INDUSTRY_QUERY_TERMS = {
    "Entertainment":              ["bowling alley", "family entertainment center", "escape room", "arcade", "mini golf"],
    "Professional Services":      ["law firm", "accounting firm", "marketing agency", "architecture firm", "insurance agency"],
    "Home & Commercial Services": ["HVAC contractor", "plumbing company", "landscaping company", "electrician", "roofing contractor"],
    "Healthcare & Wellness":      ["dental clinic", "chiropractor", "physical therapy clinic", "med spa", "family clinic"],
    "Retail & Hospitality":       ["restaurant", "boutique", "craft brewery", "auto repair shop", "coffee shop"],
    "Manufacturing & Logistics":  ["machine shop", "metal fabrication", "freight company", "manufacturer", "food producer"],
}

_TWIN_CITIES = ["Minneapolis", "St. Paul", "Bloomington", "Edina", "Plymouth", "Eden Prairie", "Maple Grove"]
_GREATER_MN  = ["Duluth", "Rochester", "St. Cloud", "Mankato", "Moorhead", "Brainerd"]

# Reverse lookup: term -> industry label, for tagging discovered leads.
_TERM_INDUSTRY = {}
for _ind, _terms in _INDUSTRY_QUERY_TERMS.items():
    for _t in _terms:
        _TERM_INDUSTRY[_t] = _ind

_MAX_QUERIES = 8


def _cities_for_region(region, refinements):
    rc = refinements.get("city")
    if rc:
        return [rc.replace(" MN", "").strip()]
    if region == "Twin Cities":
        return list(_TWIN_CITIES)
    if region == "Greater MN":
        return list(_GREATER_MN)
    # Both — interleave so the plan spans the whole state
    out = []
    for a, b in zip(_TWIN_CITIES, _GREATER_MN):
        out.extend([a, b])
    return out


def _terms_for_industry(industry, refinements, keywords):
    """Pick concrete search terms. Refinement/keyword/industry aware; never generic."""
    ref_ind = refinements.get("industry")
    target = ref_ind or industry
    if target and target in _INDUSTRY_QUERY_TERMS:
        return list(_INDUSTRY_QUERY_TERMS[target]), target
    # If the user typed a keyword that reads like a business type, lead with it
    if keywords and len(keywords.split()) <= 4:
        return [keywords.strip()], (target or "")
    # "All industries" — spread one strong term from each industry for breadth
    spread = [terms[0] for terms in _INDUSTRY_QUERY_TERMS.values()]
    return spread, ""


def _build_query_plan(industry, region, keywords, refinements):
    """Produce up to _MAX_QUERIES concrete {q, term, city, industry} search tasks."""
    terms, _ = _terms_for_industry(industry, refinements, keywords)
    cities = _cities_for_region(region, refinements)
    kw = keywords.strip() if keywords else ""
    # Don't duplicate keyword into the query when it's already the term
    kw_suffix = f" {kw}" if (kw and not any(kw.lower() in t.lower() for t in terms)) else ""

    plan = []
    for i in range(min(_MAX_QUERIES, max(len(terms), len(cities)))):
        term = terms[i % len(terms)]
        city = cities[i % len(cities)]
        plan.append({
            "term": term,
            "city": city,
            "industry": _TERM_INDUSTRY.get(term, refinements.get("industry") or industry or ""),
            "q": f"{term} {city} MN{kw_suffix}".strip(),
        })
        if len(plan) >= _MAX_QUERIES:
            break
    return plan


@clientfinder_bp.route("/discover", methods=["POST"])
def discover():
    """Stage 1: Multi-query discovery across DuckDuckGo, Bing, Google Maps & Yellow Pages.

    Streams Server-Sent Events when the client requests it ({"stream": true}); otherwise
    aggregates every query and returns a single JSON payload for direct API use.
    """
    data = request.get_json(silent=True) or {}
    industry    = data.get("industry", "")
    region      = data.get("region", "Both")
    keywords    = data.get("keywords", "")
    refinements = data.get("refinements", {}) or {}
    want_per_q  = int(data.get("per_query", 8))
    overall_cap = int(data.get("max_results", 12))
    stream      = bool(data.get("stream"))

    plan = _build_query_plan(industry, region, keywords, refinements)

    def _emit(q):
        async def _run():
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                q.put({"event": "error", "data": {"msg": "Playwright not available"}})
                q.put(None)
                return
            seen = set()
            candidates = []
            pool_cap = overall_cap * 3   # gather extra so verification can drop dead ones
            launch_opts = dict(headless=True,
                               args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_opts)
                try:
                    # ── Phase A: search every query, gather unique candidates ──
                    for idx, task in enumerate(plan):
                        q.put({"event": "query", "data": {
                            "idx": idx, "total": len(plan),
                            "q": task["q"], "industry": task["industry"], "city": task["city"],
                        }})
                        try:
                            found = await _multi_source_search(
                                browser, task["q"], task["industry"], task["city"], want=want_per_q)
                        except Exception as e:
                            q.put({"event": "query_error", "data": {"idx": idx, "error": str(e)[:120]}})
                            found = []
                        new_count = 0
                        for biz in found:
                            domain = _extract_domain(biz.get("website", ""))
                            if not domain or domain in seen or _is_aggregator(domain):
                                continue
                            if not _looks_like_real_domain(domain):
                                continue
                            seen.add(domain)
                            biz["industry"] = biz.get("industry") or task["industry"]
                            candidates.append(biz)
                            new_count += 1
                        q.put({"event": "query_done", "data": {
                            "idx": idx, "q": task["q"], "found": new_count,
                            "running_total": len(candidates)}})
                        if len(candidates) >= pool_cap:
                            break

                    # ── Phase B: verify reachability + capture screenshot ──────
                    q.put({"event": "verifying", "data": {"candidates": len(candidates)}})
                    ctx = await browser.new_context(
                        viewport={"width": 1280, "height": 800},
                        user_agent="Mozilla/5.0 (compatible; ClientFinder/1.0)",
                    )
                    sem = asyncio.Semaphore(5)

                    async def _verify(biz):
                        async with sem:
                            try:
                                site = await _scrape_site(ctx, biz)
                                return ("ok", {**biz, **site})
                            except Exception as e:
                                return ("drop", {"name": biz.get("name", ""),
                                                 "website": biz.get("website", ""),
                                                 "reason": str(e)[:80]})

                    tasks = [asyncio.create_task(_verify(b)) for b in candidates[:pool_cap]]
                    verified = 0
                    try:
                        for fut in asyncio.as_completed(tasks):
                            kind, payload = await fut
                            if kind == "ok":
                                if verified >= overall_cap:
                                    continue
                                verified += 1
                                q.put({"event": "business", "data": payload})
                            else:
                                q.put({"event": "dropped", "data": payload})
                            if verified >= overall_cap:
                                break
                    finally:
                        for t2 in tasks:
                            t2.cancel()
                        await ctx.close()
                finally:
                    await browser.close()
            q.put({"event": "done", "data": {"total": verified}})
            q.put(None)
        asyncio.run(_run())

    # ── Streaming mode ─────────────────────────────────────────────────────────
    if stream:
        result_q = queue.Queue()
        t = threading.Thread(target=_emit, args=(result_q,), daemon=True)
        t.start()

        def generate():
            yield f"data: {json.dumps({'event': 'start', 'queries': len(plan)})}\n\n"
            while True:
                try:
                    item = result_q.get(timeout=45)
                except queue.Empty:
                    yield "data: {\"event\":\"timeout\"}\n\n"
                    break
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"

        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ── Aggregated JSON mode (direct API / curl) ───────────────────────────────
    result_q = queue.Queue()
    t = threading.Thread(target=_emit, args=(result_q,), daemon=True)
    t.start()
    businesses, queries = [], []
    deadline = time.time() + 160
    while time.time() < deadline:
        try:
            item = result_q.get(timeout=45)
        except queue.Empty:
            break
        if item is None:
            break
        if item.get("event") == "business":
            businesses.append(item["data"])
        elif item.get("event") == "query":
            queries.append(item["data"]["q"])
    return jsonify({"ok": True, "businesses": businesses,
                    "queries": queries, "count": len(businesses)})



async def _scrape_site(ctx, biz):
    """Screenshot + tech/contact extraction for one site. Returns a data dict."""
    raw = biz.get("website", "")
    domain = _extract_domain(raw) or _clean_url(raw)
    name = biz.get("name", "")
    city = biz.get("city", "")

    # Try several URL variants so http-only / www-only sites still resolve.
    variants = []
    if raw.startswith("http"):
        variants.append(raw)
    if domain:
        if domain.startswith("www."):
            variants += [f"https://{domain}", f"https://{domain[4:]}", f"http://{domain}"]
        else:
            variants += [f"https://{domain}", f"https://www.{domain}", f"http://{domain}"]
    # De-dupe while preserving order
    seen_v, variants = set(), [v for v in variants if not (v in seen_v or seen_v.add(v))]

    page = await ctx.new_page()
    try:
        resp, used_url = None, ""
        for url in variants:
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=9000)
                if resp and resp.status < 400:
                    used_url = page.url
                    break
                resp = None
            except Exception:
                resp = None
                continue
        if not resp:
            raise RuntimeError(f"unreachable: {domain}")

        status_code = resp.status
        await page.wait_for_timeout(700)

        # Reject parked / empty-shell domains: require a title or real body text.
        title = (await page.title() or "").strip()
        body_text = await page.evaluate(
            "() => document.body ? (document.body.innerText || '').trim() : ''")
        if len(body_text) < 40 and len(title) < 3:
            raise RuntimeError(f"empty/parked: {domain}")
        parked_markers = ("domain is for sale", "buy this domain", "parked free",
                          "godaddy.com/domainsearch", "is parked", "domain for sale")
        if any(m in (body_text.lower()) for m in parked_markers):
            raise RuntimeError(f"parked: {domain}")

        # Screenshot
        fname = f"{uuid.uuid4().hex[:12]}.png"
        fpath = _SCREENSHOTS_DIR / fname
        await page.screenshot(path=str(fpath), full_page=False,
                              clip={"x": 0, "y": 0, "width": 1280, "height": 800})
        screenshot_url = f"/clientfinder/screenshot/{fname}"

        # Extract contact + stack signals
        html = await page.content()
        emails = list(set(re.findall(r'[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}', html)))[:3]
        phones = list(set(re.findall(r'\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}', html)))[:2]
        stack_flags = []
        html_lower = html.lower()
        if 'wp-content' in html_lower or 'wordpress' in html_lower:
            stack_flags.append("Legacy WordPress")
        if 'jquery' in html_lower and ('jquery/1.' in html_lower or 'jquery/2.' in html_lower):
            stack_flags.append("Outdated jQuery")
        final_url = page.url
        if status_code and not final_url.startswith("https://"):
            stack_flags.append("Missing HTTPS")
        viewport_meta = await page.evaluate("() => !!document.querySelector('meta[name=viewport]')")
        if not viewport_meta:
            stack_flags.append("No meta viewport")
        is_responsive = await page.evaluate("""() => {
            const w = window.innerWidth;
            window.resizeTo(375,812);
            const changed = document.body.scrollWidth <= 450;
            window.resizeTo(w, 800);
            return changed;
        }""")
        if not is_responsive:
            stack_flags.append("Non-responsive layout")

        return {
            "name": name, "website": _extract_domain(final_url) or domain, "city": city,
            "title": title[:120],
            "screenshot_url": screenshot_url, "emails": emails, "phones": phones,
            "stack_flags": stack_flags, "status_code": status_code,
        }
    finally:
        await page.close()


def _scores_from_flags(stack_flags):
    """Heuristic 1-10 sub-scores + composite from detected outdated-stack flags."""
    penalty = min(len(stack_flags or []) * 1.4, 7)
    base = max(1.0, 8.0 - penalty)
    modernity = max(1, min(10, round(base)))
    mobile    = max(1, min(10, round(base - (1 if any('responsive' in f.lower() or 'viewport' in f.lower() for f in (stack_flags or [])) else 0))))
    function  = max(1, min(10, round(base + 0.5)))
    composite = round((modernity + mobile + function) / 3, 1)
    return modernity, mobile, function, composite


@clientfinder_bp.route("/scrape", methods=["POST"])
def scrape_stream():
    """Stage 2: Playwright SSE stream — screenshot + tech extraction per site."""
    data = request.get_json(silent=True) or {}
    businesses = data.get("businesses", [])  # [{name, website, city, ...}]

    result_q = queue.Queue()

    def run_playwright():
        async def _scrape():
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                result_q.put({"event": "error", "data": {"msg": "Playwright not available"}})
                result_q.put(None)
                return

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                )
                ctx = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (compatible; ClientFinder/1.0)",
                )
                for idx, biz in enumerate(businesses):
                    try:
                        site = await _scrape_site(ctx, biz)
                        site["idx"] = idx
                        result_q.put({"event": "site", "data": site})
                    except Exception as e:
                        result_q.put({
                            "event": "site_error",
                            "data": {"idx": idx, "name": biz.get("name", ""),
                                     "website": biz.get("website", ""), "error": str(e)[:120]}
                        })
                await browser.close()
            result_q.put(None)  # sentinel

        asyncio.run(_scrape())

    t = threading.Thread(target=run_playwright, daemon=True)
    t.start()

    def generate():
        yield "data: {\"event\":\"start\",\"total\":" + str(len(businesses)) + "}\n\n"
        while True:
            try:
                item = result_q.get(timeout=30)
            except queue.Empty:
                yield "data: {\"event\":\"timeout\"}\n\n"
                break
            if item is None:
                yield "data: {\"event\":\"done\"}\n\n"
                break
            yield f"data: {json.dumps(item)}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@clientfinder_bp.route("/rescrape", methods=["POST"])
def rescrape_stream():
    """Re-run Playwright capture for existing CRM leads (by id) and persist the results.

    Body: {"lead_ids": [1,2,3]}. Streams SSE progress; each lead's screenshot, stack
    flags, recomputed scores and any newly found contact info are written back to the DB.
    """
    data = request.get_json(silent=True) or {}
    lead_ids = data.get("lead_ids", [])
    try:
        lead_ids = [int(x) for x in lead_ids]
    except (TypeError, ValueError):
        lead_ids = []

    # Load the target leads up front (website + current contact fields)
    conn = _get_conn()
    targets = []
    if lead_ids:
        placeholders = ",".join("?" * len(lead_ids))
        rows = conn.execute(
            f"SELECT id, company_name, website, city, email, phone FROM leads "
            f"WHERE id IN ({placeholders})", lead_ids).fetchall()
        targets = [dict(r) for r in rows]
    conn.close()

    result_q = queue.Queue()

    def run_playwright():
        async def _run():
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                result_q.put({"event": "error", "data": {"msg": "Playwright not available"}})
                result_q.put(None)
                return

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                )
                ctx = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (compatible; ClientFinder/1.0)",
                )
                for idx, lead in enumerate(targets):
                    if not lead.get("website"):
                        result_q.put({"event": "lead_error", "data": {
                            "idx": idx, "id": lead["id"], "name": lead["company_name"],
                            "error": "No website on file"}})
                        continue
                    try:
                        site = await _scrape_site(ctx, {
                            "name": lead["company_name"], "website": lead["website"],
                            "city": lead["city"],
                        })
                        modernity, mobile, function, composite = _scores_from_flags(site["stack_flags"])
                        patch = {
                            "screenshot_url": site["screenshot_url"],
                            "stack_flags": site["stack_flags"],
                            "outdated_stack": 1 if site["stack_flags"] else 0,
                            "score_modernity": modernity,
                            "score_mobile": mobile,
                            "score_function": function,
                            "composite_score": composite,
                        }
                        # Only fill contact info that was previously empty
                        if not lead.get("email") and site["emails"]:
                            patch["email"] = site["emails"][0]
                        if not lead.get("phone") and site["phones"]:
                            patch["phone"] = site["phones"][0]

                        c2 = _get_conn()
                        sets, params = [], []
                        for k, v in patch.items():
                            sets.append(f"{k}=?")
                            params.append(json.dumps(v) if k == "stack_flags" else v)
                        sets.append("updated_at=?"); params.append(_now())
                        params.append(lead["id"])
                        c2.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=?", params)
                        c2.commit(); c2.close()

                        result_q.put({"event": "lead", "data": {
                            "idx": idx, "id": lead["id"], "name": lead["company_name"],
                            "screenshot_url": site["screenshot_url"],
                            "stack_flags": site["stack_flags"],
                            "outdated_stack": bool(site["stack_flags"]),
                            "score_modernity": modernity, "score_mobile": mobile,
                            "score_function": function, "composite_score": composite,
                            "email": patch.get("email", lead.get("email")),
                            "phone": patch.get("phone", lead.get("phone")),
                            "status_code": site["status_code"],
                        }})
                    except Exception as e:
                        result_q.put({"event": "lead_error", "data": {
                            "idx": idx, "id": lead["id"], "name": lead["company_name"],
                            "error": str(e)[:120]}})
                await browser.close()
            result_q.put(None)

        asyncio.run(_run())

    t = threading.Thread(target=run_playwright, daemon=True)
    t.start()

    def generate():
        yield f"data: {json.dumps({'event': 'start', 'total': len(targets)})}\n\n"
        while True:
            try:
                item = result_q.get(timeout=30)
            except queue.Empty:
                yield "data: {\"event\":\"timeout\"}\n\n"
                break
            if item is None:
                yield "data: {\"event\":\"done\"}\n\n"
                break
            yield f"data: {json.dumps(item)}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})



@clientfinder_bp.route("/enrich", methods=["POST"])
def enrich():
    """Stage 4: Mock Apollo enrichment — deterministic DM info per company."""
    data = request.get_json(silent=True) or {}
    companies = data.get("companies", [])  # [{name, industry, city}]
    results = []
    for c in companies:
        dm = _mock_enrich(c.get("name",""), c.get("industry",""), c.get("city",""))
        results.append({"name": c.get("name"), **dm})
    return jsonify({"ok": True, "results": results})


# ─── Seed data (50 MN leads) ──────────────────────────────────────────────────

_SEED_LEADS = [
  # ENTERTAINMENT (7)
  {"company_name":"Memory Lanes Entertainment","industry":"Entertainment","city":"Minneapolis","employee_count":28,"website":"memorylanes.com","score_modernity":3,"score_mobile":2,"score_function":3,"composite_score":2.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout"],"dm_name":"Dale R.","dm_title":"Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@memorylanes.com","phone":"(612) 788-8188","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-05-28T14:22:00Z"},
  {"company_name":"Brunswick Zone Brooklyn Park","industry":"Entertainment","city":"Brooklyn Park","employee_count":45,"website":"brunswickzone.com","score_modernity":5,"score_mobile":5,"score_function":6,"composite_score":5.3,"outdated_stack":False,"stack_flags":["Outdated jQuery"],"dm_name":"Mark T.","dm_title":"General Manager","dm_seniority":"Manager","dm_linkedin":"","email":None,"phone":"(763) 315-8200","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-05-29T09:10:00Z"},
  {"company_name":"Pinstripes Edina","industry":"Entertainment","city":"Edina","employee_count":120,"website":"pinstripes.com","score_modernity":7,"score_mobile":7,"score_function":7,"composite_score":7.0,"outdated_stack":False,"stack_flags":[],"dm_name":"Amy K.","dm_title":"Director of Operations","dm_seniority":"Director","dm_linkedin":"https://linkedin.com/in/amyk-pinstripes","email":None,"phone":"(952) 835-0090","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-05-29T10:05:00Z"},
  {"company_name":"XP League Gaming Lounge","industry":"Entertainment","city":"Minneapolis","employee_count":12,"website":"xpleague.gg","score_modernity":7,"score_mobile":6,"score_function":5,"composite_score":6.0,"outdated_stack":False,"stack_flags":["No meta viewport"],"dm_name":"Chris M.","dm_title":"Franchise Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"minneapolis@xpleague.gg","phone":None,"contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-05-30T08:45:00Z"},
  {"company_name":"Brookview Golf Course","industry":"Entertainment","city":"Golden Valley","employee_count":22,"website":"goldenvalleymn.gov","score_modernity":2,"score_mobile":2,"score_function":3,"composite_score":2.3,"outdated_stack":True,"stack_flags":["Non-responsive layout","Missing HTTPS","Outdated jQuery","No meta viewport"],"dm_name":"Susan L.","dm_title":"Recreation Director","dm_seniority":"Director","dm_linkedin":"","email":"brookview@goldenvalleymn.gov","phone":"(763) 512-2300","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-05-30T11:30:00Z"},
  {"company_name":"Wild Woods Family Entertainment","industry":"Entertainment","city":"Duluth","employee_count":35,"website":"wildwoodsduluth.com","score_modernity":3,"score_mobile":3,"score_function":3,"composite_score":3.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Todd B.","dm_title":"Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@wildwoodsduluth.com","phone":"(218) 729-7529","contact_form_url":"wildwoodsduluth.com/contact","outreach_status":"New","notes":"","created_at":"2026-06-01T13:20:00Z"},
  {"company_name":"The Machine Shop Minneapolis","industry":"Entertainment","city":"Minneapolis","employee_count":55,"website":"themachineshopmpls.com","score_modernity":5,"score_mobile":4,"score_function":6,"composite_score":5.0,"outdated_stack":False,"stack_flags":["Outdated jQuery","No meta viewport"],"dm_name":"Ryan P.","dm_title":"Venue Director","dm_seniority":"Director","dm_linkedin":"","email":"booking@themachineshopmpls.com","phone":"(612) 722-1111","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-02T09:00:00Z"},
  # PROFESSIONAL SERVICES (10)
  {"company_name":"Periscope Creative","industry":"Professional Services","city":"Minneapolis","employee_count":75,"website":"periscope.com","score_modernity":8,"score_mobile":8,"score_function":7,"composite_score":7.7,"outdated_stack":False,"stack_flags":[],"dm_name":"Sarah J.","dm_title":"Chief Executive Officer","dm_seniority":"C-Suite","dm_linkedin":"https://linkedin.com/in/sarahj-periscope","email":None,"phone":"(612) 399-0600","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-05-28T15:00:00Z"},
  {"company_name":"Mono Advertising","industry":"Professional Services","city":"Minneapolis","employee_count":40,"website":"monoculture.com","score_modernity":8,"score_mobile":7,"score_function":8,"composite_score":7.7,"outdated_stack":False,"stack_flags":[],"dm_name":"James H.","dm_title":"Co-Founder & CCO","dm_seniority":"C-Suite","dm_linkedin":"https://linkedin.com/in/jamesh-mono","email":"hello@monoculture.com","phone":None,"contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-05-29T14:00:00Z"},
  {"company_name":"Zeus Jones Marketing","industry":"Professional Services","city":"Minneapolis","employee_count":30,"website":"zeusjones.com","score_modernity":7,"score_mobile":6,"score_function":7,"composite_score":6.7,"outdated_stack":False,"stack_flags":[],"dm_name":"Bill H.","dm_title":"Founding Partner","dm_seniority":"C-Suite","dm_linkedin":"https://linkedin.com/in/billh-zeusjones","email":None,"phone":"(612) 200-6262","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-05-30T10:15:00Z"},
  {"company_name":"North Loop Creative Agency","industry":"Professional Services","city":"Minneapolis","employee_count":15,"website":"northloopcreative.com","score_modernity":4,"score_mobile":3,"score_function":4,"composite_score":3.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","No SSL certificate","Non-responsive layout"],"dm_name":"Lisa M.","dm_title":"Creative Director","dm_seniority":"C-Suite","dm_linkedin":"","email":"hello@northloopcreative.com","phone":"(612) 555-0141","contact_form_url":"northloopcreative.com/contact","outreach_status":"New","notes":"","created_at":"2026-06-01T08:30:00Z"},
  {"company_name":"Summit Law Group","industry":"Professional Services","city":"Minneapolis","employee_count":18,"website":"summitlawmn.com","score_modernity":3,"score_mobile":2,"score_function":3,"composite_score":2.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout","Outdated jQuery"],"dm_name":"David K.","dm_title":"Managing Partner","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@summitlawmn.com","phone":"(612) 555-0213","contact_form_url":"summitlawmn.com/contact","outreach_status":"New","notes":"","created_at":"2026-06-02T11:00:00Z"},
  {"company_name":"Felhaber Larson Law","industry":"Professional Services","city":"St. Paul","employee_count":50,"website":"felhaber.com","score_modernity":4,"score_mobile":5,"score_function":5,"composite_score":4.7,"outdated_stack":False,"stack_flags":["Outdated jQuery"],"dm_name":"Patricia W.","dm_title":"Managing Partner","dm_seniority":"C-Suite","dm_linkedin":"","email":None,"phone":"(651) 222-5005","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-02T13:45:00Z"},
  {"company_name":"Granite Accounting Partners","industry":"Professional Services","city":"Bloomington","employee_count":22,"website":"graniteaccounting.com","score_modernity":3,"score_mobile":2,"score_function":3,"composite_score":2.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","No meta viewport"],"dm_name":"Greg F.","dm_title":"CPA & Principal","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@graniteaccounting.com","phone":"(952) 555-0182","contact_form_url":"graniteaccounting.com/contact","outreach_status":"New","notes":"","created_at":"2026-06-03T09:20:00Z"},
  {"company_name":"Northfield Architecture Studio","industry":"Professional Services","city":"Minneapolis","employee_count":8,"website":"northfieldarchstudio.com","score_modernity":4,"score_mobile":3,"score_function":4,"composite_score":3.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Outdated jQuery","Non-responsive layout"],"dm_name":"Anna C.","dm_title":"Principal Architect","dm_seniority":"C-Suite","dm_linkedin":"","email":"studio@northfieldarchstudio.com","phone":"(612) 555-0094","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-03T10:30:00Z"},
  {"company_name":"Lakeside Accounting Group","industry":"Professional Services","city":"Edina","employee_count":12,"website":"lakesideaccounting.com","score_modernity":2,"score_mobile":2,"score_function":3,"composite_score":2.3,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout","No meta viewport"],"dm_name":"Robert M.","dm_title":"Founding Partner","dm_seniority":"C-Suite","dm_linkedin":"","email":"contact@lakesideaccounting.com","phone":"(952) 555-0273","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-03T14:00:00Z"},
  {"company_name":"Prairie Marketing Collective","industry":"Professional Services","city":"St. Cloud","employee_count":9,"website":"prairiemarketingco.com","score_modernity":3,"score_mobile":3,"score_function":3,"composite_score":3.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout"],"dm_name":"Michelle H.","dm_title":"Owner & Strategist","dm_seniority":"C-Suite","dm_linkedin":"","email":"hello@prairiemarketingco.com","phone":"(320) 555-0148","contact_form_url":"prairiemarketingco.com/contact","outreach_status":"New","notes":"","created_at":"2026-06-04T08:00:00Z"},
  # HOME & COMMERCIAL SERVICES (9)
  {"company_name":"Genz-Ryan Heating & Cooling","industry":"Home & Commercial Services","city":"Burnsville","employee_count":120,"website":"genzryan.com","score_modernity":6,"score_mobile":6,"score_function":6,"composite_score":6.0,"outdated_stack":False,"stack_flags":["Outdated jQuery"],"dm_name":"Jim R.","dm_title":"President","dm_seniority":"C-Suite","dm_linkedin":"https://linkedin.com/in/jimr-genzryan","email":None,"phone":"(952) 767-1000","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-05-28T16:00:00Z"},
  {"company_name":"Sedgwick Heating & Air Conditioning","industry":"Home & Commercial Services","city":"Minneapolis","employee_count":35,"website":"sedgwickheating.com","score_modernity":3,"score_mobile":2,"score_function":3,"composite_score":2.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Missing HTTPS"],"dm_name":"Kevin S.","dm_title":"Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@sedgwickheating.com","phone":"(612) 827-2561","contact_form_url":"sedgwickheating.com/contact","outreach_status":"New","notes":"","created_at":"2026-06-01T15:00:00Z"},
  {"company_name":"Standard Heating & Air Conditioning","industry":"Home & Commercial Services","city":"Minneapolis","employee_count":55,"website":"standardheating.com","score_modernity":5,"score_mobile":5,"score_function":5,"composite_score":5.0,"outdated_stack":False,"stack_flags":["Outdated jQuery","No meta viewport"],"dm_name":"Tom A.","dm_title":"CEO","dm_seniority":"C-Suite","dm_linkedin":"","email":"contact@standardheating.com","phone":"(612) 824-3981","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-02T09:30:00Z"},
  {"company_name":"All American Plumbing & Heating","industry":"Home & Commercial Services","city":"Roseville","employee_count":18,"website":"allamericanplumbingmn.com","score_modernity":2,"score_mobile":2,"score_function":2,"composite_score":2.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout","No meta viewport","No SSL certificate"],"dm_name":"Bob D.","dm_title":"Owner & Master Plumber","dm_seniority":"C-Suite","dm_linkedin":"","email":"bob@allamericanplumbingmn.com","phone":"(651) 555-0237","contact_form_url":"allamericanplumbingmn.com/contact","outreach_status":"New","notes":"","created_at":"2026-06-02T10:45:00Z"},
  {"company_name":"Northshore Landscaping & Design","industry":"Home & Commercial Services","city":"Duluth","employee_count":25,"website":"northshorelandscaping.com","score_modernity":3,"score_mobile":3,"score_function":3,"composite_score":3.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Paul N.","dm_title":"Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"paul@northshorelandscaping.com","phone":"(218) 555-0134","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-03T11:00:00Z"},
  {"company_name":"Great Lawns Landscaping","industry":"Home & Commercial Services","city":"Bloomington","employee_count":14,"website":"greatlawnsbloomington.com","score_modernity":2,"score_mobile":2,"score_function":2,"composite_score":2.0,"outdated_stack":True,"stack_flags":["Missing HTTPS","Non-responsive layout","No SSL certificate","No meta viewport"],"dm_name":"Dave W.","dm_title":"Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@greatlawnsbloomington.com","phone":"(952) 555-0189","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-03T13:30:00Z"},
  {"company_name":"Superior Commercial Cleaning","industry":"Home & Commercial Services","city":"St. Cloud","employee_count":40,"website":"superiorcleaningmn.com","score_modernity":3,"score_mobile":2,"score_function":3,"composite_score":2.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Missing HTTPS"],"dm_name":"Maria G.","dm_title":"Operations Manager","dm_seniority":"Manager","dm_linkedin":"","email":"maria@superiorcleaningmn.com","phone":"(320) 555-0211","contact_form_url":"superiorcleaningmn.com/contact","outreach_status":"New","notes":"","created_at":"2026-06-04T09:00:00Z"},
  {"company_name":"Lakes Area Electric","industry":"Home & Commercial Services","city":"Brainerd","employee_count":12,"website":"lakesareaelectric.com","score_modernity":3,"score_mobile":3,"score_function":3,"composite_score":3.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Outdated jQuery","Non-responsive layout"],"dm_name":"Eric H.","dm_title":"Master Electrician & Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"eric@lakesareaelectric.com","phone":"(218) 555-0167","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-04T11:15:00Z"},
  {"company_name":"Tri-City Building Services","industry":"Home & Commercial Services","city":"Moorhead","employee_count":28,"website":"tricitybuilding.com","score_modernity":2,"score_mobile":2,"score_function":2,"composite_score":2.0,"outdated_stack":True,"stack_flags":["Missing HTTPS","Non-responsive layout","No meta viewport","No SSL certificate"],"dm_name":"Mike B.","dm_title":"President","dm_seniority":"C-Suite","dm_linkedin":"","email":"office@tricitybuilding.com","phone":"(218) 555-0093","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-04T14:00:00Z"},
  # HEALTHCARE & WELLNESS (9)
  {"company_name":"Uptown Dental Studio","industry":"Healthcare & Wellness","city":"Minneapolis","employee_count":15,"website":"uptowndentalstudio.com","score_modernity":5,"score_mobile":5,"score_function":5,"composite_score":5.0,"outdated_stack":False,"stack_flags":["Outdated jQuery"],"dm_name":"Dr. Jennifer L.","dm_title":"Lead Dentist & Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"appointments@uptowndentalstudio.com","phone":"(612) 824-4600","contact_form_url":"uptowndentalstudio.com/contact","outreach_status":"New","notes":"","created_at":"2026-06-03T15:00:00Z"},
  {"company_name":"Summit Dental Partners","industry":"Healthcare & Wellness","city":"Edina","employee_count":22,"website":"summitdentalpartners.com","score_modernity":4,"score_mobile":3,"score_function":4,"composite_score":3.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Dr. Kevin R.","dm_title":"Owner & General Dentist","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@summitdentalpartners.com","phone":"(952) 555-0188","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-04T08:30:00Z"},
  {"company_name":"Lakes Area Family Dentistry","industry":"Healthcare & Wellness","city":"Brainerd","employee_count":10,"website":"lakesareadentistry.com","score_modernity":2,"score_mobile":2,"score_function":2,"composite_score":2.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout","No meta viewport"],"dm_name":"Dr. Thomas W.","dm_title":"Owner & DDS","dm_seniority":"C-Suite","dm_linkedin":"","email":"office@lakesareadentistry.com","phone":"(218) 555-0142","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-04T10:00:00Z"},
  {"company_name":"Rochester Family Dentistry","industry":"Healthcare & Wellness","city":"Rochester","employee_count":18,"website":"rochesterfamilydentistry.com","score_modernity":3,"score_mobile":3,"score_function":3,"composite_score":3.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Dr. Susan O.","dm_title":"Owner & Dentist","dm_seniority":"C-Suite","dm_linkedin":"","email":"appointments@rochesterfamilydentistry.com","phone":"(507) 555-0215","contact_form_url":"rochesterfamilydentistry.com/appointment","outreach_status":"New","notes":"","created_at":"2026-06-04T13:00:00Z"},
  {"company_name":"Lakeside Physical Therapy","industry":"Healthcare & Wellness","city":"Minnetonka","employee_count":12,"website":"lakesideptmn.com","score_modernity":4,"score_mobile":3,"score_function":4,"composite_score":3.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Amy T.","dm_title":"Clinical Director","dm_seniority":"Director","dm_linkedin":"","email":"info@lakesideptmn.com","phone":"(952) 555-0176","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-05T09:00:00Z"},
  {"company_name":"North Star Chiropractic","industry":"Healthcare & Wellness","city":"Eagan","employee_count":8,"website":"northstarchiro.com","score_modernity":3,"score_mobile":2,"score_function":3,"composite_score":2.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout"],"dm_name":"Dr. Mark F.","dm_title":"Owner & Chiropractor","dm_seniority":"C-Suite","dm_linkedin":"","email":"drmark@northstarchiro.com","phone":"(651) 555-0122","contact_form_url":"northstarchiro.com/schedule","outreach_status":"New","notes":"","created_at":"2026-06-05T10:30:00Z"},
  {"company_name":"FORM Wellness Collective","industry":"Healthcare & Wellness","city":"Minneapolis","employee_count":20,"website":"formwellnesscollective.com","score_modernity":7,"score_mobile":7,"score_function":6,"composite_score":6.7,"outdated_stack":False,"stack_flags":[],"dm_name":"Claire B.","dm_title":"Founder & CEO","dm_seniority":"C-Suite","dm_linkedin":"https://linkedin.com/in/claireb-form","email":"hello@formwellnesscollective.com","phone":"(612) 555-0108","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-05T12:00:00Z"},
  {"company_name":"Skin Perfect Med Spa","industry":"Healthcare & Wellness","city":"Eden Prairie","employee_count":10,"website":"skinperfectmedspa.com","score_modernity":5,"score_mobile":4,"score_function":5,"composite_score":4.7,"outdated_stack":False,"stack_flags":["Outdated jQuery","No meta viewport"],"dm_name":"Natalie K.","dm_title":"Owner & Aesthetician","dm_seniority":"C-Suite","dm_linkedin":"","email":"appointments@skinperfectmedspa.com","phone":"(952) 555-0194","contact_form_url":"skinperfectmedspa.com/book","outreach_status":"New","notes":"","created_at":"2026-06-05T14:00:00Z"},
  {"company_name":"Iron Fitness MN","industry":"Healthcare & Wellness","city":"Plymouth","employee_count":16,"website":"ironfitnessplymouth.com","score_modernity":4,"score_mobile":3,"score_function":4,"composite_score":3.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Jason M.","dm_title":"Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@ironfitnessplymouth.com","phone":"(763) 555-0139","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-05T15:30:00Z"},
  # RETAIL & HOSPITALITY (8)
  {"company_name":"Indeed Brewing Company","industry":"Retail & Hospitality","city":"Minneapolis","employee_count":42,"website":"indeedbrewing.com","score_modernity":7,"score_mobile":7,"score_function":7,"composite_score":7.0,"outdated_stack":False,"stack_flags":[],"dm_name":"Tom H.","dm_title":"Co-Founder","dm_seniority":"C-Suite","dm_linkedin":"https://linkedin.com/in/tomh-indeed","email":"hello@indeedbrewing.com","phone":"(612) 843-5090","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-01T09:00:00Z"},
  {"company_name":"Fulton Beer","industry":"Retail & Hospitality","city":"Minneapolis","employee_count":38,"website":"fultonbeer.com","score_modernity":6,"score_mobile":6,"score_function":6,"composite_score":6.0,"outdated_stack":False,"stack_flags":["Outdated jQuery"],"dm_name":"Ryan P.","dm_title":"Co-Founder & COO","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@fultonbeer.com","phone":"(612) 333-3208","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-01T10:30:00Z"},
  {"company_name":"Lake & City Brewing Co.","industry":"Retail & Hospitality","city":"St. Paul","employee_count":18,"website":"lakeandcitybrewing.com","score_modernity":4,"score_mobile":3,"score_function":4,"composite_score":3.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Mike C.","dm_title":"Founder & Head Brewer","dm_seniority":"C-Suite","dm_linkedin":"","email":"mike@lakeandcitybrewing.com","phone":"(651) 555-0156","contact_form_url":"lakeandcitybrewing.com/contact","outreach_status":"New","notes":"","created_at":"2026-06-04T16:00:00Z"},
  {"company_name":"Tattersall Distilling","industry":"Retail & Hospitality","city":"Minneapolis","employee_count":28,"website":"tattersalldistilling.com","score_modernity":7,"score_mobile":6,"score_function":7,"composite_score":6.7,"outdated_stack":False,"stack_flags":[],"dm_name":"Dan S.","dm_title":"Co-Founder & CEO","dm_seniority":"C-Suite","dm_linkedin":"https://linkedin.com/in/dans-tattersall","email":"info@tattersalldistilling.com","phone":"(612) 584-4152","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-04T17:00:00Z"},
  {"company_name":"Birchwood Cafe","industry":"Retail & Hospitality","city":"Minneapolis","employee_count":45,"website":"birchwoodcafe.com","score_modernity":5,"score_mobile":4,"score_function":5,"composite_score":4.7,"outdated_stack":False,"stack_flags":["Outdated jQuery","No meta viewport"],"dm_name":"Tracy R.","dm_title":"Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@birchwoodcafe.com","phone":"(612) 722-4474","contact_form_url":"birchwoodcafe.com/contact","outreach_status":"New","notes":"","created_at":"2026-06-05T08:00:00Z"},
  {"company_name":"City Garage Auto Repair","industry":"Retail & Hospitality","city":"Minneapolis","employee_count":14,"website":"citygaragempls.com","score_modernity":2,"score_mobile":2,"score_function":2,"composite_score":2.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout","No SSL certificate","No meta viewport"],"dm_name":"Steve M.","dm_title":"Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"steve@citygaragempls.com","phone":"(612) 555-0261","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-05T09:30:00Z"},
  {"company_name":"Rochester Farmers Market Hub","industry":"Retail & Hospitality","city":"Rochester","employee_count":6,"website":"rochesterfarmersmarket.com","score_modernity":3,"score_mobile":2,"score_function":3,"composite_score":2.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","No meta viewport"],"dm_name":"Linda H.","dm_title":"Executive Director","dm_seniority":"Director","dm_linkedin":"","email":"info@rochesterfarmersmarket.com","phone":"(507) 555-0178","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-06T10:00:00Z"},
  {"company_name":"Northern Threads Boutique","industry":"Retail & Hospitality","city":"Mankato","employee_count":8,"website":"northernthreadsboutique.com","score_modernity":4,"score_mobile":3,"score_function":4,"composite_score":3.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Jenna L.","dm_title":"Owner & Buyer","dm_seniority":"C-Suite","dm_linkedin":"","email":"jenna@northernthreadsboutique.com","phone":"(507) 555-0203","contact_form_url":"northernthreadsboutique.com/contact","outreach_status":"New","notes":"","created_at":"2026-06-06T11:30:00Z"},
  # MANUFACTURING & LOGISTICS (7)
  {"company_name":"Great Northern Machining","industry":"Manufacturing & Logistics","city":"St. Cloud","employee_count":65,"website":"greatnorthernmachining.com","score_modernity":2,"score_mobile":2,"score_function":2,"composite_score":2.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout","No SSL certificate","No meta viewport"],"dm_name":"Carl B.","dm_title":"President & Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"carl@greatnorthernmachining.com","phone":"(320) 555-0147","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-06T13:00:00Z"},
  {"company_name":"Precision Machine Works","industry":"Manufacturing & Logistics","city":"Mankato","employee_count":35,"website":"precisionmachineworks.com","score_modernity":2,"score_mobile":2,"score_function":3,"composite_score":2.3,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Missing HTTPS","Outdated jQuery"],"dm_name":"Richard F.","dm_title":"Owner & CNC Specialist","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@precisionmachineworks.com","phone":"(507) 555-0166","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-06T14:30:00Z"},
  {"company_name":"Northland Logistics Solutions","industry":"Manufacturing & Logistics","city":"Duluth","employee_count":85,"website":"northlandlogistics.com","score_modernity":3,"score_mobile":3,"score_function":4,"composite_score":3.3,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Gary L.","dm_title":"CEO","dm_seniority":"C-Suite","dm_linkedin":"https://linkedin.com/in/garyl-northland","email":"operations@northlandlogistics.com","phone":"(218) 555-0133","contact_form_url":"northlandlogistics.com/quote","outreach_status":"New","notes":"","created_at":"2026-06-07T08:00:00Z"},
  {"company_name":"Minnesota Metal Fabricators","industry":"Manufacturing & Logistics","city":"Bloomington","employee_count":45,"website":"mnmetalfab.com","score_modernity":2,"score_mobile":2,"score_function":2,"composite_score":2.0,"outdated_stack":True,"stack_flags":["Missing HTTPS","Non-responsive layout","No SSL certificate","No meta viewport"],"dm_name":"Dennis S.","dm_title":"Owner & Plant Manager","dm_seniority":"C-Suite","dm_linkedin":"","email":"dennis@mnmetalfab.com","phone":"(952) 555-0181","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-07T09:30:00Z"},
  {"company_name":"River Valley Food Producers","industry":"Manufacturing & Logistics","city":"Winona","employee_count":55,"website":"rivervalleyfood.com","score_modernity":3,"score_mobile":3,"score_function":3,"composite_score":3.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Ellen P.","dm_title":"President","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@rivervalleyfood.com","phone":"(507) 555-0192","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-07T11:00:00Z"},
  {"company_name":"Twin Cities Metal Works","industry":"Manufacturing & Logistics","city":"St. Louis Park","employee_count":28,"website":"twincitiesmetalworks.com","score_modernity":2,"score_mobile":2,"score_function":2,"composite_score":2.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout","No SSL certificate"],"dm_name":"Bruce N.","dm_title":"Shop Owner & Welder","dm_seniority":"C-Suite","dm_linkedin":"","email":"bruce@twincitiesmetalworks.com","phone":"(952) 555-0116","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-07T13:00:00Z"},
  {"company_name":"North Star Freight Solutions","industry":"Manufacturing & Logistics","city":"Moorhead","employee_count":95,"website":"northstarfreight.com","score_modernity":4,"score_mobile":3,"score_function":4,"composite_score":3.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Mike H.","dm_title":"VP of Operations","dm_seniority":"Director","dm_linkedin":"https://linkedin.com/in/mikeh-northstar","email":"dispatch@northstarfreight.com","phone":"(218) 555-0178","contact_form_url":"northstarfreight.com/contact","outreach_status":"New","notes":"","created_at":"2026-06-08T09:00:00Z"},
]

# Auto-initialize on import
init_clientfinder_db()
