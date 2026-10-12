"""Inbox dashboard: inbox_core logic + the /inbox blueprint against a fake Apps Script."""
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from flask import Flask

import inbox_blueprint as ib
import inbox_core as core

SECRET = "jwt-test-secret"
SCRIPT_SECRET = "script-secret"


# ─── inbox_core ───────────────────────────────────────────────────────────────

def test_parse_sender_variants():
    assert core.parse_sender('"Dana W." <Dana@Example.net>') == ("Dana W.", "dana@example.net")
    assert core.parse_sender("alerts@bank.example") == ("alerts", "alerts@bank.example")
    assert core.parse_sender("") == ("", "")


def test_people_matching_exact_and_domain():
    people = core.normalize_people([
        {"match": "Grandma@Example.org", "name": "Grandma", "kind": "family"},
        {"match": "@whitfield.example", "name": "Whitfields"},
        {"match": "", "name": "ignored"},
        {"match": "grandma@example.org", "name": "duplicate"},
    ])
    assert len(people) == 2
    assert core.match_person("grandma@example.org", people)["name"] == "Grandma"
    assert core.match_person("dana@mail.whitfield.example", people)["name"] == "Whitfields"
    assert core.match_person("someone@example.org", people) is None


def test_parse_actions_accepts_every_shape_chatgpt_produces():
    fenced = '```json\n[{"id":"t1","labels":"A. Read","importance":"HIGH","archive_ok":"true"},{"summary":"Batch."}]\n```'
    actions, summary = core.parse_actions(fenced)
    assert actions == [{"id": "t1", "summary": "", "labels": ["A. Read"], "importance": "high",
                        "personal": False, "archive_ok": True}]
    assert summary == "Batch."
    prose = 'Sure! Here you go:\n{"actions":[{"id":"t2","labels":["X","x"],"personal":true}],"summary":"S"}\nHope that helps.'
    actions, summary = core.parse_actions(prose)
    assert actions[0]["labels"] == ["X"] and actions[0]["personal"] is True and summary == "S"
    curly = '[{“id”: “t3”, “archive_ok”: true}]'
    assert core.parse_actions(curly)[0][0]["id"] == "t3"
    with pytest.raises(ValueError):
        core.parse_actions("not json at all")
    with pytest.raises(ValueError):
        core.parse_actions('[{"summary":"only a summary"}]')


def test_people_rules_override_the_model():
    people = core.normalize_people([{"match": "mom@example.org", "name": "Mom"}])
    actions = [{"id": "a", "importance": "low", "personal": False, "archive_ok": True},
               {"id": "b", "importance": "high", "personal": False, "archive_ok": False}]
    senders = {"a": "Mom <mom@example.org>", "b": "Boss <boss@work.example>"}
    out = core.apply_people_rules(actions, senders, people, True, True)
    assert out[0]["personal"] is True and out[0]["archive_ok"] is False and out[0]["keep_unread"] is True
    assert out[1]["keep_unread"] is True and "personal" in out[1]


def test_prompt_fills_labels_and_people_and_rescues_formatless_prompts():
    people = core.normalize_people([{"match": "mom@example.org", "name": "Mom"}])
    text = core.fill_prompt("", ["A. Read", "A. Financial/Statements"], people)
    assert "  - A. Financial/Statements" in text and "Mom <mom@example.org> (family)" in text
    assert "{{" not in text
    custom = core.fill_prompt("Label my emails please.", [], [])
    assert custom.endswith(core.JSON_SHAPE_REMINDER)


def test_export_blocks_match_the_sheet_format():
    tz = timezone(timedelta(hours=-5))
    text = core.export_blocks([{"id": "t1", "from": "A <a@x>", "subject": "Hi", "date": "2026-10-10T14:05:00.000Z",
                                "body": "Hello"}], tz)
    assert text == "---EMAIL-1---\nID: t1\nFrom: A <a@x>\nSubject: Hi\nDate: Sat Oct 10 2026 9:05 AM\nBody: Hello\n"


