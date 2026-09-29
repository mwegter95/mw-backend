"""What people are looking for: job categories, target titles and seniority levels from their profiles.

Nothing here is specific to one person or field. A profile picks any number of job categories (Marketing,
Software, Finance…), lists target titles in its own words, and optionally the levels it wants. From that:

* ``Interests.match(title)`` → "pass" | "maybe" | "fail" for one listing title:
  pass  = the title is in one of the categories or reads like a target title, at a wanted level;
  maybe = same field, different level (e.g. a senior specialist when only managers were asked for);
  fail  = anything else, and always interns / co-ops.
* ``Interests.search_terms()`` → the words used to search big careers boards (Workday, Oracle) that are
  too large to read whole.

The sweep stores ``jobs.prefilter`` from ``combined(conn)`` — every profile's interests together — so it
knows which listings deserve a detail fetch and AI scoring. Each person's job list is then filtered with
their own ``for_profile(profile)``.
"""
import re
from dataclasses import dataclass, field

from . import db
from .normalize import title_matches, title_tier, title_tokens
from .taxonomy import JOB_CATEGORY_IDS, JOB_CATEGORY_LABEL, LEVELS

# "digital" is narrowed so "Digital Engineer" and "Digital Health Analyst" don't count as marketing.
_DIGITAL_NOT = (r"engineer|engineering|technology|technologies|transformation|product|health|solutions?|"
                r"services|infrastructure|workplace|architect|systems|data|analytics|operations|security|"
                r"pathology|imaging|success|support|forensics")
# Titles that are plainly a hands-on technical or clinical job even when a marketing word appears.
_NOT_OFFICE = (r"engineer|engineering|technician|developer|nurse|rn|physician|pharmacist|therapist|dispatcher|"
               r"mechanic|welder|machinist|driver|custodian|electrician")

