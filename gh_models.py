"""
AI client for the Life Dashboard's smart-reminder feature.

Historically this called the GitHub Models inference API. **GitHub Models was
fully retired on July 30, 2026** — the playground, catalog and inference API all
went away, and calls now return `410 github_models_retirement_brownout`. Nothing
about that is temporary despite the word "brownout".

The module kept its name so the rest of the app didn't have to move, but it is
now provider-agnostic. Pick with `LIFE_AI_PROVIDER`:

  copilot  (default)  the official GitHub Copilot SDK — GA since June 2026 and
                      covered by any Copilot plan, including Copilot Free. It
                      drives the Copilot CLI runtime, so it is a supported path
                      rather than one of the reverse-engineered proxies that put
                      your Copilot access at risk.
  openai              any OpenAI-compatible /chat/completions endpoint (OpenAI,
                      Groq, OpenRouter, Azure Foundry, Ollama, …). Kept as an
                      escape hatch so switching providers is an env change, not
                      another rewrite.

Environment:
  LIFE_AI_PROVIDER      "copilot" (default) or "openai"
  LIFE_AI_MODEL         copilot: "auto" (default), "gpt-5", "claude-sonnet-4.5", …
                        openai:  whatever that endpoint calls the model
  LIFE_AI_GITHUB_TOKEN  copilot: optional. Left unset, the SDK uses whoever is
                        logged in to the Copilot CLI — which is the normal setup
                        on the server. Do NOT point this at an old
                        GITHUB_MODELS_TOKEN; that scope is gone.
  LIFE_AI_API_BASE      openai: base URL, e.g. https://api.groq.com/openai/v1
  LIFE_AI_API_KEY       openai: bearer key (falls back to OPENAI_API_KEY)

Prerequisite for the copilot provider: `pip install github-copilot-sdk` (Python
3.11+) and a Copilot CLI runtime — `python -m copilot download-runtime`, or an
already-authenticated `copilot` on PATH. Verify with GET /api/life/ai/health.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

PROVIDER = (os.environ.get("LIFE_AI_PROVIDER") or "copilot").strip().lower()

# Cheapest selectable model in Copilot's catalog as of 2026-09 ($0.20 / $1.25 per
# 1M tokens in / out), which is what this job wants: it classifies a handful of
# short calendar-event titles once a day. Note that gpt-4o-mini is NOT an option
# here — Copilot runs it only as an internal utility model and won't let you
# select it — and that Copilot Free and Student plans get auto selection only.
# Both cases are handled by the fallback in _CopilotRuntime.ask.
COPILOT_DEFAULT_MODEL = "gpt-5.4-nano"
_DEFAULT_MODEL_BY_PROVIDER = {"copilot": COPILOT_DEFAULT_MODEL, "openai": "gpt-4o-mini"}

# Ids that can't be selected in Copilot: retired GitHub Models spellings, and
# OpenAI models Copilot only uses internally. Asking for one fails, so translate.
_COPILOT_UNSELECTABLE = {"gpt-4o-mini", "gpt-4o", "o1-mini", "o3-mini", "gpt-4", "gpt-4-turbo"}
DEFAULT_MODEL = (
    os.environ.get("LIFE_AI_MODEL")
    or _DEFAULT_MODEL_BY_PROVIDER.get(PROVIDER, "auto")
).strip()

# OpenAI-compatible provider settings.
API_BASE = (os.environ.get("LIFE_AI_API_BASE") or "https://api.openai.com/v1").rstrip("/")
API_KEY = os.environ.get("LIFE_AI_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""

# Copilot provider settings. Token resolution lives in _resolve_copilot_token().
COPILOT_TOKEN = os.environ.get("LIFE_AI_GITHUB_TOKEN") or ""


# ── Copilot authentication ────────────────────────────────────────────────────
# The SDK spawns the Copilot CLI, and that process needs a GitHub token of its
# own. It reads COPILOT_GITHUB_TOKEN, then GH_TOKEN, then GITHUB_TOKEN, and
# otherwise falls back to a `copilot login` credential in the OS keychain (or an
# authenticated `gh`). A server running as a service often can't see that
# keychain entry — the login belongs to an interactive desktop session — which
# surfaces as:
#
#   session error: execution failed: invalidArg,
#   No github oauth token or copilot hmac key provided
#
# So resolve a token here and hand it over explicitly, rather than hoping the
# spawned process finds one.
#
# Accepted token types (github/copilot-cli):
#   gho_         OAuth, what `copilot login` mints
#   ghu_         GitHub App user-to-server, what the editor extensions store
#   github_pat_  fine-grained PAT — needs the "Copilot Requests" ACCOUNT
#                permission, and must be personal rather than org-owned
#   ghp_         CLASSIC PAT — explicitly NOT supported, whatever its scopes
_COPILOT_TOKEN_ENV_VARS = (
    "LIFE_AI_GITHUB_TOKEN",   # ours, wins so this app can differ from the rest
    "COPILOT_GITHUB_TOKEN",   # the CLI's own first choice
    "GH_TOKEN",
    "GITHUB_TOKEN",
)


def _from_gh_cli():
    """An authenticated `gh` is a perfectly good token source and is often
    already set up on a box that deploys with git."""
    try:
        out = subprocess.run(["gh", "auth", "token"],
                             capture_output=True, text=True, timeout=5)
        return (out.stdout or "").strip() or None
    except Exception:
        return None


def _from_copilot_apps():
    """Copilot's editor extensions cache a ghu_ user-to-server token in
    apps.json. If the Surface has Copilot in VS Code, this is already there."""
    candidates = [Path.home() / ".config" / "github-copilot" / "apps.json"]
    for env_var in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        base = os.environ.get(env_var)
        if base:
            candidates.append(Path(base) / "github-copilot" / "apps.json")
    seen = set()
    for path in candidates:
        if str(path) in seen:
            continue
        seen.add(str(path))
        try:
            if not path.exists():
                continue
            apps = json.loads(path.read_text(encoding="utf-8"))
            entries = list(apps.values()) if isinstance(apps, dict) else []
            for want_prefix in ("ghu_", "gho_", ""):
                for entry in entries:
                    tok = (entry or {}).get("oauth_token") or ""
                    if tok and tok.startswith(want_prefix) and len(tok) > 10:
                        return tok
        except Exception:
            continue
    return None


def _warn_bad_token_type(token, source):
    if token.startswith("ghp_"):
        log.warning(
            "[life-ai] %s holds a classic PAT (ghp_). Copilot CLI does not "
            "accept classic PATs at all, whatever scopes they carry. Use a "
            "fine-grained PAT with the 'Copilot Requests' account permission, "
            "or run `copilot login`.", source)
        return False
    return True


def _resolve_copilot_token():
    """Return (token, source). Either may be None — no token is a valid state
    when `copilot login` has stored a credential the server process can read."""
    for var in _COPILOT_TOKEN_ENV_VARS:
        tok = (os.environ.get(var) or "").strip()
        if tok:
            _warn_bad_token_type(tok, var)
            return tok, var
    tok = _from_gh_cli()
    if tok and _warn_bad_token_type(tok, "`gh auth token`"):
        return tok, "gh-cli"
    tok = _from_copilot_apps()
    if tok:
        return tok, "copilot-apps.json"
    return None, None


class AIProviderError(RuntimeError):
    """The AI provider was unreachable, unauthorized, or returned an error.
    Carries the HTTP status and any Retry-After (seconds) for 429 handling."""

    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


# life_smart.py catches this by name; keep the old spelling working.
GitHubModelsError = AIProviderError


def _model_for_copilot(model):
    """Normalise a configured model id to something Copilot will actually take.

    GitHub Models used publisher-prefixed ids ("openai/gpt-4o-mini"); Copilot
    does not. And gpt-4o/gpt-4o-mini are not selectable in Copilot at all — they
    power background features internally. Either would otherwise fail on every
    call with an opaque error, so translate and say so."""
    m = (model or "").strip()
    if not m:
        return COPILOT_DEFAULT_MODEL
    bare = m.split("/", 1)[1] if "/" in m else m
    if bare.lower() in _COPILOT_UNSELECTABLE:
        log.warning(
            "[life-ai] LIFE_AI_MODEL=%r isn't selectable in Copilot (it's a "
            "retired GitHub Models id or an internal-only model). Using %s "
            "instead — the cheapest model Copilot does expose.",
            m, COPILOT_DEFAULT_MODEL)
        return COPILOT_DEFAULT_MODEL
    if "/" in m:
        log.warning(
            "[life-ai] LIFE_AI_MODEL=%r carries a publisher prefix; Copilot "
            "doesn't use those. Trying %r.", m, bare)
    return bare


# ── Copilot SDK backend ───────────────────────────────────────────────────────

class _CopilotRuntime:
    """Owns one asyncio loop on a background thread, plus one CopilotClient.

    Two reasons it's shaped this way. The SDK is async while everything calling
    into it (Flask request threads, the nightly scheduler thread) is not, so
    coroutines get marshalled onto a loop that belongs to nobody in particular.
    And starting the client spins up the Copilot CLI runtime as a subprocess —
    far too slow to pay per batch on a Surface Pro 3 — so it starts once, lazily,
    and is reused for the life of the process.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._loop = None
        self._thread = None
        self._client = None
        self._auth_source = None
        # Set once a pinned model is refused, so the fallback is paid for once
        # per process instead of on every call.
        self._forced_auto = False

    def _ensure_loop(self):
        if self._loop is not None:
            return
        loop = asyncio.new_event_loop()
        t = threading.Thread(
            target=loop.run_forever, name="life-ai-copilot", daemon=True)
        t.start()
        self._loop, self._thread = loop, t

    def _import_sdk(self):
        try:
            from copilot import CopilotClient
            from copilot.session import PermissionHandler
        except ImportError as e:
            raise AIProviderError(
                "The GitHub Copilot SDK isn't installed. Run "
                "`pip install github-copilot-sdk` and "
                "`python -m copilot download-runtime` on the server, or set "
                "LIFE_AI_PROVIDER=openai to use a different provider."
            ) from e
        return CopilotClient, PermissionHandler

    async def _start_client(self):
        CopilotClient, _ = self._import_sdk()
        token, source = _resolve_copilot_token()
        self._auth_source = source or "copilot-cli-login"
        kwargs = {}
        if token:
            kwargs["github_token"] = token
            # Belt and braces. The SDK is supposed to forward the constructor
            # token to the spawned CLI via COPILOT_SDK_AUTH_TOKEN, but there are
            # open reports of that forwarding not landing, and the CLI reads
            # COPILOT_GITHUB_TOKEN from its inherited environment regardless.
            # Setting both costs nothing and closes that gap.
            os.environ.setdefault("COPILOT_GITHUB_TOKEN", token)
        client = CopilotClient(**kwargs)
        await client.start()
        return client

    def _get_client(self):
        """Start the client on the loop thread, once. Callers hold no lock while
        awaiting, so a failed start doesn't wedge the next attempt."""
        with self._lock:
            self._ensure_loop()
            if self._client is not None:
                return self._client
            fut = asyncio.run_coroutine_threadsafe(self._start_client(), self._loop)
            try:
                self._client = fut.result(timeout=120)
            except AIProviderError:
                raise
            except Exception as e:
                raise AIProviderError(
                    f"Couldn't start the Copilot runtime: {e.__class__.__name__}: {e}. "
                    "Check that `copilot --version` works on the server and that "
                    "the CLI is signed in to a GitHub account with Copilot."
                ) from e
            return self._client

    async def _ask(self, prompt, model):
        client = self._client
        _, PermissionHandler = self._import_sdk()
        # No tools are offered, so approve_all can't actually approve anything
        # interesting — it just keeps a prompt from hanging on a permission ask.
        async with await client.create_session(
            on_permission_request=PermissionHandler.approve_all,
            model=model,
        ) as session:
            resp = await session.send_and_wait(prompt)
            return _response_text(resp)

    def _ask_sync(self, prompt, model, timeout):
        self._get_client()
        fut = asyncio.run_coroutine_threadsafe(self._ask(prompt, model), self._loop)
        try:
            # Grace on top of the caller's budget: the SDK may still be handing
            # back the last of a stream when the nominal timeout lands.
            return fut.result(timeout=max(30, timeout) + 20)
        except AIProviderError:
            raise
        except TimeoutError as e:
            fut.cancel()
            raise AIProviderError(f"Copilot timed out after {timeout}s") from e
        except Exception as e:
            raise AIProviderError(
                f"Copilot request failed: {e.__class__.__name__}: {e}") from e

    def ask(self, prompt, model, timeout):
        if self._forced_auto:
            model = "auto"
        try:
            return self._ask_sync(prompt, model, timeout)
        except AIProviderError as e:
            # Two plausible reasons a pinned model is refused: the id is wrong
            # (Copilot's catalog moves fast), or this is a Copilot Free/Student
            # plan, which only gets auto selection. Neither is worth failing the
            # whole generation over when "auto" will work.
            if _is_auth_failure(e):
                raise AIProviderError(_AUTH_HELP) from e
            if model != "auto" and _is_model_rejection(e):
                log.warning(
                    "[life-ai] Copilot wouldn't accept model %r (%s). Falling "
                    "back to auto selection for the rest of this process.",
                    model, str(e)[:160])
                self._forced_auto = True
                return self._ask_sync(prompt, "auto", timeout)
            raise

    def reset(self):
        """Drop the cached client so the next call starts a fresh runtime."""
        with self._lock:
            client, self._client = self._client, None
        if client is not None and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(client.stop(), self._loop).result(timeout=20)
            except Exception:
                pass


