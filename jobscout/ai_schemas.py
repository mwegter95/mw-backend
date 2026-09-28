"""Strict JSON schemas for the three AI task kinds (contract §6 + §9.5) and validate/clamp helpers.

The worker sends the schema to LM Studio as response_format json_schema (strict); the primary
re-validates every result with `validate(kind, result)` before persisting it.
"""
from .taxonomy import (BUSINESS_MODELS, EMPLOYEE_BANDS, ENTITY_TYPES, INDUSTRY_IDS, LOCAL_PRESENCE, OWNERSHIP,
                       ROLE_FAMILIES, SENIORITIES, WORKPLACE)


def _nullable(schema):
    out = dict(schema)
    out["type"] = [schema["type"], "null"]
    if "enum" in schema:
        out["enum"] = list(schema["enum"]) + [None]
    return out


def _obj(properties):
    return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}


_STR_LIST = lambda n, m: {"type": "array", "maxItems": n, "items": {"type": "string", "maxLength": m}}  # noqa: E731

SCORE_JOB = _obj({
    "fit": {"type": "integer", "minimum": 0, "maximum": 100},
    "role_family": {"type": "string", "enum": ROLE_FAMILIES},
    "seniority": {"type": "string", "enum": SENIORITIES},
    "workplace": {"type": "string", "enum": WORKPLACE},
    "salary_min": _nullable({"type": "number"}),
    "salary_max": _nullable({"type": "number"}),
    "salary_period": _nullable({"type": "string", "enum": ["year", "hour"]}),
    "tags": _STR_LIST(6, 24),
    "why": {"type": "string", "maxLength": 220},
    "dealbreakers": _STR_LIST(4, 120),
})

ENRICH_COMPANY = _obj({
    "industry": {"type": "string", "enum": INDUSTRY_IDS},
    "sub_industry": {"type": "string", "maxLength": 80},
    "products": _STR_LIST(8, 60),
    "summary": {"type": "string", "maxLength": 300},
    "business_model": {"type": "string", "enum": BUSINESS_MODELS},
    "ownership": {"type": "string", "enum": OWNERSHIP},
    "parent_company": _nullable({"type": "string", "maxLength": 120}),
    "employee_band": {"type": "string", "enum": EMPLOYEE_BANDS},
    "founded_year": _nullable({"type": "integer", "minimum": 1800, "maximum": 2100}),
    "well_known": {"type": "boolean"},
    "hq_city": _nullable({"type": "string", "maxLength": 60}),
    "hq_state": _nullable({"type": "string", "maxLength": 30}),
    "tags": _STR_LIST(6, 30),
    "entity_type": {"type": "string", "enum": ENTITY_TYPES},
    "local_presence": {"type": "string", "enum": LOCAL_PRESENCE},
})

PARSE_PAGE = _obj({
    "jobs": {"type": "array", "maxItems": 40, "items": _obj({
        "title": {"type": "string", "maxLength": 160},
        "location_text": _nullable({"type": "string", "maxLength": 160}),
        "url": _nullable({"type": "string", "maxLength": 500}),
        "workplace": _nullable({"type": "string", "enum": WORKPLACE}),
        "salary_text": _nullable({"type": "string", "maxLength": 160}),
        "summary": _nullable({"type": "string", "maxLength": 300}),
    })},
})

SCHEMAS = {"score_job": SCORE_JOB, "enrich_company": ENRICH_COMPANY, "parse_page": PARSE_PAGE}


# ── validation / clamping ───────────────────────────────────────────────────

def _require(result, keys):
    if not isinstance(result, dict):
        raise ValueError("result must be an object")
    missing = [k for k in keys if k not in result]
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")


def _enum(value, allowed, default):
    v = str(value).strip().lower() if value is not None else ""
    return v if v in allowed else default


def _text(value, limit, default=""):
    if value is None:
        return default
    return str(value).strip()[:limit]


def _opt_text(value, limit):
    text = _text(value, limit, None)
    return text or None


def _strings(value, max_items, max_len):
    if not isinstance(value, list):
        return []
    out = [str(v).strip()[:max_len] for v in value if isinstance(v, (str, int, float)) and str(v).strip()]
    return list(dict.fromkeys(out))[:max_items]


def _number(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _int_in(value, lo, hi, default=None):
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def validate_score(result):
    _require(result, ["fit", "why"])
    fit = _int_in(result.get("fit"), 0, 100)
    if fit is None:
        raise ValueError("fit must be a number 0-100")
    period = result.get("salary_period")
    return {
        "fit": fit,
        "role_family": _enum(result.get("role_family"), ROLE_FAMILIES, "other"),
        "seniority": _enum(result.get("seniority"), SENIORITIES, "ic"),
        "workplace": _enum(result.get("workplace"), WORKPLACE, "unknown"),
        "salary_min": _number(result.get("salary_min")),
        "salary_max": _number(result.get("salary_max")),
        "salary_period": period if period in ("year", "hour") else None,
        "tags": _strings(result.get("tags"), 6, 24),
        "why": _text(result.get("why"), 220),
        "dealbreakers": _strings(result.get("dealbreakers"), 4, 120),
    }


def validate_enrich(result):
    _require(result, ["industry", "summary"])
    founded = _int_in(result.get("founded_year"), 1800, 2100)
    return {
        "industry": _enum(result.get("industry"), INDUSTRY_IDS, "other"),
        "sub_industry": _opt_text(result.get("sub_industry"), 80),
        "products": _strings(result.get("products"), 8, 60),
        "summary": _opt_text(result.get("summary"), 300),
        "business_model": _enum(result.get("business_model"), BUSINESS_MODELS, "unknown"),
        "ownership": _enum(result.get("ownership"), OWNERSHIP, "unknown"),
        "parent_company": _opt_text(result.get("parent_company"), 120),
        "employee_band": _enum(result.get("employee_band"), EMPLOYEE_BANDS, "unknown"),
        "founded_year": founded,
        "well_known": bool(result.get("well_known")) if result.get("well_known") is not None else None,
        "hq_city": _opt_text(result.get("hq_city"), 60),
        "hq_state": _opt_text(result.get("hq_state"), 30),
        "tags": _strings(result.get("tags"), 6, 30),
        "entity_type": _enum(result.get("entity_type"), ENTITY_TYPES, "company"),
        "local_presence": _enum(result.get("local_presence"), LOCAL_PRESENCE, "unknown"),
    }


def validate_parse_page(result):
    _require(result, ["jobs"])
    if not isinstance(result["jobs"], list):
        raise ValueError("jobs must be a list")
    jobs = []
    for j in result["jobs"][:40]:
        if not isinstance(j, dict) or not _text(j.get("title"), 160):
            continue
        wp = j.get("workplace")
        jobs.append({
            "title": _text(j.get("title"), 160),
            "location_text": _opt_text(j.get("location_text"), 160),
            "url": _opt_text(j.get("url"), 500),
            "workplace": wp if wp in WORKPLACE else None,
            "salary_text": _opt_text(j.get("salary_text"), 160),
            "summary": _opt_text(j.get("summary"), 300),
        })
    return {"jobs": jobs}


_VALIDATORS = {"score_job": validate_score, "enrich_company": validate_enrich, "parse_page": validate_parse_page}


def validate(kind, result):
    """Validate and clamp a worker result; raises ValueError when unusable."""
    if kind not in _VALIDATORS:
        raise ValueError(f"unknown task kind {kind!r}")
    return _VALIDATORS[kind](result)
