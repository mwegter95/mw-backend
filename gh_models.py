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
    "GITHUB_MODELS_API", "https://models.inference.ai.azure.com"
).rstrip("/")
DEFAULT_MODEL = os.environ.get("LIFE_AI_MODEL", "gpt-4o-mini")

_TOKEN_CACHE = {"token": None, "ts": 0.0}
_TOKEN_TTL = 300  # re-resolve at most every 5 minutes


class GitHubModelsError(RuntimeError):
    """Raised when the Models API is unreachable, unauthorized, or errors out."""


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

def chat_completion(
    messages,
    model=None,
    temperature=0.3,
    max_tokens=2000,
    json_object=False,
    timeout=60,
):
    """POST to the OpenAI-compatible chat/completions endpoint and return the
    assistant message text. Set json_object=True to ask for a JSON response."""
    token = resolve_token()
    if not token:
        raise GitHubModelsError(
            "No GitHub token available for the Models API. Set GITHUB_MODELS_TOKEN "
            "(or GITHUB_TOKEN) on the server, or authenticate the GitHub CLI."
        )

    body = {
        "model": model or DEFAULT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": 1,
        "stream": False,
    }
    if json_object:
        body["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        f"{GITHUB_MODELS_API}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:500]
        raise GitHubModelsError(f"GitHub Models API {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise GitHubModelsError(f"GitHub Models API unreachable: {e}")

    choices = payload.get("choices") or [{}]
    return ((choices[0].get("message") or {}).get("content") or "")


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
