"""OpenAI-compatible client used by the AI worker (LM Studio on wegter-pc).

chat_json(kind, payload) → (result dict, model id). Requests use response_format json_schema
(strict) with the schemas in ai_schemas; content is parsed defensively (``` fences, stray text).
Prompt order for score_job is system rubric → PROFILE → JOB so llama.cpp can reuse the cached
profile prefix across jobs.
"""
import json
import re

import requests

from . import ai_schemas, config, taxonomy

MAX_TOKENS = {"score_job": 700, "enrich_company": 800, "parse_page": 3000}

SCORE_SYSTEM = """You are a careful career advisor scoring how well ONE job fits ONE candidate.
Score "fit" 0-100:
 90-100 near-perfect match of title, level, industry and compensation;
 70-89 strong match; 50-69 plausible stretch; 30-49 weak; below 30 wrong function or level.
Penalize anything on the candidate's avoid list and pay clearly below their salary floor.
Reward industries they want and lesser-known local employers ("hidden gem") modestly.
role_family: marketing|communications|brand|content|demand_gen|product_marketing|pr|digital|other.
seniority: exec|director|manager|senior_ic|ic|intern. workplace: onsite|hybrid|remote|unknown (from the posting).
salary_min/salary_max/salary_period: the posted base pay if the text states it (else null); period year or hour.
tags: up to 6 short facts useful at a glance (e.g. "B2B", "team of 3", "brand + demand gen").
why: one or two plain sentences (<= 220 chars) on the fit. dealbreakers: up to 4 concrete conflicts, else [].
Answer with JSON only."""

ENRICH_SYSTEM = """You categorize an organization from text taken from its own website.
The site might NOT be a normal company site: judge from the text whether it is a business directory or listing
site, a news/media article site, a government body, a school, a single store/branch page of a chain, or a
franchise location.
entity_type: company|directory|news|government|school|retail_location|franchise_location|other.
local_presence (relative to the Minneapolis-St. Paul metro and western Wisconsin): hq (headquartered there),
major_office (large office/plant there, HQ elsewhere), branch (a store/branch of a larger chain), none, unknown.
industry must be one of these ids:
{industries}
employee_band: 1-49|50-199|200-999|1000-4999|5000+|unknown.
ownership: private|family|pe_backed|public|subsidiary|nonprofit|government|cooperative|unknown.
business_model: b2b|b2c|b2b2c|mixed|nonprofit|public_sector|unknown.
well_known: would an average Twin Cities adult recognize the name? (true for 3M, Target, Andersen Windows;
false for a 200-person injection molder).
summary: <= 300 chars, factual, what they make/do and for whom. products: up to 8 short product/service names.
Use null or "unknown" when the text does not say. Answer with JSON only."""

PARSE_SYSTEM = """You extract job postings from the text of a company's careers page.
Return only real, currently open positions listed on the page (at most 40). Ignore navigation, benefits text,
blog posts and generic "join our talent community" links. Use null for anything the text does not state.
Answer with JSON only."""


def _block(title, data):
    lines = [title]
    for key, value in data.items():
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {value if value not in (None, '') else '-'}")
    return "\n".join(lines)


def build_messages(kind, payload):
    if kind == "score_job":
        user = _block("PROFILE", payload["profile"]) + "\n\n" + _block("JOB", payload["job"])
        return [{"role": "system", "content": SCORE_SYSTEM}, {"role": "user", "content": user}]
    if kind == "enrich_company":
        industries = "\n".join(f"  {i} = {label} ({group})" for i, label, group in taxonomy.INDUSTRIES)
        return [{"role": "system", "content": ENRICH_SYSTEM.format(industries=industries)},
                {"role": "user", "content": _block("ORGANIZATION", payload)}]
    if kind == "parse_page":
        user = f"COMPANY: {payload['company_name']}\nPAGE: {payload['page_url']}\n\n{payload['page_text']}"
        return [{"role": "system", "content": PARSE_SYSTEM}, {"role": "user", "content": user}]
    raise ValueError(f"unknown task kind {kind!r}")


def parse_json_content(text):
    """Parse model output as a JSON object, tolerating ``` fences and leading/trailing prose."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def chat_json(kind, payload, base=None, model=None, api_key=None, timeout=180, session=None):
    """One structured completion → (result dict, model id). Raises on HTTP/parse errors."""
    base = (base or config.ai_api_base()).rstrip("/")
    model = model or config.ai_model()
    body = {
        "model": model, "temperature": 0.2, "max_tokens": MAX_TOKENS[kind], "messages": build_messages(kind, payload),
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": kind, "strict": True, "schema": ai_schemas.SCHEMAS[kind]}},
    }
    http = session or requests
    resp = http.post(f"{base}/chat/completions", json=body, timeout=timeout,
                     headers={"Authorization": f"Bearer {api_key or config.ai_api_key()}"})
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return parse_json_content(content), data.get("model") or model


def health(base=None, timeout=5, session=None):
    """(ok, [model ids]) from GET {base}/models."""
    base = (base or config.ai_api_base()).rstrip("/")
    try:
        resp = (session or requests).get(f"{base}/models", timeout=timeout,
                                         headers={"Authorization": f"Bearer {config.ai_api_key()}"})
        resp.raise_for_status()
        return True, [m.get("id") for m in resp.json().get("data") or []]
    except (requests.RequestException, ValueError):
        return False, []
