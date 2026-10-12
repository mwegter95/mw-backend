"""
Inbox sorting on wegter-pc's own AI (LM Studio) — Flask Blueprint at /jobs/local-ai.

Registered by jobscout_server.py, so it runs on wegter-pc beside LM Studio and is published through
wegter-pc's tunnel at https://jobs.michaelwegter.com/jobs/local-ai. The Surface never talks to it:
the inbox dashboard in the browser carries the emails here (from the Surface's /inbox/triage/export),
a few at a time, and posts the answers back to the Surface's /inbox/triage/apply. LM Studio stays
on localhost, exactly as JOBSCOUT_SETUP.md sets it up.

  GET  /jobs/local-ai/health            is LM Studio up, and is the configured model there?
  POST /jobs/local-ai/inbox/sort        {prompt, emails, ids} → {actions, summary, model, seconds}
  POST /jobs/local-ai/inbox/summarize   {lines} → {summary}

Owner only: a valid michaelwegter.com sign-in (checked like Job Scout's, against the Surface's
/auth/me) whose email is INBOX_OWNER_EMAIL. Uses Job Scout's LM Studio settings
(JOBS_AI_API_BASE, JOBS_AI_MODEL, JOBS_AI_API_KEY).

Every call finishes in under ~90 s, inside Cloudflare's 100 s limit — that is why the browser sends
a handful of emails per call. Failures answer 424 (never 502/504: Cloudflare swaps those for a page
without CORS headers) or 409 when LM Studio is already busy with an inbox sort.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from functools import wraps

import requests
from flask import Blueprint, g, jsonify, request

from jobscout import ai as jobs_ai
from jobscout import config as jobs_config
from jobscout.auth import require_user

log = logging.getLogger(__name__)

local_ai_bp = Blueprint("inbox_local_ai", __name__, url_prefix="/jobs/local-ai")

OWNER_EMAIL = (os.environ.get("INBOX_OWNER_EMAIL") or "zweetztuph@gmail.com").strip().lower()
MAX_EMAILS_PER_CALL = 12
MAX_TEXT = 60000
CALL_TIMEOUT = 85          # seconds; Cloudflare gives up at 100
TOKENS_PER_EMAIL = 220
FAILED = 424
IMPORTANCE = ["high", "medium", "low"]

_busy = threading.Lock()   # one inbox call at a time on the GPU


def require_owner(f):
    """Job Scout's sign-in check, then: only the inbox's owner."""
    @wraps(f)
    def check(*args, **kwargs):
        if (g.jobs_user.get("email") or "").lower() != OWNER_EMAIL:
            return jsonify({"error": "This is private."}), 403
        return f(*args, **kwargs)
    return require_user(check)


def sort_schema(ids: list[str]) -> dict:
    """Strict JSON schema: exactly one action per email, ids limited to the ones sent."""
    return {
        "type": "object", "additionalProperties": False, "required": ["actions", "summary"],
        "properties": {
            "actions": {
                "type": "array", "minItems": len(ids), "maxItems": len(ids),
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["id", "summary", "labels", "importance", "personal", "archive_ok"],
                    "properties": {
                        "id": {"type": "string", "enum": ids},
                        "summary": {"type": "string", "maxLength": 300},
                        "labels": {"type": "array", "minItems": 1, "maxItems": 2,
                                   "items": {"type": "string", "maxLength": 80}},
                        "importance": {"type": "string", "enum": IMPORTANCE},
                        "personal": {"type": "boolean"},
                        "archive_ok": {"type": "boolean"},
                    },
                },
            },
            "summary": {"type": "string", "maxLength": 600},
        },
    }


SUMMARY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary"],
    "properties": {"summary": {"type": "string", "maxLength": 600}},
}

SUMMARY_PROMPT = """You write the note at the top of Michael's inbox after his email was sorted.
Given one line per email (sender, subject, importance, what it is), write 2-3 plain sentences: what needs
Michael and by when, then what was routine. No greeting, no lists. Answer with JSON only."""


class LocalAIError(RuntimeError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def complete(messages: list[dict], schema: dict, name: str, max_tokens: int, timeout: float = CALL_TIMEOUT):
    """One structured completion from LM Studio → (dict, model id). Raises LocalAIError."""
    base = jobs_config.ai_api_base().rstrip("/")
    model = jobs_config.ai_model()
    body = {
        "model": model, "temperature": 0.2, "max_tokens": max_tokens, "messages": messages,
        "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
    }
    try:
        resp = requests.post(f"{base}/chat/completions", json=body, timeout=timeout,
                             headers={"Authorization": f"Bearer {jobs_config.ai_api_key()}"})
    except requests.Timeout:
        raise LocalAIError("wegter-pc's model took too long. It may be busy with Job Scout; try again in a minute.",
                           "timeout")
    except requests.ConnectionError:
        raise LocalAIError("LM Studio isn't answering on wegter-pc. Open LM Studio and start its server "
                           "(Developer tab, port 1234).", "lmstudio_down")
    if resp.status_code >= 400:
        detail = resp.text[:300].strip()
        raise LocalAIError(f"LM Studio answered HTTP {resp.status_code}: {detail}", "lmstudio_error")
    try:
        data = resp.json()
    except ValueError:
        raise LocalAIError("LM Studio's answer wasn't JSON.", "bad_answer")
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    try:
        return jobs_ai.parse_json_content(content), data.get("model") or model
    except ValueError:
        if choice.get("finish_reason") == "length" or not jobs_ai._REASONING.sub("", content).strip():
            raise LocalAIError(f"The model ran out of room before finishing — {jobs_ai.THINKING_HINT}.", "cut_off")
        raise LocalAIError("The model's answer wasn't valid JSON. Try again.", "bad_answer")


def _fail(e: LocalAIError):
    return jsonify({"error": str(e), "code": e.code}), FAILED


@local_ai_bp.get("/health")
@require_owner
def health():
    ok, models = jobs_ai.health()
    model = jobs_config.ai_model()
    return jsonify({"ok": ok, "model": model, "model_ready": ok and (not models or model in models),
                    "models": models, "busy": _busy.locked()})


@local_ai_bp.post("/inbox/sort")
@require_owner
def inbox_sort():
    body = request.get_json(silent=True) or {}
    prompt = str(body.get("prompt") or "").strip()
    emails = str(body.get("emails") or "").strip()
    ids = []
    for i in body.get("ids") or []:
        i = str(i).strip()
        if i and i not in ids:
            ids.append(i)
    if not prompt or not emails or not ids:
        return jsonify({"error": "Send the prompt, the emails and their ids."}), 400
    if len(ids) > MAX_EMAILS_PER_CALL or len(emails) + len(prompt) > MAX_TEXT:
        return jsonify({"error": f"Send at most {MAX_EMAILS_PER_CALL} emails per call."}), 413
    if not _busy.acquire(blocking=False):
        return jsonify({"error": "wegter-pc is already sorting. Wait for it to finish.", "code": "busy"}), 409
    started = time.monotonic()
    try:
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": f"EMAILS ({len(ids)})\n\n{emails}"}]
        result, model = complete(messages, sort_schema(ids), "inbox_sort",
                                 max_tokens=min(4096, 400 + TOKENS_PER_EMAIL * len(ids)))
    except LocalAIError as e:
        log.info("[local-ai] inbox sort of %d emails failed: %s", len(ids), e)
        return _fail(e)
    finally:
        _busy.release()
    actions, seen = [], set()
    for a in result.get("actions") or []:
        if isinstance(a, dict) and a.get("id") in ids and a["id"] not in seen:
            seen.add(a["id"])
            actions.append({
                "id": a["id"],
                "summary": str(a.get("summary") or "")[:300],
                "labels": [str(x).strip()[:80] for x in (a.get("labels") or []) if str(x).strip()][:2],
                "importance": a.get("importance") if a.get("importance") in IMPORTANCE else "low",
                "personal": a.get("personal") is True,
                "archive_ok": a.get("archive_ok") is True,
            })
    return jsonify({"actions": actions, "summary": str(result.get("summary") or "")[:600], "model": model,
                    "seconds": round(time.monotonic() - started, 1), "missing": [i for i in ids if i not in seen]})


@local_ai_bp.post("/inbox/summarize")
@require_owner
def inbox_summarize():
    body = request.get_json(silent=True) or {}
    lines = [str(x).strip()[:400] for x in (body.get("lines") or []) if str(x).strip()][:100]
    if not lines:
        return jsonify({"error": "Nothing to summarize."}), 400
    if not _busy.acquire(blocking=False):
        return jsonify({"error": "wegter-pc is busy.", "code": "busy"}), 409
    try:
        result, _ = complete([{"role": "system", "content": SUMMARY_PROMPT},
                              {"role": "user", "content": "\n".join(lines)}],
                             SUMMARY_SCHEMA, "inbox_summary", max_tokens=600, timeout=60)
    except LocalAIError as e:
        return _fail(e)
    finally:
        _busy.release()
    return jsonify({"summary": str(result.get("summary") or "")[:600]})