_AUTH_HELP = (
    "Copilot has no usable GitHub token. The SDK spawns the Copilot CLI, which "
    "needs its own credential and often can't read a `copilot login` stored in "
    "an interactive desktop session when the server runs as a service.\n"
    "Fix it by putting a token in the server's .env as LIFE_AI_GITHUB_TOKEN "
    "(COPILOT_GITHUB_TOKEN, GH_TOKEN and GITHUB_TOKEN also work):\n"
    "  - a fine-grained PAT with the 'Copilot Requests' ACCOUNT permission, "
    "owned by you rather than an org (github_pat_...), or\n"
    "  - the gho_ token that `copilot login` mints.\n"
    "Classic PATs (ghp_) are never accepted, whatever scopes they carry — that "
    "includes any old GITHUB_MODELS_TOKEN. Alternatively run `copilot login` as "
    "the same user the server runs as."
)


def _is_auth_failure(exc):
    msg = str(exc).lower()
    return (
        "no github oauth token" in msg
        or "copilot hmac key" in msg
        or ("token" in msg and ("unauthorized" in msg or "not authenticated" in msg))
    )


_MODEL_REJECTION_HINTS = (
    "model", "not found", "unknown", "unavailable", "not supported",
    "unsupported", "invalid", "no access", "not entitled", "not enabled",
)


