"""
Inbox dashboard — pure logic shared by inbox_blueprint.py.

Nothing here touches Flask or the database, so it is easy to test:
  * ScriptClient     signed calls to the Gmail triage Apps Script web app
  * sign / verify    the HMAC scheme both directions use
  * the triage prompt (default text, placeholders, the text to paste into Claude)
  * parse_actions    lenient parsing of what Claude or wegter-pc's AI returns
  * sender + people  "Name <email>" parsing and family/close-contact matching

The Apps Script side of every contract here lives in
email-automate-google-script/dashboard-api.gs.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr

SIGNATURE_WINDOW_SECONDS = 300
IMPORTANCE_LEVELS = ("high", "medium", "low")
MAX_LABELS_PER_EMAIL = 5
MAX_SUMMARY_CHARS = 600


# ─── Signing ──────────────────────────────────────────────────────────────────

def sign(secret: str, ts: str, body: str) -> str:
    """HMAC-SHA256 hex of "<ts>.<body>". Bodies are ASCII-only JSON on both
    sides, so the bytes signed never depend on a text encoding."""
    return hmac.new(secret.encode("utf-8"), f"{ts}.{body}".encode("utf-8"), hashlib.sha256).hexdigest()


def verify(secret: str, ts: str, body: bytes, signature: str, now: float | None = None) -> str:
    """'' when authentic, else the reason. `body` is the raw request bytes."""
    if not secret:
        return "Inbox ingest is off. Set GMAIL_SCRIPT_SECRET on the server."
    if not ts or not signature:
        return "Missing X-Inbox-Timestamp / X-Inbox-Signature"
    try:
        ts_val = int(float(ts))
    except ValueError:
        return "Bad timestamp"
    if abs((now or time.time()) - ts_val) > SIGNATURE_WINDOW_SECONDS:
        return "Timestamp outside the allowed window"
    expected = hmac.new(secret.encode("utf-8"), ts.encode("utf-8") + b"." + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip().lower()):
        return "Bad signature"
    return ""


# ─── Apps Script client ───────────────────────────────────────────────────────

class ScriptError(RuntimeError):
    """Apps Script refused or failed a call. `code` is the script's own code
    ('auth', 'not_found', 'bad_request', 'error') or one of ours ('config',
    'network', 'not_json')."""

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


class ScriptClient:
    """Signed JSON calls to the deployed web app (dashboard-api.gs).

    A POST to a /exec URL runs doPost, then Google answers 302 → a
    googleusercontent.com URL that serves the output; `requests` follows that
    redirect with a GET, which is exactly what Google expects."""

    def __init__(self, url: str, secret: str, session=None):
        self.url = (url or "").strip()
        self.secret = (secret or "").strip()
        self._session = session

    @property
    def configured(self) -> bool:
        return bool(self.url and self.secret)

    def _http(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def call(self, op: str, timeout: float = 60, **args):
        if not self.configured:
            raise ScriptError(
                "Gmail isn't connected yet. Set GMAIL_SCRIPT_URL and GMAIL_SCRIPT_SECRET on the server.",
                "config")
        payload = json.dumps({"op": op, **args}, separators=(",", ":"))  # ensure_ascii → pure ASCII
        ts = str(int(time.time()))
        body = json.dumps({"ts": ts, "payload": payload, "sig": sign(self.secret, ts, payload)})
        try:
            resp = self._http().post(self.url, data=body.encode("ascii"),
                                     headers={"Content-Type": "application/json"},
                                     timeout=timeout, allow_redirects=True)
        except Exception as e:  # requests.RequestException and friends
            raise ScriptError(f"Couldn't reach Apps Script: {e.__class__.__name__}: {e}", "network")
        text = resp.content.decode("utf-8", "replace")
        if resp.status_code >= 400:
            raise ScriptError(f"Apps Script answered HTTP {resp.status_code}", "network")
        try:
            data = json.loads(text)
        except ValueError:
            raise ScriptError(
                "Apps Script sent back a web page instead of JSON. Check the web app is deployed "
                "with 'Execute as: Me' and 'Who has access: Anyone', and that it was redeployed "
                "after adding dashboard-api.gs.", "not_json")
        if not isinstance(data, dict) or not data.get("ok"):
            err = (data or {}).get("error") if isinstance(data, dict) else None
            code = (data or {}).get("code", "error") if isinstance(data, dict) else "error"
            raise ScriptError(_explain(err or "Apps Script returned an error", code), code)
        return data


_AUTH_HINTS = {
    "Bad signature": " GMAIL_SCRIPT_SECRET on the server doesn't match the script's secret. Run "
                     "setupDashboard() in Apps Script and copy its GMAIL_SCRIPT_SECRET line into .env again.",
    "Timestamp outside": " The server's clock may be off.",
}


def _explain(err: str, code: str) -> str:
    """Script refusals are terse on purpose; say what to do about them here."""
    if code == "auth":
        for start, hint in _AUTH_HINTS.items():
            if err.startswith(start):
                return f"Apps Script refused the request: {err}.{hint}".replace("..", ".")
        return f"Apps Script refused the request: {err}"
    return err


# ─── Senders and people ───────────────────────────────────────────────────────

def parse_sender(raw: str) -> tuple[str, str]:
    """'"Dana W." <dana@x.com>' → ('Dana W.', 'dana@x.com'). Falls back to the
    mailbox's local part when there is no display name."""
    raw = (raw or "").strip()
    name, addr = parseaddr(raw)
    addr = (addr or "").strip().lower()
    name = (name or "").strip().strip('"').strip()
    if not addr and "@" in raw:
        addr = raw.strip("<> ").lower()
    if not name:
        name = addr.split("@")[0] if addr else raw
    return name, addr


