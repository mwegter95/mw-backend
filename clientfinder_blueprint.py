"""Client Finder 1.0 — Flask Blueprint. Mounts under /clientfinder."""
import json
import sqlite3
import csv
import io
import os
import re
import uuid
import random
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
            screenshots TEXT DEFAULT '[]',
            quality_notes TEXT DEFAULT '[]',
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

    # Migrate older DBs that predate the multi-screenshot column.
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(leads)").fetchall()]
        if "screenshots" not in cols:
            conn.execute("ALTER TABLE leads ADD COLUMN screenshots TEXT DEFAULT '[]'")
            conn.commit()
        if "quality_notes" not in cols:
            conn.execute("ALTER TABLE leads ADD COLUMN quality_notes TEXT DEFAULT '[]'")
            conn.commit()
    except Exception:
        pass

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
        try:
            d['screenshots'] = json.loads(d.get('screenshots') or '[]')
        except Exception:
            d['screenshots'] = []
        try:
            d['quality_notes'] = json.loads(d.get('quality_notes') or '[]')
        except Exception:
            d['quality_notes'] = []
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
                screenshot_url,screenshots,quality_notes,score_modernity,score_mobile,score_function,composite_score,
                outdated_stack,stack_flags,dm_name,dm_title,dm_seniority,email,phone,
                contact_form_url,outreach_status,notes,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            item.get('company_name', ''), item.get('industry', ''), item.get('city', ''),
            item.get('employee_count'), item.get('website', ''), item.get('screenshot_url', ''),
            json.dumps(item.get('screenshots', []) or []),
            json.dumps(item.get('quality_notes', []) or []),
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
               'website', 'industry', 'city', 'employee_count', 'screenshots', 'quality_notes'}
    sets, params = [], []
    for k, v in data.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(
                json.dumps(v) if k in ('stack_flags', 'screenshots', 'quality_notes')
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
    "merriam-webster.com","dictionary.com","britannica.com","bing.com","wiktionary.org",
    "thefreedictionary.com","collinsdictionary.com","vocabulary.com","quora.com",
    "wikihow.com","investopedia.com","glassdoor.com","indeed.com","ziprecruiter.com",
    "msn.com","microsoft.com","yahoo.com","duckduckgo.com","fandom.com","britannica.com",
}

def _is_aggregator(domain):
    return any(domain.endswith(d) for d in _SKIP_DOMAINS)


# National brands / chains / enterprises that are not realistic small-business web
# leads (you won't be building a site for USPS or AutoZone). Dropped during discovery.
_SKIP_BRANDS = {
    # shipping / logistics
    "usps.com", "ups.com", "fedex.com", "dhl.com", "uline.com", "freightquote.com",
    # big box / national retail
    "walmart.com", "target.com", "homedepot.com", "lowes.com", "menards.com",
    "bestbuy.com", "costco.com", "samsclub.com", "ikea.com", "macys.com", "kohls.com",
    "autozone.com", "oreillyauto.com", "advanceautoparts.com", "napaonline.com",
    "acehardware.com", "truevalue.com", "petco.com", "petsmart.com", "michaels.com",
    "dickssportinggoods.com", "officedepot.com", "staples.com", "guitarcenter.com",
    # grocery / pharmacy chains
    "cvs.com", "walgreens.com", "kroger.com", "cub.com", "hy-vee.com", "aldi.us",
    "wholefoodsmarket.com", "traderjoes.com", "target.com",
    # food / restaurant chains
    "mcdonalds.com", "starbucks.com", "subway.com", "chipotle.com", "dominos.com",
    "pizzahut.com", "wendys.com", "burgerking.com", "tacobell.com", "kfc.com",
    "dunkindonuts.com", "chick-fil-a.com", "panerabread.com", "applebees.com",
    "olivegarden.com", "buffalowildwings.com", "culvers.com", "cariboucoffee.com",
    # hotels / travel
    "marriott.com", "hilton.com", "hyatt.com", "ihg.com", "choicehotels.com",
    "bestwestern.com", "wyndhamhotels.com", "airbnb.com", "vrbo.com",
    # telecom / utilities / banks / insurance nationals
    "verizon.com", "att.com", "t-mobile.com", "xfinity.com", "comcast.com",
    "centurylink.com", "chase.com", "bankofamerica.com", "wellsfargo.com",
    "usbank.com", "citi.com", "capitalone.com", "statefarm.com", "geico.com",
    "progressive.com", "allstate.com", "libertymutual.com", "aaa.com",
    # fitness / services chains
    "planetfitness.com", "lifetime.life", "anytimefitness.com", "lacarpet.com",
    "jiffylube.com", "midas.com", "meineke.com", "valvoline.com", "firestone.com",
    "discounttire.com", "hrblock.com", "jackson-hewitt.com", "libertytax.com",
    # auto manufacturers / dealers national
    "ford.com", "chevrolet.com", "toyota.com", "honda.com", "carmax.com", "carvana.com",
}


def _is_national_brand(domain):
    d = domain[4:] if domain.startswith("www.") else domain
    return d in _SKIP_BRANDS or any(d.endswith("." + b) for b in _SKIP_BRANDS)


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


