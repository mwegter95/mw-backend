"""Client Finder 1.0 — Flask Blueprint. Mounts under /clientfinder."""
import json
import sqlite3
import csv
import io
from pathlib import Path
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, Response

clientfinder_bp = Blueprint("clientfinder", __name__, url_prefix="/clientfinder")

_DB = Path(__file__).parent / "data" / "clientfinder.db"


def _get_conn():
    conn = sqlite3.connect(str(_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_clientfinder_db():
    """Idempotent: only seeds if table is empty."""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            industry TEXT,
            city TEXT,
            state TEXT DEFAULT 'MN',
            employee_count INTEGER,
            website TEXT,
            screenshot_url TEXT,
            score_modernity REAL,
            score_mobile REAL,
            score_function REAL,
            composite_score REAL,
            outdated_stack INTEGER DEFAULT 0,
            stack_flags TEXT DEFAULT '[]',
            dm_name TEXT,
            dm_title TEXT,
            dm_seniority TEXT,
            dm_source TEXT DEFAULT 'Apollo',
            dm_linkedin TEXT,
            email TEXT,
            phone TEXT,
            contact_form_url TEXT,
            outreach_status TEXT DEFAULT 'New',
            notes TEXT DEFAULT '',
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    if count == 0:
        for lead in _SEED_LEADS:
            conn.execute("""
                INSERT INTO leads (company_name,industry,city,state,employee_count,website,
                    screenshot_url,score_modernity,score_mobile,score_function,composite_score,
                    outdated_stack,stack_flags,dm_name,dm_title,dm_seniority,dm_linkedin,
                    email,phone,contact_form_url,outreach_status,notes,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                lead['company_name'], lead['industry'], lead['city'], 'MN',
                lead['employee_count'], lead['website'], lead.get('screenshot_url', ''),
                lead['score_modernity'], lead['score_mobile'], lead['score_function'],
                lead['composite_score'], 1 if lead['outdated_stack'] else 0,
                json.dumps(lead.get('stack_flags', [])),
                lead.get('dm_name', ''), lead.get('dm_title', ''), lead.get('dm_seniority', ''),
                lead.get('dm_linkedin', ''), lead.get('email'), lead.get('phone'),
                lead.get('contact_form_url'),
                lead.get('outreach_status', 'New'), lead.get('notes', ''),
                lead.get('created_at', _now()), _now()
            ))
        conn.commit()
    conn.close()


# ─── Routes ────────────────────────────────────────────────────────────────────

@clientfinder_bp.route("/leads", methods=["GET"])
def get_leads():
    conn = _get_conn()
    q = "SELECT * FROM leads WHERE 1=1"
    params = []
    if industry := request.args.get('industry'):
        q += " AND industry=?"; params.append(industry)
    if status := request.args.get('status'):
        q += " AND outreach_status=?"; params.append(status)
    if region := request.args.get('region'):
        tc = ('Minneapolis','St. Paul','Bloomington','Edina','Eden Prairie','Plymouth',
              'Minnetonka','Eagan','Roseville','St. Louis Park','Burnsville','Golden Valley',
              'Woodbury','Brooklyn Park')
        placeholders = ','.join('?' * len(tc))
        if region == 'twin_cities':
            q += f" AND city IN ({placeholders})"; params.extend(tc)
        elif region == 'greater_mn':
            q += f" AND city NOT IN ({placeholders})"; params.extend(tc)
    q += " ORDER BY created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    leads = []
    for r in rows:
        d = dict(r)
        d['stack_flags'] = json.loads(d.get('stack_flags') or '[]')
        d['outdated_stack'] = bool(d['outdated_stack'])
        leads.append(d)
    return jsonify({"leads": leads})


@clientfinder_bp.route("/leads", methods=["POST"])
def add_lead():
    data = request.get_json(silent=True) or {}
    items = data if isinstance(data, list) else [data]
    conn = _get_conn()
    ids = []
    for item in items:
        cur = conn.execute("""
            INSERT INTO leads (company_name,industry,city,employee_count,website,
                screenshot_url,score_modernity,score_mobile,score_function,composite_score,
                outdated_stack,stack_flags,dm_name,dm_title,dm_seniority,email,phone,
                contact_form_url,outreach_status,notes,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            item.get('company_name', ''), item.get('industry', ''), item.get('city', ''),
            item.get('employee_count'), item.get('website', ''), item.get('screenshot_url', ''),
            item.get('score_modernity', 5), item.get('score_mobile', 5), item.get('score_function', 5),
            item.get('composite_score', 5), 1 if item.get('outdated_stack') else 0,
            json.dumps(item.get('stack_flags', [])), item.get('dm_name', ''), item.get('dm_title', ''),
            item.get('dm_seniority', ''), item.get('email'), item.get('phone'),
            item.get('contact_form_url'), item.get('outreach_status', 'New'), item.get('notes', ''),
            _now(), _now()
        ))
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "ids": ids}), 201