# id, label (as in taxonomy.JOB_CATEGORIES), title pattern, pattern that disqualifies a match (or None),
# board search terms
CATEGORIES = [
    ("marketing", "Marketing",
     r"marketing|marketer|brand(ing)?|demand\s+gen(eration)?|growth|content|lifecycle|seo|copywrit(er|ing)|"
     r"creative|engagement|events?|paid\s+(search|social|media)|media\s+(planner|planning|buyer|buying|strategist)|"
     rf"social\s+(media|marketing|content|strategy|engagement)|digital(?!\s+({_DIGITAL_NOT}))",
     _NOT_OFFICE, ["marketing", "brand"]),
    ("communications", "Communications & PR",
     r"communications?|comms|pr|public\s+relations|public\s+affairs|media\s+relations|corporate\s+affairs|"
     r"external\s+affairs|internal\s+communications|spokesperson",
     _NOT_OFFICE, ["communications", "public relations"]),
    ("sales", "Sales & Business Development",
     r"sales|account\s+(executive|manager|director)|business\s+development|key\s+account|territory\s+(manager|representative)|"
     r"partnerships?|channel\s+(manager|sales)", None, ["sales", "business development"]),
    ("customer", "Customer Success & Service",
     r"customer\s+(success|service|support|experience|care)|client\s+(services|success|relations)",
     None, ["customer success", "customer service"]),
    ("product", "Product Management",
     r"product\s+(manager|management|owner|director|lead)", None, ["product manager"]),
    ("software", "Software & Web Development",
     r"software|developer|programmer|(web|front[\s-]?end|back[\s-]?end|full[\s-]?stack|devops|cloud|platform)\s+"
     r"(engineer|developer)|site\s+reliability", None, ["software engineer", "developer"]),
    ("it", "IT & Cybersecurity",
     r"information\s+technology|systems?\s+administrator|network\s+(engineer|administrator)|help\s*desk|"
     r"service\s+desk|cyber\s*security|information\s+security|security\s+analyst", None,
     ["information technology", "IT"]),
    ("data", "Data & Analytics",
     r"data\s+(analyst|scientist|engineer|architect)|analytics|business\s+intelligence|\bbi\s+(analyst|developer)|"
     r"reporting\s+analyst|statistician", None, ["data", "analytics"]),
    ("design", "Design & UX",
     r"designer|ux|ui|user\s+experience|art\s+director|creative\s+director|graphic|visual\s+design",
     None, ["designer"]),
    ("engineering", "Engineering (non-software)",
     r"(mechanical|electrical|manufacturing|process|quality|industrial|civil|structural|chemical|project|"
     r"design|product|controls|packaging|field|application)\s+engineer(ing)?|engineering\s+manager",
     None, ["engineer"]),
    ("operations", "Operations & Supply Chain",
     r"operations|supply\s+chain|logistics|procurement|purchasing|buyer|planner|planning|warehouse|"
     r"distribution|plant\s+manager|production\s+(manager|supervisor)|materials\s+manager",
     None, ["operations", "supply chain"]),
    ("finance", "Finance & Accounting",
     r"finance|financial|accounting|accountant|controller|fp&a|payroll|treasury|audit(or)?|tax|bookkeep(er|ing)|"
     r"billing|accounts\s+(payable|receivable)", None, ["finance", "accounting"]),
    ("hr", "HR & Recruiting",
     r"human\s+resources|\bhr\b|people\s+(operations|partner|team)|talent|recruit(er|ing|ment)|benefits|"
     r"compensation|learning\s+(and|&)\s+development|training\s+(manager|specialist)", None,
     ["human resources", "recruiter"]),
    ("project", "Project & Program Management",
     r"project\s+(manager|management|coordinator|director)|program\s+(manager|director)|pmo|scrum\s+master",
     None, ["project manager", "program manager"]),
    ("admin", "Administration & Office",
     r"administrative|office\s+manager|executive\s+assistant|office\s+administrator|receptionist",
     None, ["administrative", "office manager"]),
    ("legal", "Legal & Compliance",
     r"counsel|attorney|lawyer|paralegal|legal|compliance|contracts\s+(manager|administrator)",
     None, ["legal", "compliance"]),
    ("fundraising", "Fundraising & Development",
     r"fundrais(ing|er)|development\s+(director|officer|manager|associate)|advancement|donor|grants?|philanthropy",
     None, ["development director", "fundraising"]),
    ("healthcare", "Healthcare & Clinical",
     r"nurse|nursing|\brn\b|lpn|physician|clinical|medical\s+assistant|therapist|pharmacist|care\s+coordinator",
     None, ["nurse", "clinical"]),
    ("education", "Education & Training",
     r"teacher|instructor|professor|faculty|curriculum|tutor|educator", None, ["teacher", "instructor"]),
    ("leadership", "General Management",
     r"general\s+manager|chief\s+(executive|operating)|\bceo\b|\bcoo\b|president|executive\s+director|"
     r"managing\s+director|business\s+unit\s+(leader|manager)", None, ["general manager"]),
    ("trades", "Skilled Trades & Production",
     r"technician|machinist|welder|electrician|maintenance|assembler|operator|mechanic|installer|fabricator",
     None, ["technician"]),
]
assert [c[0] for c in CATEGORIES] == JOB_CATEGORY_IDS and all(JOB_CATEGORY_LABEL[c[0]] == c[1] for c in CATEGORIES)
# "IT" only as a capitalized word ("IT Manager"), never the word "it". It also marks a title as an IT job
# for the marketing and communications categories ("IT Digital Workplace Lead" isn't marketing).
_IT = re.compile(r"\bIT\b")
_CASE_SENSITIVE = {"it": _IT}
_CASE_SENSITIVE_NOT = {"marketing": _IT, "communications": _IT}
_COMPILED = {cid: (re.compile(rf"\b(?:{pat})\b", re.I), re.compile(rf"\b(?:{neg})\b", re.I) if neg else None)
             for cid, _label, pat, neg, _terms in CATEGORIES}
_SEARCH_TERMS = {c[0]: c[4] for c in CATEGORIES}

# Words that say how senior a title is, not what the job is ("Marketing Director" → "marketing").
_LEVEL_WORDS = {"chief", "head", "vice", "president", "vp", "svp", "evp", "avp", "director", "manager", "managing",
                "lead", "leader", "senior", "principal", "staff", "supervisor", "associate", "assistant", "junior",
                "coordinator", "specialist", "executive", "officer", "i", "ii", "iii", "iv"}


def core_words(title) -> list:
    """The words of a target title that name the field ("Director of Supply Chain" → ["supply", "chain"])."""
    return [w for w in title_tokens(title) if w not in _LEVEL_WORDS]