# A realistic desktop-Chrome context. Big sites behind WAFs (Akamai/Cloudflare)
# return 403/503 for bot-looking UAs, so every Playwright context uses this.
_REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_BROWSER_CTX_OPTS = dict(
    viewport={"width": 1280, "height": 800},
    user_agent=_REAL_UA,
    locale="en-US",
    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
)
_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--disable-blink-features=AutomationControlled"]


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
    """DuckDuckGo HTML — best-effort. Prefers the .result__url display text for the
    domain (stable); falls back to decoding the uddg redirect on the anchor href."""
    page = await ctx.new_page()
    results = []
    diag = {"source": "ddg", "loaded": False, "title": "", "rows": 0, "kept": 0, "note": ""}
    try:
        safe_q = _urlencode(
            query + " -site:yelp.com -site:yellowpages.com -site:facebook.com -site:linkedin.com"
        )
        resp = await page.goto(f"https://html.duckduckgo.com/html/?q={safe_q}&kl=us-en",
                               wait_until="domcontentloaded", timeout=15000)
        diag["status"] = resp.status if resp else 0
        await page.wait_for_timeout(700)
        diag["loaded"] = True
        try:
            diag["title"] = (await page.title() or "")[:60]
        except Exception:
            pass

        rows = (await page.query_selector_all(".result, .web-result"))[:16]
        diag["rows"] = len(rows)
        filtered = 0
        for item in rows:
            try:
                title = ""
                a = await item.query_selector("a.result__a, .result__title a")
                if a:
                    title = (await a.inner_text()).strip()

                domain = ""
                url_el = await item.query_selector(".result__url")
                if url_el:
                    disp = (await url_el.inner_text()).strip()
                    # _extract_domain strips scheme + path itself, so just take the
                    # first whitespace token (never pre-split on '/').
                    domain = _extract_domain(re.split(r'\s', disp)[0] if disp else "")
                if not domain and a:
                    domain = _extract_domain(_decode_ddg_href(await a.get_attribute("href") or ""))
                if not domain or _is_aggregator(domain) or not _looks_like_real_domain(domain):
                    filtered += 1
                    continue
                results.append({
                    "name": _clean_name(title) or domain, "website": domain,
                    "city": city, "address": "", "source": "DuckDuckGo",
                })
                if len(results) >= 10:
                    break
            except Exception:
                continue
        diag["kept"] = len(results)
        if diag["rows"] == 0:
            diag["note"] = f"0 result rows on page (title='{diag['title']}'). DDG likely served a blank/challenge page"
        else:
            diag["note"] = f"{diag['rows']} rows, {filtered} filtered (aggregator/invalid), {diag['kept']} kept"
    except Exception as e:
        diag["note"] = f"error: {str(e)[:90]}"
    finally:
        await page.close()
    return results, diag


async def _search_bing(ctx, query, city):
    """Bing HTML — reliable fallback. The real domain is in the <cite> display URL;
    the anchor href is a bing.com/ck/a tracking redirect, so we must NOT use it."""
    page = await ctx.new_page()
    results = []
    diag = {"source": "bing", "loaded": False, "title": "", "rows": 0, "kept": 0, "note": ""}
    try:
        safe_q = _urlencode(
            query + " -site:yelp.com -site:yellowpages.com -site:facebook.com -site:linkedin.com"
        )
        resp = await page.goto(f"https://www.bing.com/search?q={safe_q}&setlang=en-us&cc=us",
                               wait_until="domcontentloaded", timeout=15000)
        diag["status"] = resp.status if resp else 0
        await page.wait_for_timeout(700)
        diag["loaded"] = True
        try:
            diag["title"] = (await page.title() or "")[:60]
        except Exception:
            pass

        rows = (await page.query_selector_all("li.b_algo"))[:16]
        diag["rows"] = len(rows)
        no_cite = 0
        filtered = 0
        for item in rows:
            try:
                title = ""
                a = await item.query_selector("h2 a")
                if a:
                    title = (await a.inner_text()).strip()

                # Real domain comes from the cite/display URL, e.g.
                # "https://www.example.com › about" -> example.com.
                # NOTE: split only on whitespace/breadcrumb, never on '/', or
                # "https://..." becomes "https:" and every result gets dropped.
                domain = ""
                cite = await item.query_selector("cite")
                if cite:
                    ctext = (await cite.inner_text()).strip()
                    first = re.split(r'\s|›', ctext)[0] if ctext else ""
                    domain = _extract_domain(first)
                else:
                    no_cite += 1
                if not domain or _is_aggregator(domain) or not _looks_like_real_domain(domain):
                    filtered += 1
                    continue
                results.append({
                    "name": _clean_name(title) or domain, "website": domain,
                    "city": city, "address": "", "source": "Bing",
                })
                if len(results) >= 10:
                    break
            except Exception:
                continue
        diag["kept"] = len(results)
        if diag["rows"] == 0:
            diag["note"] = f"0 b_algo rows on page (title='{diag['title']}'). Bing served no organic results / consent page"
        else:
            diag["note"] = f"{diag['rows']} rows, {no_cite} missing <cite>, {filtered} filtered, {diag['kept']} kept"
    except Exception as e:
        diag["note"] = f"error: {str(e)[:90]}"
    finally:
        await page.close()
    return results, diag


