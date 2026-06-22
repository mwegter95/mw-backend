"""
Solo Law CMS — Flask Blueprint
Mounts under /solo-law on mw-backend.
Auth: per-boot JWT secret (demo). Storage: SQLite WAL at data/mw.db.
OOP design: SoloLawDB, PracticeAreaModel, PublicationModel, BrandAssetModel, UserModel, SeedManager.
"""
import sqlite3
import datetime
import secrets
import re
from pathlib import Path
from functools import wraps

import bcrypt
import jwt as _jwt
from flask import Blueprint, request, jsonify

solo_law_bp = Blueprint("solo_law", __name__, url_prefix="/solo-law")

_JWT_SECRET = secrets.token_hex(32)
_JWT_ALG = "HS256"
_JWT_TTL = datetime.timedelta(hours=24)

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "mw.db"


# ── DB helpers ──────────────────────────────────────────────────────────────

class SoloLawDB:
    """Context-manager wrapper; each request opens its own short-lived conn."""
    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")

    def __enter__(self): return self.conn
    def __exit__(self, *_): self.conn.close()


def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return s.strip("-")


# ── Auth ────────────────────────────────────────────────────────────────────

def _make_token(email, role):
    payload = {
        "email": email,
        "role": role,
        "exp": datetime.datetime.utcnow() + _JWT_TTL,
    }
    return _jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALG)


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401
        try:
            request.sl_user = _jwt.decode(
                auth[7:], _JWT_SECRET, algorithms=[_JWT_ALG]
            )
        except _jwt.ExpiredSignatureError:
            return jsonify({"error": "Token expired"}), 401
        except Exception:
            return jsonify({"error": "Invalid token"}), 401
        return f(*args, **kwargs)
    return wrapper


# ── Models ───────────────────────────────────────────────────────────────────

class UserModel:
    @staticmethod
    def get_by_email(email):
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM sl_cms_users WHERE email=?", (email,)
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def verify(email, password):
        user = UserModel.get_by_email(email)
        if not user:
            return None
        if bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
            return user
        return None


