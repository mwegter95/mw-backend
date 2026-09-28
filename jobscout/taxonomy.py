"""Taxonomies shared with the frontend (contract §2). Single source of truth for the backend."""

INDUSTRIES = [
    ("mfg_plastics_packaging", "Plastics & Packaging", "Manufacturing"),
    ("mfg_building_materials", "Building Materials & Construction Products", "Manufacturing"),
    ("mfg_chemicals_coatings", "Chemicals, Adhesives & Coatings", "Manufacturing"),
    ("mfg_industrial_equipment", "Industrial Equipment & Machinery", "Manufacturing"),
    ("mfg_electrical_electronics", "Electrical & Electronics", "Manufacturing"),
    ("mfg_medical_devices", "Medical Devices", "Manufacturing"),
    ("mfg_food_beverage", "Food & Beverage Manufacturing", "Manufacturing"),
    ("mfg_consumer_products", "Consumer & Outdoor Products", "Manufacturing"),
    ("mfg_metals_fabrication", "Metals & Fabrication", "Manufacturing"),
    ("mfg_other", "Other Manufacturing", "Manufacturing"),
    ("construction_real_estate", "Construction & Real Estate", "Built environment"),
    ("distribution_logistics", "Distribution & Logistics", "Commerce"),
    ("retail_ecommerce", "Retail & E-commerce", "Commerce"),
    ("agriculture", "Agriculture & Agribusiness", "Commerce"),
    ("tech_software", "Technology & Software", "Technology"),
    ("it_services", "IT Services & Consulting", "Technology"),
    ("professional_services", "Professional Services", "Services"),
    ("agency_marketing", "Marketing, Advertising & PR Agencies", "Services"),
    ("financial_insurance", "Financial Services & Insurance", "Services"),
    ("healthcare", "Healthcare & Health Systems", "Health"),
    ("education", "Education", "Public & nonprofit"),
    ("nonprofit", "Nonprofit & Associations", "Public & nonprofit"),
    ("government", "Government & Public Sector", "Public & nonprofit"),
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

# AI result enums (contract §6).
ROLE_FAMILIES = ["marketing", "communications", "brand", "content", "demand_gen", "product_marketing",
                 "pr", "digital", "other"]
SENIORITIES = ["exec", "director", "manager", "senior_ic", "ic", "intern"]
SALARY_PERIODS = ["year", "hour"]

ENTITY_TYPES = ["company", "directory", "news", "government", "school", "retail_location", "franchise_location",
                "other"]
LOCAL_PRESENCE = ["hq", "major_office", "branch", "none", "unknown"]
DISCOVER_SOURCES = ["maps", "search", "osm", "places"]
ENRICH_STATUSES = ["pending", "queued", "done", "failed"]

RUN_KINDS = ["sweep", "discover", "pipeline", "enrich", "detect", "company"]
TASK_KINDS = ["score_job", "enrich_company", "parse_page"]

# Industries that count toward the gem score's "industrial" bonus (contract §4).
GEM_INDUSTRY_BONUS = {"distribution_logistics", "construction_real_estate"}


def industry_label(slug):
    return INDUSTRY_LABEL.get(slug) if slug else None


def is_maker(slug) -> bool:
    """Companies that make or move physical products: the "hidden gem" employers (a 200-person
    injection molder, a millwork shop) as opposed to local service firms."""
    return INDUSTRY_GROUP.get(slug) == "Manufacturing" or slug == "distribution_logistics"


def is_industrial(slug) -> bool:
    return INDUSTRY_GROUP.get(slug) == "Manufacturing" or slug in GEM_INDUSTRY_BONUS


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
        "employee_bands": EMPLOYEE_BANDS,
        "ownership": OWNERSHIP,
        "workplace": WORKPLACE,
        "title_tiers": TITLE_TIERS,
        "statuses": JOB_STATUSES,
        "company_statuses": COMPANY_STATUSES,
        "business_models": BUSINESS_MODELS,
    }