@clientfinder_bp.route("/leads/<int:lead_id>", methods=["PATCH"])
def update_lead(lead_id):
    data = request.get_json(silent=True) or {}
    allowed = {'outreach_status', 'notes', 'score_modernity', 'score_mobile', 'score_function',
               'composite_score', 'outdated_stack', 'stack_flags', 'dm_name', 'dm_title',
               'dm_seniority', 'email', 'phone', 'contact_form_url'}
    sets, params = [], []
    for k, v in data.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(
                json.dumps(v) if k == 'stack_flags'
                else (1 if v is True else (0 if v is False else v))
            )
    if not sets:
        return jsonify({"ok": False, "error": "no valid fields"}), 400
    sets.append("updated_at=?")
    params.append(_now())
    params.append(lead_id)
    conn = _get_conn()
    conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@clientfinder_bp.route("/leads/<int:lead_id>", methods=["DELETE"])
def delete_lead(lead_id):
    conn = _get_conn()
    conn.execute("DELETE FROM leads WHERE id=?", (lead_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@clientfinder_bp.route("/leads/export", methods=["GET"])
def export_leads():
    conn = _get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM leads ORDER BY created_at DESC").fetchall()]
    conn.close()
    out = io.StringIO()
    if rows:
        w = csv.DictWriter(out, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    return Response(
        out.getvalue(),
        mimetype='text/csv',
        headers={"Content-Disposition": "attachment; filename=client-finder-leads.csv"}
    )


@clientfinder_bp.route("/leads/stats", methods=["GET"])
def lead_stats():
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    by_status = {r[0]: r[1] for r in conn.execute(
        "SELECT outreach_status, COUNT(*) FROM leads GROUP BY outreach_status").fetchall()}
    avg_score = conn.execute("SELECT AVG(composite_score) FROM leads").fetchone()[0]
    outdated_count = conn.execute("SELECT COUNT(*) FROM leads WHERE outdated_stack=1").fetchone()[0]
    by_industry = {r[0]: r[1] for r in conn.execute(
        "SELECT industry, COUNT(*) FROM leads GROUP BY industry").fetchall()}
    conn.close()
    return jsonify({
        "total": total,
        "by_status": by_status,
        "avg_composite_score": round(avg_score or 0, 1),
        "outdated_stack_count": outdated_count,
        "outdated_stack_pct": round((outdated_count / total * 100) if total else 0, 1),
        "by_industry": by_industry,
    })


# ─── Seed data (50 MN leads) ──────────────────────────────────────────────────

_SEED_LEADS = [
  # ENTERTAINMENT (7)
  {"company_name":"Memory Lanes Entertainment","industry":"Entertainment","city":"Minneapolis","employee_count":28,"website":"memorylanes.com","score_modernity":3,"score_mobile":2,"score_function":3,"composite_score":2.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout"],"dm_name":"Dale R.","dm_title":"Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@memorylanes.com","phone":"(612) 788-8188","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-05-28T14:22:00Z"},
  {"company_name":"Brunswick Zone Brooklyn Park","industry":"Entertainment","city":"Brooklyn Park","employee_count":45,"website":"brunswickzone.com","score_modernity":5,"score_mobile":5,"score_function":6,"composite_score":5.3,"outdated_stack":False,"stack_flags":["Outdated jQuery"],"dm_name":"Mark T.","dm_title":"General Manager","dm_seniority":"Manager","dm_linkedin":"","email":None,"phone":"(763) 315-8200","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-05-29T09:10:00Z"},
  {"company_name":"Pinstripes Edina","industry":"Entertainment","city":"Edina","employee_count":120,"website":"pinstripes.com","score_modernity":7,"score_mobile":7,"score_function":7,"composite_score":7.0,"outdated_stack":False,"stack_flags":[],"dm_name":"Amy K.","dm_title":"Director of Operations","dm_seniority":"Director","dm_linkedin":"https://linkedin.com/in/amyk-pinstripes","email":None,"phone":"(952) 835-0090","contact_form_url":None,"outreach_status":"Ignored","notes":"Too large, well-established web presence","created_at":"2026-05-29T10:05:00Z"},
  {"company_name":"XP League Gaming Lounge","industry":"Entertainment","city":"Minneapolis","employee_count":12,"website":"xpleague.gg","score_modernity":7,"score_mobile":6,"score_function":5,"composite_score":6.0,"outdated_stack":False,"stack_flags":["No meta viewport"],"dm_name":"Chris M.","dm_title":"Franchise Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"minneapolis@xpleague.gg","phone":None,"contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-05-30T08:45:00Z"},
  {"company_name":"Brookview Golf Course","industry":"Entertainment","city":"Golden Valley","employee_count":22,"website":"goldenvalleymn.gov","score_modernity":2,"score_mobile":2,"score_function":3,"composite_score":2.3,"outdated_stack":True,"stack_flags":["Non-responsive layout","Missing HTTPS","Outdated jQuery","No meta viewport"],"dm_name":"Susan L.","dm_title":"Recreation Director","dm_seniority":"Director","dm_linkedin":"","email":"brookview@goldenvalleymn.gov","phone":"(763) 512-2300","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-05-30T11:30:00Z"},
  {"company_name":"Wild Woods Family Entertainment","industry":"Entertainment","city":"Duluth","employee_count":35,"website":"wildwoodsduluth.com","score_modernity":3,"score_mobile":3,"score_function":3,"composite_score":3.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Todd B.","dm_title":"Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@wildwoodsduluth.com","phone":"(218) 729-7529","contact_form_url":"wildwoodsduluth.com/contact","outreach_status":"Contacted","notes":"Left voicemail 6/2","created_at":"2026-06-01T13:20:00Z"},
  {"company_name":"The Machine Shop Minneapolis","industry":"Entertainment","city":"Minneapolis","employee_count":55,"website":"themachineshopmpls.com","score_modernity":5,"score_mobile":4,"score_function":6,"composite_score":5.0,"outdated_stack":False,"stack_flags":["Outdated jQuery","No meta viewport"],"dm_name":"Ryan P.","dm_title":"Venue Director","dm_seniority":"Director","dm_linkedin":"","email":"booking@themachineshopmpls.com","phone":"(612) 722-1111","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-02T09:00:00Z"},
  # PROFESSIONAL SERVICES (10)
  {"company_name":"Periscope Creative","industry":"Professional Services","city":"Minneapolis","employee_count":75,"website":"periscope.com","score_modernity":8,"score_mobile":8,"score_function":7,"composite_score":7.7,"outdated_stack":False,"stack_flags":[],"dm_name":"Sarah J.","dm_title":"Chief Executive Officer","dm_seniority":"C-Suite","dm_linkedin":"https://linkedin.com/in/sarahj-periscope","email":None,"phone":"(612) 399-0600","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-05-28T15:00:00Z"},
  {"company_name":"Mono Advertising","industry":"Professional Services","city":"Minneapolis","employee_count":40,"website":"monoculture.com","score_modernity":8,"score_mobile":7,"score_function":8,"composite_score":7.7,"outdated_stack":False,"stack_flags":[],"dm_name":"James H.","dm_title":"Co-Founder & CCO","dm_seniority":"C-Suite","dm_linkedin":"https://linkedin.com/in/jamesh-mono","email":"hello@monoculture.com","phone":None,"contact_form_url":None,"outreach_status":"In Progress","notes":"Email sent 6/5, awaiting reply","created_at":"2026-05-29T14:00:00Z"},
  {"company_name":"Zeus Jones Marketing","industry":"Professional Services","city":"Minneapolis","employee_count":30,"website":"zeusjones.com","score_modernity":7,"score_mobile":6,"score_function":7,"composite_score":6.7,"outdated_stack":False,"stack_flags":[],"dm_name":"Bill H.","dm_title":"Founding Partner","dm_seniority":"C-Suite","dm_linkedin":"https://linkedin.com/in/billh-zeusjones","email":None,"phone":"(612) 200-6262","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-05-30T10:15:00Z"},
  {"company_name":"North Loop Creative Agency","industry":"Professional Services","city":"Minneapolis","employee_count":15,"website":"northloopcreative.com","score_modernity":4,"score_mobile":3,"score_function":4,"composite_score":3.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","No SSL certificate","Non-responsive layout"],"dm_name":"Lisa M.","dm_title":"Creative Director","dm_seniority":"C-Suite","dm_linkedin":"","email":"hello@northloopcreative.com","phone":"(612) 555-0141","contact_form_url":"northloopcreative.com/contact","outreach_status":"Contacted","notes":"Emailed 6/3 — no response yet","created_at":"2026-06-01T08:30:00Z"},
  {"company_name":"Summit Law Group","industry":"Professional Services","city":"Minneapolis","employee_count":18,"website":"summitlawmn.com","score_modernity":3,"score_mobile":2,"score_function":3,"composite_score":2.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout","Outdated jQuery"],"dm_name":"David K.","dm_title":"Managing Partner","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@summitlawmn.com","phone":"(612) 555-0213","contact_form_url":"summitlawmn.com/contact","outreach_status":"Contacted","notes":"Reached out via contact form 6/4","created_at":"2026-06-02T11:00:00Z"},
  {"company_name":"Felhaber Larson Law","industry":"Professional Services","city":"St. Paul","employee_count":50,"website":"felhaber.com","score_modernity":4,"score_mobile":5,"score_function":5,"composite_score":4.7,"outdated_stack":False,"stack_flags":["Outdated jQuery"],"dm_name":"Patricia W.","dm_title":"Managing Partner","dm_seniority":"C-Suite","dm_linkedin":"","email":None,"phone":"(651) 222-5005","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-02T13:45:00Z"},
  {"company_name":"Granite Accounting Partners","industry":"Professional Services","city":"Bloomington","employee_count":22,"website":"graniteaccounting.com","score_modernity":3,"score_mobile":2,"score_function":3,"composite_score":2.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","No meta viewport"],"dm_name":"Greg F.","dm_title":"CPA & Principal","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@graniteaccounting.com","phone":"(952) 555-0182","contact_form_url":"graniteaccounting.com/contact","outreach_status":"New","notes":"","created_at":"2026-06-03T09:20:00Z"},
  {"company_name":"Northfield Architecture Studio","industry":"Professional Services","city":"Minneapolis","employee_count":8,"website":"northfieldarchstudio.com","score_modernity":4,"score_mobile":3,"score_function":4,"composite_score":3.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Outdated jQuery","Non-responsive layout"],"dm_name":"Anna C.","dm_title":"Principal Architect","dm_seniority":"C-Suite","dm_linkedin":"","email":"studio@northfieldarchstudio.com","phone":"(612) 555-0094","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-03T10:30:00Z"},
  {"company_name":"Lakeside Accounting Group","industry":"Professional Services","city":"Edina","employee_count":12,"website":"lakesideaccounting.com","score_modernity":2,"score_mobile":2,"score_function":3,"composite_score":2.3,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout","No meta viewport"],"dm_name":"Robert M.","dm_title":"Founding Partner","dm_seniority":"C-Suite","dm_linkedin":"","email":"contact@lakesideaccounting.com","phone":"(952) 555-0273","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-03T14:00:00Z"},
  {"company_name":"Prairie Marketing Collective","industry":"Professional Services","city":"St. Cloud","employee_count":9,"website":"prairiemarketingco.com","score_modernity":3,"score_mobile":3,"score_function":3,"composite_score":3.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout"],"dm_name":"Michelle H.","dm_title":"Owner & Strategist","dm_seniority":"C-Suite","dm_linkedin":"","email":"hello@prairiemarketingco.com","phone":"(320) 555-0148","contact_form_url":"prairiemarketingco.com/contact","outreach_status":"Contacted","notes":"Intro call scheduled 6/10","created_at":"2026-06-04T08:00:00Z"},
  # HOME & COMMERCIAL SERVICES (9)
  {"company_name":"Genz-Ryan Heating & Cooling","industry":"Home & Commercial Services","city":"Burnsville","employee_count":120,"website":"genzryan.com","score_modernity":6,"score_mobile":6,"score_function":6,"composite_score":6.0,"outdated_stack":False,"stack_flags":["Outdated jQuery"],"dm_name":"Jim R.","dm_title":"President","dm_seniority":"C-Suite","dm_linkedin":"https://linkedin.com/in/jimr-genzryan","email":None,"phone":"(952) 767-1000","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-05-28T16:00:00Z"},
  {"company_name":"Sedgwick Heating & Air Conditioning","industry":"Home & Commercial Services","city":"Minneapolis","employee_count":35,"website":"sedgwickheating.com","score_modernity":3,"score_mobile":2,"score_function":3,"composite_score":2.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Missing HTTPS"],"dm_name":"Kevin S.","dm_title":"Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@sedgwickheating.com","phone":"(612) 827-2561","contact_form_url":"sedgwickheating.com/contact","outreach_status":"Contacted","notes":"Spoke with receptionist 6/5","created_at":"2026-06-01T15:00:00Z"},
  {"company_name":"Standard Heating & Air Conditioning","industry":"Home & Commercial Services","city":"Minneapolis","employee_count":55,"website":"standardheating.com","score_modernity":5,"score_mobile":5,"score_function":5,"composite_score":5.0,"outdated_stack":False,"stack_flags":["Outdated jQuery","No meta viewport"],"dm_name":"Tom A.","dm_title":"CEO","dm_seniority":"C-Suite","dm_linkedin":"","email":"contact@standardheating.com","phone":"(612) 824-3981","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-02T09:30:00Z"},
  {"company_name":"All American Plumbing & Heating","industry":"Home & Commercial Services","city":"Roseville","employee_count":18,"website":"allamericanplumbingmn.com","score_modernity":2,"score_mobile":2,"score_function":2,"composite_score":2.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout","No meta viewport","No SSL certificate"],"dm_name":"Bob D.","dm_title":"Owner & Master Plumber","dm_seniority":"C-Suite","dm_linkedin":"","email":"bob@allamericanplumbingmn.com","phone":"(651) 555-0237","contact_form_url":"allamericanplumbingmn.com/contact","outreach_status":"New","notes":"","created_at":"2026-06-02T10:45:00Z"},
  {"company_name":"Northshore Landscaping & Design","industry":"Home & Commercial Services","city":"Duluth","employee_count":25,"website":"northshorelandscaping.com","score_modernity":3,"score_mobile":3,"score_function":3,"composite_score":3.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Paul N.","dm_title":"Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"paul@northshorelandscaping.com","phone":"(218) 555-0134","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-03T11:00:00Z"},
  {"company_name":"Great Lawns Landscaping","industry":"Home & Commercial Services","city":"Bloomington","employee_count":14,"website":"greatlawnsbloomington.com","score_modernity":2,"score_mobile":2,"score_function":2,"composite_score":2.0,"outdated_stack":True,"stack_flags":["Missing HTTPS","Non-responsive layout","No SSL certificate","No meta viewport"],"dm_name":"Dave W.","dm_title":"Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@greatlawnsbloomington.com","phone":"(952) 555-0189","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-03T13:30:00Z"},
  {"company_name":"Superior Commercial Cleaning","industry":"Home & Commercial Services","city":"St. Cloud","employee_count":40,"website":"superiorcleaningmn.com","score_modernity":3,"score_mobile":2,"score_function":3,"composite_score":2.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Missing HTTPS"],"dm_name":"Maria G.","dm_title":"Operations Manager","dm_seniority":"Manager","dm_linkedin":"","email":"maria@superiorcleaningmn.com","phone":"(320) 555-0211","contact_form_url":"superiorcleaningmn.com/contact","outreach_status":"In Progress","notes":"Proposal sent 6/8, reviewing","created_at":"2026-06-04T09:00:00Z"},
  {"company_name":"Lakes Area Electric","industry":"Home & Commercial Services","city":"Brainerd","employee_count":12,"website":"lakesareaelectric.com","score_modernity":3,"score_mobile":3,"score_function":3,"composite_score":3.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Outdated jQuery","Non-responsive layout"],"dm_name":"Eric H.","dm_title":"Master Electrician & Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"eric@lakesareaelectric.com","phone":"(218) 555-0167","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-04T11:15:00Z"},
  {"company_name":"Tri-City Building Services","industry":"Home & Commercial Services","city":"Moorhead","employee_count":28,"website":"tricitybuilding.com","score_modernity":2,"score_mobile":2,"score_function":2,"composite_score":2.0,"outdated_stack":True,"stack_flags":["Missing HTTPS","Non-responsive layout","No meta viewport","No SSL certificate"],"dm_name":"Mike B.","dm_title":"President","dm_seniority":"C-Suite","dm_linkedin":"","email":"office@tricitybuilding.com","phone":"(218) 555-0093","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-04T14:00:00Z"},
  # HEALTHCARE & WELLNESS (9)
  {"company_name":"Uptown Dental Studio","industry":"Healthcare & Wellness","city":"Minneapolis","employee_count":15,"website":"uptowndentalstudio.com","score_modernity":5,"score_mobile":5,"score_function":5,"composite_score":5.0,"outdated_stack":False,"stack_flags":["Outdated jQuery"],"dm_name":"Dr. Jennifer L.","dm_title":"Lead Dentist & Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"appointments@uptowndentalstudio.com","phone":"(612) 824-4600","contact_form_url":"uptowndentalstudio.com/contact","outreach_status":"Contacted","notes":"Emailed owner directly 6/4","created_at":"2026-06-03T15:00:00Z"},
  {"company_name":"Summit Dental Partners","industry":"Healthcare & Wellness","city":"Edina","employee_count":22,"website":"summitdentalpartners.com","score_modernity":4,"score_mobile":3,"score_function":4,"composite_score":3.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Dr. Kevin R.","dm_title":"Owner & General Dentist","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@summitdentalpartners.com","phone":"(952) 555-0188","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-04T08:30:00Z"},
  {"company_name":"Lakes Area Family Dentistry","industry":"Healthcare & Wellness","city":"Brainerd","employee_count":10,"website":"lakesareadentistry.com","score_modernity":2,"score_mobile":2,"score_function":2,"composite_score":2.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout","No meta viewport"],"dm_name":"Dr. Thomas W.","dm_title":"Owner & DDS","dm_seniority":"C-Suite","dm_linkedin":"","email":"office@lakesareadentistry.com","phone":"(218) 555-0142","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-04T10:00:00Z"},
  {"company_name":"Rochester Family Dentistry","industry":"Healthcare & Wellness","city":"Rochester","employee_count":18,"website":"rochesterfamilydentistry.com","score_modernity":3,"score_mobile":3,"score_function":3,"composite_score":3.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Dr. Susan O.","dm_title":"Owner & Dentist","dm_seniority":"C-Suite","dm_linkedin":"","email":"appointments@rochesterfamilydentistry.com","phone":"(507) 555-0215","contact_form_url":"rochesterfamilydentistry.com/appointment","outreach_status":"New","notes":"","created_at":"2026-06-04T13:00:00Z"},
  {"company_name":"Lakeside Physical Therapy","industry":"Healthcare & Wellness","city":"Minnetonka","employee_count":12,"website":"lakesideptmn.com","score_modernity":4,"score_mobile":3,"score_function":4,"composite_score":3.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Amy T.","dm_title":"Clinical Director","dm_seniority":"Director","dm_linkedin":"","email":"info@lakesideptmn.com","phone":"(952) 555-0176","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-05T09:00:00Z"},
  {"company_name":"North Star Chiropractic","industry":"Healthcare & Wellness","city":"Eagan","employee_count":8,"website":"northstarchiro.com","score_modernity":3,"score_mobile":2,"score_function":3,"composite_score":2.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout"],"dm_name":"Dr. Mark F.","dm_title":"Owner & Chiropractor","dm_seniority":"C-Suite","dm_linkedin":"","email":"drmark@northstarchiro.com","phone":"(651) 555-0122","contact_form_url":"northstarchiro.com/schedule","outreach_status":"New","notes":"","created_at":"2026-06-05T10:30:00Z"},
  {"company_name":"FORM Wellness Collective","industry":"Healthcare & Wellness","city":"Minneapolis","employee_count":20,"website":"formwellnesscollective.com","score_modernity":7,"score_mobile":7,"score_function":6,"composite_score":6.7,"outdated_stack":False,"stack_flags":[],"dm_name":"Claire B.","dm_title":"Founder & CEO","dm_seniority":"C-Suite","dm_linkedin":"https://linkedin.com/in/claireb-form","email":"hello@formwellnesscollective.com","phone":"(612) 555-0108","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-05T12:00:00Z"},
  {"company_name":"Skin Perfect Med Spa","industry":"Healthcare & Wellness","city":"Eden Prairie","employee_count":10,"website":"skinperfectmedspa.com","score_modernity":5,"score_mobile":4,"score_function":5,"composite_score":4.7,"outdated_stack":False,"stack_flags":["Outdated jQuery","No meta viewport"],"dm_name":"Natalie K.","dm_title":"Owner & Aesthetician","dm_seniority":"C-Suite","dm_linkedin":"","email":"appointments@skinperfectmedspa.com","phone":"(952) 555-0194","contact_form_url":"skinperfectmedspa.com/book","outreach_status":"New","notes":"","created_at":"2026-06-05T14:00:00Z"},
  {"company_name":"Iron Fitness MN","industry":"Healthcare & Wellness","city":"Plymouth","employee_count":16,"website":"ironfitnessplymouth.com","score_modernity":4,"score_mobile":3,"score_function":4,"composite_score":3.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Jason M.","dm_title":"Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@ironfitnessplymouth.com","phone":"(763) 555-0139","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-05T15:30:00Z"},
  # RETAIL & HOSPITALITY (8)
  {"company_name":"Indeed Brewing Company","industry":"Retail & Hospitality","city":"Minneapolis","employee_count":42,"website":"indeedbrewing.com","score_modernity":7,"score_mobile":7,"score_function":7,"composite_score":7.0,"outdated_stack":False,"stack_flags":[],"dm_name":"Tom H.","dm_title":"Co-Founder","dm_seniority":"C-Suite","dm_linkedin":"https://linkedin.com/in/tomh-indeed","email":"hello@indeedbrewing.com","phone":"(612) 843-5090","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-01T09:00:00Z"},
  {"company_name":"Fulton Beer","industry":"Retail & Hospitality","city":"Minneapolis","employee_count":38,"website":"fultonbeer.com","score_modernity":6,"score_mobile":6,"score_function":6,"composite_score":6.0,"outdated_stack":False,"stack_flags":["Outdated jQuery"],"dm_name":"Ryan P.","dm_title":"Co-Founder & COO","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@fultonbeer.com","phone":"(612) 333-3208","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-01T10:30:00Z"},
  {"company_name":"Lake & City Brewing Co.","industry":"Retail & Hospitality","city":"St. Paul","employee_count":18,"website":"lakeandcitybrewing.com","score_modernity":4,"score_mobile":3,"score_function":4,"composite_score":3.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Mike C.","dm_title":"Founder & Head Brewer","dm_seniority":"C-Suite","dm_linkedin":"","email":"mike@lakeandcitybrewing.com","phone":"(651) 555-0156","contact_form_url":"lakeandcitybrewing.com/contact","outreach_status":"Contacted","notes":"Sent intro email 6/6","created_at":"2026-06-04T16:00:00Z"},
  {"company_name":"Tattersall Distilling","industry":"Retail & Hospitality","city":"Minneapolis","employee_count":28,"website":"tattersalldistilling.com","score_modernity":7,"score_mobile":6,"score_function":7,"composite_score":6.7,"outdated_stack":False,"stack_flags":[],"dm_name":"Dan S.","dm_title":"Co-Founder & CEO","dm_seniority":"C-Suite","dm_linkedin":"https://linkedin.com/in/dans-tattersall","email":"info@tattersalldistilling.com","phone":"(612) 584-4152","contact_form_url":None,"outreach_status":"Ignored","notes":"Strong site, not a priority","created_at":"2026-06-04T17:00:00Z"},
  {"company_name":"Birchwood Cafe","industry":"Retail & Hospitality","city":"Minneapolis","employee_count":45,"website":"birchwoodcafe.com","score_modernity":5,"score_mobile":4,"score_function":5,"composite_score":4.7,"outdated_stack":False,"stack_flags":["Outdated jQuery","No meta viewport"],"dm_name":"Tracy R.","dm_title":"Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@birchwoodcafe.com","phone":"(612) 722-4474","contact_form_url":"birchwoodcafe.com/contact","outreach_status":"In Progress","notes":"Had a call 6/9 — interested in custom reservation system","created_at":"2026-06-05T08:00:00Z"},
  {"company_name":"City Garage Auto Repair","industry":"Retail & Hospitality","city":"Minneapolis","employee_count":14,"website":"citygaragempls.com","score_modernity":2,"score_mobile":2,"score_function":2,"composite_score":2.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout","No SSL certificate","No meta viewport"],"dm_name":"Steve M.","dm_title":"Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"steve@citygaragempls.com","phone":"(612) 555-0261","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-05T09:30:00Z"},
  {"company_name":"Rochester Farmers Market Hub","industry":"Retail & Hospitality","city":"Rochester","employee_count":6,"website":"rochesterfarmersmarket.com","score_modernity":3,"score_mobile":2,"score_function":3,"composite_score":2.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","No meta viewport"],"dm_name":"Linda H.","dm_title":"Executive Director","dm_seniority":"Director","dm_linkedin":"","email":"info@rochesterfarmersmarket.com","phone":"(507) 555-0178","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-06T10:00:00Z"},
  {"company_name":"Northern Threads Boutique","industry":"Retail & Hospitality","city":"Mankato","employee_count":8,"website":"northernthreadsboutique.com","score_modernity":4,"score_mobile":3,"score_function":4,"composite_score":3.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Jenna L.","dm_title":"Owner & Buyer","dm_seniority":"C-Suite","dm_linkedin":"","email":"jenna@northernthreadsboutique.com","phone":"(507) 555-0203","contact_form_url":"northernthreadsboutique.com/contact","outreach_status":"Contacted","notes":"DM'd on Instagram 6/7","created_at":"2026-06-06T11:30:00Z"},
  # MANUFACTURING & LOGISTICS (7)
  {"company_name":"Great Northern Machining","industry":"Manufacturing & Logistics","city":"St. Cloud","employee_count":65,"website":"greatnorthernmachining.com","score_modernity":2,"score_mobile":2,"score_function":2,"composite_score":2.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout","No SSL certificate","No meta viewport"],"dm_name":"Carl B.","dm_title":"President & Owner","dm_seniority":"C-Suite","dm_linkedin":"","email":"carl@greatnorthernmachining.com","phone":"(320) 555-0147","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-06T13:00:00Z"},
  {"company_name":"Precision Machine Works","industry":"Manufacturing & Logistics","city":"Mankato","employee_count":35,"website":"precisionmachineworks.com","score_modernity":2,"score_mobile":2,"score_function":3,"composite_score":2.3,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Missing HTTPS","Outdated jQuery"],"dm_name":"Richard F.","dm_title":"Owner & CNC Specialist","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@precisionmachineworks.com","phone":"(507) 555-0166","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-06T14:30:00Z"},
  {"company_name":"Northland Logistics Solutions","industry":"Manufacturing & Logistics","city":"Duluth","employee_count":85,"website":"northlandlogistics.com","score_modernity":3,"score_mobile":3,"score_function":4,"composite_score":3.3,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Gary L.","dm_title":"CEO","dm_seniority":"C-Suite","dm_linkedin":"https://linkedin.com/in/garyl-northland","email":"operations@northlandlogistics.com","phone":"(218) 555-0133","contact_form_url":"northlandlogistics.com/quote","outreach_status":"Contacted","notes":"Called main line 6/9","created_at":"2026-06-07T08:00:00Z"},
  {"company_name":"Minnesota Metal Fabricators","industry":"Manufacturing & Logistics","city":"Bloomington","employee_count":45,"website":"mnmetalfab.com","score_modernity":2,"score_mobile":2,"score_function":2,"composite_score":2.0,"outdated_stack":True,"stack_flags":["Missing HTTPS","Non-responsive layout","No SSL certificate","No meta viewport"],"dm_name":"Dennis S.","dm_title":"Owner & Plant Manager","dm_seniority":"C-Suite","dm_linkedin":"","email":"dennis@mnmetalfab.com","phone":"(952) 555-0181","contact_form_url":None,"outreach_status":"Ignored","notes":"Declined — not looking for web work","created_at":"2026-06-07T09:30:00Z"},
  {"company_name":"River Valley Food Producers","industry":"Manufacturing & Logistics","city":"Winona","employee_count":55,"website":"rivervalleyfood.com","score_modernity":3,"score_mobile":3,"score_function":3,"composite_score":3.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Ellen P.","dm_title":"President","dm_seniority":"C-Suite","dm_linkedin":"","email":"info@rivervalleyfood.com","phone":"(507) 555-0192","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-07T11:00:00Z"},
  {"company_name":"Twin Cities Metal Works","industry":"Manufacturing & Logistics","city":"St. Louis Park","employee_count":28,"website":"twincitiesmetalworks.com","score_modernity":2,"score_mobile":2,"score_function":2,"composite_score":2.0,"outdated_stack":True,"stack_flags":["Legacy WordPress","Missing HTTPS","Non-responsive layout","No SSL certificate"],"dm_name":"Bruce N.","dm_title":"Shop Owner & Welder","dm_seniority":"C-Suite","dm_linkedin":"","email":"bruce@twincitiesmetalworks.com","phone":"(952) 555-0116","contact_form_url":None,"outreach_status":"New","notes":"","created_at":"2026-06-07T13:00:00Z"},
  {"company_name":"North Star Freight Solutions","industry":"Manufacturing & Logistics","city":"Moorhead","employee_count":95,"website":"northstarfreight.com","score_modernity":4,"score_mobile":3,"score_function":4,"composite_score":3.7,"outdated_stack":True,"stack_flags":["Legacy WordPress","Non-responsive layout","Outdated jQuery"],"dm_name":"Mike H.","dm_title":"VP of Operations","dm_seniority":"Director","dm_linkedin":"https://linkedin.com/in/mikeh-northstar","email":"dispatch@northstarfreight.com","phone":"(218) 555-0178","contact_form_url":"northstarfreight.com/contact","outreach_status":"In Progress","notes":"RFP drafted, sending 6/12","created_at":"2026-06-08T09:00:00Z"},
]

# Auto-initialize on import
init_clientfinder_db()
