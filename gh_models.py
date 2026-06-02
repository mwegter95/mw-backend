"""
GitHub Models client for the Life Dashboard's AI smart-reminder feature.

This ports code-genius/src/server/llm.ts to Python. We call the GitHub Models
inference API (OpenAI-compatible) authenticated with a GitHub token — the same
"piggyback on your existing GitHub auth, no separate API key" approach.

This backend runs on Windows (a Surface Pro 3), so token resolution is
env-var-first and the macOS-Keychain branch from code-genius is intentionally
omitted. Token sources, in priority order:

  1. GITHUB_MODELS_TOKEN or GITHUB_TOKEN env var   (recommended on the server)
  2. `gh auth token`                                (GitHub CLI, if installed)
  3. GitHub Copilot's apps.json                     (~/.config or %LOCALAPPDATA%)

Set GITHUB_MODELS_TOKEN to a GitHub personal-access token that has Models
access. LIFE_AI_MODEL overrides the model (default gpt-4o-mini).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

GITHUB_MODELS_API = os.environ.get(
    "GITHUB_MODELS_API", "https://models.github.ai/inference"
).rstrip("/")
# GitHub Models inference uses publisher-prefixed ids (e.g. "openai/gpt-4o-mini").
# Default is gpt-4o-mini: it's NOT a reasoning model, so it reliably returns JSON
# (gpt-5-mini burned its whole token budget on reasoning and returned empty
# content here), and it has higher free-tier limits. Override with LIFE_AI_MODEL;
# gpt-5.4-mini is Copilot-only and not in this inference catalog. Confirm ids via:
#   curl -H "Authorization: Bearer <token>" https://models.github.ai/catalog/models
DEFAULT_MODEL = os.environ.get("LIFE_AI_MODEL", "openai/gpt-4o-mini")

_TOKEN_CACHE = {"token": None, "ts": 0.0}
_TOKEN_TTL = 300  # re-resolve at most every 5 minutes


class GitHubModelsError(RuntimeError):
    """Raised when the Models API is unreachable, unauthorized, or errors out.
    Carries the HTTP status and any Retry-After (seconds) for 429 handling."""

    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


# ── Token resolution ──────────────────────────────────────────────────────────

def _from_env():
    return os.environ.get("GITHUB_MODELS_TOKEN") or os.environ.get("GITHUB_TOKEN") or None


def _from_gh_cli():
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=4,
        )
        tok = (out.stdout or "").strip()
        return tok or None
    except Exception:
        return None


def _from_copilot_apps():
    candidates = [Path.home() / ".config" / "github-copilot" / "apps.json"]
    for env_var in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        base = os.environ.get(env_var)
        if base:
            candidates.append(Path(base) / "github-copilot" / "apps.json")
    seen = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if not path.exists():
                continue
            apps = json.loads(path.read_text(encoding="utf-8"))
            entries = list(apps.values()) if isinstance(apps, dict) else []
            # Prefer ghu_ tokens (GitHub App user tokens) — they have API access.
            for entry in entries:
                tok = (entry or {}).get("oauth_token")
                if tok and tok.startswith("ghu_"):
                    return tok
            for entry in entries:
                tok = (entry or {}).get("oauth_token")
                if tok and len(tok) > 10:
                    return tok
        except Exception:
            continue
    return None


def resolve_token(force=False):
    now = time.time()
    if not force and _TOKEN_CACHE["token"] and (now - _TOKEN_CACHE["ts"] < _TOKEN_TTL):
        return _TOKEN_CACHE["token"]
    token = _from_env() or _from_gh_cli() or _from_copilot_apps()
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["ts"] = now
    return token


# ── Chat completion (non-streaming; the generator just needs the final text) ──

def _uses_completion_tokens(model):
    """GPT-5 family + o-series reasoning models use `max_completion_tokens` and
    reject custom `temperature`/`top_p`."""
    m = (model or "").lower()
    return m.startswith("gpt-5") or "gpt-5" in m or m.startswith(("o1", "o3", "o4"))


def _post_chat(body, timeout):
    token = resolve_token()
    if not token:
        raise GitHubModelsError(
            "No GitHub token available for the Models API. Set GITHUB_MODELS_TOKEN "
            "(or GITHUB_TOKEN) on the server, or authenticate the GitHub CLI."
        )
    req = urllib.request.Request(
        f"{GITHUB_MODELS_API}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:600]
        retry_after = None
        try:
            ra = e.headers.get("Retry-After") or e.headers.get("retry-after")
            if ra:
                retry_after = int(float(ra))
        except Exception:
            pass
        raise GitHubModelsError(f"GitHub Models API {e.code}: {detail}",
                                status=e.code, retry_after=retry_after)
    except urllib.error.URLError as e:
        raise GitHubModelsError(f"GitHub Models API unreachable: {e}")
    choices = payload.get("choices") or [{}]
    return ((choices[0].get("message") or {}).get("content") or "")


def chat_completion(
    messages,
    model=None,
    temperature=0.3,
    max_tokens=2000,
    json_object=False,
    timeout=60,
):
    """POST to the OpenAI-compatible chat/completions endpoint and return the
    assistant message text. Adapts params to the model family and falls back to
    a minimal body if the endpoint rejects an optional parameter."""
    model = model or DEFAULT_MODEL
    body = {"model": model, "messages": messages, "stream": False}
    if json_object:
        body["response_format"] = {"type": "json_object"}
    if _uses_completion_tokens(model):
        body["max_completion_tokens"] = max_tokens   # GPT-5 / reasoning family
    else:
        body["max_tokens"] = max_tokens
        body["temperature"] = temperature
        body["top_p"] = 1

    # Up to 2 retries on 429 (free-tier rate limit). Respect Retry-After when
    # it's short; if it's long (a daily quota) don't block — raise so the caller
    # can surface it.
    for attempt in range(3):
        try:
            return _post_chat(body, timeout)
        except GitHubModelsError as e:
            if e.status == 429 and attempt < 2:
                wait = e.retry_after if e.retry_after is not None else 5 * (attempt + 1)
                if wait > 60:
                    raise          # long/daily limit — pointless to sleep on it
                time.sleep(wait)
                continue
            msg = str(e).lower()
            # Some deployments reject specific params — retry once, stripped down.
            param_err = "400" in msg and any(
                k in msg for k in (
                    "temperature", "top_p", "max_tokens", "max_completion_tokens",
                    "response_format", "unsupported", "unknown", "not supported",
                )
            )
            if not param_err:
                raise
            minimal = {"model": model, "messages": messages, "stream": False}
            minimal["max_completion_tokens" if _uses_completion_tokens(model) else "max_tokens"] = max_tokens
            return _post_chat(minimal, timeout)


def health():
    """Quick check used by /api/life/ai/health so the token can be verified on
    the Surface without running a full generation."""
    token = resolve_token(force=True)
    if not token:
        return {"available": False, "model": DEFAULT_MODEL, "error": "No GitHub token found"}
    try:
        sample = chat_completion([{"role": "user", "content": "ping"}], max_tokens=5, timeout=30)
        return {"available": True, "model": DEFAULT_MODEL, "sample": sample[:40]}
    except GitHubModelsError as e:
        return {"available": False, "model": DEFAULT_MODEL, "error": str(e)}