@dataclass(frozen=True)
class Interests:
    categories: tuple = ()
    titles: tuple = ()
    levels: tuple = ()      # empty = any level (interns never match)
    _cores: tuple = field(default=(), compare=False, repr=False)

    @classmethod
    def build(cls, categories=(), titles=(), levels=()):
        cats = tuple(c for c in dict.fromkeys(categories or ()) if c in _COMPILED)
        ttl = tuple(t for t in dict.fromkeys(str(t).strip() for t in titles or ()) if t)
        lvl = tuple(level for level in dict.fromkeys(levels or ()) if level in LEVELS)
        cores = tuple(tuple(core_words(t)) for t in ttl)
        return cls(cats, ttl, lvl, tuple(c for c in cores if c))

    @property
    def empty(self) -> bool:
        return not self.categories and not self.titles

    def categories_for(self, title) -> list:
        """Which of the selected categories a title belongs to."""
        title = title or ""
        out = []
        for cid in self.categories:
            pattern, neg = _COMPILED[cid]
            hit = pattern.search(title) or (cid in _CASE_SENSITIVE and _CASE_SENSITIVE[cid].search(title))
            blocked = (neg and neg.search(title)) or (cid in _CASE_SENSITIVE_NOT and _CASE_SENSITIVE_NOT[cid].search(title))
            if hit and not blocked:
                out.append(cid)
        return out

    def in_field(self, title) -> bool:
        """The title is one of the selected categories or reads like one of the target titles."""
        if self.categories_for(title):
            return True
        if title_matches(title, self.titles):
            return True
        tokens = set(title_tokens(title))
        return any(set(core) <= tokens for core in self._cores)

    def match(self, title, tier=None) -> str:
        tier = tier or title_tier(title)
        if tier == "intern" or not self.in_field(title):
            return "fail"
        return "pass" if not self.levels or tier in self.levels else "maybe"

    def search_terms(self, limit=10) -> list:
        """Words to search large careers boards with: each category's terms, then target titles' core words."""
        terms = [t for cid in self.categories for t in _SEARCH_TERMS[cid]]
        terms += [" ".join(core) for core in self._cores]
        return list(dict.fromkeys(t for t in terms if t))[:limit]

    def describe(self) -> dict:
        return {"categories": [JOB_CATEGORY_LABEL[c] for c in self.categories], "titles": list(self.titles),
                "levels": list(self.levels)}


def for_profile(profile) -> Interests:
    p = profile or {}
    return Interests.build(p.get("job_categories") or [], p.get("target_titles") or [], p.get("seniority") or [])


def combined(conn) -> Interests:
    """Everyone's interests together (what the sweep looks for). A level list only narrows the union when
    every profile with interests has one; otherwise any level passes."""
    cats, titles, levels, all_have_levels = [], [], [], True
    for row in conn.execute("SELECT job_categories, target_titles, seniority FROM profiles"):
        c, t, lv = db.loads(row["job_categories"], []), db.loads(row["target_titles"], []), db.loads(row["seniority"], [])
        if not c and not t:
            continue
        cats += c
        titles += t
        if lv:
            levels += lv
        else:
            all_have_levels = False
    return Interests.build(cats, titles, levels if all_have_levels else ())


def refresh_prefilter(conn) -> int:
    """Re-rate every open job's title against everyone's current interests (after a profile change).
    Returns how many jobs changed. Newly matching jobs get their details on the next sweep."""
    from .normalize import rule_score
    interests = combined(conn)
    changed = 0
    rows = conn.execute("SELECT j.id, j.title, j.title_tier, j.prefilter, j.workplace, j.salary_min, j.salary_max, "
                        "c.industry FROM jobs j JOIN companies c ON c.id = j.company_id "
                        "WHERE j.closed_at IS NULL").fetchall()
    for r in rows:
        pf = interests.match(r["title"], r["title_tier"])
        if pf != r["prefilter"]:
            job = dict(r, prefilter=pf)
            db.update(conn, "jobs", "id", r["id"], {"prefilter": pf, "rule_score": rule_score(job, None, r["industry"])})
            changed += 1
    conn.commit()
    return changed
