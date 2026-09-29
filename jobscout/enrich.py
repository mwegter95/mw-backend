"""Company facts, categorisation and the gem score.

Pipeline steps 1–3 (contract §9.4):
  collect_facts   homepage (+ about page) → title, meta/og, JSON-LD Organization, about/footer text,
                  address hints, logo;
  categorize      keyword classifier → industry, entity_type, local_presence, ownership… so the UI is
                  useful even while the AI worker is offline (enrich_source 'heuristic');
  AI enrichment   enqueue enrich_company; apply_enrichment() validates the result, recomputes the gem
                  score and applies the auto-ignore rules.
"""
import logging
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from . import ats, browser, config, db, geo, taxonomy
from .http import FetchError, get_fetcher
from .normalize import html_to_text, parse_location

log = logging.getLogger("jobscout")

ABOUT_TEXT = re.compile(r"\b(about( us)?|our (company|story|history)|who we are|company)\b", re.I)
ABOUT_HREF = re.compile(r"/(about|company|who-we-are|our-story|history)(/|$|-)", re.I)
_STREET = (r"\d{1,6}\s+[A-Za-z0-9 .'#-]{2,40}?\b(Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Boulevard|Blvd|Lane|Ln|"
           r"Parkway|Pkwy|Court|Ct|Circle|Cir|Way|Highway|Hwy|Trail|Place|Pl|Terrace)\.?")
_CITY_ST_ZIP = re.compile(r"([A-Z][A-Za-z.' -]{1,30}),\s*(MN|Minnesota|WI|Wisconsin|[A-Z]{2})\.?\s+(\d{5})(?:-\d{4})?")
_ADDRESS = re.compile(_STREET + r"[,\s]+(?:(?:Suite|Ste\.?|Unit|#)\s*\w+[,\s]+)?" + _CITY_ST_ZIP.pattern)

# ── facts ───────────────────────────────────────────────────────────────────


def _meta(soup, **attrs):
    tag = soup.find("meta", attrs=attrs)
    return (tag.get("content") or "").strip() if tag else ""


def _icon(soup, base_url, domain):
    for rel in ("apple-touch-icon", "icon", "shortcut icon"):
        tag = soup.find("link", rel=lambda r: r and rel in (" ".join(r) if isinstance(r, list) else r).lower())
        if tag and tag.get("href"):
            return urljoin(base_url, tag["href"])
    return f"https://{domain}/favicon.ico"