class PracticeAreaModel:
    @staticmethod
    def get_all(locale="en", published_only=True):
        with _db() as conn:
            q = "SELECT * FROM sl_practice_areas WHERE locale=?"
            params = [locale]
            if published_only:
                q += " AND published=1"
            q += " ORDER BY display_order"
            return [dict(r) for r in conn.execute(q, params)]

    @staticmethod
    def get_by_slug(slug, locale="en"):
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM sl_practice_areas WHERE slug=? AND locale=?",
                (slug, locale)
            ).fetchone()
            if not row:
                return None
            pa = dict(row)
            # include related publications
            pubs = conn.execute(
                "SELECT * FROM sl_publications WHERE practice_area_id=? AND locale=? AND published=1 ORDER BY published_at DESC LIMIT 5",
                (pa["id"], locale)
            ).fetchall()
            pa["publications"] = [dict(p) for p in pubs]
            return pa

    @staticmethod
    def get_by_id(pa_id):
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM sl_practice_areas WHERE id=?", (pa_id,)
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def create(data):
        slug = data.get("slug") or _slugify(data.get("title", ""))
        now = datetime.datetime.utcnow().isoformat()
        with _db() as conn:
            cur = conn.execute(
                """INSERT INTO sl_practice_areas
                   (slug, locale, title, tagline, body_html, icon_emoji, hero_image_url, display_order, published, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (slug, data.get("locale", "en"), data.get("title", ""),
                 data.get("tagline", ""), data.get("body_html", ""),
                 data.get("icon_emoji", ""), data.get("hero_image_url", ""),
                 data.get("display_order", 99), 1 if data.get("published", True) else 0, now)
            )
            conn.commit()
            return cur.lastrowid

    @staticmethod
    def update(pa_id, data):
        now = datetime.datetime.utcnow().isoformat()
        with _db() as conn:
            conn.execute(
                """UPDATE sl_practice_areas SET
                   locale=?, title=?, tagline=?, body_html=?, icon_emoji=?,
                   hero_image_url=?, display_order=?, published=?, updated_at=?
                   WHERE id=?""",
                (data.get("locale", "en"), data.get("title", ""),
                 data.get("tagline", ""), data.get("body_html", ""),
                 data.get("icon_emoji", ""), data.get("hero_image_url", ""),
                 data.get("display_order", 1), 1 if data.get("published", True) else 0,
                 now, pa_id)
            )
            conn.commit()

    @staticmethod
    def delete(pa_id):
        with _db() as conn:
            conn.execute("DELETE FROM sl_practice_areas WHERE id=?", (pa_id,))
            conn.commit()


class PublicationModel:
    @staticmethod
    def get_all(locale="en", practice_area_id=None, page=1, per_page=10, published_only=True):
        with _db() as conn:
            q = "SELECT * FROM sl_publications WHERE locale=?"
            params = [locale]
            if published_only:
                q += " AND published=1"
            if practice_area_id:
                q += " AND practice_area_id=?"
                params.append(practice_area_id)
            q += " ORDER BY published_at DESC"
            total = conn.execute(q.replace("*", "COUNT(*)"), params).fetchone()[0]
            q += " LIMIT ? OFFSET ?"
            params += [per_page, (page - 1) * per_page]
            items = [dict(r) for r in conn.execute(q, params)]
            return {"items": items, "total": total, "page": page, "per_page": per_page}

    @staticmethod
    def get_by_slug(slug, locale="en"):
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM sl_publications WHERE slug=? AND locale=?",
                (slug, locale)
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def get_by_id(pub_id):
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM sl_publications WHERE id=?", (pub_id,)
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def create(data):
        slug = data.get("slug") or _slugify(data.get("title", ""))
        now = datetime.datetime.utcnow().isoformat()
        with _db() as conn:
            cur = conn.execute(
                """INSERT INTO sl_publications
                   (slug, locale, title, excerpt, body_html, author, practice_area_id, published_at, published, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (slug, data.get("locale", "en"), data.get("title", ""),
                 data.get("excerpt", ""), data.get("body_html", ""),
                 data.get("author", ""), data.get("practice_area_id"),
                 data.get("published_at", now[:10]),
                 1 if data.get("published", True) else 0, now)
            )
            conn.commit()
            return cur.lastrowid

    @staticmethod
    def update(pub_id, data):
        now = datetime.datetime.utcnow().isoformat()
        with _db() as conn:
            conn.execute(
                """UPDATE sl_publications SET
                   locale=?, title=?, excerpt=?, body_html=?, author=?,
                   practice_area_id=?, published_at=?, published=?, updated_at=?
                   WHERE id=?""",
                (data.get("locale", "en"), data.get("title", ""),
                 data.get("excerpt", ""), data.get("body_html", ""),
                 data.get("author", ""), data.get("practice_area_id"),
                 data.get("published_at", now[:10]),
                 1 if data.get("published", True) else 0, now, pub_id)
            )
            conn.commit()

    @staticmethod
    def delete(pub_id):
        with _db() as conn:
            conn.execute("DELETE FROM sl_publications WHERE id=?", (pub_id,))
            conn.commit()


class BrandAssetModel:
    @staticmethod
    def get_all():
        with _db() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM sl_brand_assets ORDER BY created_at DESC"
            )]

    @staticmethod
    def create(data):
        now = datetime.datetime.utcnow().isoformat()
        with _db() as conn:
            cur = conn.execute(
                "INSERT INTO sl_brand_assets (name, type, value, tag, created_at) VALUES (?,?,?,?,?)",
                (data.get("name", ""), data.get("type", "text"),
                 data.get("value", ""), data.get("tag", ""), now)
            )
            conn.commit()
            return cur.lastrowid

    @staticmethod
    def delete(asset_id):
        with _db() as conn:
            conn.execute("DELETE FROM sl_brand_assets WHERE id=?", (asset_id,))
            conn.commit()