def test_signature_roundtrip_and_rejections():
    body = json.dumps({"items": [{"subject": "Café — \U0001F3A7"}]})  # ensure_ascii → ASCII bytes
    ts = str(int(time.time()))
    sig = core.sign(SCRIPT_SECRET, ts, body)
    assert core.verify(SCRIPT_SECRET, ts, body.encode(), sig) == ""
    assert core.verify(SCRIPT_SECRET, ts, body.encode(), "0" * 64) == "Bad signature"
    assert "window" in core.verify(SCRIPT_SECRET, str(int(time.time()) - 900), body.encode(),
                                   core.sign(SCRIPT_SECRET, str(int(time.time()) - 900), body))
    assert "is off" in core.verify("", ts, body.encode(), sig)


# ─── Blueprint fixtures ───────────────────────────────────────────────────────

class FakeScript:
    """Answers the dashboard-api.gs operations from an in-memory mailbox."""
    configured = True

    def __init__(self):
        now = datetime.now(timezone.utc)
        self.threads = {}
        rows = [
            ("t-mom", "Mom <mom@example.org>", "Sunday dinner", 1),
            ("t-bank", "Northwind CU <alerts@northwind.example>", "Statement ready", 2),
            ("t-promo", "Pine Street <deals@pine.example>", "30% off", 3),
            ("t-dj", "Priya <priya@example.com>", "Wedding DJ inquiry", 4),
        ]
        for tid, frm, subj, h in rows:
            self.threads[tid] = {"id": tid, "messageId": "m-" + tid, "from": frm, "subject": subj,
                                 "date": (now - timedelta(hours=h)).isoformat().replace("+00:00", "Z"),
                                 "unread": True, "inbox": True, "labels": [], "starred": False}
        self.calls = []
        self.fail = None

    def call(self, op, timeout=60, **args):
        self.calls.append((op, args))
        if self.fail:
            raise self.fail
        if op == "stats":
            waiting = sum(1 for t in self.threads.values() if t["inbox"] and t["unread"])
            return {"ok": True, "inboxUnread": waiting, "waiting": waiting, "waitingCapped": False, "query": "q"}
        if op == "export":
            items = [t for t in self.threads.values() if t["inbox"] and t["unread"]][: args.get("limit", 50)]
            return {"ok": True, "items": [{**t, "body": "Body of " + t["subject"]} for t in items]}
        if op == "apply":
            out = []
            for a in args["actions"]:
                t = self.threads.get(a["id"])
                if not t:
                    continue
                t["labels"] += a.get("labels", [])
                t["unread"] = bool(a.get("keep_unread"))
                if a.get("archive_ok"):
                    t["inbox"] = False
                out.append({"id": t["id"], "messageId": t["messageId"], "from": t["from"], "subject": t["subject"],
                            "date": t["date"], "summary": a.get("summary", ""), "labels": a.get("labels", []),
                            "importance": a.get("importance", ""), "personal": a.get("personal", False),
                            "archived": bool(a.get("archive_ok")), "markedRead": not a.get("keep_unread"),
                            "starred": False, "snippet": "snip", "messageCount": 1})
            return {"ok": True, "items": out, "applied": len(out), "missing": len(args["actions"]) - len(out),
                    "errored": 0, "message": f"Applied {len(out)}.", "summary": args.get("summary", "")}
        if op == "thread":
            t = self.threads.get(args["id"])
            if not t:
                raise core.ScriptError("That email is no longer in Gmail.", "not_found")
            return {"ok": True, "thread": {"id": t["id"], "subject": t["subject"], "inInbox": t["inbox"],
                                           "starred": t["starred"], "messages": [
                                               {"id": t["messageId"], "from": t["from"], "html": "<p>Hello</p>",
                                                "plain": "Hello", "attachments": []}]}}
        if op == "modify":
            results = []
            for tid in args["ids"]:
                t = self.threads.get(tid)
                if not t:
                    results.append({"id": tid, "ok": False, "error": "not found"})
                    continue
                act = args["action"]
                if act == "read": t["unread"] = False
                if act == "unread": t["unread"] = True
                if act == "archive": t["inbox"] = False
                if act == "inbox": t["inbox"] = True
                results.append({"id": tid, "ok": True})
            return {"ok": True, "results": results, "failed": sum(not r["ok"] for r in results)}
        if op == "inbox":
            items = [{"id": t["id"], "messageId": t["messageId"], "from": t["from"], "subject": t["subject"],
                      "date": t["date"], "snippet": "snip", "unread": t["unread"], "starred": t["starred"],
                      "labels": t["labels"], "messageCount": 1} for t in self.threads.values() if t["inbox"]]
            return {"ok": True, "items": items[: args.get("limit", 50)], "inboxUnread": 2}
        if op == "labels":
            return {"ok": True, "labels": ["A. Read", "A. Financial/Statements", "Receipts"]}
        if op == "rollup":
            rows = [{"processedAt": "2026-09-01T15:00:00Z", "id": f"old-{i}", "from": "X <x@x.example>",
                     "subject": f"Old {i}", "summary": "", "labels": ["A. Read"], "importance": "low",
                     "archived": True, "date": "2026-09-01T12:00:00Z"} for i in range(3)]
            off = args.get("offset", 0)
            return {"ok": True, "total": 3, "offset": off, "rows": rows[off: off + args.get("limit", 500)]}
        if op == "ping":
            return {"ok": True, "version": 1, "inboxUnread": 2, "query": "q", "reportsTo": "x"}
        raise AssertionError(f"unexpected op {op}")