async def _search_google_maps(ctx, query, city):
    """Google Maps — click each card to retrieve the website link from the detail panel."""
    page = await ctx.new_page()
    results = []
    diag = {"source": "maps", "loaded": False, "title": "", "rows": 0, "kept": 0,
            "consent": False, "no_website": 0, "note": ""}
    try:
        resp = await page.goto(
            f"https://www.google.com/maps/search/{_urlencode(query)}?hl=en&gl=us",
            wait_until="domcontentloaded", timeout=20000,
        )
        diag["status"] = resp.status if resp else 0
        await page.wait_for_timeout(2200)
        diag["loaded"] = True
        try:
            diag["title"] = (await page.title() or "")[:60]
            diag["url"] = (page.url or "")[:80]
        except Exception:
            pass

        # Dismiss a consent dialog if Google shows one.
        for sel in ['button[aria-label*="Accept all" i]', 'button[aria-label*="Agree" i]',
                    'form[action*="consent"] button']:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    diag["consent"] = True
                    await btn.click()
                    await page.wait_for_timeout(1200)
                    break
            except Exception:
                pass
        if "consent.google" in (page.url or "") or "/sorry/" in (page.url or ""):
            diag["consent"] = True

        # Scroll the results feed a couple times so more cards load.
        try:
            feed = await page.query_selector('[role="feed"]')
            for _ in range(3):
                if feed:
                    await feed.evaluate("el => el.scrollBy(0, el.scrollHeight)")
                    await page.wait_for_timeout(900)
        except Exception:
            pass

        # Result cards are role=article or .Nv2PK
        cards = await page.query_selector_all('[role="article"]')
        if not cards:
            cards = await page.query_selector_all(".Nv2PK")
        diag["rows"] = len(cards)

        for card in cards[:6]:
            try:
                # Grab name from card before clicking
                heading = await card.query_selector('[role="heading"]')
                name = (await heading.inner_text()).strip() if heading else ""
                if not name:
                    continue

                await card.click()
                await page.wait_for_timeout(1300)

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

                dom = _extract_domain(website)
                if not dom:
                    diag["no_website"] += 1
                elif _is_aggregator(dom) or not _looks_like_real_domain(dom):
                    pass
                else:
                    results.append({
                        "name": name, "website": dom,
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
        diag["kept"] = len(results)
        if diag["consent"]:
            diag["note"] = f"blocked by Google consent/sorry page (url='{diag.get('url','')}'). datacenter IP likely flagged"
        elif diag["rows"] == 0:
            diag["note"] = f"0 result cards found (title='{diag['title']}'). selectors stale or no local results"
        else:
            diag["note"] = f"{diag['rows']} cards, {diag['no_website']} had no website link, {diag['kept']} kept"
    except Exception as e:
        diag["note"] = f"error: {str(e)[:90]}"
    finally:
        await page.close()
    return results, diag


async def _search_yellow_pages(ctx, industry, city):
    """Yellow Pages search — website link is in-card, no click-through needed."""
    page = await ctx.new_page()
    results = []
    diag = {"source": "yp", "loaded": False, "title": "", "rows": 0, "kept": 0, "no_website": 0, "note": ""}
    try:
        city_yp = city.replace(" ", "+")
        url = (
            f"https://www.yellowpages.com/search"
            f"?search_terms={_urlencode(industry)}"
            f"&geo_location_terms={city_yp}%2C+MN"
        )
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        diag["status"] = resp.status if resp else 0
        await page.wait_for_timeout(1000)
        diag["loaded"] = True
        try:
            diag["title"] = (await page.title() or "")[:60]
        except Exception:
            pass

        rows = (await page.query_selector_all(".result"))[:8]
        diag["rows"] = len(rows)
        for listing in rows:
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
                if not website:
                    diag["no_website"] += 1
                    continue

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
        diag["kept"] = len(results)
        if diag["rows"] == 0:
            diag["note"] = f"0 .result rows (title='{diag['title']}')"
        else:
            diag["note"] = f"{diag['rows']} rows, {diag['no_website']} had no website link, {diag['kept']} kept"
    except Exception as e:
        diag["note"] = f"error: {str(e)[:90]}"
    finally:
        await page.close()
    return results, diag


async def _multi_source_search(browser, query, industry, city, want=12):
    """Run search sources concurrently in isolated contexts, dedupe by domain.

    Returns (businesses, source_counts, diags) — diags carries a per-source note
    explaining exactly what each engine did (so the UI can show why a source got 0).
    """
    contexts = [await browser.new_context(**_BROWSER_CTX_OPTS) for _ in range(4)]
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

    source_names = ["ddg", "bing", "maps", "yp"]
    counts = {n: 0 for n in source_names}
    diags = []
    seen, businesses = set(), []
    for name, item in zip(source_names, raw):
        if isinstance(item, Exception):
            diags.append({"source": name, "note": f"crashed: {str(item)[:90]}"})
            continue
        batch, diag = item
        diags.append(diag)
        counts[name] = len(batch or [])
        for biz in (batch or []):
            domain = _extract_domain(biz.get("website", ""))
            key = domain or (biz.get("name", "") or "").lower()
            if key and key not in seen:
                seen.add(key)
                biz["industry"] = industry
                businesses.append(biz)
    return businesses[:want], counts, diags


# ─── Query planning ────────────────────────────────────────────────────────────
# Concrete, searchable business types per industry — never the literal "local business".
# Pools are intentionally large so each run can sample a different slice.
_INDUSTRY_QUERY_TERMS = {
    "Entertainment": [
        "bowling alley", "family entertainment center", "escape room", "arcade",
        "mini golf", "go kart track", "trampoline park", "laser tag", "axe throwing",
        "comedy club", "banquet hall", "event venue", "pottery studio", "dance studio"],
    "Professional Services": [
        "law firm", "accounting firm", "marketing agency", "advertising agency",
        "digital marketing agency", "branding agency", "architecture firm",
        "insurance agency", "financial advisor", "consulting firm", "engineering firm",
        "staffing agency", "tax preparation service", "bookkeeping service",
        "public relations agency", "title company", "real estate brokerage"],
    "Home & Commercial Services": [
        "HVAC contractor", "plumbing company", "landscaping company", "electrician",
        "roofing contractor", "painting contractor", "pest control company",
        "garage door company", "fence company", "concrete contractor",
        "commercial cleaning service", "tree service", "appliance repair", "handyman service"],
    "Healthcare & Wellness": [
        "dental clinic", "chiropractor", "physical therapy clinic", "med spa",
        "family medicine clinic", "optometrist", "dermatology clinic", "pediatric clinic",
        "orthodontist", "veterinary clinic", "massage therapy clinic", "acupuncture clinic",
        "audiology clinic", "podiatry clinic"],
    "Retail & Hospitality": [
        "restaurant", "boutique", "craft brewery", "auto repair shop", "coffee shop",
        "bakery", "florist", "jewelry store", "furniture store", "wine bar",
        "catering company", "bike shop", "pet store", "hardware store"],
    "Manufacturing & Logistics": [
        "machine shop", "metal fabrication shop", "freight company", "manufacturer",
        "food producer", "plastics manufacturer", "packaging company", "tool and die shop",
        "welding shop", "cabinet maker", "sign company", "printing company",
        "trucking company", "distribution company"],
}

_TWIN_CITIES = [
    "Minneapolis", "St. Paul", "Bloomington", "Edina", "Plymouth", "Eden Prairie",
    "Maple Grove", "Minnetonka", "Eagan", "Burnsville", "Woodbury", "Maplewood",
    "Roseville", "St. Louis Park", "Brooklyn Park", "Coon Rapids", "Apple Valley",
    "Lakeville", "Shakopee", "Blaine"]
_GREATER_MN = [
    "Duluth", "Rochester", "St. Cloud", "Mankato", "Moorhead", "Brainerd", "Winona",
    "Bemidji", "Hibbing", "Faribault", "Owatonna", "Willmar", "Alexandria", "Marshall",
    "Austin", "Albert Lea", "Fergus Falls", "Hutchinson"]

# Varied phrasings so the same term+city pair doesn't always produce an identical query.
_QUERY_TEMPLATES = [
    "{term} {city} MN",
    "{term} in {city} Minnesota",
    "best {term} {city} MN",
    "{term} near {city} MN",
    "local {term} in {city} Minnesota",
    "{term} company {city} MN",
    "family owned {term} {city} MN",
    "small {term} {city} Minnesota",
]

# Reverse lookup: term -> industry label, for tagging discovered leads.
_TERM_INDUSTRY = {}
for _ind, _terms in _INDUSTRY_QUERY_TERMS.items():
    for _t in _terms:
        _TERM_INDUSTRY[_t] = _ind

_MAX_QUERIES = 6


def _next_rotation():
    """Persisted rotating offset so consecutive runs explore a different slice."""
    try:
        conn = _get_conn()
        row = conn.execute("SELECT value FROM meta WHERE key='discover_rot'").fetchone()
        rot = int(row[0]) if row and str(row[0]).lstrip('-').isdigit() else 0
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('discover_rot', ?)", (str(rot + 5),))
        conn.commit()
        conn.close()
        return rot
    except Exception:
        return random.randint(0, 9999)


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
    out.extend(_TWIN_CITIES[len(_GREATER_MN):])
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
    # "All industries" — full cross-industry pool for maximum breadth/variety
    spread = []
    for terms in _INDUSTRY_QUERY_TERMS.values():
        spread.extend(terms)
    return spread, ""


def _build_query_plan(industry, region, keywords, refinements):
    """Produce up to _MAX_QUERIES varied {q, term, city, industry} search tasks.

    Each run shuffles the term/city/template pools and applies a persisted rotating
    offset, so the same filters yield different concrete searches on every run.
    """
    terms, _ = _terms_for_industry(industry, refinements, keywords)
    cities = _cities_for_region(region, refinements)
    kw = keywords.strip() if keywords else ""
    kw_suffix = f" {kw}" if (kw and not any(kw.lower() in t.lower() for t in terms)) else ""

    rot = _next_rotation()
    rng = random.Random()  # unseeded -> fresh variety each run
    terms = terms[:]; cities = cities[:]; templates = _QUERY_TEMPLATES[:]
    rng.shuffle(terms); rng.shuffle(cities); rng.shuffle(templates)

    plan, used = [], set()
    i = 0
    attempts = 0
    while len(plan) < _MAX_QUERIES and attempts < _MAX_QUERIES * 8:
        attempts += 1
        term = terms[(rot + i) % len(terms)]
        city = cities[(rot + i) % len(cities)]
        tmpl = templates[i % len(templates)]
        i += 1
        q = (tmpl.format(term=term, city=city) + kw_suffix).strip()
        if q in used:
            continue
        used.add(q)
        plan.append({
            "term": term,
            "city": city,
            "industry": _TERM_INDUSTRY.get(term, refinements.get("industry") or industry or ""),
            "q": q,
        })
    return plan


def _plan_from_queries(queries, industry, region, refinements):
    """Build a discovery plan from externally supplied (e.g. LLM-generated) queries."""
    cities = _cities_for_region(region, refinements)
    plan = []
    for i, raw in enumerate(queries):
        qs = (str(raw) or "").strip()
        if not qs:
            continue
        # Best-effort city guess from the query text, else rotate through region cities.
        city = ""
        for c in (_TWIN_CITIES + _GREATER_MN):
            if c.lower() in qs.lower():
                city = c
                break
        if not city and cities:
            city = cities[i % len(cities)]
        plan.append({"q": qs, "term": qs, "city": city, "industry": industry or ""})
        if len(plan) >= _MAX_QUERIES:
            break
    return plan


async def _run_search_plan(plan, want_per_q):
    """Search-only pass (no verify/screenshot). Returns candidates grouped per query."""
    from playwright.async_api import async_playwright
    out = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        try:
            for start in range(0, len(plan), 2):
                batch = plan[start:start + 2]
                results = await asyncio.gather(*[
                    _multi_source_search(browser, t["q"], t["term"], t["city"], want=want_per_q)
                    for t in batch
                ], return_exceptions=True)
                for t, r in zip(batch, results):
                    if isinstance(r, Exception):
                        out.append({"q": t["q"], "industry": t["industry"], "city": t["city"],
                                    "sources": {}, "diags": [], "candidates": []})
                        continue
                    found, counts, diags = r
                    seen, cands = set(), []
                    for biz in found:
                        d = _extract_domain(biz.get("website", ""))
                        if not d or d in seen or _is_aggregator(d) or _is_national_brand(d):
                            continue
                        if not _looks_like_real_domain(d):
                            continue
                        seen.add(d)
                        cands.append({
                            "name": biz.get("name", "") or d, "website": d,
                            "city": biz.get("city") or t["city"],
                            "industry": t["industry"], "source": biz.get("source", ""),
                        })
                    out.append({"q": t["q"], "industry": t["industry"], "city": t["city"],
                                "sources": counts, "diags": diags, "candidates": cands})
        finally:
            await browser.close()
    return out


# ─── Assisted harvest (human-in-the-loop) ──────────────────────────────────
# A bookmarklet running in the user's real, logged-in Chrome scrapes business
# domains off a Google Search / Google Maps results page and POSTs them here.
# The demo polls /harvest/<token> and audits those sites with the backend robot.
# This sidesteps the datacenter-IP bot blocks that kill the server-side scrapers.
_HARVEST = {}              # token -> {"ts": float, "items": {domain: {...}}}
_HARVEST_LOCK = threading.Lock()
_HARVEST_TTL = 6 * 3600    # forget a token's buffer after 6h of inactivity
_HARVEST_MAX = 400         # cap businesses kept per token


def _harvest_prune(now):
    for t in [t for t, v in _HARVEST.items() if now - v.get("ts", 0) > _HARVEST_TTL]:
        _HARVEST.pop(t, None)


def _clean_harvest_token(tok):
    return re.sub(r'[^a-zA-Z0-9_-]', '', str(tok or ''))[:64]


@clientfinder_bp.route("/ingest", methods=["POST", "OPTIONS"])
def harvest_ingest():
    """Receive scraped domains from the in-browser bookmarklet. Cross-origin and
    credential-free; filters aggregators / national brands / junk before storing."""
    if request.method == "OPTIONS":
        resp = Response("", status=204)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    # navigator.sendBeacon posts text/plain, so parse the raw body as JSON.
    raw = request.get_data(as_text=True) or ""
    try:
        data = json.loads(raw) if raw.strip() else (request.get_json(silent=True) or {})
    except Exception:
        data = request.get_json(silent=True) or {}

    token = _clean_harvest_token(data.get("token"))
    source = (data.get("source") or "")[:40]
    query = (data.get("query") or "")[:160]
    items = data.get("items") or []
    added = total = 0
    if token and isinstance(items, list):
        now = time.time()
        with _HARVEST_LOCK:
            _harvest_prune(now)
            bucket = _HARVEST.setdefault(token, {"ts": now, "items": {}})
            bucket["ts"] = now
            for it in items[:200]:
                it = it or {}
                dom = _extract_domain(it.get("website") or it.get("domain") or "")
                if not dom or _is_aggregator(dom) or _is_national_brand(dom) or not _looks_like_real_domain(dom):
                    continue
                if dom in bucket["items"] or len(bucket["items"]) >= _HARVEST_MAX:
                    continue
                bucket["items"][dom] = {
                    "name": _clean_name(it.get("name") or "") or dom,
                    "website": dom,
                    "source": source or "harvest",
                    "query": query,
                }
                added += 1
            total = len(bucket["items"])
    resp = jsonify({"ok": True, "added": added, "total": total})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@clientfinder_bp.route("/harvest/<token>", methods=["GET"])
def harvest_list(token):
    token = _clean_harvest_token(token)
    with _HARVEST_LOCK:
        bucket = _HARVEST.get(token) or {"items": {}}
        businesses = list(bucket["items"].values())
    return jsonify({"ok": True, "total": len(businesses), "businesses": businesses})


@clientfinder_bp.route("/harvest/<token>/clear", methods=["POST"])
def harvest_clear(token):
    token = _clean_harvest_token(token)
    with _HARVEST_LOCK:
        _HARVEST.pop(token, None)
    return jsonify({"ok": True})


@clientfinder_bp.route("/plan", methods=["POST"])
def query_plan():
    """Return a clean, deterministic query plan (no scraping). Assisted mode turns
    each query into one-click Google / Google Maps search links."""
    data = request.get_json(silent=True) or {}
    plan = _build_query_plan(
        data.get("industry", ""), data.get("region", "Both"),
        data.get("keywords", ""), data.get("refinements", {}) or {})
    return jsonify({"ok": True, "queries": plan})


@clientfinder_bp.route("/search", methods=["POST"])
def search_only():
    """Run search sources only (no screenshot/verify) and return candidates grouped
    by query. Used by the reflection loop: search -> AI reflects -> improved query."""
    data = request.get_json(silent=True) or {}
    industry    = data.get("industry", "")
    region      = data.get("region", "Both")
    keywords    = data.get("keywords", "")
    refinements = data.get("refinements", {}) or {}
    want_per_q  = int(data.get("per_query", 10))

    ext_queries = data.get("queries")
    if isinstance(ext_queries, list) and any((str(x) or "").strip() for x in ext_queries):
        plan = _plan_from_queries(ext_queries, industry, region, refinements)
    else:
        plan = _build_query_plan(industry, region, keywords, refinements)

    try:
        from playwright.async_api import async_playwright  # noqa: F401 (availability check)
    except ImportError:
        return jsonify({"ok": False, "error": "Playwright not available", "by_query": []}), 502
    try:
        by_query = asyncio.run(_run_search_plan(plan, want_per_q))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200], "by_query": []}), 500
    return jsonify({"ok": True, "by_query": by_query, "queries": [t["q"] for t in plan]})


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

    ext_queries = data.get("queries")
    if isinstance(ext_queries, list) and any((str(x) or "").strip() for x in ext_queries):
        plan = _plan_from_queries(ext_queries, industry, region, refinements)
    else:
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
            pool_cap = overall_cap * 2   # gather extra so verification can drop dead ones
            launch_opts = dict(headless=True, args=_LAUNCH_ARGS)
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_opts)
                try:
                    # ── Phase A: search queries in small concurrent batches ────
                    for start in range(0, len(plan), 2):
                        batch = plan[start:start + 2]
                        for j, task in enumerate(batch):
                            q.put({"event": "query", "data": {
                                "idx": start + j, "total": len(plan),
                                "q": task["q"], "industry": task["industry"], "city": task["city"],
                            }})
                        batch_results = await asyncio.gather(*[
                            _multi_source_search(browser, t["q"], t["term"], t["city"], want=want_per_q)
                            for t in batch
                        ], return_exceptions=True)
                        for j, (task, result) in enumerate(zip(batch, batch_results)):
                            if isinstance(result, Exception):
                                q.put({"event": "query_error", "data": {
                                    "idx": start + j, "error": str(result)[:120]}})
                                found, counts, diags = [], {}, []
                            else:
                                found, counts, diags = result
                            new_count = 0
                            for biz in found:
                                domain = _extract_domain(biz.get("website", ""))
                                if not domain or domain in seen or _is_aggregator(domain):
                                    continue
                                if _is_national_brand(domain):
                                    continue
                                if not _looks_like_real_domain(domain):
                                    continue
                                seen.add(domain)
                                biz["industry"] = task["industry"]
                                candidates.append(biz)
                                new_count += 1
                            q.put({"event": "query_done", "data": {
                                "idx": start + j, "q": task["q"], "found": new_count,
                                "raw": sum(counts.values()) if counts else 0,
                                "sources": counts, "diags": diags,
                                "running_total": len(candidates)}})
                        if len(candidates) >= pool_cap:
                            break

                    # ── Phase B: verify reachability + capture screenshot ──────
                    q.put({"event": "verifying", "data": {"candidates": len(candidates)}})
                    ctx = await browser.new_context(**_BROWSER_CTX_OPTS)
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