class I18nModel:
    @staticmethod
    def upsert(locale, key, value):
        now = datetime.datetime.utcnow().isoformat()
        with _db() as conn:
            conn.execute(
                """INSERT INTO sl_i18n_overrides (locale, key, value, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(locale, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (locale, key, value, now)
            )
            conn.commit()

    @staticmethod
    def get_overrides(locale):
        with _db() as conn:
            rows = conn.execute(
                "SELECT key, value FROM sl_i18n_overrides WHERE locale=?", (locale,)
            ).fetchall()
            return {r["key"]: r["value"] for r in rows}


# ── Seed Manager ─────────────────────────────────────────────────────────────

class SeedManager:
    PA_EN = [
        {
            "slug": "corporate-transactions", "locale": "en", "display_order": 1,
            "icon_emoji": "🏛", "hero_image_url": "",
            "title": "Corporate Transactions",
            "tagline": "Strategic counsel for complex business transactions.",
            "body_html": "<p>We advise founders, private equity sponsors, and established enterprises on mergers, acquisitions, divestitures, joint ventures, and capital markets transactions. Our practice combines deep transactional experience with an understanding of each client's long-term business objectives, enabling us to structure deals that create lasting value.</p><p>From initial term sheet through closing, we provide hands-on guidance on deal structuring, due diligence, negotiation, regulatory compliance, and post-closing integration.</p>",
        },
        {
            "slug": "commercial-litigation", "locale": "en", "display_order": 2,
            "icon_emoji": "⚖️", "hero_image_url": "",
            "title": "Commercial Litigation",
            "tagline": "Resolute advocacy in high-stakes commercial disputes.",
            "body_html": "<p>Our litigation practice represents sophisticated clients in complex commercial disputes before state and federal courts, arbitration panels, and regulatory bodies. We bring the same rigor and preparation to every matter — from contract disputes and business torts to securities litigation and class action defense.</p><p>We approach every case with a trial mindset from day one. Our attorneys are experienced in the full litigation lifecycle: pre-dispute risk assessment, discovery strategy, dispositive motions, trial, and appeal.</p>",
        },
        {
            "slug": "intellectual-property", "locale": "en", "display_order": 3,
            "icon_emoji": "💡", "hero_image_url": "",
            "title": "Intellectual Property",
            "tagline": "Protecting what you have built, at every stage.",
            "body_html": "<p>We counsel innovators, creators, and brand owners on the full spectrum of IP strategy: patent prosecution and portfolio management, trademark registration and enforcement, copyright licensing, and trade secret protection. Our approach integrates legal strategy with business objectives to maximize the commercial value of our clients' intellectual assets.</p><p>For technology companies and startups, we design IP programs that scale with the business, establishing foundational protections early and building defensible portfolios.</p>",
        },
        {
            "slug": "regulatory-compliance", "locale": "en", "display_order": 4,
            "icon_emoji": "📋", "hero_image_url": "",
            "title": "Regulatory & Compliance",
            "tagline": "Navigating regulatory complexity with confidence.",
            "body_html": "<p>Our regulatory practice guides clients through evolving federal and state regulatory requirements across industries. We advise on compliance program design, government investigations, agency rulemaking comment, and enterprise-wide risk assessment, positioning clients to operate with confidence in a complex regulatory environment.</p><p>We work closely with in-house legal and compliance teams to develop practical, scalable compliance frameworks.</p>",
        },
    ]

    PA_ES = [
        {"slug": "corporate-transactions", "locale": "es", "display_order": 1, "icon_emoji": "🏛", "hero_image_url": "", "title": "Transacciones Corporativas", "tagline": "Asesoramiento estratégico para transacciones empresariales complejas.", "body_html": "<p>Asesoramos a fundadores, patrocinadores de capital privado y empresas consolidadas en fusiones, adquisiciones, desinversiones, empresas conjuntas y transacciones en mercados de capitales. Nuestra práctica combina una amplia experiencia transaccional con una comprensión profunda de los objetivos empresariales a largo plazo de cada cliente.</p><p>Desde la hoja de términos inicial hasta el cierre, proporcionamos orientación práctica sobre estructuración de transacciones, diligencia debida, negociación, cumplimiento normativo e integración posterior al cierre.</p>"},
        {"slug": "commercial-litigation", "locale": "es", "display_order": 2, "icon_emoji": "⚖️", "hero_image_url": "", "title": "Litigación Comercial", "tagline": "Representación decidida en disputas comerciales de alto impacto.", "body_html": "<p>Nuestro equipo de litigación representa a clientes sofisticados en disputas comerciales complejas ante tribunales estatales y federales, paneles de arbitraje y organismos regulatorios. Aplicamos el mismo rigor y preparación a cada asunto, desde disputas contractuales y agravios comerciales hasta litigación de valores y defensa en acciones colectivas.</p><p>Abordamos cada caso con una mentalidad de juicio desde el primer día.</p>"},
        {"slug": "intellectual-property", "locale": "es", "display_order": 3, "icon_emoji": "💡", "hero_image_url": "", "title": "Propiedad Intelectual", "tagline": "Protegiendo lo que ha construido, en cada etapa.", "body_html": "<p>Asesoramos a innovadores, creadores y propietarios de marcas en el espectro completo de estrategia de PI: tramitación de patentes y gestión de portafolios, registro y aplicación de marcas, licencias de derechos de autor y protección de secretos comerciales. Nuestro enfoque integra la estrategia legal con los objetivos empresariales para maximizar el valor comercial.</p>"},
        {"slug": "regulatory-compliance", "locale": "es", "display_order": 4, "icon_emoji": "📋", "hero_image_url": "", "title": "Regulatorio y Cumplimiento", "tagline": "Navegando la complejidad regulatoria con confianza.", "body_html": "<p>Nuestra práctica regulatoria guía a los clientes a través de los requisitos regulatorios federales y estatales en constante evolución en múltiples industrias. Asesoramos sobre el diseño de programas de cumplimiento, investigaciones gubernamentales, comentarios a reglamentaciones de agencias y evaluación integral de riesgos empresariales.</p>"},
    ]

    PUB_EN = [
        {"slug": "force-majeure-supply-chain", "locale": "en", "practice_area_slug": "corporate-transactions", "author": "Michael A. Wegter", "published_at": "2025-11-15", "title": "When the Deal Breaks: Force Majeure Clauses in an Era of Supply Chain Disruption", "excerpt": "Recent supply chain disruptions have tested contract terms that practitioners rarely invoked. This article examines how courts have interpreted force majeure provisions and offers drafting guidance for sophisticated commercial agreements.", "body_html": "<p>The commercial disruptions of recent years have elevated force majeure clauses from boilerplate to boardroom priority. Courts across jurisdictions have grappled with what constitutes a qualifying event, whether government-mandated shutdowns suffice, and how foreseeability doctrines apply to unprecedented disruption cascades.</p><p>This article surveys key judicial decisions, identifies the drafting failures that allowed disputes to arise, and proposes a framework for force majeure provisions that allocate risk with precision: explicit triggering events, notice mechanics, mitigation obligations, and termination rights.</p><p><strong>Key takeaways:</strong> Broad \"acts of God\" language provides less protection than practitioners assume. Specificity matters. Economic hardship alone does not excuse performance in most jurisdictions without an explicit provision.</p>"},
        {"slug": "securities-class-action-defense", "locale": "en", "practice_area_slug": "commercial-litigation", "author": "Michael A. Wegter", "published_at": "2025-09-22", "title": "Defending Against Securities Class Actions: Key Motions Practice Strategies", "excerpt": "The standard playbook for defending securities class actions has evolved significantly. We examine the most effective motion-to-dismiss strategies under both state and federal standards, with analysis of recent court decisions.", "body_html": "<p>Securities class action litigation has long been a vehicle for shareholder claims following stock price drops, but the defense landscape has shifted materially. Recent Supreme Court and circuit court decisions have refined the pleading standards that defendants can invoke at the motion-to-dismiss stage, creating new opportunities for early dismissal of meritless claims.</p><p>This article examines the most effective pre-answer strategies: challenging loss causation with precision, deploying PSLRA safe harbor for forward-looking statements, and attacking class certification on ascertainability and predominance grounds.</p><p><strong>Practical note:</strong> The first 90 days after a securities class action is filed set the entire trajectory of the case.</p>"},
        {"slug": "trade-secrets-remote-work", "locale": "en", "practice_area_slug": "intellectual-property", "author": "Michael A. Wegter", "published_at": "2025-07-08", "title": "Trade Secret Protection in the Age of Remote Work", "excerpt": "The shift to distributed workforces has created new vulnerabilities in trade secret protection. We outline the proactive steps companies must take to preserve protectability and enforce their rights.", "body_html": "<p>The Defend Trade Secrets Act (DTSA) requires that trade secret owners take reasonable measures to maintain secrecy. Remote work has fundamentally complicated what \"reasonable measures\" means: home networks, personal devices, cloud collaboration tools, and informal communication channels all introduce vectors for inadvertent disclosure and theft.</p><p>This article provides a practical framework for companies reassessing their trade secret programs: access tier classification, remote-work specific confidentiality agreements, technical controls (DLP, MFA, endpoint management), and the exit-interview and offboarding practices that courts scrutinize most carefully.</p><p><strong>Critical point:</strong> A trade secret owner that cannot demonstrate it treated the information as secret will lose on the threshold question.</p>"},
        {"slug": "ftc-non-compete-rulemaking", "locale": "en", "practice_area_slug": "regulatory-compliance", "author": "Michael A. Wegter", "published_at": "2025-05-19", "title": "Navigating the FTC's Evolving Approach to Non-Compete Agreements", "excerpt": "The FTC's rulemaking signals on non-compete clauses represent a fundamental shift in how employers must approach workforce agreements. This analysis reviews the current landscape and practical compliance implications.", "body_html": "<p>The Federal Trade Commission's scrutiny of non-compete agreements marks one of the most significant shifts in employment regulation in decades. While the FTC's broad ban has faced significant judicial headwinds, the enforcement posture and state-level legislative movement have combined to make legacy non-compete programs a material legal and talent risk.</p><p>This article surveys the current state of federal and state non-compete law, identifies the industries and roles where non-competes retain enforceability, and provides a framework for employers to audit their agreements and design alternative protection strategies.</p>"},
        {"slug": "cross-border-ma-regulatory-clearance", "locale": "en", "practice_area_slug": "corporate-transactions", "author": "Michael A. Wegter", "published_at": "2025-03-04", "title": "Cross-Border M&A: Structuring for Regulatory Clearance in Multiple Jurisdictions", "excerpt": "Complex cross-border transactions require proactive regulatory strategy from the earliest stages of deal design. This article provides a framework for anticipating and managing multi-jurisdictional clearance requirements.", "body_html": "<p>Cross-border mergers and acquisitions have never been more complex from a regulatory perspective. The convergence of competition authority activism, foreign investment screening (CFIUS in the US, NSIA in the UK, equivalent regimes across the EU), and sector-specific regulatory reviews means that deal certainty depends on regulatory strategy as much as commercial and legal execution.</p><p>This article provides a practical transaction-structuring framework: pre-signing regulatory risk mapping, jurisdiction selection and timing strategy, merger notification filing coordination, behavioral remedy negotiation, and walk-away rights design.</p><p><strong>Key insight:</strong> Regulatory strategy that begins at LOI execution shortens the critical path by six to eight weeks in multi-jurisdiction transactions.</p>"},
    ]

    PUB_ES = [
        {"slug": "force-majeure-supply-chain", "locale": "es", "practice_area_slug": "corporate-transactions", "author": "Michael A. Wegter", "published_at": "2025-11-15", "title": "Cuando el acuerdo se rompe: cláusulas de fuerza mayor en la era de la disrupción de cadenas de suministro", "excerpt": "Las recientes disrupciones en las cadenas de suministro han puesto a prueba cláusulas contractuales que los profesionales rara vez invocaban.", "body_html": "<p>Las perturbaciones comerciales de los últimos años han elevado las cláusulas de fuerza mayor de texto estándar a prioridad de sala de juntas. Los tribunales de diversas jurisdicciones han debatido qué constituye un evento calificado, si los cierres ordenados por el gobierno son suficientes, y cómo se aplican las doctrinas de previsibilidad.</p><p>Este artículo analiza las decisiones judiciales clave, identifica los fallos de redacción que permitieron que surgieran disputas y propone un marco para las disposiciones de fuerza mayor que asignan el riesgo con precisión.</p>"},
        {"slug": "securities-class-action-defense", "locale": "es", "practice_area_slug": "commercial-litigation", "author": "Michael A. Wegter", "published_at": "2025-09-22", "title": "Defensa contra acciones colectivas de valores: estrategias clave de práctica de mociones", "excerpt": "El manual estándar para defender acciones colectivas de valores ha evolucionado significativamente. Examinamos las estrategias de moción de desestimación más efectivas.", "body_html": "<p>El litigio de acciones colectivas de valores ha sido durante mucho tiempo un vehículo para las reclamaciones de los accionistas tras caídas en el precio de las acciones, pero el panorama defensivo ha cambiado materialmente.</p><p>Este artículo examina las estrategias previas a la respuesta más efectivas: impugnar la causalidad de pérdidas con precisión, desplegar el puerto seguro PSLRA para declaraciones prospectivas, y atacar la certificación de clase antes de que continúe el descubrimiento.</p>"},
        {"slug": "trade-secrets-remote-work", "locale": "es", "practice_area_slug": "intellectual-property", "author": "Michael A. Wegter", "published_at": "2025-07-08", "title": "Protección de secretos comerciales en la era del trabajo remoto", "excerpt": "El cambio a fuerzas laborales distribuidas ha creado nuevas vulnerabilidades en la protección de secretos comerciales.", "body_html": "<p>La Ley de Defensa de Secretos Comerciales (DTSA) requiere que los propietarios de secretos comerciales tomen medidas razonables para mantener el secreto. El trabajo remoto ha complicado fundamentalmente lo que \"medidas razonables\" significa.</p><p>Este artículo proporciona un marco práctico: clasificación de niveles de acceso, acuerdos de confidencialidad específicos para trabajo remoto, controles técnicos y prácticas de desvinculación que los tribunales examinan más detenidamente.</p>"},
        {"slug": "ftc-non-compete-rulemaking", "locale": "es", "practice_area_slug": "regulatory-compliance", "author": "Michael A. Wegter", "published_at": "2025-05-19", "title": "Navegando el enfoque evolutivo de la FTC hacia los acuerdos de no competencia", "excerpt": "Las señales de reglamentación de la FTC sobre las cláusulas de no competencia representan un cambio fundamental en cómo los empleadores deben abordar los acuerdos laborales.", "body_html": "<p>El escrutinio de la Comisión Federal de Comercio (FTC) sobre los acuerdos de no competencia marca uno de los cambios más significativos en la regulación laboral en décadas. Si bien la prohibición amplia de la FTC ha enfrentado importantes obstáculos judiciales, la postura de cumplimiento y el movimiento legislativo a nivel estatal han combinado para hacer que los programas heredados representen un riesgo material.</p>"},
        {"slug": "cross-border-ma-regulatory-clearance", "locale": "es", "practice_area_slug": "corporate-transactions", "author": "Michael A. Wegter", "published_at": "2025-03-04", "title": "Fusiones y adquisiciones transfronterizas: estructuración para la aprobación regulatoria en múltiples jurisdicciones", "excerpt": "Las transacciones transfronterizas complejas requieren una estrategia regulatoria proactiva desde las etapas más tempranas del diseño de la transacción.", "body_html": "<p>Las fusiones y adquisiciones transfronterizas nunca han sido más complejas desde una perspectiva regulatoria. La convergencia del activismo de las autoridades de competencia, el escrutinio de inversiones extranjeras y las revisiones regulatorias específicas del sector significan que la certeza de la transacción depende tanto de la estrategia regulatoria como de la ejecución comercial.</p>"},
    ]

    @classmethod
    def seed(cls):
        """Idempotent: only runs if tables are empty."""
        with _db() as conn:
            if conn.execute("SELECT COUNT(*) FROM sl_cms_users").fetchone()[0] > 0:
                return  # already seeded

            # CMS user
            pw_hash = bcrypt.hashpw(b"Demo2026!", bcrypt.gensalt()).decode()
            conn.execute(
                "INSERT INTO sl_cms_users (email, password_hash, role, created_at) VALUES (?,?,?,?)",
                ("editor@solo-law.demo", pw_hash, "editor", datetime.datetime.utcnow().isoformat())
            )

            # Practice areas
            pa_id_map = {}  # slug -> {locale -> id}
            for pa in cls.PA_EN + cls.PA_ES:
                slug = pa["slug"]
                locale = pa["locale"]
                cur = conn.execute(
                    """INSERT OR IGNORE INTO sl_practice_areas
                       (slug, locale, title, tagline, body_html, icon_emoji, hero_image_url, display_order, published, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,1,?)""",
                    (slug, locale, pa["title"], pa["tagline"], pa["body_html"],
                     pa["icon_emoji"], pa["hero_image_url"], pa["display_order"],
                     datetime.datetime.utcnow().isoformat())
                )
                row_id = cur.lastrowid
                if slug not in pa_id_map:
                    pa_id_map[slug] = {}
                pa_id_map[slug][locale] = row_id

            conn.commit()

            # Re-fetch IDs after commit
            rows = conn.execute("SELECT id, slug, locale FROM sl_practice_areas").fetchall()
            pa_id_map = {}
            for r in rows:
                slug, locale = r["slug"], r["locale"]
                if slug not in pa_id_map:
                    pa_id_map[slug] = {}
                pa_id_map[slug][locale] = r["id"]

            # Publications
            for pub in cls.PUB_EN + cls.PUB_ES:
                pa_slug = pub.pop("practice_area_slug")
                locale = pub["locale"]
                pa_id = pa_id_map.get(pa_slug, {}).get(locale)
                conn.execute(
                    """INSERT OR IGNORE INTO sl_publications
                       (slug, locale, title, excerpt, body_html, author, practice_area_id, published_at, published, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,1,?)""",
                    (pub["slug"], locale, pub["title"], pub["excerpt"], pub["body_html"],
                     pub["author"], pa_id, pub["published_at"],
                     datetime.datetime.utcnow().isoformat())
                )
            conn.commit()


# ── Schema init ──────────────────────────────────────────────────────────────

def _init_schema():
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sl_practice_areas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL,
                locale TEXT NOT NULL DEFAULT 'en',
                title TEXT,
                tagline TEXT,
                body_html TEXT,
                icon_emoji TEXT,
                hero_image_url TEXT,
                display_order INTEGER DEFAULT 0,
                published INTEGER DEFAULT 1,
                updated_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_sl_pa ON sl_practice_areas(slug, locale);

            CREATE TABLE IF NOT EXISTS sl_publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL,
                locale TEXT NOT NULL DEFAULT 'en',
                title TEXT,
                excerpt TEXT,
                body_html TEXT,
                author TEXT,
                practice_area_id INTEGER,
                published_at TEXT,
                published INTEGER DEFAULT 1,
                updated_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_sl_pub ON sl_publications(slug, locale);

            CREATE TABLE IF NOT EXISTS sl_brand_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                type TEXT,
                value TEXT,
                tag TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sl_cms_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'editor',
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sl_i18n_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                locale TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                updated_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_sl_i18n ON sl_i18n_overrides(locale, key);
        """)


