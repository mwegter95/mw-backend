"""OpenAI-compatible client used by the AI worker (LM Studio on wegter-pc).

chat_json(kind, payload) → (result dict, model id). Requests use response_format json_schema
(strict) with the schemas in ai_schemas; content is parsed defensively (``` fences, stray text).
Prompt order for score_job is system rubric → PROFILE → JOB so llama.cpp can reuse the cached
profile prefix across jobs.
"""
import json
import logging
import re

import requests

from . import ai_schemas, config, taxonomy

log = logging.getLogger("jobscout")

# Caps, not budgets: the JSON answers are a few hundred tokens, but if the model's "thinking" (LM Studio's
# Reasoning setting) is on, its reasoning counts against max_tokens too and a tight cap cuts the JSON off.
MAX_TOKENS = {"score_job": 1500, "enrich_company": 1500, "parse_page": 4000}
# When an answer is cut off at the cap (finish_reason "length"), ask once more with this much room.
RETRY_MAX_TOKENS = 6144
RETRY_TIMEOUT = 420  # seconds; thousands of reasoning tokens take minutes on a 12 GB card
THINKING_HINT = "turn off Reasoning in this model's settings in LM Studio"
# Reasoning blocks some models (Gemma 4 with thinking on, Qwen, DeepSeek) put in the content.
# (A block with no end means the budget ran out mid-thought.)
_REASONING = re.compile(r"<think>.*?(?:</think>|\Z)|<\|channel\|?>\s*thought.*?(?:<\|?channel\|>|\Z)|<\|think\|>",
                        re.S | re.I)

SCORE_SYSTEM = """You are a careful career advisor scoring how well ONE job fits ONE candidate.
The candidate says what they want in their own words (want_text), what to avoid (avoid_text), the kinds of
jobs they are looking for (job_categories and target_titles) and the levels they want (levels; empty = any).
Score "fit" 0-100:
 90-100 near-perfect match of role, level, industry and compensation;
 70-89 strong match; 50-69 plausible stretch; 30-49 weak; below 30 wrong kind of job or level.
Weigh the want statement heavily. Penalize anything on the avoid list and pay clearly below their salary floor.
Reward industries they want and lesser-known local employers ("hidden gem") modestly.
role_family: the job category the posting belongs to: {categories}, or other.
seniority: exec|director|manager|senior_ic|ic|intern. workplace: onsite|hybrid|remote|unknown (from the posting).
salary_min/salary_max/salary_period: the posted base pay if the text states it (else null); period year or hour.
tags: up to 6 short facts useful at a glance (e.g. "B2B", "team of 3", "hybrid 2 days").
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
false for a 200-person regional company most people have never heard of, in any industry).
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
        system = SCORE_SYSTEM.format(categories="|".join(taxonomy.JOB_CATEGORY_IDS))
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]
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
    text = _REASONING.sub("", text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


class AnswerCutOff(ValueError):
    """The model stopped at max_tokens before finishing its JSON (or before writing any)."""


def _reasoning_tokens(data):
    details = (data.get("usage") or {}).get("completion_tokens_details") or {}
    return details.get("reasoning_tokens")


def chat_json(kind, payload, base=None, model=None, api_key=None, timeout=180, session=None):
    """One structured completion → (result dict, model id). Raises on HTTP/parse errors.

    If the answer is cut off at max_tokens — which is what happens when the model's thinking is on and
    uses up the budget — it asks once more with RETRY_MAX_TOKENS before giving up with AnswerCutOff."""
    base = (base or config.ai_api_base()).rstrip("/")
    model = model or config.ai_model()
    http = session or requests
    attempts = ((MAX_TOKENS[kind], timeout), (max(RETRY_MAX_TOKENS, MAX_TOKENS[kind]), max(timeout, RETRY_TIMEOUT)))
    for n, (cap, wait) in enumerate(attempts):
        last = n == len(attempts) - 1
        body = {
            "model": model, "temperature": 0.2, "max_tokens": cap, "messages": build_messages(kind, payload),
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": kind, "strict": True, "schema": ai_schemas.SCHEMAS[kind]}},
        }
        resp = http.post(f"{base}/chat/completions", json=body, timeout=wait,
                         headers={"Authorization": f"Bearer {api_key or config.ai_api_key()}"})
        resp.raise_for_status()
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        cut_off = choice.get("finish_reason") == "length"
        text = _REASONING.sub("", content).strip()
        error = None
        try:
            if text:
                return parse_json_content(content), data.get("model") or model
        except ValueError as exc:
            error = exc
        # No answer, or JSON that doesn't parse: almost always the budget ran out mid-answer.
        if not last:
            log.info("ai: %s answer %s at %d tokens%s; retrying with %d", kind,
                     "cut off" if cut_off else "unusable", cap, _thinking_note(data), attempts[-1][0])
            continue
        if not text:
            raise AnswerCutOff(f"model returned no answer within {cap} tokens{_thinking_note(data)} — "
                               f"{THINKING_HINT}")
        if cut_off:
            raise AnswerCutOff(f"answer cut off at {cap} tokens{_thinking_note(data)} — {THINKING_HINT}") from error
        raise error
    raise AssertionError("unreachable")


def _thinking_note(data):
    used = _reasoning_tokens(data)
    return f" ({used} spent thinking)" if used else ""


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