def ai_answer(messages, **_):
    """A stand-in model: high + personal for Mom and the DJ lead, the rest low."""
    text = messages[-1]["content"]
    ids = [line[4:] for line in text.splitlines() if line.startswith("ID: ")]
    actions = []
    for tid in ids:
        hot = tid in ("t-mom", "t-dj")
        actions.append({"id": tid, "summary": f"About {tid}", "labels": ["A. Read"] if not hot else ["A. Personal"],
                        "importance": "high" if hot else "low", "personal": tid == "t-mom", "archive_ok": not hot})
    return json.dumps({"actions": actions, "summary": "Two need you."})


@pytest.fixture
def app(tmp_path, monkeypatch):
    db = tmp_path / "mw.db"
    secret_file = tmp_path / ".secret_key"
    secret_file.write_text(SECRET)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT)")
    conn.executemany("INSERT INTO users VALUES (?, ?)", [("1", "zweetztuph@gmail.com"), ("2", "guest@example.com")])
    conn.commit()
    conn.close()
    fake = FakeScript()
    monkeypatch.setattr(ib, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ib, "DB_PATH", db)
    monkeypatch.setattr(ib, "SECRET_FILE", secret_file)
    monkeypatch.setattr(ib, "SCRIPT_SECRET", SCRIPT_SECRET)
    monkeypatch.setattr(ib, "_secret_cache", None)
    monkeypatch.setattr(ib, "_tables_ready", False)
    monkeypatch.setattr(ib, "_scheduler_started", True)
    monkeypatch.setattr(ib, "_cache", {})
    monkeypatch.setattr(ib, "get_script_client", lambda: fake)
    monkeypatch.setattr(ib, "ai_chat", ai_answer)
    monkeypatch.setattr(ib, "ai_provider_name", lambda: "fake")
    monkeypatch.setattr(ib, "spawn", lambda fn, *a: fn(*a))
    flask_app = Flask(__name__)
    flask_app.register_blueprint(ib.inbox_bp)
    flask_app.fake = fake
    return flask_app


def _token(sub):
    return jwt.encode({"sub": sub, "exp": int(time.time()) + 600}, SECRET, algorithm="HS256")


@pytest.fixture
def client(app):
    c = app.test_client()
    c.environ_base["HTTP_AUTHORIZATION"] = "Bearer " + _token("1")
    return c


def _sort_with_ai(client, review=False):
    r = client.post("/inbox/triage/run", json={"review": review})
    assert r.status_code == 202, r.get_json()
    return r.get_json()["batch"]


# ─── Blueprint behaviour ──────────────────────────────────────────────────────

