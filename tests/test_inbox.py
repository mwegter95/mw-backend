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


def test_parse_actions_accepts_every_shape_claude_or_chatgpt_produce():
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
    out = core.apply_people_rules(actions, senders, people, True)
    assert out[0]["personal"] is True and out[0]["archive_ok"] is False
    assert out[1] == actions[1] and "keep_unread" not in out[0]


def test_highlights_kept_in_the_inbox_stay_unread():
    actions = [{"id": "mom", "importance": "medium", "personal": True, "archive_ok": False},
               {"id": "dj", "importance": "high", "personal": False, "archive_ok": False},
               {"id": "bank", "importance": "high", "personal": False, "archive_ok": True},
               {"id": "promo", "importance": "low", "personal": False, "archive_ok": False},
               {"id": "asked", "importance": "low", "personal": False, "archive_ok": True, "keep_unread": True}]
    out = {a["id"]: a["keep_unread"] for a in core.unread_rule(actions)}
    assert out == {"mom": True, "dj": True, "bank": False, "promo": False, "asked": True}
    off = {a["id"]: a["keep_unread"] for a in core.unread_rule(actions, keep_highlights_unread=False)}
    assert off == {"mom": False, "dj": False, "bank": False, "promo": False, "asked": True}


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
        self.kept = {}  # thread id → message id left unread by triage (KEPT_UNREAD in the script)

    def _to_sort(self):
        return [t for t in self.threads.values()
                if t["inbox"] and t["unread"] and self.kept.get(t["id"]) != t["messageId"]]

    def call(self, op, timeout=60, **args):
        self.calls.append((op, args))
        if self.fail:
            raise self.fail
        if op == "stats":
            unread = sum(1 for t in self.threads.values() if t["inbox"] and t["unread"])
            return {"ok": True, "inboxUnread": unread, "waiting": len(self._to_sort()), "waitingCapped": False,
                    "query": "q"}
        if op == "export":
            items = self._to_sort()[: args.get("limit", 50)]
            return {"ok": True, "items": [{**t, "body": "Body of " + t["subject"]} for t in items]}
        if op == "apply":
            out = []
            for a in args["actions"]:
                t = self.threads.get(a["id"])
                if not t:
                    continue
                t["labels"] += a.get("labels", [])
                assert isinstance(a.get("keep_unread"), bool), "the server always decides"
                t["unread"] = a["keep_unread"]
                if a.get("archive_ok"):
                    t["inbox"] = False
                if t["unread"] and t["inbox"]:
                    self.kept[t["id"]] = t["messageId"]
                else:
                    self.kept.pop(t["id"], None)
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
                if act in ("read", "archive", "trash"): self.kept.pop(tid, None)
                if act == "unread" and tid in args.get("sorted", []): self.kept[tid] = t["messageId"]
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


def claude_answer(text):
    """What Claude sends back for an export: high + personal for Mom and the DJ lead, the rest low."""
    ids = [line[4:] for line in text.splitlines() if line.startswith("ID: ")]
    actions = []
    for tid in ids:
        hot = tid in ("t-mom", "t-dj")
        actions.append({"id": tid, "summary": f"About {tid}", "labels": ["A. Read"] if not hot else ["A. Personal"],
                        "importance": "high" if hot else "low", "personal": tid == "t-mom", "archive_ok": not hot})
    return {"actions": actions, "summary": "Two need you."}


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
    monkeypatch.setattr(ib, "_cache", {})
    monkeypatch.setattr(ib, "get_script_client", lambda: fake)
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


def _sort(client, review=False, source="claude"):
    """Export, have "Claude" (or wegter-pc) answer, apply. Claude's answer is pasted as text."""
    export = client.post("/inbox/triage/export", json={}).get_json()
    answer = claude_answer(export["text"])
    body = ({"json": "```json\n" + json.dumps(answer) + "\n```"} if source == "claude"
            else {"actions": answer["actions"], "summary": answer["summary"], "source": source})
    r = client.post("/inbox/triage/apply", json={**body, "review": review})
    assert r.status_code == (201 if review else 202), r.get_json()
    return r.get_json()["batch"]


