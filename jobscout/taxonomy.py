"""Taxonomies shared with the frontend (contract §2). Single source of truth for the backend."""

# Industry groups and industries in plain alphabetical order ("Other" last) so no sector is featured.
INDUSTRIES = [
    ("construction_real_estate", "Construction & Real Estate", "Built environment"),
    ("agriculture", "Agriculture & Agribusiness", "Commerce"),
    ("distribution_logistics", "Distribution & Logistics", "Commerce"),
    ("retail_ecommerce", "Retail & E-commerce", "Commerce"),
    ("healthcare", "Healthcare & Health Systems", "Health"),
    ("mfg_building_materials", "Building Materials & Construction Products", "Manufacturing"),
    ("mfg_chemicals_coatings", "Chemicals, Adhesives & Coatings", "Manufacturing"),
    ("mfg_consumer_products", "Consumer & Outdoor Products", "Manufacturing"),
    ("mfg_electrical_electronics", "Electrical & Electronics", "Manufacturing"),
    ("mfg_food_beverage", "Food & Beverage Manufacturing", "Manufacturing"),
    ("mfg_industrial_equipment", "Industrial Equipment & Machinery", "Manufacturing"),
    ("mfg_medical_devices", "Medical Devices", "Manufacturing"),
    ("mfg_metals_fabrication", "Metals & Fabrication", "Manufacturing"),
    ("mfg_plastics_packaging", "Plastics & Packaging", "Manufacturing"),
    ("mfg_other", "Other Manufacturing", "Manufacturing"),
    ("education", "Education", "Public & nonprofit"),
    ("government", "Government & Public Sector", "Public & nonprofit"),
    ("nonprofit", "Nonprofit & Associations", "Public & nonprofit"),
    ("agency_marketing", "Marketing, Advertising & PR Agencies", "Services"),
    ("financial_insurance", "Financial Services & Insurance", "Services"),
    ("professional_services", "Professional Services", "Services"),
    ("it_services", "IT Services & Consulting", "Technology"),
    ("tech_software", "Technology & Software", "Technology"),
    ("energy_utilities", "Energy & Utilities", "Other"),
    ("hospitality_media", "Hospitality, Recreation & Media", "Other"),
    ("other", "Other", "Other"),
]
INDUSTRY_IDS = [i[0] for i in INDUSTRIES]
INDUSTRY_LABEL = {i[0]: i[1] for i in INDUSTRIES}
INDUSTRY_GROUP = {i[0]: i[2] for i in INDUSTRIES}

EMPLOYEE_BANDS = ["1-49", "50-199", "200-999", "1000-4999", "5000+", "unknown"]
OWNERSHIP = ["private", "family", "pe_backed", "public", "subsidiary", "nonprofit", "government",
             "cooperative", "unknown"]
BUSINESS_MODELS = ["b2b", "b2c", "b2b2c", "mixed", "nonprofit", "public_sector", "unknown"]
WORKPLACE = ["onsite", "hybrid", "remote", "unknown"]
TITLE_TIERS = ["exec", "director", "manager", "lead", "ic", "intern"]
PREFILTER = ["pass", "maybe", "fail"]
COMPANY_STATUSES = ["pending", "active", "no_careers", "no_ats", "blocked", "manual_check", "ignored"]
JOB_STATUSES = ["new", "saved", "applied", "interviewing", "offer", "rejected", "hidden"]
ATS_TYPES = ["workday", "oracle", "greenhouse", "lever", "ashby", "smartrecruiters", "bamboohr", "icims",
             "ukg", "adp", "paylocity", "paycom", "dayforce", "successfactors", "taleo", "jobvite",
             "workable", "jazzhr", "rippling", "recruitee", "breezy", "phenom", "jsonld", "html", "none"]

# Job categories a profile can pick (the title patterns behind them live in interests.py).
JOB_CATEGORIES = [
    ("marketing", "Marketing"), ("communications", "Communications & PR"),
    ("sales", "Sales & Business Development"), ("customer", "Customer Success & Service"),
    ("product", "Product Management"), ("software", "Software & Web Development"),
    ("it", "IT & Cybersecurity"), ("data", "Data & Analytics"), ("design", "Design & UX"),
    ("engineering", "Engineering (non-software)"), ("operations", "Operations & Supply Chain"),
    ("finance", "Finance & Accounting"), ("hr", "HR & Recruiting"), ("project", "Project & Program Management"),
    ("admin", "Administration & Office"), ("legal", "Legal & Compliance"),
    ("fundraising", "Fundraising & Development"), ("healthcare", "Healthcare & Clinical"),
    ("education", "Education & Training"), ("leadership", "General Management"),
    ("trades", "Skilled Trades & Production"),
]
JOB_CATEGORY_IDS = [c[0] for c in JOB_CATEGORIES]
JOB_CATEGORY_LABEL = dict(JOB_CATEGORIES)
# Seniority levels a profile can ask for (title tiers minus intern).
LEVELS = ["exec", "director", "manager", "lead", "ic"]

# AI result enums (contract §6). role_family is the job category the AI reads the posting as.
ROLE_FAMILIES = JOB_CATEGORY_IDS + ["other"]
SENIORITIES = ["exec", "director", "manager", "senior_ic", "ic", "intern"]
SALARY_PERIODS = ["year", "hour"]

ENTITY_TYPES = ["company", "directory", "news", "government", "school", "retail_location", "franchise_location",
                "other"]
LOCAL_PRESENCE = ["hq", "major_office", "branch", "none", "unknown"]
DISCOVER_SOURCES = ["maps", "search", "osm", "places"]
ENRICH_STATUSES = ["pending", "queued", "done", "failed"]

RUN_KINDS = ["find", "sweep", "discover", "pipeline", "enrich", "detect", "company"]
TASK_KINDS = ["score_job", "enrich_company", "parse_page"]



def industry_label(slug):
    return INDUSTRY_LABEL.get(slug) if slug else None


def coerce(value, allowed, default):
    """Return value if it is one of `allowed` (case-insensitive for strings), else default."""
    if isinstance(value, str):
        v = value.strip().lower()
        for a in allowed:
            if a.lower() == v:
                return a
    return default


def meta(places_enabled=False) -> dict:
    """Payload for GET /jobs/api/meta."""
    return {
        "places_enabled": bool(places_enabled),
        "entity_types": ENTITY_TYPES,
        "local_presence": LOCAL_PRESENCE,
        "discover_sources": DISCOVER_SOURCES,
        "industries": [{"id": i, "label": l, "group": g} for i, l, g in INDUSTRIES],
        "job_categories": [{"id": i, "label": l} for i, l in JOB_CATEGORIES],
        "levels": LEVELS,
        "employee_bands": EMPLOYEE_BANDS,
        "ownership": OWNERSHIP,
        "workplace": WORKPLACE,
        "title_tiers": TITLE_TIERS,
        "statuses": JOB_STATUSES,
        "company_statuses": COMPANY_STATUSES,
        "business_models": BUSINESS_MODELS,
    }