def _is_model_rejection(exc):
    """Does this error read like 'that model isn't available to you'?

    Deliberately broad. Guessing wrong costs one extra request on auto; guessing
    too narrowly costs the whole night's generation."""
    msg = str(exc).lower()
    return "model" in msg and any(h in msg for h in _MODEL_REJECTION_HINTS if h != "model")


_copilot = _CopilotRuntime()


def _response_text(resp):
    """Pull the assistant text out of whatever shape send_and_wait returns."""
    for path in (("data", "content"), ("content",), ("text",)):
        cur = resp
        for attr in path:
            cur = getattr(cur, attr, None)
            if cur is None:
                break
        if isinstance(cur, str) and cur.strip():
            return cur
    if isinstance(resp, str):
        return resp
    return str(resp or "")


def _flatten(messages, json_object):
    """The SDK takes one prompt string, not a role array. Render the turns
    plainly and, when JSON is expected, say so — there's no response_format
    knob to lean on the way the Models API had."""
    parts = []
    for m in messages or []:
        role = (m.get("role") or "user").lower()
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            parts.append(content)
        elif role == "assistant":
            parts.append(f"[your previous reply]\n{content}")
        else:
            parts.append(content)
    prompt = "\n\n".join(parts)
    if json_object:
        prompt += (
            "\n\nRespond with a single raw JSON object and nothing else — no "
            "prose, no explanation, no markdown code fences."
        )
    return prompt