def normalize_people(people) -> list[dict]:
    """Settings → [{match, name, kind}] with `match` an address or a domain."""
    out, seen = [], set()
    for p in people if isinstance(people, list) else []:
        if not isinstance(p, dict):
            continue
        match = str(p.get("match") or "").strip().lower()
        if not match or match in seen:
            continue
        seen.add(match)
        kind = str(p.get("kind") or "family").strip().lower()
        if kind not in ("family", "friend", "vip", "work"):
            kind = "family"
        out.append({"match": match[:200], "name": str(p.get("name") or "").strip()[:120], "kind": kind})
    return out[:500]


def match_person(email: str, people: list[dict]) -> dict | None:
    """An exact address wins; otherwise a domain entry ('example.org' or
    '@example.org') matches that domain and its subdomains."""
    email = (email or "").lower()
    if "@" not in email:
        return None
    domain = email.split("@", 1)[1]
    for p in people:
        if "@" in p["match"] and not p["match"].startswith("@") and p["match"] == email:
            return p
    for p in people:
        m = p["match"].lstrip("@")
        if "@" not in m and (domain == m or domain.endswith("." + m)):
            return p
    return None


# ─── The triage prompt ────────────────────────────────────────────────────────

DEFAULT_PROMPT = """You sort Michael's Gmail. For each email below, decide where it belongs and whether he needs to see it.

Return ONLY JSON, exactly this shape, with no prose and no code fence:
{"actions":[{"id":"<copy the ID line exactly>","summary":"<one plain sentence: what it is, plus any date, amount or deadline>","labels":["<label>"],"importance":"high|medium|low","personal":false,"archive_ok":true}],"summary":"<2-3 sentences about the whole batch: what needs Michael and what can wait>"}

Rules
- Exactly one action per email, and copy every ID exactly.
- labels: one or two. Reuse an existing label whenever one fits:
{{labels}}
  Only create a new label when nothing fits, in the same naming style.
- importance "high": needs Michael soon. A real person writing to him; money due or at risk; security or account alerts; appointments or travel in the next 7 days; work or client requests, including DJ booking inquiries for For the Record; deadlines.
  "medium": worth knowing, no action needed. "low": newsletters, promotions, notifications, receipts.
- personal: true only when someone he knows (family, friends) wrote to him personally. Businesses and mailing lists are never personal.
- archive_ok: false for high-importance and personal email; true for everything else.
- These people are family or close contacts. Their email is always personal and never archived:
{{people}}
- The summary should stand on its own: who it's from, what it is, and any date, amount or deadline."""

JSON_SHAPE_REMINDER = (
    'Return ONLY JSON of the form {"actions":[{"id":"...","summary":"...","labels":["..."],'
    '"importance":"high|medium|low","personal":false,"archive_ok":true}],"summary":"..."}.'
)


def fill_prompt(template: str, labels: list[str], people: list[dict]) -> str:
    template = (template or "").strip() or DEFAULT_PROMPT
    label_text = "\n".join(f"  - {name}" for name in labels) if labels else \
        '  (none yet; create short, clear labels such as "A. Financial/Statements")'
    if people:
        rows = []
        for p in people:
            who = p["name"] + " " if p.get("name") else ""
            rows.append(f"  - {who}<{p['match']}> ({p['kind']})")
        people_text = "\n".join(rows)
    else:
        people_text = "  (none listed)"
    out = template.replace("{{labels}}", label_text).replace("{{people}}", people_text)
    if "{{" not in template and "json" not in template.lower():
        out += "\n\n" + JSON_SHAPE_REMINDER  # a pasted prompt that forgot the format
    return out


def _local_date_text(iso: str, tz) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return iso or ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(tz) if tz else dt
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{dt.strftime('%a %b')} {dt.day} {dt.year} {hour}:{dt.strftime('%M %p')}"