def test_only_the_owner_gets_in(app):
    c = app.test_client()
    assert c.get("/inbox/summary").status_code == 401
    assert c.get("/inbox/summary", headers={"Authorization": "Bearer " + _token("2")}).status_code == 403
    assert c.get("/inbox/summary", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert c.get("/inbox/health").status_code == 200


def test_ai_sort_applies_and_fills_the_dashboard(client, app):
    batch = _sort_with_ai(client)
    assert batch["status"] == "done", batch
    assert (batch["applied"], batch["archived"], batch["important"], batch["personal"]) == (4, 2, 2, 1)
    assert batch["summary"] == "Two need you."
    fake = app.fake
    assert fake.threads["t-promo"]["inbox"] is False and fake.threads["t-mom"]["inbox"] is True
    hl = client.get("/inbox/messages?view=highlights").get_json()["items"]
    assert {m["id"] for m in hl} == {"t-mom", "t-dj"}
    assert all(m["read"] is False for m in hl)
    summary = client.get("/inbox/summary").get_json()
    assert summary["today"]["sorted"] == 4 and summary["today"]["read"] == 0
    assert summary["highlights"] == {"unread": 2, "total": 2}
    assert summary["labels"][0] == {"name": "A. Personal", "count": 2, "unread": 2} or \
        summary["labels"][0]["name"] == "A. Read"
    # the important threads were fetched ahead so they open instantly
    assert {a["id"] for op, a in fake.calls if op == "thread"} == {"t-mom", "t-dj"}


def test_review_lets_me_edit_before_anything_changes(client, app):
    batch = _sort_with_ai(client, review=True)
    assert batch["status"] == "review"
    assert len(batch["proposal"]["actions"]) == 4
    assert not any(op == "apply" for op, _ in app.fake.calls), "nothing applied before review"
    edited = batch["proposal"]["actions"]
    for a in edited:
        if a["id"] == "t-promo":
            a["archive_ok"] = False
            a["labels"] = ["Receipts"]
    r = client.post(f"/inbox/triage/batches/{batch['id']}/apply", json={"actions": edited, "skip": ["t-bank"]})
    done = r.get_json()["batch"]
    assert done["status"] == "done" and done["applied"] == 3
    assert app.fake.threads["t-promo"]["inbox"] is True and app.fake.threads["t-promo"]["labels"] == ["Receipts"]
    assert app.fake.threads["t-bank"]["unread"] is True, "skipped emails are left for next time"
    assert client.post(f"/inbox/triage/batches/{batch['id']}/apply", json={}).status_code == 409


def test_people_list_keeps_family_in_the_inbox_whatever_the_model_says(client, app, monkeypatch):
    client.put("/inbox/settings", json={"people": [{"match": "deals@pine.example", "name": "Pine", "kind": "friend"}]})
    _sort_with_ai(client)
    assert app.fake.threads["t-promo"]["inbox"] is True
    row = [m for m in client.get("/inbox/messages?view=all").get_json()["items"] if m["id"] == "t-promo"][0]
    assert row["personal"] is True


def test_chatgpt_flow_export_then_paste(client, app):
    out = client.post("/inbox/triage/export", json={"limit": 2}).get_json()
    assert out["count"] == 2
    assert out["text"].startswith("You sort Michael's Gmail")
    assert "---EMAIL-1---\nID: t-mom" in out["text"]
    pasted = '```json\n{"actions":[{"id":"t-mom","labels":["A. Personal"],"importance":"high","personal":true,' \
             '"archive_ok":false,"summary":"Dinner"}],"summary":"Mom wrote."}\n```'
    batch = client.post("/inbox/triage/apply", json={"json": pasted}).get_json()["batch"]
    assert batch["status"] == "done" and batch["source"] == "chatgpt" and batch["applied"] == 1
    bad = client.post("/inbox/triage/apply", json={"json": "nope"})
    assert bad.status_code == 400 and "valid JSON" in bad.get_json()["error"]


def test_reading_pinning_and_unread_sync_with_gmail(client, app):
    _sort_with_ai(client)
    r = client.post("/inbox/actions", json={"ids": ["t-mom"], "action": "read"}).get_json()
    assert r["updated"][0]["read"] is True
    assert not [a for op, a in app.fake.calls if op == "modify"], "already read in Gmail: no Gmail call"
    client.post("/inbox/actions", json={"ids": ["t-mom"], "action": "read"})  # twice = still one read today
    assert client.get("/inbox/summary").get_json()["today"]["read"] == 1
    r = client.post("/inbox/actions", json={"ids": ["t-mom"], "action": "unread"}).get_json()
    assert r["updated"][0]["read"] is False and app.fake.threads["t-mom"]["unread"] is True
    client.post("/inbox/actions", json={"ids": ["t-dj"], "action": "pin"})
    pins = client.get("/inbox/summary").get_json()["pins"]
    assert [p["id"] for p in pins] == ["t-dj"]
    assert [m["id"] for m in client.get("/inbox/messages?view=pinned").get_json()["items"]] == ["t-dj"]
    client.post("/inbox/actions", json={"ids": ["t-dj"], "action": "unpin"})
    assert client.get("/inbox/summary").get_json()["pins"] == []


def test_archive_counts_as_cleared_and_failures_surface(client, app):
    _sort_with_ai(client)
    r = client.post("/inbox/actions", json={"ids": ["t-dj", "ghost"], "action": "archive"}).get_json()
    assert r["failed"] == {"ghost": "not found"}
    assert app.fake.threads["t-dj"]["inbox"] is False
    assert client.get("/inbox/summary").get_json()["today"]["cleared"] == 1
    app.fake.fail = core.ScriptError("Couldn't reach Apps Script", "network")
    r = client.post("/inbox/actions", json={"ids": ["t-mom"], "action": "archive"})
    assert r.status_code == 424 and "reach" in r.get_json()["error"]
    # opening an email still counts as read when Gmail is unreachable
    assert client.post("/inbox/actions", json={"ids": ["t-mom"], "action": "read"}).status_code == 200
    assert client.post("/inbox/actions", json={"ids": ["t-mom"], "action": "explode"}).status_code == 400


def test_thread_is_cached_and_gone_threads_are_marked(client, app):
    _sort_with_ai(client)
    app.fake.calls.clear()
    first = client.get("/inbox/thread/t-bank").get_json()
    assert first["thread"]["messages"][0]["html"] == "<p>Hello</p>" and first["cached"] is False
    again = client.get("/inbox/thread/t-bank").get_json()
    assert again["cached"] is True
    assert [op for op, _ in app.fake.calls].count("thread") == 1
    del app.fake.threads["t-promo"]
    assert client.get("/inbox/thread/t-promo").status_code == 404
    ids = [m["id"] for m in client.get("/inbox/messages?view=all").get_json()["items"]]
    assert "t-promo" not in ids


def test_live_inbox_merges_untriaged_mail_and_tracks_reads_elsewhere(client, app):
    live = client.get("/inbox/live").get_json()
    assert {m["id"] for m in live["items"]} == set(app.fake.threads)
    assert all(m["source"] == "inbox" and m["read"] is False for m in live["items"])
    client.put("/inbox/settings", json={"keep_important_unread": True})
    _sort_with_ai(client)
    mom = [m for m in client.get("/inbox/messages?view=highlights").get_json()["items"] if m["id"] == "t-mom"][0]
    assert mom["gmail_unread"] is True
    app.fake.threads["t-mom"]["unread"] = False  # read on the phone
    live = client.get("/inbox/live?refresh=1").get_json()
    mom = [m for m in live["items"] if m["id"] == "t-mom"][0]
    assert mom["read"] is True
    ids = {m["id"] for m in live["items"]}
    assert "t-promo" not in ids, "archived by triage"
    assert client.get("/inbox/messages?view=inbox").get_json()["total"] == len(ids)


def test_ingest_from_the_sheet_requires_a_valid_signature(app):
    c = app.test_client()
    body = json.dumps({"source": "sheet", "summary": "From the sheet.", "items": [
        {"id": "t-sheet", "messageId": "m1", "from": "Mom <mom@example.org>", "subject": "Hi", "date": "",
         "labels": ["A. Personal"], "importance": "high", "archived": False, "markedRead": True}]})
    ts = str(int(time.time()))
    bad = c.post("/inbox/ingest", data=body, headers={"X-Inbox-Timestamp": ts, "X-Inbox-Signature": "00"})
    assert bad.status_code == 401
    ok = c.post("/inbox/ingest", data=body, headers={"X-Inbox-Timestamp": ts,
                                                      "X-Inbox-Signature": core.sign(SCRIPT_SECRET, ts, body)})
    assert ok.get_json() == {"ok": True, "stored": 1, "batch_id": 1}
    owner = app.test_client()
    owner.environ_base["HTTP_AUTHORIZATION"] = "Bearer " + _token("1")
    batches = owner.get("/inbox/triage/batches").get_json()["batches"]
    assert batches[0]["source"] == "sheet" and batches[0]["summary"] == "From the sheet."


def test_settings_roundtrip_and_detected_labels(client):
    s = client.get("/inbox/settings").get_json()
    assert s["prompt_is_default"] is True and s["batch_size"] == 50
    client.get("/inbox/labels")  # caches Gmail's labels
    s = client.put("/inbox/settings", json={"batch_size": 500, "prompt": "  Custom  ", "auto_sort_hours": -3,
                                            "people": [{"match": "Mom@Example.org"}], "unknown": 1}).get_json()
    assert s["batch_size"] == 100 and s["prompt"] == "Custom" and s["auto_sort_hours"] == 0
    assert s["people"] == [{"match": "mom@example.org", "name": "", "kind": "family"}]
    assert "A. Financial/Statements" in s["detected_labels"] and "Receipts" not in s["detected_labels"]
    s = client.put("/inbox/settings", json={"prompt": core.DEFAULT_PROMPT}).get_json()
    assert s["prompt_is_default"] is True


def test_rollup_import_is_incremental_and_marks_history_read(client, app):
    batch = client.post("/inbox/import").get_json()["batch"]
    assert batch["status"] == "done" and batch["applied"] == 3
    again = client.post("/inbox/import").get_json()["batch"]
    assert again["applied"] == 0
    items = client.get("/inbox/messages?view=sorted").get_json()["items"]
    assert len(items) == 3 and all(m["read"] for m in items)


def test_one_sort_at_a_time(client, app, monkeypatch):
    monkeypatch.setattr(ib, "spawn", lambda fn, *a: None)  # leave the first batch "running"
    assert client.post("/inbox/triage/run", json={}).status_code == 202
    r = client.post("/inbox/triage/run", json={})
    assert r.status_code == 409 and r.get_json()["batch_id"] == 1


def test_scheduled_sort_respects_the_interval(client, app):
    assert ib._scheduler_tick() is None  # off by default
    client.put("/inbox/settings", json={"auto_sort_hours": 6})
    first = ib._scheduler_tick()
    assert first is not None
    assert ib._scheduler_tick() is None  # sorted less than 6 hours ago


def test_ai_that_skips_emails_gets_a_second_ask(client, app, monkeypatch):
    calls = []

    def lazy_model(messages, **kw):
        calls.append(messages)
        out = json.loads(ai_answer(messages))
        if len(calls) == 1:
            out["actions"] = out["actions"][:2]
        return json.dumps(out)
    monkeypatch.setattr(ib, "ai_chat", lazy_model)
    batch = _sort_with_ai(client)
    assert batch["applied"] == 4 and len(calls) == 2
    assert calls[1][-1]["content"].count("---EMAIL-") == 2


def test_gmail_not_connected_is_explained(client, app, monkeypatch):
    monkeypatch.setattr(ib, "get_script_client", lambda: core.ScriptClient("", ""))
    r = client.post("/inbox/triage/run", json={})
    assert r.status_code == 424 and "GMAIL_SCRIPT_URL" in r.get_json()["error"]
    assert client.get("/inbox/summary").get_json()["configured"]["script"] is False


def test_gmail_failures_never_use_502_or_504(client, app):
    """Cloudflare replaces 502/504 with a page that has no CORS headers."""
    for code in ("network", "not_json", "auth", "error", "config"):
        app.fake.fail = core.ScriptError("Apps Script refused the request: Bad signature.", code)
        for path in ("/inbox/status?refresh=1", "/inbox/live?refresh=1", "/inbox/labels"):
            r = client.get(path)
            assert r.status_code not in (502, 504), (code, path, r.status_code)
        r = client.get("/inbox/status?refresh=1")
        assert r.status_code == 424 and r.get_json()["code"] == code


def test_script_refusals_explain_what_to_do():
    assert "GMAIL_SCRIPT_SECRET" in core._explain("Bad signature", "auth")
    assert "clock" in core._explain("Timestamp outside the allowed window. Check the server's clock.", "auth")
    assert core._explain("Dashboard API is off. Run setupDashboard() in the Apps Script editor.", "auth") \
        .startswith("Apps Script refused the request: Dashboard API is off.")
    assert core._explain("Thread gone", "not_found") == "Thread gone"
