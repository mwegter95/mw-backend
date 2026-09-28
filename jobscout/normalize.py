"""Deterministic normalisation: HTML, titles, salary, workplace, locations, hashes.

Everything here is pure (no DB, no network) so it is cheap to unit-test.
Rule-score / prefilter semantics follow contract §4 exactly.
"""
import difflib
import hashlib
import html as _html
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Comment

from .config import LOCAL_STATES

# ── HTML ─────────────────────────────────────────────────────────────────────

_ALLOWED_TAGS = {"p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "h2", "h3", "h4", "a"}
_DROP_TAGS = {"script", "style", "iframe", "noscript", "svg", "head", "object", "embed", "form", "input",
              "button", "select", "textarea", "img", "picture", "video", "audio", "canvas", "template",
              "meta", "link", "title"}
_RENAME = {"h1": "h2", "h5": "h4", "h6": "h4"}
_BLOCKS = {"div", "section", "article", "header", "footer", "main", "aside", "blockquote", "table",
           "tbody", "thead", "tfoot", "tr", "td", "th", "dl", "dt", "dd", "figure", "figcaption",
           "center", "pre", "address"}
_BLOCK_CHILDREN = {"p", "ul", "ol", "li", "h2", "h3", "h4"} | _BLOCKS
_SAFE_SCHEMES = ("http://", "https://", "mailto:")


def _soup(markup):
    return BeautifulSoup(markup or "", "html.parser")


def sanitize_html(markup) -> str:
    """Reduce arbitrary job-description HTML to the contract allowlist:
    p, br, ul, ol, li, strong, b, em, i, h2–h4 and a[href] (target=_blank rel=noopener)."""
    if not markup:
        return ""
    soup = _soup(markup)
    for node in soup.find_all(string=lambda s: isinstance(s, Comment)):
        node.extract()
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    # Reverse document order visits descendants before their ancestors.
    for tag in reversed(soup.find_all(True)):
        name = _RENAME.get(tag.name, tag.name)
        tag.name = name
        if name in _BLOCKS:
            nested = tag.find_parent(["p", "li", "h2", "h3", "h4"]) is not None
            if nested or tag.find(_BLOCK_CHILDREN):
                tag.unwrap()
            else:
                tag.name, tag.attrs = "p", {}
        elif name == "a":
            href = (tag.get("href") or "").strip()
            if href.lower().startswith(_SAFE_SCHEMES):
                tag.attrs = {"href": href, "target": "_blank", "rel": "noopener"}
            else:
                tag.unwrap()
        elif name in _ALLOWED_TAGS:
            tag.attrs = {}
        else:
            tag.unwrap()
    for tag in soup.find_all(["p", "li", "strong", "b", "em", "i", "h2", "h3", "h4"]):
        if getattr(tag, "decomposed", False):
            continue
        if not tag.get_text(strip=True).replace("\xa0", "") and not tag.find("br"):
            tag.decompose()
    out = str(soup).strip()
    return re.sub(r"(<br/?>\s*){3,}", "<br/><br/>", out)


def html_to_text(markup) -> str:
    """Readable plain text with paragraph breaks preserved."""
    if not markup:
        return ""
    soup = _soup(markup)
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"]):
        tag.insert_before("\n")
        tag.insert_after("\n")
    text = soup.get_text().replace("\xa0", " ")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def unescape_html(markup) -> str:
    """Some ATS APIs (Greenhouse) return entity-escaped HTML."""
    return _html.unescape(markup or "")


# ── titles: tier, function, prefilter ────────────────────────────────────────

_TIER_RULES = [
    ("intern", r"\b(intern|interns|internship|co-?op|apprentice(ship)?|student worker)\b"),
    ("exec", r"\b(chief|cmo|cco|vp|svp|evp|avp|vice[\s-]+president|head\s+of|president)\b"),
    ("director", r"\b(director|dir\.)(?=\W|$)"),
    ("manager", r"\b(manager|mgr|managing)\b"),
    ("lead", r"\b(lead|leader|senior|sr|principal|staff|supervisor)\b"),
]
_TIER_RES = [(tier, re.compile(p, re.I)) for tier, p in _TIER_RULES]

# Contract §4 function list, plus a few unambiguous marketing synonyms (marketer, copywriter,
# public affairs, media relations, paid media, SEO). "digital" and "social" are narrowed so
# "Digital Engineer" and "Social Worker" don't count.
_DIGITAL_NOT = (r"engineer|engineering|technology|technologies|transformation|product|health|solutions?|"
                r"services|infrastructure|workplace|architect|systems|data|analytics|operations|security|"
                r"pathology|imaging|success|support|forensics")
_FUNCTION_RE = re.compile(
    r"\b(marketing|marketer|communications?|comms|brand(ing)?|content|demand\s+gen(eration)?|growth|"
    r"pr|public\s+relations|public\s+affairs|media\s+relations|corporate\s+affairs|external\s+affairs|"
    r"advancement|events?|lifecycle|engagement|creative|copywrit(er|ing)|seo|"
    r"paid\s+(search|social|media)|media\s+(planner|planning|buyer|buying|strategist)|"
    r"social\s+(media|marketing|content|strategy|impact|engagement)|"
    rf"digital(?!\s+({_DIGITAL_NOT})))\b",
    re.I)
_ADJACENT_RE = re.compile(
    r"\b(strategy|customer\s+experience|product\s+manag(er|ement)|business\s+development|"
    r"market\s+development|market\s+research|customer\s+insights|investor\s+relations)\b",
    re.I)
# Titles that are clearly another profession even if a function word appears.
_NOT_FUNCTION_RE = re.compile(
    r"\b(engineer|engineering|technician|developer|nurse|rn|physician|pharmacist|therapist|dispatcher|"
    r"mechanic|welder|machinist|driver|custodian|electrician)\b", re.I)

_LEADERSHIP = {"exec", "director", "manager", "lead"}
_MANAGER_PLUS = {"exec", "director", "manager"}


def title_tier(title) -> str:
    for tier, rx in _TIER_RES:
        if rx.search(title or ""):
            return tier
    return "ic"


_IT_RE = re.compile(r"\bIT\b")  # "IT Digital …" is an IT role; case-sensitive on purpose


def has_function(title) -> bool:
    title = title or ""
    return bool(_FUNCTION_RE.search(title)) and not _NOT_FUNCTION_RE.search(title) and not _IT_RE.search(title)


def has_adjacent_function(title) -> bool:
    return bool(_ADJACENT_RE.search(title or ""))


def prefilter(title, tier=None) -> str:
    """pass = function match AND tier ∈ {exec, director, manager, lead};
    maybe = function match with tier ic, or tier ≥ manager with an adjacent function;
    fail = everything else (always for interns / co-ops)."""
    tier = tier or title_tier(title)
    if tier == "intern":
        return "fail"
    if has_function(title):
        return "pass" if tier in _LEADERSHIP else "maybe"
    if tier in _MANAGER_PLUS and has_adjacent_function(title):
        return "maybe"
    return "fail"


_TITLE_ABBREV = {"mgr": "manager", "dir": "director", "sr": "senior", "jr": "junior", "comms": "communications",
                 "communication": "communications", "vp": "vice president", "mktg": "marketing",
                 "pr": "public relations"}
_TITLE_STOP = {"of", "the", "and", "&", "for", "a", "an", "to", "in", "at"}


def _title_tokens(title):
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    out = []
    for w in words:
        out.extend(_TITLE_ABBREV.get(w, w).split())
    return [w for w in out if w not in _TITLE_STOP]


def title_matches(title, targets) -> bool:
    """Fuzzy match: every target word appears in the title (any order), or near-identical strings."""
    tokens = _title_tokens(title)
    token_set = set(tokens)
    for target in targets or []:
        t_tokens = _title_tokens(target)
        if not t_tokens:
            continue
        if set(t_tokens) <= token_set:
            return True
        if difflib.SequenceMatcher(None, " ".join(tokens), " ".join(t_tokens)).ratio() >= 0.85:
            return True
    return False


_TIER_POINTS = {"exec": 40, "director": 40, "manager": 32, "lead": 20, "ic": 10, "intern": 0}
DEFAULT_WORKPLACE_PREF = ["onsite", "hybrid", "remote"]


def rule_score(job: dict, profile: dict = None, industry=None) -> int:
    """Provisional 0–100 fit before AI (contract §4). `profile` uses parsed lists; None = no profile."""
    profile = profile or {}
    tier = job.get("title_tier") or title_tier(job.get("title"))
    pf = job.get("prefilter") or prefilter(job.get("title"), tier)
    score = _TIER_POINTS.get(tier, 10)
    if has_function(job.get("title")):
        score += 30
    if title_matches(job.get("title"), profile.get("target_titles")):
        score += 10
    if job.get("workplace") in (profile.get("workplace_pref") or DEFAULT_WORKPLACE_PREF):
        score += 5
    top = job.get("salary_max") or job.get("salary_min")
    if top is None:
        score += 5
    elif not profile.get("salary_floor") or top >= profile["salary_floor"]:
        score += 10
    if industry and industry in (profile.get("industries_want") or []):
        score += 5
    if industry and industry in (profile.get("industries_avoid") or []):
        score -= 25
    score = max(0, min(100, score))
    return min(score, 15) if pf == "fail" else score


# ── salary ───────────────────────────────────────────────────────────────────

HOURS_PER_YEAR = 2080
MIN_HOURLY, MAX_ANNUAL = 15.0, 1_500_000.0
_PERIOD_FACTOR = {"hour": HOURS_PER_YEAR, "day": 260, "week": 52, "month": 12, "year": 1}


@dataclass
class Salary:
    min: float
    max: float
    period: str   # "year" | "hour" — min/max are always annual USD
    text: str


_AMOUNT_RE = re.compile(r"(\$|usd\s?)?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s?([km])?(?![a-z0-9%])", re.I)
_RANGE_SEP_RE = re.compile(r"^\s*(?:-|–|—|to|and|through)\s*$", re.I)
_UNIT_AFTER = [
    ("hour", re.compile(r"^\W{0,3}(?:usd\s*)?(?:/\s*(?:hour|hr|h)\b|per\s+hour|an\s+hour|hourly|per\s+hr|p/?h\b)", re.I)),
    ("year", re.compile(r"^\W{0,3}(?:usd\s*)?(?:/\s*(?:year|yr|annum|annual)|per\s+(?:year|annum)|a\s+year|annual(?:ly)?|yearly|salary)", re.I)),
    ("month", re.compile(r"^\W{0,3}(?:usd\s*)?(?:/\s*(?:month|mo)\b|per\s+month|a\s+month|monthly)", re.I)),
    ("week", re.compile(r"^\W{0,3}(?:usd\s*)?(?:/\s*(?:week|wk)\b|per\s+week|a\s+week|weekly)", re.I)),
]
_UNIT_BEFORE_HOUR = re.compile(r"(hourly|per\s+hour|hour(ly)?\s+(rate|range|wage|pay))[^.$]{0,40}$", re.I)
_UNIT_BEFORE_YEAR = re.compile(r"(annual(ly)?|yearly|per\s+year|salary)[^.$]{0,40}$", re.I)
_PAY_WORDS = re.compile(r"(salary|pay|compensation|wage|range|rate|earn|base)", re.I)
# Amounts that belong to a benefit rather than base pay: the benefit word sits right before
# ("signing bonus of $5,000") or right after ("$5,000 sign-on bonus") the number.
_EXCLUDE_BEFORE = re.compile(
    r"(bonus|sign[\s-]?on|signing|referral|relocation|stipend|tuition|reimburse\w*|allowance|"
    r"401\(?k\)?|403\(?b\)?|retention|credit)[^.$\d]{0,25}$", re.I)
_EXCLUDE_AFTER = re.compile(
    r"^[^.$\d]{0,12}(bonus|sign[\s-]?on|signing|referral|relocation|stipend|tuition|reimburse|allowance|"
    r"in\s+(equity|stock|rsus?))", re.I)


def _amount_value(match, borrowed_suffix=None):
    value = float(match.group(2).replace(",", "") + (match.group(3) or ""))
    suffix = (match.group(4) or borrowed_suffix or "").lower()
    return value * (1000 if suffix == "k" else 1_000_000 if suffix == "m" else 1)


def _annualize(value, period):
    return value * _PERIOD_FACTOR[period]


def _valid(annual_values, period):
    for v in annual_values:
        hourly = v / HOURS_PER_YEAR
        if hourly < MIN_HOURLY or v > MAX_ANNUAL:
            return False
    return not (period == "hour" and max(annual_values) / HOURS_PER_YEAR > 500)


def make_salary(lo, hi, period, text=None):
    """Build a Salary from structured ATS data. period accepts year/hour/month/week/day and
    ATS spellings such as "1 YEAR", "per-hour-wage", "HOUR"."""
    if lo is None and hi is None:
        return None
    p = (period or "year").lower()
    unit = next((u for u in ("hour", "day", "week", "month", "year") if u in p), "year")
    try:
        lo, hi = (float(x) if x is not None else None for x in (lo, hi))
    except (TypeError, ValueError):
        return None
    lo, hi = lo if lo is not None else hi, hi if hi is not None else lo
    if lo > hi:
        lo, hi = hi, lo
    annual = [_annualize(lo, unit), _annualize(hi, unit)]
    if not _valid(annual, unit):
        return None
    period_out = "hour" if unit == "hour" else "year"
    return Salary(round(annual[0]), round(annual[1]), period_out, text or _format_salary(lo, hi, unit))


def _format_salary(lo, hi, unit):
    def fmt(v):
        return f"${v:,.2f}" if unit == "hour" else f"${v:,.0f}"
    base = fmt(lo) if lo == hi else f"{fmt(lo)} - {fmt(hi)}"
    return base + (" per hour" if unit == "hour" else "" if unit == "year" else f" per {unit}")


def _candidates(text):
    matches = list(_AMOUNT_RE.finditer(text))
    i = 0
    while i < len(matches):
        m = matches[i]
        nxt = matches[i + 1] if i + 1 < len(matches) else None
        if nxt and _RANGE_SEP_RE.match(text[m.end():nxt.start()]):
            yield m, nxt
            i += 2
        else:
            yield m, None
            i += 1


def _unit_near(text, start, end):
    after = text[end:end + 40]
    for unit, rx in _UNIT_AFTER:
        if rx.search(after):
            return unit
    before = text[max(0, start - 80):start]
    if _UNIT_BEFORE_HOUR.search(before):
        return "hour"
    if _UNIT_BEFORE_YEAR.search(before):
        return "year"
    return None


def _evaluate(text, lo_m, hi_m):
    start, end = lo_m.start(), (hi_m or lo_m).end()
    if _EXCLUDE_BEFORE.search(text[max(0, start - 40):start]) or _EXCLUDE_AFTER.search(text[end:end + 30]):
        return None
    lo_suffix, hi_suffix = lo_m.group(4), hi_m.group(4) if hi_m else None
    lo = _amount_value(lo_m, hi_suffix if (hi_suffix and not lo_suffix and float(lo_m.group(2).replace(",", "")) < 1000) else None)
    hi = _amount_value(hi_m) if hi_m else lo
    has_currency = bool(lo_m.group(1) or (hi_m and hi_m.group(1)))
    unit = _unit_near(text, start, end)
    pay_context = bool(_PAY_WORDS.search(text[max(0, start - 80):start]))
    if not (has_currency or unit or (pay_context and (lo_suffix or hi_suffix or "," in lo_m.group(2)))):
        return None
    if unit is None:
        unit = "hour" if max(lo, hi) < 300 else "year" if min(lo, hi) >= 10000 else None
    if unit is None:
        return None
    if lo > hi:
        lo, hi = hi, lo
    if lo and hi / lo > 6:
        return None
    annual = [_annualize(lo, unit), _annualize(hi, unit)]
    if not _valid(annual, unit):
        return None
    snippet_end = end
    tail = text[end:end + 40]
    for _, rx in _UNIT_AFTER:
        m = rx.search(tail)
        if m:
            snippet_end = end + m.end()
            break
    snippet = re.sub(r"\s+", " ", text[start:snippet_end]).strip()
    rank = (pay_context, hi_m is not None, has_currency)
    return rank, Salary(round(annual[0]), round(annual[1]), "hour" if unit == "hour" else "year", snippet)


def parse_salary(text):
    """Find the most salary-like amount or range in free text → Salary | None.

    Handles "$164,612 - $201,193", "$120K–$150K", "$55.00 - $70.00 per hour",
    "120,000 to 150,000 annually", "Pay Range: $95,000.00 - $120,000.00/yr". Ignores
    < $15/hr, > $1.5M/yr, 401(k)/bonus/percentages.
    """
    if not text:
        return None
    text = text.replace("\xa0", " ")
    best = None
    for lo_m, hi_m in _candidates(text):
        found = _evaluate(text, lo_m, hi_m)
        if found and (best is None or found[0] > best[0]):
            best = found
    return best[1] if best else None


# ── workplace ────────────────────────────────────────────────────────────────

_WP_ALIASES = {
    "onsite": "onsite", "inoffice": "onsite", "office": "onsite", "oraonsite": "onsite", "inperson": "onsite",
    "hybrid": "hybrid", "orahybrid": "hybrid", "flexible": "hybrid", "hyrbid": "hybrid",
    "remote": "remote", "oraremote": "remote", "fullyremote": "remote", "telecommute": "remote",
    "remotefirst": "remote", "workfromhome": "remote", "wfh": "remote",
}
_HYBRID_RE = re.compile(r"\b(hybrid|hyrbid|hybird)\b", re.I)
_REMOTE_NEG_RE = re.compile(r"\b(not|no|isn'?t)\s+(a\s+|an\s+)?(fully\s+)?remote\b", re.I)
_REMOTE_TEXT_RE = re.compile(
    r"\b(fully\s+remote|100%\s+remote|remote\s+(position|role|opportunity|job|work)|work\s+from\s+home|"
    r"work\s+remotely|remote[\s-]first|telecommut\w*|this\s+(position|role)\s+is\s+remote)\b", re.I)
_ONSITE_TEXT_RE = re.compile(r"\b(on[\s-]?site|in[\s-]office|in[\s-]person)\b", re.I)


def workplace_from_value(value):
    """Map an ATS field (Workday remoteType, Lever/Ashby workplaceType, Oracle code…) → enum or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "remote" if value else None
    key = re.sub(r"[^a-z]", "", str(value).lower())
    return _WP_ALIASES.get(key)


def detect_workplace(ats_value=None, title="", location="", description="") -> str:
    """ATS field first, then explicit words in title/location, then the description."""
    wp = workplace_from_value(ats_value)
    if wp:
        return wp
    head = f"{title or ''} {location or ''}"
    if _HYBRID_RE.search(head):
        return "hybrid"
    if re.search(r"\bremote\b", head, re.I):
        return "remote"
    body = description or ""
    if _HYBRID_RE.search(body):
        return "hybrid"
    if _REMOTE_NEG_RE.search(body):
        return "onsite"
    if _REMOTE_TEXT_RE.search(body):
        return "remote"
    if _ONSITE_TEXT_RE.search(body):
        return "onsite"
    return "unknown"


# ── locations ────────────────────────────────────────────────────────────────

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}
_STATE_BY_NAME = {v.lower(): k for k, v in US_STATES.items()}
_US_NAMES = {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america", "america"}
_COUNTRIES = {
    "canada": "CA", "mexico": "MX", "india": "IN", "china": "CN", "japan": "JP", "germany": "DE",
    "france": "FR", "poland": "PL", "netherlands": "NL", "the netherlands": "NL", "united kingdom": "GB",
    "uk": "GB", "england": "GB", "ireland": "IE", "brazil": "BR", "philippines": "PH", "singapore": "SG",
    "australia": "AU", "spain": "ES", "italy": "IT", "sweden": "SE", "czechia": "CZ", "czech republic": "CZ",
    "israel": "IL", "costa rica": "CR", "argentina": "AR", "colombia": "CO", "korea": "KR",
    "south korea": "KR", "taiwan": "TW", "belgium": "BE", "switzerland": "CH", "denmark": "DK",
    "norway": "NO", "finland": "FI", "portugal": "PT", "hungary": "HU", "romania": "RO", "thailand": "TH",
    "vietnam": "VN", "malaysia": "MY", "indonesia": "ID", "south africa": "ZA", "chile": "CL", "peru": "PE",
    "turkey": "TR", "united arab emirates": "AE", "uae": "AE", "egypt": "EG", "austria": "AT",
    "new zealand": "NZ", "hong kong": "HK", "emea": "XX", "apac": "XX", "latam": "XX", "europe": "XX",
}
_ZIP_TAIL = re.compile(r"^(.*?)[\s,]*\b(\d{5})(?:-\d{4})?$")
_MULTI_RE = re.compile(r"^\s*(\d+|multiple|several|various)\s+locations?\s*$", re.I)
_REMOTE_WORDS = re.compile(r"\b(remote|telecommute|work from home|wfh|virtual|anywhere|nationwide)\b", re.I)
_STRIP_WORDS = re.compile(r"\b(remote|telecommute|work from home|wfh|virtual|anywhere|nationwide|hybrid|"
                          r"on-?site|in office|office|hq|headquarters|campus)\b", re.I)


@dataclass
class Location:
    city: str = None
    state: str = None      # two-letter US state
    country: str = None    # ISO-2
    remote: bool = False
    multi: bool = False
    zip: str = None


def state_code(value):
    if not value:
        return None
    v = value.strip().strip(".").strip()
    if len(v) == 2 and v.upper() in US_STATES:
        return v.upper()
    return _STATE_BY_NAME.get(v.lower())


def _country_code(value):
    v = (value or "").strip().lower()
    if v in _US_NAMES:
        return "US"
    return _COUNTRIES.get(v)


def _parse_single(text) -> Location:
    loc = Location()
    if _MULTI_RE.match(text):
        loc.multi = True
        return loc
    loc.remote = bool(_REMOTE_WORDS.search(text))
    core = _STRIP_WORDS.sub(" ", text)
    core = re.sub(r"[()\[\]]", ",", core)
    parts = [p.strip(" -–—./:") for p in re.split(r"\s*,\s*|\s+[-–—/]\s+", core)]
    parts = [re.sub(r"\s+", " ", p) for p in parts if p and p.strip(" -–—./:")]
    if not parts:
        return loc
    zip_m = _ZIP_TAIL.match(parts[-1])
    if zip_m:
        loc.zip = zip_m.group(2)
        parts[-1] = zip_m.group(1).strip(" ,")
        parts = [p for p in parts if p]
    if parts and _country_code(parts[-1]):
        loc.country = _country_code(parts[-1])
        parts = parts[:-1]
    elif len(parts) >= 2 and re.fullmatch(r"[A-Z]{2}", parts[0]) and not state_code(parts[-1]):
        # Workday style "US, Minnesota, Maplewood" / "IN, Bangalore Kar".
        loc.country = parts[0]
        parts = parts[1:]
        if loc.country == "US" and parts and state_code(parts[0]):
            loc.state = state_code(parts[0])
            parts = parts[1:]
        loc.city = parts[-1] if parts else None
        return loc
    if loc.country not in (None, "US"):
        loc.city = parts[0] if parts else None
        return loc
    if parts and state_code(parts[-1]):
        loc.state = state_code(parts[-1])
        parts = parts[:-1]
    elif len(parts) >= 2 and state_code(parts[0]):
        loc.state = state_code(parts[0])
        parts = parts[1:]
    if parts:
        loc.city = parts[-1] if loc.state else parts[0]
    if loc.state and not loc.country:
        loc.country = "US"
    return loc


def parse_location(text) -> Location:
    """Parse ATS location strings: "US, Minnesota, Maplewood", "Bloomington, MN, United States",
    "Maplewood, MN 55144", "Remote - US", "3 Locations", "Austin, TX; Minneapolis, MN" …
    For multi-location strings the first local (MN/WI) entry wins."""
    text = (text or "").strip()
    if not text:
        return Location()
    pieces = [p for p in re.split(r"\s*(?:;|\||\bor\b|\n)\s*", text) if p.strip()]
    if len(pieces) <= 1:
        return _parse_single(text)
    parsed = [_parse_single(p) for p in pieces]
    best = next((p for p in parsed if p.state in LOCAL_STATES), None) \
        or next((p for p in parsed if p.remote and p.country in (None, "US") and not p.state), None) \
        or parsed[0]
    best.multi = True
    return best


def is_local(loc: Location) -> bool:
    """Keep MN/WI jobs, US-wide remote jobs, and jobs whose location is unknown."""
    if loc.country and loc.country != "US":
        return False
    if loc.state:
        return loc.state in LOCAL_STATES
    return True


# ── hashes ───────────────────────────────────────────────────────────────────

def content_hash(*parts) -> str:
    blob = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(blob.encode("utf-8", "ignore")).hexdigest()[:16]