def email_blocks(items: list[dict], tz=None) -> list[str]:
    """One ---EMAIL-n--- block per email, the format the Sheet export has always produced."""
    return [
        f"---EMAIL-{i}---\n"
        f"ID: {it.get('id', '')}\n"
        f"From: {it.get('from', '')}\n"
        f"Subject: {it.get('subject', '')}\n"
        f"Date: {_local_date_text(it.get('date', ''), tz)}\n"
        f"Body: {it.get('body', '')}\n"
        for i, it in enumerate(items, 1)
    ]


def export_blocks(items: list[dict], tz=None) -> str:
    return "\n".join(email_blocks(items, tz))


def paste_text(prompt: str, items: list[dict], tz=None) -> str:
    """Everything to paste into Claude: the prompt, then the emails."""
    return f"{prompt}\n\nEMAILS ({len(items)})\n\n{export_blocks(items, tz)}"


# ─── Parsing what Claude (or wegter-pc's AI) returns ──────────────────────────

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text or "").strip()


def _smart_quote_fix(text: str) -> str:
    return (text.replace("“", '"').replace("”", '"').replace("″", '"')
            .replace("‘", "'").replace("’", "'").replace("′", "'")
            .replace(" ", " ").replace("​", " ").replace("﻿", " "))


def _first_json_value(text: str):
    """json.loads, or the first {...}/[...] embedded in surrounding prose."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "[{":
            try:
                value, _ = decoder.raw_decode(text[i:])
                return value
            except ValueError:
                continue
    raise ValueError("no JSON found")


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def normalize_action(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    tid = str(item.get("id") or "").strip()
    if not tid:
        return None
    labels, seen = [], set()
    raw_labels = item.get("labels")
    if isinstance(raw_labels, str):
        raw_labels = [raw_labels]
    for name in raw_labels if isinstance(raw_labels, list) else []:
        name = str(name or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            labels.append(name[:120])
    importance = str(item.get("importance") or "").strip().lower()
    out = {
        "id": tid,
        "summary": str(item.get("summary") or "").strip()[:MAX_SUMMARY_CHARS],
        "labels": labels[:MAX_LABELS_PER_EMAIL],
        "importance": importance if importance in IMPORTANCE_LEVELS else "",
        "personal": _as_bool(item.get("personal", False)),
        "archive_ok": _as_bool(item.get("archive_ok", False)),
    }
    if _as_bool(item.get("keep_unread", False)):
        out["keep_unread"] = True
    if _as_bool(item.get("star", False)):
        out["star"] = True
    return out


def parse_actions(text: str) -> tuple[list[dict], str]:
    """What Claude (or wegter-pc) answers: a JSON array (items + an optional {"summary": ...}) or
    {"actions": [...], "summary": "..."}, with or without a code fence or
    surrounding prose. Raises ValueError with a readable message."""
    raw = _strip_fences(text)
    if not raw:
        raise ValueError("Paste the JSON Claude returned.")
    try:
        parsed = _first_json_value(raw)
    except ValueError:
        try:
            parsed = _first_json_value(_smart_quote_fix(raw))
        except ValueError:
            raise ValueError("That isn't valid JSON. Copy Claude's whole reply, from the first [ or { to the last ] or }.")
    summary = ""
    if isinstance(parsed, list):
        items = []
        for it in parsed:
            if isinstance(it, dict) and it.get("id"):
                items.append(it)
            elif isinstance(it, dict) and isinstance(it.get("summary"), str) and not summary:
                summary = it["summary"].strip()
    elif isinstance(parsed, dict) and isinstance(parsed.get("actions"), list):
        items = parsed["actions"]
        summary = str(parsed.get("summary") or "").strip()
    else:
        raise ValueError('Expected a JSON array, or {"actions": [...], "summary": "..."}.')
    actions, seen = [], set()
    for it in items:
        a = normalize_action(it)
        if a and a["id"] not in seen:
            seen.add(a["id"])
            actions.append(a)
    if not actions:
        raise ValueError("No emails found in that JSON. Each item needs an \"id\".")
    return actions, summary[:2000]


def apply_people_rules(actions: list[dict], senders: dict[str, str], people: list[dict],
                       keep_people_in_inbox: bool = True, keep_important_unread: bool = False) -> list[dict]:
    """Family and close contacts are always personal and (by default) never
    archived, whatever the model said. `senders` maps thread id → from line."""
    out = []
    for a in actions:
        a = dict(a)
        _, email = parse_sender(senders.get(a["id"], ""))
        if match_person(email, people):
            a["personal"] = True
            if keep_people_in_inbox:
                a["archive_ok"] = False
        if keep_important_unread and (a.get("personal") or a.get("importance") == "high"):
            a["keep_unread"] = True
        out.append(a)
    return out