# ─── Blueprint behaviour ──────────────────────────────────────────────────────

def test_only_the_owner_gets_in(app):
    c = app.test_client()
    assert c.get("/inbox/summary").status_code == 401
    assert c.get("/inbox/summary", headers={"Authorization": "Bearer " + _token("2")}).status_code == 403
    assert c.get("/inbox/summary", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert c.get("/inbox/health").status_code == 200


def test_claude_sort_applies_and_fills_the_dashboard(client, app):
    batch = _sort(client)
    assert batch["status"] == "done", batch
    assert (batch["applied"], batch["archived"], batch["important"], batch["personal"]) == (4, 2, 2, 1)
    assert batch["summary"] == "Two need you." and batch["source"] == "claude"
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
    batch = _sort(client, review=True)
    assert batch["status"] == "review"
    assert len(batch["proposal"]["actions"]) == 4
    mom = [a for a in batch["proposal"]["actions"] if a["id"] == "t-mom"][0]
    assert mom["from"] == "Mom <mom@example.org>" and mom["subject"] == "Sunday dinner", "review shows who and what"
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


def test_highlights_stay_unread_in_gmail_and_are_not_sorted_again(client, app):
    _sort(client)
    fake = app.fake
    assert fake.threads["t-mom"]["unread"] is True and fake.threads["t-dj"]["unread"] is True
    assert fake.threads["t-bank"]["unread"] is False and fake.threads["t-promo"]["unread"] is False
    hl = {m["id"]: m for m in client.get("/inbox/messages?view=highlights").get_json()["items"]}
    assert hl["t-mom"]["gmail_unread"] is True and hl["t-mom"]["read"] is False
    assert client.post("/inbox/triage/export", json={}).get_json()["count"] == 0, "nothing new to sort"
    assert client.get("/inbox/status?refresh=1").get_json()["waiting"] == 0

    client.put("/inbox/settings", json={"keep_highlights_unread": False})
    assert client.get("/inbox/settings").get_json()["keep_highlights_unread"] is False
    fake.threads["t-dj"].update(messageId="m-t-dj-2")  # a reply arrives
    _sort(client)
    assert fake.threads["t-dj"]["unread"] is False, "with the setting off, triage marks everything read"


def test_review_edits_decide_what_stays_unread(client, app):
    batch = _sort(client, review=True)
    edited = batch["proposal"]["actions"]
    for a in edited:
        if a["id"] == "t-dj":
            a["archive_ok"] = True
    client.post(f"/inbox/triage/batches/{batch['id']}/apply", json={"actions": edited})
    applied = [a for op, a in app.fake.calls if op == "apply"][-1]["actions"]
    assert {a["id"]: a["keep_unread"] for a in applied} == {"t-mom": True, "t-dj": False, "t-bank": False,
                                                           "t-promo": False}
    assert app.fake.threads["t-dj"]["inbox"] is False and app.fake.threads["t-dj"]["unread"] is False


def test_recently_sorted_lists_the_latest_sorts_first(client, app, monkeypatch):
    client.post("/inbox/import")  # history from September: too old for "recent"
    _sort(client)
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    monkeypatch.setattr(ib, "_now", lambda: later)
    app.fake.threads["t-promo"].update(unread=True, inbox=True, messageId="m-t-promo-2")
    _sort(client)
    recent = client.get("/inbox/messages?view=recent").get_json()
    assert [m["id"] for m in recent["items"]][0] == "t-promo", "newest sort first"
    assert {m["id"] for m in recent["items"]} == {"t-mom", "t-bank", "t-promo", "t-dj"}
    assert len({m["batch_id"] for m in recent["items"]}) == 2
    assert client.get("/inbox/messages?view=unread").get_json()["view"] == "recent", "the old name still works"
    assert client.get("/inbox/messages?view=sorted").get_json()["total"] == 7
    summary = client.get("/inbox/summary").get_json()
    assert "unread" not in summary
    labels = {c["name"]: c for c in summary["labels"]}
    assert labels["A. Personal"]["unread"] == 2, "highlights waiting in the inbox"
    assert labels["A. Read"]["unread"] == 0, "archived and read by triage: nothing waiting"


def test_people_list_keeps_family_in_the_inbox_whatever_the_model_says(client, app, monkeypatch):
    client.put("/inbox/settings", json={"people": [{"match": "deals@pine.example", "name": "Pine", "kind": "friend"}]})
    _sort(client)
    assert app.fake.threads["t-promo"]["inbox"] is True
    row = [m for m in client.get("/inbox/messages?view=all").get_json()["items"] if m["id"] == "t-promo"][0]
    assert row["personal"] is True


def test_claude_flow_export_then_paste(client, app):
    out = client.post("/inbox/triage/export", json={"limit": 2}).get_json()
    assert out["count"] == 2
    assert out["text"].startswith("You sort Michael's Gmail")
    assert "---EMAIL-1---\nID: t-mom" in out["text"]
    assert out["prompt"].startswith("You sort Michael's Gmail") and len(out["blocks"]) == 2
    assert out["blocks"][1].startswith("---EMAIL-2---\nID: t-bank")
    pasted = '```json\n{"actions":[{"id":"t-mom","labels":["A. Personal"],"importance":"high","personal":true,' \
             '"archive_ok":false,"summary":"Dinner"}],"summary":"Mom wrote."}\n```'
    batch = client.post("/inbox/triage/apply", json={"json": pasted}).get_json()["batch"]
    assert batch["status"] == "done" and batch["source"] == "claude" and batch["applied"] == 1
    bad = client.post("/inbox/triage/apply", json={"json": "nope"})
    assert bad.status_code == 400 and "valid JSON" in bad.get_json()["error"]


def test_reading_pinning_and_unread_sync_with_gmail(client, app):
    _sort(client)
    client.post("/inbox/actions", json={"ids": ["t-bank"], "action": "read"})
    assert not [a for op, a in app.fake.calls if op == "modify"], "already read in Gmail: no Gmail call"
    assert app.fake.threads["t-mom"]["unread"] is True, "triage left the highlight unread"
    r = client.post("/inbox/actions", json={"ids": ["t-mom"], "action": "read"}).get_json()
    assert r["updated"][0]["read"] is True and r["updated"][0]["gmail_unread"] is False
    assert app.fake.threads["t-mom"]["unread"] is False, "opening it here marks it read in Gmail"
    client.post("/inbox/actions", json={"ids": ["t-mom"], "action": "read"})  # twice = still one read today
    assert client.get("/inbox/summary").get_json()["today"]["read"] == 2
    r = client.post("/inbox/actions", json={"ids": ["t-mom"], "action": "unread"}).get_json()
    assert r["updated"][0]["read"] is False and app.fake.threads["t-mom"]["unread"] is True
    assert [a for op, a in app.fake.calls if op == "modify"][-1]["sorted"] == ["t-mom"]
    client.post("/inbox/actions", json={"ids": ["t-dj"], "action": "pin"})
    pins = client.get("/inbox/summary").get_json()["pins"]
    assert [p["id"] for p in pins] == ["t-dj"]
    assert [m["id"] for m in client.get("/inbox/messages?view=pinned").get_json()["items"]] == ["t-dj"]
    client.post("/inbox/actions", json={"ids": ["t-dj"], "action": "unpin"})
    assert client.get("/inbox/summary").get_json()["pins"] == []


def test_archive_counts_as_cleared_and_failures_surface(client, app):
    _sort(client)
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
    _sort(client)
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
    _sort(client)
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
    s = client.put("/inbox/settings", json={"batch_size": 500, "prompt": "  Custom  ", "auto_sort_hours": 3,
                                            "people": [{"match": "Mom@Example.org"}], "unknown": 1}).get_json()
    assert s["batch_size"] == 100 and s["prompt"] == "Custom" and "auto_sort_hours" not in s
    assert "last_export" not in s
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
    monkeypatch.setattr(ib, "spawn", lambda fn, *a: None)  # leave the first batch "applying"
    pasted = json.dumps(claude_answer(client.post("/inbox/triage/export", json={}).get_json()["text"]))
    assert client.post("/inbox/triage/apply", json={"json": pasted}).status_code == 202
    r = client.post("/inbox/triage/apply", json={"json": pasted})
    assert r.status_code == 409


def test_wegter_pc_results_apply_under_their_own_name(client, app):
    batch = _sort(client, source="wegter-pc")
    assert batch["status"] == "done" and batch["source"] == "wegter-pc" and batch["applied"] == 4
    for t in app.fake.threads.values():  # new mail arrives
        t.update(unread=True, inbox=True, messageId=t["messageId"] + "-2")
    reviewed = _sort(client, review=True, source="wegter-pc")
    done = client.post(f"/inbox/triage/batches/{reviewed['id']}/apply", json={}).get_json()["batch"]
    assert done["source"] == "wegter-pc" and done["status"] == "done"
    assert client.post("/inbox/triage/apply", json={"actions": [], "source": "wegter-pc"}).status_code == 400


def test_a_new_review_replaces_an_unfinished_one(client, app):
    first = _sort(client, review=True)
    second = _sort(client, review=True)
    assert client.get(f"/inbox/triage/batches/{first['id']}").get_json()["batch"]["status"] == "discarded"
    assert client.get(f"/inbox/triage/batches/{second['id']}").get_json()["batch"]["status"] == "review"


def test_this_server_never_calls_an_ai():
    import pathlib
    src = pathlib.Path(ib.__file__).read_text()
    assert "gh_models" not in src and "copilot" not in src.lower() and "/triage/run" not in src


def test_gmail_not_connected_is_explained(client, app, monkeypatch):
    monkeypatch.setattr(ib, "get_script_client", lambda: core.ScriptClient("", ""))
    r = client.post("/inbox/triage/export", json={})
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


class _Resp:
    def __init__(self, status, text=""):
        self.status_code, self.content = status, text.encode()


class _Session:
    def __init__(self, status, text=""):
        self.status, self.text, self.calls = status, text, 0

    def post(self, *a, **k):
        self.calls += 1
        return _Resp(self.status, self.text)


GOOD_URL = "https://script.google.com/macros/s/AKfycbxAbCdEfGhIjKlMnOp123/exec"


def test_script_url_mistakes_are_caught_before_calling_google():
    for url, words in [
        ("https://script.google.com/macros/s/AKfycbxAbC/dev", "/dev"),
        ("https://script.google.com/macros/u/0/s/AKfycbxAbC/exec", "account segment"),
        ("https://script.google.com/home/projects/1abc/edit", "doesn't look like"),
    ]:
        session = _Session(200, '{"ok": true}')
        with pytest.raises(core.ScriptError) as err:
            core.ScriptClient(url, "s", session=session).call("ping")
        assert words in str(err.value) and err.value.code == "config" and session.calls == 0
    assert core.ScriptClient(GOOD_URL + "/", "s").url_problem() == ""
    assert core.ScriptClient("https://script.google.com/a/macros/acme.com/s/AKfy123/exec", "s").url_problem() == ""


def test_google_401_explains_the_access_setting():
    with pytest.raises(core.ScriptError) as err:
        core.ScriptClient(GOOD_URL, "s", session=_Session(401, "<html>Unauthorized</html>")).call("ping")
    assert err.value.code == "access"
    assert "Who has access" in str(err.value) and "Anyone with Google account" in str(err.value)
    with pytest.raises(core.ScriptError) as err:
        core.ScriptClient(GOOD_URL, "s", session=_Session(404)).call("ping")
    assert "can't find that web app" in str(err.value)


def test_url_hint_hides_most_of_the_id():
    hint = core.ScriptClient(GOOD_URL, "s").url_hint()
    assert hint == "https://script.google.com/macros/s/AKfycb…p123/exec"