# ── Route Registration ────────────────────────────────────────────────────────

@solo_law_bp.route("/health")
def health():
    return jsonify({"ok": True, "service": "solo-law"})


# Auth
@solo_law_bp.route("/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(silent=True) or {}
    user = UserModel.verify(data.get("email", ""), data.get("password", ""))
    if not user:
        return jsonify({"error": "Invalid credentials"}), 401
    token = _make_token(user["email"], user["role"])
    return jsonify({"token": token, "user": {"email": user["email"], "role": user["role"]}})


# Public: Practice Areas
@solo_law_bp.route("/practice-areas")
def get_practice_areas():
    locale = request.args.get("locale", "en")
    return jsonify(PracticeAreaModel.get_all(locale))


@solo_law_bp.route("/practice-areas/<slug>")
def get_practice_area(slug):
    locale = request.args.get("locale", "en")
    pa = PracticeAreaModel.get_by_slug(slug, locale)
    if not pa:
        return jsonify({"error": "Not found"}), 404
    return jsonify(pa)


# Public: Publications
@solo_law_bp.route("/publications")
def get_publications():
    locale = request.args.get("locale", "en")
    pa = request.args.get("practice_area")
    page = int(request.args.get("page", 1))
    return jsonify(PublicationModel.get_all(locale=locale, practice_area_id=pa, page=page))


@solo_law_bp.route("/publications/<slug>")
def get_publication(slug):
    locale = request.args.get("locale", "en")
    pub = PublicationModel.get_by_slug(slug, locale)
    if not pub:
        return jsonify({"error": "Not found"}), 404
    return jsonify(pub)


# Public: Brand Assets
@solo_law_bp.route("/brand-assets")
def get_brand_assets():
    return jsonify(BrandAssetModel.get_all())


# Public: i18n
@solo_law_bp.route("/i18n")
def get_i18n():
    locale = request.args.get("locale", "en")
    overrides = I18nModel.get_overrides(locale)
    return jsonify({"locale": locale, "overrides": overrides})


# CMS: Stats
@solo_law_bp.route("/cms/stats")
@require_auth
def cms_stats():
    with _db() as conn:
        pa_count = conn.execute("SELECT COUNT(DISTINCT slug) FROM sl_practice_areas").fetchone()[0]
        pub_count = conn.execute("SELECT COUNT(DISTINCT slug) FROM sl_publications").fetchone()[0]
        asset_count = conn.execute("SELECT COUNT(*) FROM sl_brand_assets").fetchone()[0]
    return jsonify({
        "practice_areas": pa_count,
        "publications": pub_count,
        "brand_assets": asset_count,
        "languages": 3,
    })


# CMS: Practice Areas
@solo_law_bp.route("/cms/practice-areas")
@require_auth
def cms_list_pa():
    locale = request.args.get("locale", "en")
    return jsonify(PracticeAreaModel.get_all(locale, published_only=False))


@solo_law_bp.route("/cms/practice-areas/<int:pa_id>")
@require_auth
def cms_get_pa(pa_id):
    pa = PracticeAreaModel.get_by_id(pa_id)
    if not pa:
        return jsonify({"error": "Not found"}), 404
    return jsonify(pa)


@solo_law_bp.route("/cms/practice-areas", methods=["POST"])
@require_auth
def cms_create_pa():
    data = request.get_json(silent=True) or {}
    new_id = PracticeAreaModel.create(data)
    return jsonify({"id": new_id}), 201


@solo_law_bp.route("/cms/practice-areas/<int:pa_id>", methods=["PUT"])
@require_auth
def cms_update_pa(pa_id):
    data = request.get_json(silent=True) or {}
    PracticeAreaModel.update(pa_id, data)
    return jsonify({"ok": True})


@solo_law_bp.route("/cms/practice-areas/<int:pa_id>", methods=["DELETE"])
@require_auth
def cms_delete_pa(pa_id):
    PracticeAreaModel.delete(pa_id)
    return jsonify({"ok": True})


# CMS: Publications
@solo_law_bp.route("/cms/publications")
@require_auth
def cms_list_pub():
    locale = request.args.get("locale", "en")
    result = PublicationModel.get_all(locale=locale, published_only=False, per_page=100)
    return jsonify(result["items"])


@solo_law_bp.route("/cms/publications/<int:pub_id>")
@require_auth
def cms_get_pub(pub_id):
    pub = PublicationModel.get_by_id(pub_id)
    if not pub:
        return jsonify({"error": "Not found"}), 404
    return jsonify(pub)


@solo_law_bp.route("/cms/publications", methods=["POST"])
@require_auth
def cms_create_pub():
    data = request.get_json(silent=True) or {}
    new_id = PublicationModel.create(data)
    return jsonify({"id": new_id}), 201


@solo_law_bp.route("/cms/publications/<int:pub_id>", methods=["PUT"])
@require_auth
def cms_update_pub(pub_id):
    data = request.get_json(silent=True) or {}
    PublicationModel.update(pub_id, data)
    return jsonify({"ok": True})


@solo_law_bp.route("/cms/publications/<int:pub_id>", methods=["DELETE"])
@require_auth
def cms_delete_pub(pub_id):
    PublicationModel.delete(pub_id)
    return jsonify({"ok": True})


# CMS: Brand Assets
@solo_law_bp.route("/cms/brand-assets", methods=["POST"])
@require_auth
def cms_create_asset():
    data = request.get_json(silent=True) or {}
    new_id = BrandAssetModel.create(data)
    return jsonify({"id": new_id}), 201


@solo_law_bp.route("/cms/brand-assets/<int:asset_id>", methods=["DELETE"])
@require_auth
def cms_delete_asset(asset_id):
    BrandAssetModel.delete(asset_id)
    return jsonify({"ok": True})


# CMS: i18n overrides
@solo_law_bp.route("/cms/i18n", methods=["POST"])
@require_auth
def cms_upsert_i18n():
    data = request.get_json(silent=True) or {}
    I18nModel.upsert(data.get("locale", "en"), data.get("key", ""), data.get("value", ""))
    return jsonify({"ok": True})


# ── Init ──────────────────────────────────────────────────────────────────────

_init_schema()
SeedManager.seed()