def _org_summary(orgs):
    if not orgs:
        return None
    org = orgs[0]
    addr = org.get("address") or {}
    addr = addr[0] if isinstance(addr, list) and addr else addr
    employees = org.get("numberOfEmployees")
    if isinstance(employees, dict):
        employees = employees.get("value") or employees.get("maxValue") or employees.get("minValue")
    logo = org.get("logo")
    return {
        "name": org.get("name"), "legalName": org.get("legalName"), "foundingDate": org.get("foundingDate"),
        "numberOfEmployees": employees, "description": (org.get("description") or "")[:600] or None,
        "logo": logo.get("url") if isinstance(logo, dict) else logo,
        "parentOrganization": (org.get("parentOrganization") or {}).get("name")
        if isinstance(org.get("parentOrganization"), dict) else org.get("parentOrganization"),
        "address": {k: addr.get(k) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode")}
        if isinstance(addr, dict) else addr,
        "types": org.get("@type"),
    }


def address_hints(text):
    """Street addresses and "City, ST 55xxx" strings found in page text (first ones first)."""
    hints = [re.sub(r"\s+", " ", m.group(0)).strip() for m in _ADDRESS.finditer(text or "")]
    hints += [re.sub(r"\s+", " ", m.group(0)).strip() for m in _CITY_ST_ZIP.finditer(text or "")]
    return list(dict.fromkeys(hints))[:6]


def page_facts(html, url, domain):
    """Facts from one fetched homepage (pure — used by collect_facts and tests)."""
    soup = BeautifulSoup(html or "", "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    footer = soup.find("footer")
    footer_text = html_to_text(str(footer))[:1500] if footer else ""
    body_text = html_to_text(html)
    links = [urljoin(url, a["href"]) for a in soup.find_all("a", href=True)]
    own = {domain, (urlsplit(url).hostname or "").removeprefix("www.")}  # the domain may redirect elsewhere
    ext_hosts = [(urlsplit(l).hostname or "").removeprefix("www.") for l in links if l.startswith("http")]
    ext_hosts = [h for h in ext_hosts if h and h not in own]
    jsonld_types = sorted({str(t) for n in ats._jsonld_objects(html)
                           for t in (n.get("@type") if isinstance(n.get("@type"), list) else [n.get("@type")]) if t})
    return {
        "homepage_url": url,
        "homepage_title": title[:200],
        "meta_description": _meta(soup, name="description")[:500],
        "og_description": _meta(soup, property="og:description")[:500],
        "og_site_name": _meta(soup, property="og:site_name")[:120],
        "og_type": _meta(soup, property="og:type")[:40],
        "jsonld_org": _org_summary(ats.extract_jsonld_orgs(html)),
        "jsonld_types": jsonld_types[:20],
        "footer_text": footer_text,
        "home_text": body_text[:4000],
        "address_hints": address_hints(footer_text + "\n" + body_text),
        "logo_url": _icon(soup, url, domain),
        "external_link_ratio": round(len(ext_hosts) / len(links), 2) if links else 0.0,
        "external_domains": len(set(ext_hosts)),
        "_about_link": _about_link(soup, url, domain),
    }


def _about_link(soup, base_url, domain):
    """First same-site link that looks like an About/Company page."""
    for a in soup.find_all("a", href=True):
        link = urljoin(base_url, a["href"]).split("#")[0]
        if (urlsplit(link).hostname or "").removeprefix("www.") != domain:
            continue
        if ABOUT_HREF.search(urlsplit(link).path) or ABOUT_TEXT.fullmatch(a.get_text(" ", strip=True)):
            return link
    return None


def collect_facts(company, fetcher=None):
    """Fetch homepage (+ about page) → (facts, homepage (html, final_url) | None)."""
    fetcher = fetcher or get_fetcher()
    domain = company["domain"]
    html = final = None
    errors = []
    # Bare https:// domains often 404 or have broken TLS; try the www host and plain http too.
    for url in dict.fromkeys([company.get("homepage_url") or f"https://{domain}", f"https://www.{domain}",
                              f"http://{domain}"]):
        try:
            html, final, _ = browser.page_html(url, fetcher)
            break
        except FetchError as exc:
            errors.append(f"{url}: {str(exc)[:120]}")
    if html is None:
        # Big corporate sites (Akamai and friends) often stall plain HTTP clients until they time out
        # rather than answering 403; a real browser gets the page.
        rendered = browser.fetch_rendered(f"https://www.{domain}")
        if rendered and rendered.get("html"):
            html, final = rendered["html"], rendered["final_url"]
    if html is None:
        return {"fetch_error": " | ".join(errors)[:500], "fetched_at": db.now_iso()}, None
    facts = page_facts(html, final, domain)
    about_url = facts.pop("_about_link")
    if about_url and about_url.rstrip("/") != final.rstrip("/"):
        try:
            about_html, about_final = fetcher.get_page(about_url)
            facts["about_url"] = about_final
            facts["about_text"] = html_to_text(about_html)[:6000]
            facts["address_hints"] = list(dict.fromkeys(facts["address_hints"] + address_hints(facts["about_text"])))[:6]
        except FetchError as exc:
            log.debug("enrich: about page failed: %s", exc)
    facts["hq_hint"] = hq_hint(facts)
    facts["fetched_at"] = db.now_iso()
    return facts, (html, final)


def hq_hint(facts):
    """"City, ST" from JSON-LD address or the first address hint."""
    addr = (facts.get("jsonld_org") or {}).get("address")
    if isinstance(addr, dict) and addr.get("addressLocality"):
        st = parse_location(f"x, {addr.get('addressRegion') or ''}").state or addr.get("addressRegion")
        return f"{addr['addressLocality']}, {st}" if st else addr["addressLocality"]
    for hint in facts.get("address_hints") or []:
        m = _CITY_ST_ZIP.search(hint)
        if m:
            st = parse_location(f"x, {m.group(2)}").state or m.group(2)
            return f"{m.group(1).strip()}, {st}"
    return None


# ── heuristic categorisation ─────────────────────────────────────────────────

# slug → [(regex, weight)]; phrases matched case-insensitively on word boundaries.
_INDUSTRY_KEYWORDS = {
    "mfg_plastics_packaging": [("injection mold(ing|ed|er)?s?", 4), ("plastics?", 2), ("thermoform(ing|ed)?", 4),
                               ("extru(sion|ded|der)", 3), ("blow mold(ing)?", 4), ("rotational mold(ing)?", 4),
                               ("polymers?", 1), ("resins?", 1), ("packaging", 3), ("corrugated", 3),
                               ("flexible packaging", 4), ("labels", 1), ("containers", 1), ("molders?", 3),
                               ("polyethylene|polypropylene|pvc", 2), ("foam fabricat\\w+", 3)],
    "mfg_building_materials": [("building (materials|products)", 4), ("windows?( and|&) doors?", 4), ("siding", 2),
                               ("roofing", 2), ("insulation", 2), ("lumber", 2), ("millwork", 3), ("cabinet(s|ry)", 2),
                               ("countertops?", 2), ("precast", 4), ("concrete", 2), ("masonry", 2), ("trusses", 3),
                               ("drywall", 2), ("flooring", 2), ("decking", 2), ("glazing", 2), ("aggregates?", 2),
                               ("fenestration", 3), ("architectural (glass|products|metals)", 3)],
    "mfg_chemicals_coatings": [("chemicals?", 2), ("adhesives?", 4), ("coatings?", 3), ("paints?", 2),
                               ("sealants?", 3), ("lubricants?", 3), ("specialty chemical", 4), ("formulat\\w+", 1),
                               ("inks?", 1), ("cleaning (and|&) sanitation", 3)],
    "mfg_industrial_equipment": [("industrial equipment", 4), ("machinery", 3), ("automation", 2), ("conveyors?", 3),
                                 ("pumps?", 2), ("valves?", 2), ("compressors?", 2), ("hydraulics?", 2),
                                 ("material handling", 3), ("filtration", 2), ("robotics?", 2), ("machine build\\w+", 3),
                                 ("oem", 1), ("engines?", 1)],
    "mfg_electrical_electronics": [("electronics?", 3), ("circuit boards?|pcbs?", 4), ("sensors?", 2),
                                   ("electrical (products|components|equipment)", 3), ("semiconductors?", 3),
                                   ("connectors?", 2), ("wire harness(es)?", 4), ("power supplies", 3),
                                   ("transformers?", 2), ("electronic manufacturing services|ems provider", 4),
                                   ("controls", 1), ("lighting", 1)],
    "mfg_medical_devices": [("medical devices?", 5), ("iso 13485", 4), ("fda", 2), ("catheters?", 3), ("implants?", 3),
                            ("orthopedic", 2), ("surgical", 2), ("diagnostics?", 2), ("cardiovascular", 2),
                            ("neuromodulation", 3), ("510\\(k\\)", 3)],
    "mfg_food_beverage": [("food (products|manufactur\\w+|processing|company)", 4), ("beverages?", 3),
                          ("brew(ery|ing)", 3), ("dairy", 3), ("bakery|baked goods", 3), ("snacks?", 2), ("meats?", 1),
                          ("poultry", 2), ("ingredients", 2), ("co-?pack(er|ing)", 3), ("confection\\w*|candy", 3),
                          ("coffee roast\\w+", 3), ("frozen foods?", 3), ("foods", 1),
                          ("winer(y|ies)|vineyards?|distiller(y|ies)|cider(y|ies)", 3)],
    "mfg_consumer_products": [("consumer products?", 4), ("outdoor (gear|products|recreation)", 3), ("sporting goods", 4),
                              ("apparel", 2), ("footwear", 3), ("toys?", 2), ("furniture", 2), ("housewares", 3),
                              ("pet products", 3), ("boats?", 2), ("snowmobiles?|atvs?", 3), ("fishing", 1),
                              ("hunting", 1), ("camping", 1), ("bikes?|bicycles?", 2)],
    "mfg_metals_fabrication": [("metal fabricat\\w+", 5), ("fabrication", 2), ("machining", 3), ("cnc", 3),
                               ("sheet metal", 4), ("welding", 2), ("metal stamping|stampings?", 3), ("castings?", 3),
                               ("foundry", 4), ("forgings?", 3), ("steel", 1), ("aluminum", 1), ("powder coating", 2),
                               ("precision machined", 4), ("tool (and|&) die", 4)],
    "mfg_other": [("manufactur(er|ers|ing)", 1), ("contract manufactur\\w+", 2), ("factory|plant", 1)],
    "construction_real_estate": [("construction", 2), ("general contractors?", 4), ("contractors?", 1),
                                 ("home ?builders?", 3), ("real estate", 3), ("property management", 3),
                                 ("commercial real estate", 4), ("excavation", 3), ("remodel(ing)?", 2),
                                 ("design-build", 3), ("mechanical contractor", 3),
                                 ("paving|asphalt|sealcoat\\w*", 3), ("home services", 3), ("hvac", 3),
                                 ("plumb(ing|ers?)", 3), ("heating (and|&) (air|cooling)", 3), ("exteriors", 2),
                                 ("(roof|roofing|window|door|siding) (repair|replacement|installation|contractors?)", 3),
                                 ("free (estimates?|quotes?)", 2), ("licensed (and|&) insured", 2)],
    "distribution_logistics": [("distributors?", 3), ("distribution", 2), ("wholesale", 3), ("logistics", 3),
                               ("freight", 3), ("trucking", 3), ("warehous(e|ing)", 2), ("supply chain", 2),
                               ("3pl|third-party logistics", 4), ("fulfillment", 2)],
    "retail_ecommerce": [("retail(er)?", 2), ("online store", 3), ("shop now", 2), ("e-?commerce", 3), ("add to cart", 3),
                         ("boutique", 2), ("free shipping", 2)],
    "agriculture": [("agricultur(e|al)", 3), ("farms?|farming", 2), ("agribusiness", 4), ("crops?", 2), ("seeds?", 1),
                    ("grain", 2), ("livestock", 3), ("agronom\\w+", 3), ("animal feed|feed mill", 3)],
    "tech_software": [("software", 3), ("saas", 4), ("platform", 1), ("cloud", 1), ("apis?", 1), ("analytics", 1),
                      ("artificial intelligence|machine learning", 2), ("cybersecurity", 2), ("fintech", 3)],
    "it_services": [("it services", 4), ("managed services", 3), ("msp", 3), ("it consulting", 4), ("help desk", 2),
                    ("network(ing)? solutions", 2), ("systems integrat\\w+", 3), ("technology consulting", 3)],
    "professional_services": [("consulting", 2), ("engineering firm|engineering services", 3), ("law firm", 4),
                              ("attorneys?", 3), ("accounting|cpas?", 3), ("architects?|architecture", 3),
                              ("staffing", 3), ("recruiting", 2), ("advisory", 2)],
    "agency_marketing": [("marketing agency", 5), ("advertising( agency)?", 3), ("creative agency", 4), ("branding", 2),
                         ("public relations", 3), ("digital marketing", 3), ("seo", 1), ("media buying", 3),
                         ("design agency", 3)],
    "financial_insurance": [("bank(ing)?", 3), ("credit union", 4), ("insurance", 3), ("financial services", 3),
                            ("wealth management", 3), ("investments?", 2), ("mortgages?", 2), ("lending", 2),
                            ("payments", 1)],
    "healthcare": [("health ?care", 3), ("hospitals?", 3), ("clinics?", 2), ("medical center", 3), ("patients?", 2),
                   ("physicians", 2), ("senior living", 3), ("dental", 2), ("pharmacy", 2), ("behavioral health", 3)],
    "education": [("school district", 4), ("universit(y|ies)", 3), ("college", 2), ("k-12", 3),
                  ("admissions", 2), ("tuition", 2), ("curriculum", 1), ("students", 1)],
    "nonprofit": [("non-?profit", 4), ("501\\(c\\)\\(3\\)", 4), ("foundation", 1), ("donate", 2), ("charity", 3),
                  ("association", 1), ("volunteers?", 1)],
    "government": [("municipal(ity)?", 3), ("public works", 3), ("city (council|hall|government)", 3),
                   ("county (board|commissioners?|government|sheriff)", 3), ("state agency", 3),
                   ("department of (transportation|natural resources|human services|revenue|health)", 3)],
    "energy_utilities": [("energy", 2), ("utilit(y|ies)", 3), ("electric cooperative", 4), ("solar", 2), ("wind energy", 3),
                         ("power generation", 3), ("natural gas", 3), ("renewable", 2), ("pipelines?", 1)],
    "hospitality_media": [("hotels?", 3), ("restaurants?", 2), ("catering", 2), ("event venue", 3), ("entertainment", 2),
                          ("media company", 3), ("publishing", 2), ("broadcast\\w*", 3), ("magazine", 2),
                          ("golf", 1), ("resort", 2)],
}
_COMPILED_KW = {slug: [(re.compile(rf"\b(?:{p})\b", re.I), w) for p, w in pats]
                for slug, pats in _INDUSTRY_KEYWORDS.items()}
_MANUFACTURING_HINT = re.compile(r"\bmanufactur(er|ers|ing|es)\b", re.I)
_MAKER_HINT = re.compile(r"\b(we (make|manufacture|produce|fabricate|mold|extrude|brew)|our (plant|factory|production)|"
                         r"production facility|made in (the )?(usa|u\.s\.a|america|minnesota|wisconsin)|iso 9001|"
                         r"brewery|winery|distillery)\b", re.I)
_OSM_MAKER_CATEGORIES = {"works", "factory", "manufacturing", "brewery", "winery", "distillery", "sawmill"}

_NEWS_DOMAIN = re.compile(r"(news|times|tribune|journal|gazette|herald|press|daily|post|patch|bizjournals|"
                          r"startribune|twincities|mprnews|kare11|kstp|wcco|fox9)", re.I)
_DIRECTORY = re.compile(r"\b(business directory|directory of|find (a|local)|near me|top \d+|best \d+|listings?|"
                        r"yellow ?pages|chamber of commerce|compare (quotes|companies)|companies in|list of)\b", re.I)
# Explainer/article pages that web search returns for a product word ("Types of injections: uses...").
_ARTICLE_TITLE = re.compile(r"^\s*(types of|how to|what (is|are)|why |when to|\d+ (tips|ways|things|reasons)|"
                            r"(the )?(complete|ultimate|beginner'?s) guide)|: (what you need to know|uses|techniques|"
                            r"tips|explained|a guide)\b", re.I)
# Evidence a site belongs to someone in the Twin Cities / western Wisconsin: a place name, a state
# abbreviation after a comma, or a local area-code phone number.
_LOCAL_TEXT = re.compile(r"\b(minnesota|wisconsin|twin cities|minneapolis|st\.? paul|saint paul)\b|,\s*(mn|wi)\b|"
                         r"\(?\b(651|612|763|952|320|507|218|715)\)?[-. ]\d{3}[-. ]\d{4}", re.I)
_STORE_PAGE = re.compile(r"\b(store hours|get directions|store locator|find a store|visit (our|this) store|"
                         r"weekly ad|shop in[- ]store|pharmacy hours)\b", re.I)
_FRANCHISE = re.compile(r"\b(independently owned and operated|franchise (location|owner)|franchisee|"
                        r"locally owned franchise)\b", re.I)
_OWNERSHIP_RULES = [
    ("family", re.compile(r"\bfamily[- ]owned\b", re.I)),
    ("pe_backed", re.compile(r"\b(portfolio company of|backed by .{0,40}(capital|partners|equity))\b", re.I)),
    ("subsidiary", re.compile(r"\b(a subsidiary of|a division of|wholly[- ]owned subsidiary|part of the .{0,30} family of companies)\b", re.I)),
    ("public", re.compile(r"\b(nyse|nasdaq)\s*:\s*[A-Z]{1,5}\b|\bpublicly traded\b", re.I)),
    ("cooperative", re.compile(r"\b(cooperative|co-op)\b", re.I)),
    ("nonprofit", re.compile(r"\b(non-?profit|501\(c\)\(3\))\b", re.I)),
    ("private", re.compile(r"\b(employee[- ]owned|privately[- ]held|esop)\b", re.I)),
]


def _band(employees):
    try:
        n = int(re.sub(r"[^\d]", "", str(employees)) or 0)
    except ValueError:
        return None
    if n <= 0:
        return None
    return "1-49" if n < 50 else "50-199" if n < 200 else "200-999" if n < 1000 else "1000-4999" if n < 5000 else "5000+"


def _year(value):
    m = re.search(r"\b(1[89]\d\d|20[0-2]\d)\b", str(value or ""))
    return int(m.group(1)) if m else None


def _weighted_text(facts):
    org = facts.get("jsonld_org") or {}
    return [
        (facts.get("homepage_title") or "", 3), (facts.get("og_site_name") or "", 1),
        ((facts.get("meta_description") or "") + " " + (facts.get("og_description") or ""), 2),
        (org.get("description") or "", 2), (facts.get("about_text") or "", 1), (facts.get("home_text") or "", 1),
    ]


def industry_scores(facts, prior=None):
    scores = {}
    for text, weight in _weighted_text(facts):
        if not text:
            continue
        for slug, rules in _COMPILED_KW.items():
            for rx, w in rules:
                hits = len(rx.findall(text))
                if hits:
                    scores[slug] = scores.get(slug, 0) + w * weight * min(hits, 3)
    if prior in scores:
        scores[prior] += 4
    return scores


def makes_things(facts):
    """Evidence the company manufactures something, not just installs or sells it."""
    text = " ".join(t for t, _ in _weighted_text(facts))
    category = str((facts.get("discovery") or {}).get("category") or "").lower()
    return bool(_MANUFACTURING_HINT.search(text) or _MAKER_HINT.search(text) or category in _OSM_MAKER_CATEGORIES)


def heuristic_industry(facts, prior=None):
    """Best taxonomy slug from keyword evidence. Manufacturing categories need manufacturing evidence
    (a roofing installer mentions "roofing" but makes nothing); without it they drop out and the best
    non-manufacturing slug wins. Nothing conclusive → the discovery prior (if still allowed) or "other"."""
    makes = makes_things(facts)
    scores = industry_scores(facts, prior)
    if not makes:
        scores = {k: v for k, v in scores.items() if taxonomy.INDUSTRY_GROUP.get(k) != "Manufacturing"}
    specific = {k: v for k, v in scores.items() if k != "mfg_other"}
    if specific:
        best = max(specific, key=specific.get)
        if specific[best] >= 6:
            return best
    text = " ".join(t for t, _ in _weighted_text(facts))
    if makes:
        return prior if prior and taxonomy.INDUSTRY_GROUP.get(prior) == "Manufacturing" else "mfg_other"
    if prior and taxonomy.INDUSTRY_GROUP.get(prior) != "Manufacturing":
        return prior
    return "other" if text.strip() else None


def guess_entity_type(domain, facts, url=None):
    """company | directory | news | government | school | retail_location | franchise_location | other."""
    domain = (domain or "").lower()
    text = " ".join(t for t, _ in _weighted_text(facts)) + " " + (facts.get("footer_text") or "")
    head = f"{facts.get('homepage_title') or ''} {facts.get('meta_description') or ''}"
    types = {t.lower() for t in facts.get("jsonld_types") or []}
    path = urlsplit(url or facts.get("homepage_url") or "").path.lower()
    if domain.endswith(".gov") or re.search(r"\.(mn|wi|state)\.us$", domain) or re.search(r"^(city|county|state) of\b", head.strip(), re.I):
        return "government"
    if domain.endswith(".edu") or re.search(r"\b(school district|isd \d+|public schools)\b", head, re.I):
        return "school"
    if types & {"newsarticle", "newsmediaorganization", "article"} or facts.get("og_type") == "article" \
            or _NEWS_DOMAIN.search(domain.split(".")[0]) or _ARTICLE_TITLE.search(facts.get("homepage_title") or ""):
        return "news"
    link_farm = (facts.get("external_link_ratio") or 0) > 0.6 and (facts.get("external_domains") or 0) >= 15
    if _DIRECTORY.search(head) or link_farm:
        return "directory"
    if _FRANCHISE.search(text):
        return "franchise_location"
    if (_STORE_PAGE.search(text) and re.search(r"/(stores?|locations?)/", path)) or types & {"store"}:
        return "retail_location"
    return "company"


def guess_local_presence(entity_type, lat, lng, facts):
    """hq when the site's own address is within 60 mi of home; branch for chain-store pages; none when
    the only address found is elsewhere; unknown otherwise."""
    if entity_type in ("retail_location", "franchise_location"):
        return "branch"
    if lat is not None and lng is not None:
        return "hq" if geo.haversine_miles(lat, lng, config.HOME_LAT, config.HOME_LNG) <= 60 else "none"
    text = " ".join(str(facts.get(k) or "") for k in ("homepage_title", "meta_description", "home_text",
                                                      "about_text", "footer_text"))
    text += " " + " ".join(facts.get("address_hints") or [])
    return "unknown" if _LOCAL_TEXT.search(text) else "none"


def guess_ownership(facts):
    text = " ".join(t for t, _ in _weighted_text(facts)) + " " + (facts.get("footer_text") or "")
    for value, rx in _OWNERSHIP_RULES:
        if rx.search(text):
            return value
    return None


def locate_hq(facts, fetcher=None):
    """(hq_address, city, state, lat, lng, precision) from JSON-LD/footer addresses."""
    street = next((h for h in facts.get("address_hints") or [] if re.match(r"\d", h)), None)
    city_state = facts.get("hq_hint")
    if street and _CITY_ST_ZIP.search(street):
        city_state = _CITY_ST_ZIP.search(street).group(0)
    loc = parse_location(city_state) if city_state else parse_location("")
    if street:
        hit = geo.census_geocode(street, fetcher)
        if hit:
            return street, loc.city, loc.state, hit[0], hit[1], "address"
    hit = geo.lookup_city(loc.city, loc.state) if loc.city else None
    if hit:
        return None, loc.city, loc.state, hit[0], hit[1], "city"
    return None, loc.city, loc.state, None, None, None


def categorize(company, facts, fetcher=None):
    """Heuristic field updates for a company (never overrides AI/seed/user values)."""
    prior = facts.get("_query_industry")
    fields = {}
    trusted = company.get("enrich_source") in ("ai", "seed")
    if company.get("lat") is None:
        address, city, state, lat, lng, precision = locate_hq(facts, fetcher)
        fields.update({k: v for k, v in dict(hq_address=address, hq_city=city, hq_state=state, lat=lat, lng=lng,
                                             geo_precision=precision).items() if v is not None})
    lat = fields.get("lat", company.get("lat"))
    lng = fields.get("lng", company.get("lng"))
    entity_type = company.get("entity_type") if trusted and company.get("entity_type") else \
        guess_entity_type(company.get("domain"), facts)
    guesses = {
        "industry": heuristic_industry(facts, prior),
        "entity_type": entity_type,
        "local_presence": guess_local_presence(entity_type, lat, lng, facts),
        "ownership": guess_ownership(facts),
        "summary": (facts.get("meta_description") or facts.get("og_description") or
                    (facts.get("jsonld_org") or {}).get("description") or "")[:300] or None,
        "employee_band": _band((facts.get("jsonld_org") or {}).get("numberOfEmployees")),
        "founded_year": _year((facts.get("jsonld_org") or {}).get("foundingDate")),
        "parent_company": (facts.get("jsonld_org") or {}).get("parentOrganization"),
        "logo_url": facts.get("logo_url"),
    }
    for key, value in guesses.items():
        current = company.get(key)
        empty = current in (None, "", "unknown", "[]")
        if value is not None and (empty or (not trusted and key not in ("logo_url",))):
            fields[key] = value
    if not trusted and fields.get("industry"):
        fields["enrich_source"] = "heuristic"
    return fields


# ── AI enrichment ───────────────────────────────────────────────────────────


def build_payload(company):
    """enrich_company payload (contract §6), built at claim time from stored facts."""
    facts = db.loads(company.get("facts"), {})
    about = "\n\n".join(x for x in (facts.get("about_text"), facts.get("home_text"), facts.get("footer_text")) if x)
    return {
        "name": company["name"], "domain": company["domain"],
        "homepage_title": facts.get("homepage_title"), "meta_description": facts.get("meta_description"),
        "og_description": facts.get("og_description"), "jsonld_org": facts.get("jsonld_org"),
        "about_text": about[:6000],
        "hq_hint": facts.get("hq_hint") or (f"{company['hq_city']}, {company['hq_state']}"
                                            if company.get("hq_city") and company.get("hq_state") else None),
    }


def apply_enrichment(conn, company_id, result):
    """Persist a validated enrich_company result, recompute gem, apply auto-ignore rules."""
    company = db.row_dict(conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone())
    if not company:
        return None
    seed = company.get("enrich_source") == "seed"
    fields = {}
    for key in ("industry", "sub_industry", "summary", "business_model", "ownership", "parent_company",
                "employee_band", "founded_year", "entity_type", "local_presence"):
        value = result.get(key)
        if value is not None and (not seed or company.get(key) in (None, "", "unknown")):
            fields[key] = value
    for key in ("products", "tags"):
        if result.get(key) and (not seed or db.loads(company.get(key), []) == []):
            fields[key] = db.dumps(result[key])
    if result.get("well_known") is not None and (not seed or company.get("well_known") is None):
        fields["well_known"] = 1 if result["well_known"] else 0
    if result.get("hq_city") and (company.get("lat") is None or company.get("geo_precision") != "address"):
        hit = geo.lookup_city(result["hq_city"], result.get("hq_state"))
        fields.update(hq_city=result["hq_city"], hq_state=result.get("hq_state"))
        if hit and company.get("lat") is None:
            fields.update(lat=hit[0], lng=hit[1], geo_precision="city")
    fields.update(enrich_status="done", enriched_at=db.now_iso(), updated_at=db.now_iso())
    if not seed:
        fields["enrich_source"] = "ai"
    company.update(fields)
    fields.update(gem_fields(conn, company))
    fields.update(auto_ignore(company))
    db.update(conn, "companies", "id", company_id, fields)
    return fields


AUTO_IGNORE_REASONS = ("not an employer site", "local branch of a chain", "no local presence found")


def auto_ignore(company):
    """§9.2: directories/news → ignored; chain/franchise branches → ignored — unless the user restored it.
    A company auto-ignored on earlier (heuristic) evidence is un-ignored when a newer verdict disagrees."""
    if db.loads(company.get("facts"), {}).get("_user_restored"):
        return {}
    et, presence = company.get("entity_type"), company.get("local_presence")
    if et in ("directory", "news"):
        verdict = {"status": "ignored", "status_reason": "not an employer site"}
    elif et in ("retail_location", "franchise_location") and presence == "branch":
        verdict = {"status": "ignored", "status_reason": "local branch of a chain"}
    elif presence == "none":
        verdict = {"status": "ignored", "status_reason": "no local presence found"}
    else:
        verdict = {}
    if company.get("status") == "ignored":
        if company.get("status_reason") in AUTO_IGNORE_REASONS and not verdict:
            return {"status": "pending", "status_reason": None}
        return {}
    return verdict


# ── gem score ───────────────────────────────────────────────────────────────


def home_points(conn):
    points = [(config.HOME_LAT, config.HOME_LNG)]
    for r in conn.execute("SELECT home_lat, home_lng FROM profiles WHERE home_lat IS NOT NULL AND home_lng IS NOT NULL"):
        points.append((r["home_lat"], r["home_lng"]))
    return points


# A hidden gem is an established local employer most people haven't heard of — in any industry. Size is
# what makes a company a real employer for someone's next role (a 3-person shop rarely hires a director),
# so the badge needs 50+ people; industry never counts.
GEM_SIZE_POINTS = {"50-199": 25, "200-999": 25, "1000-4999": 12}
GEM_SIZES = set(GEM_SIZE_POINTS)


def compute_gem(company, homes):
    """(gem_score 0–100, hidden_gem 0/1): local HQ 35 · not well known 25 (unknown 10) · privately held 15
    (subsidiary 5) · 50–999 people 25 (1,000–4,999: 12). The badge also needs the AI's categorization,
    an HQ or major office here, a real company site and 50–4,999 people."""
    lat, lng = company.get("lat"), company.get("lng")
    near = lat is not None and lng is not None and any(geo.haversine_miles(lat, lng, h[0], h[1]) <= 60 for h in homes)
    local = near and company.get("local_presence") not in ("branch", "none")
    score = 35 if local else 0
    wk = company.get("well_known")
    score += 25 if wk == 0 else 10 if wk is None else 0
    score += {"private": 15, "family": 15, "pe_backed": 15, "subsidiary": 5}.get(company.get("ownership"), 0)
    score += GEM_SIZE_POINTS.get(company.get("employee_band"), 0)
    confirmed = company.get("enrich_source") in ("ai", "seed")
    hidden = (confirmed and score >= 70 and wk != 1 and local and company.get("entity_type") == "company"
              and company.get("local_presence") in ("hq", "major_office")
              and company.get("employee_band") in GEM_SIZES)
    return score, int(hidden)


def gem_fields(conn, company):
    score, hidden = compute_gem(company, home_points(conn))
    return {"gem_score": score, "hidden_gem": hidden}


def recompute_gems(conn):
    """Re-derive gem_score/hidden_gem for every company (after a rules change or new profile home)."""
    homes, changed = home_points(conn), 0
    for row in conn.execute("SELECT * FROM companies").fetchall():
        company = db.row_dict(row)
        score, hidden = compute_gem(company, homes)
        if (company.get("gem_score"), company.get("hidden_gem")) != (score, hidden):
            db.update(conn, "companies", "id", company["id"], {"gem_score": score, "hidden_gem": hidden})
            changed += 1
    conn.commit()
    return changed