# ── OpenAI-compatible backend ─────────────────────────────────────────────────

def _uses_completion_tokens(model):
    """GPT-5 family + o-series reasoning models use `max_completion_tokens` and
    reject custom `temperature`/`top_p`."""
    m = (model or "").lower()
    return "gpt-5" in m or m.startswith(("o1", "o3", "o4"))


def _post_chat(body, timeout):
    if not API_KEY:
        raise AIProviderError(
            "No API key for the OpenAI-compatible provider. Set LIFE_AI_API_KEY "
            "(and LIFE_AI_API_BASE) on the server.")
    req = urllib.request.Request(
        f"{API_BASE}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
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
        raise AIProviderError(f"AI provider {e.code}: {detail}",
                              status=e.code, retry_after=retry_after)
    except urllib.error.URLError as e:
        raise AIProviderError(f"AI provider unreachable: {e}")
    choices = payload.get("choices") or [{}]
    return ((choices[0].get("message") or {}).get("content") or "")


def _chat_openai(messages, model, temperature, max_tokens, json_object, timeout):
    body = {"model": model, "messages": messages, "stream": False}
    if json_object:
        body["response_format"] = {"type": "json_object"}
    if _uses_completion_tokens(model):
        body["max_completion_tokens"] = max_tokens
    else:
        body["max_tokens"] = max_tokens
        body["temperature"] = temperature
        body["top_p"] = 1

    # Up to 2 retries on 429. Respect Retry-After when it's short; if it's long
    # (a daily quota) raise instead of blocking a request thread on it.
    for attempt in range(3):
        try:
            return _post_chat(body, timeout)
        except AIProviderError as e:
            if e.status == 429 and attempt < 2:
                wait = e.retry_after if e.retry_after is not None else 5 * (attempt + 1)
                if wait > 60:
                    raise
                time.sleep(wait)
                continue
            msg = str(e).lower()
            # Some endpoints reject specific params — retry once, stripped down.
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


# ── Public interface ──────────────────────────────────────────────────────────

def chat_completion(
    messages,
    model=None,
    temperature=0.3,
    max_tokens=2000,
    json_object=False,
    timeout=60,
):
    """Send a chat-style exchange and return the assistant's message text.

    `temperature` and `max_tokens` apply to the OpenAI-compatible provider only;
    the Copilot SDK exposes no equivalent and ignores them."""
    model = model or DEFAULT_MODEL
    if PROVIDER == "copilot":
        return _copilot.ask(_flatten(messages, json_object),
                            _model_for_copilot(model), timeout)
    return _chat_openai(messages, model, temperature, max_tokens, json_object, timeout)


def health():
    """Verify the provider actually answers. Used by GET /api/life/ai/health so
    the setup can be checked on the server without running a full generation."""
    model = _model_for_copilot(DEFAULT_MODEL) if PROVIDER == "copilot" else DEFAULT_MODEL
    info = {"provider": PROVIDER, "model": model}
    try:
        sample = chat_completion(
            [{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=5, timeout=60)
        if PROVIDER == "copilot":
            info["auth"] = _copilot._auth_source or "copilot-cli-login"
        if PROVIDER == "copilot" and _copilot._forced_auto:
            # Say so plainly — otherwise a silent downgrade to auto looks like
            # the pinned model is working.
            info["model"] = "auto"
            info["note"] = f"{model} was refused; using auto selection"
        return {**info, "available": True, "sample": (sample or "")[:40]}
    except AIProviderError as e:
        if PROVIDER == "copilot":
            tok, src = _resolve_copilot_token()
            info["auth"] = src or "none (relying on copilot login)"
            if tok:
                info["auth_token_prefix"] = tok[:8] + "…"
        return {**info, "available": False, "error": str(e)}
    except Exception as e:
        return {**info, "available": False, "error": f"{e.__class__.__name__}: {e}"}