async def _scrape_site(ctx, biz, extra_views=False, nav_keywords=None):
    """Screenshot + tech/contact extraction for one site. Returns a data dict.

    When extra_views=True, also scrolls a full-page capture and visits up to two
    key internal pages (about/services/contact, plus any AI-suggested nav_keywords),
    so the details view has several screenshots and quality notes of the site.
    """
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
    seen_v = set()
    variants = [v for v in variants if not (v in seen_v or seen_v.add(v))]

    page = await ctx.new_page()
    try:
        resp = None
        for url in variants:
            try:
                r = await page.goto(url, wait_until="domcontentloaded", timeout=12000)
            except Exception:
                continue
            if r is None:
                continue
            resp = r
            if r.status < 400:
                break  # good response — stop trying variants
            # else keep as fallback but try the next variant (www/http)
        if resp is None:
            raise RuntimeError(f"unreachable: {domain}")

        status_code = resp.status
        await page.wait_for_timeout(700)

        # Title / body text (best-effort) — used only to reject parked/empty shells.
        try:
            title = (await page.title() or "").strip()
        except Exception:
            title = ""
        try:
            body_text = await page.evaluate(
                "() => document.body ? (document.body.innerText || '').trim() : ''")
        except Exception:
            body_text = ""
        low = body_text.lower()
        parked_markers = ("domain is for sale", "buy this domain", "parked free",
                          "godaddy.com/domainsearch", "is parked", "domain for sale")
        if (len(body_text) < 25 and len(title) < 3) or any(m in low for m in parked_markers):
            raise RuntimeError(f"empty/parked: {domain}")

        # Everything below is best-effort: a reachable, real page is kept even if
        # an individual capture/extraction step fails on this environment.
        screenshots = []
        screenshot_url = ""

        async def _shot(full_page=False):
            fname = f"{uuid.uuid4().hex[:12]}.png"
            fpath = _SCREENSHOTS_DIR / fname
            if full_page:
                await page.screenshot(path=str(fpath), full_page=True)
            else:
                await page.screenshot(path=str(fpath), full_page=False,
                                      clip={"x": 0, "y": 0, "width": 1280, "height": 800})
            return f"/clientfinder/screenshot/{fname}"

        try:
            screenshot_url = await _shot(full_page=False)
            screenshots.append(screenshot_url)
        except Exception:
            screenshot_url = ""

        emails, phones, stack_flags = [], [], []
        try:
            html = await page.content()
            emails = list(set(re.findall(r'[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}', html)))[:3]
            phones = list(set(re.findall(r'\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}', html)))[:2]
            html_lower = html.lower()
            if 'wp-content' in html_lower or 'wordpress' in html_lower:
                stack_flags.append("Legacy WordPress")
            if 'jquery' in html_lower and ('jquery/1.' in html_lower or 'jquery/2.' in html_lower):
                stack_flags.append("Outdated jQuery")
        except Exception:
            pass

        final_url = page.url
        if status_code and final_url.startswith("http://"):
            stack_flags.append("Missing HTTPS")
        try:
            viewport_meta = await page.evaluate(
                "() => !!document.querySelector('meta[name=viewport]')")
            if not viewport_meta:
                stack_flags.append("No meta viewport")
        except Exception:
            pass
        try:
            is_responsive = await page.evaluate("""() => {
                const w = window.innerWidth;
                window.resizeTo(375,812);
                const changed = document.body.scrollWidth <= 450;
                window.resizeTo(w, 800);
                return changed;
            }""")
            if not is_responsive:
                stack_flags.append("Non-responsive layout")
        except Exception:
            pass

        # Deep capture: a scrolled full-page shot + up to two key internal pages,
        # giving the AI audit several views of the site instead of just the hero.
        if extra_views:
            try:
                screenshots.append(await _shot(full_page=True))
            except Exception:
                pass
            try:
                host = _extract_domain(final_url) or domain
                want_kw = ['about','service','contact','product','gallery','portfolio','menu','pricing']
                for kw in (nav_keywords or []):
                    kw = (str(kw) or '').strip().lower()
                    if kw and kw not in want_kw:
                        want_kw.insert(0, kw)
                links = await page.evaluate("""(args) => {
                    const [host, want] = args;
                    const out = [];
                    for (const a of document.querySelectorAll('a[href]')) {
                        let href = a.href || '';
                        try {
                            const u = new URL(href, location.href);
                            if (u.hostname.replace(/^www\\./,'') !== host.replace(/^www\\./,'')) continue;
                            const path = (u.pathname || '').toLowerCase();
                            if (path === '/' || path === '') continue;
                            if (want.some(w => path.includes(w)) && !out.includes(u.href)) out.push(u.href);
                        } catch (e) {}
                        if (out.length >= 4) break;
                    }
                    return out;
                }""", [host, want_kw])
            except Exception:
                links = []
            for link in (links or [])[:2]:
                try:
                    sub = await ctx.new_page()
                    try:
                        r2 = await sub.goto(link, wait_until="domcontentloaded", timeout=10000)
                        if r2 and r2.status < 400:
                            await sub.wait_for_timeout(500)
                            fname = f"{uuid.uuid4().hex[:12]}.png"
                            await sub.screenshot(path=str(_SCREENSHOTS_DIR / fname), full_page=False,
                                                 clip={"x": 0, "y": 0, "width": 1280, "height": 800})
                            screenshots.append(f"/clientfinder/screenshot/{fname}")
                    finally:
                        await sub.close()
                except Exception:
                    continue

        # Deterministic "website quality" notes from the Playwright test (no AI).
        quality_notes = []
        quality_notes.append(
            "Serves over HTTPS" if final_url.startswith("https://") else "No HTTPS (insecure http://)")
        quality_notes.append(
            "Has mobile viewport meta tag" if "No meta viewport" not in stack_flags
            else "Missing mobile viewport meta tag")
        quality_notes.append(
            "Layout adapts to mobile width" if "Non-responsive layout" not in stack_flags
            else "Layout does not adapt to mobile (likely not responsive)")
        if "Legacy WordPress" in stack_flags:
            quality_notes.append("Built on legacy WordPress")
        if "Outdated jQuery" in stack_flags:
            quality_notes.append("Loads an outdated jQuery version")
        quality_notes.append(
            f"Contact email present on site ({emails[0]})" if emails else "No contact email found on page")
        quality_notes.append(f"Phone number {'found' if phones else 'not found'} on page")
        quality_notes.append(f"{len(screenshots)} page view(s) captured")
        if title:
            quality_notes.append(f'Page title: "{title[:90]}"')
        quality_notes.append(f"Homepage returned HTTP {status_code}")

        return {
            "name": name, "website": _extract_domain(final_url) or domain, "city": city,
            "title": title[:120],
            "screenshot_url": screenshot_url, "screenshots": screenshots,
            "quality_notes": quality_notes,
            "emails": emails, "phones": phones,
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
    extra_views = bool(data.get("extra_views"))
    nav_keywords = data.get("nav_keywords") or []
    if not isinstance(nav_keywords, list):
        nav_keywords = []

    result_q = queue.Queue()

    def run_playwright():
        async def _scrape():
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                result_q.put({"event": "error", "data": {"msg": "Playwright not installed on the server"}})
                result_q.put(None)
                return
            try:
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
                    ctx = await browser.new_context(**_BROWSER_CTX_OPTS)
                    try:
                        for idx, biz in enumerate(businesses):
                            # Emit progress BEFORE the (potentially slow) per-site scrape so
                            # the stream stays fed and the client shows live activity.
                            result_q.put({"event": "progress", "data": {
                                "idx": idx, "total": len(businesses),
                                "name": biz.get("name", ""), "website": biz.get("website", "")}})
                            try:
                                site = await _scrape_site(ctx, biz, extra_views=extra_views,
                                                          nav_keywords=nav_keywords)
                                site["idx"] = idx
                                result_q.put({"event": "site", "data": site})
                            except Exception as e:
                                result_q.put({"event": "site_error", "data": {
                                    "idx": idx, "name": biz.get("name", ""),
                                    "website": biz.get("website", ""), "error": str(e)[:120]}})
                    finally:
                        await browser.close()
            except Exception as e:
                # Browser failed to launch (e.g. chromium not installed) — surface it.
                result_q.put({"event": "error", "data": {
                    "msg": f"Playwright failed to start: {str(e)[:160]}"}})
            result_q.put(None)  # sentinel

        try:
            asyncio.run(_scrape())
        except Exception as e:
            result_q.put({"event": "error", "data": {"msg": f"scrape worker crashed: {str(e)[:160]}"}})
            result_q.put(None)

    t = threading.Thread(target=run_playwright, daemon=True)
    t.start()

    def generate():
        yield "data: {\"event\":\"start\",\"total\":" + str(len(businesses)) + "}\n\n"
        idle = 0
        while True:
            try:
                item = result_q.get(timeout=15)
            except queue.Empty:
                idle += 1
                if idle >= 12:  # ~180s of total worker silence -> give up
                    yield "data: {\"event\":\"timeout\"}\n\n"
                    break
                yield ": keep-alive\n\n"  # SSE comment heartbeat keeps the stream warm
                continue
            idle = 0
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
    nav_keywords = data.get("nav_keywords") or []
    if not isinstance(nav_keywords, list):
        nav_keywords = []
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
                    args=_LAUNCH_ARGS
                )
                ctx = await browser.new_context(**_BROWSER_CTX_OPTS)
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
                        }, extra_views=True, nav_keywords=nav_keywords)
                        modernity, mobile, function, composite = _scores_from_flags(site["stack_flags"])
                        shots = site.get("screenshots") or ([site["screenshot_url"]] if site["screenshot_url"] else [])
                        qnotes = site.get("quality_notes") or []
                        patch = {
                            "screenshot_url": site["screenshot_url"],
                            "screenshots": shots,
                            "quality_notes": qnotes,
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
                            params.append(json.dumps(v) if k in ("stack_flags", "screenshots", "quality_notes") else v)
                        sets.append("updated_at=?"); params.append(_now())
                        params.append(lead["id"])
                        c2.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=?", params)
                        c2.commit(); c2.close()

                        result_q.put({"event": "lead", "data": {
                            "idx": idx, "id": lead["id"], "name": lead["company_name"],
                            "screenshot_url": site["screenshot_url"],
                            "screenshots": shots,
                            "quality_notes": qnotes,
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

        try:
            asyncio.run(_run())
        except Exception as e:
            # Browser failed to launch (e.g. chromium not installed) — surface it.
            result_q.put({"event": "error", "data": {
                "msg": f"Playwright failed to start: {str(e)[:160]}"}})
            result_q.put(None)

    t = threading.Thread(target=run_playwright, daemon=True)
    t.start()

    def generate():
        yield f"data: {json.dumps({'event': 'start', 'total': len(targets)})}\n\n"
        idle = 0
        while True:
            try:
                item = result_q.get(timeout=15)
            except queue.Empty:
                idle += 1
                if idle >= 12:  # ~180s of total worker silence -> give up
                    yield "data: {\"event\":\"timeout\"}\n\n"
                    break
                yield ": keep-alive\n\n"
                continue
            idle = 0
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
