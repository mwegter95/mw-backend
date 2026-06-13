"""
Art Print Storefront -- Flask Blueprint
Mounts under /art-store on the mw-backend server.

Backs the art-print-storefront demo at
https://michaelwegter.com/demos/art-print-storefront/.

Endpoints:
  GET  /art-store/prints      12-print in-memory catalog
  POST /art-store/login       Accept any credentials, return a signed JWT
  POST /art-store/checkout    Require valid JWT, return mock order confirmation

Auth: PyJWT with a random per-boot secret (demo only).
CORS: handled globally by server.py -- no per-blueprint CORS needed.
Storage: no DB. All data is in-memory.
"""

import time
import secrets

import jwt as _jwt
from flask import Blueprint, request, jsonify
from functools import wraps

# ─── Blueprint ────────────────────────────────────────────────────────────────

art_store_bp = Blueprint("art_store", __name__, url_prefix="/art-store")

# Per-boot secret -- good enough for a demo; resets on server restart.
_JWT_SECRET = secrets.token_hex(32)
_JWT_ALG    = "HS256"
_JWT_TTL    = 3600  # 1 hour

# ─── In-memory print catalog ──────────────────────────────────────────────────

_PRINTS = [
    {"id": 1,  "title": "Morning Fog",        "artist": "A. Chen",     "seed": "art1",  "edition": "limited", "editionSize": 25,   "editionNum": 3,    "medium": "Fine Art Inkjet", "substrate": "Hahnemuhle Photo Rag 308gsm",       "basePrice": 180},
    {"id": 2,  "title": "Silver Forest",      "artist": "M. Okafor",   "seed": "art2",  "edition": "open",    "editionSize": None,  "editionNum": None, "medium": "C-Type Print",    "substrate": "Fuji Crystal Archive Lustre",       "basePrice": 120},
    {"id": 3,  "title": "Coastal Light",      "artist": "S. Reyes",    "seed": "art3",  "edition": "limited", "editionSize": 15,   "editionNum": 7,    "medium": "Pigment Print",   "substrate": "Canson Platine Fibre Rag 310gsm",   "basePrice": 220},
    {"id": 4,  "title": "Brutalist Form",     "artist": "T. Nakamura", "seed": "art4",  "edition": "limited", "editionSize": 20,   "editionNum": 1,    "medium": "Fine Art Inkjet", "substrate": "Epson Ultra Premium Matte",         "basePrice": 200},
    {"id": 5,  "title": "Tide Pool",          "artist": "A. Chen",     "seed": "art5",  "edition": "open",    "editionSize": None,  "editionNum": None, "medium": "C-Type Print",    "substrate": "Fuji Crystal Archive Lustre",       "basePrice": 110},
    {"id": 6,  "title": "Golden Hour",        "artist": "L. Fontaine", "seed": "art6",  "edition": "limited", "editionSize": 30,   "editionNum": 12,   "medium": "Pigment Print",   "substrate": "Hahnemuhle German Etching 310gsm",  "basePrice": 160},
    {"id": 7,  "title": "Urban Geometry",     "artist": "T. Nakamura", "seed": "art7",  "edition": "limited", "editionSize": 10,   "editionNum": 5,    "medium": "Fine Art Inkjet", "substrate": "Canson Rag Photographique 310gsm",  "basePrice": 280},
    {"id": 8,  "title": "Salt Flats",         "artist": "S. Reyes",    "seed": "art8",  "edition": "open",    "editionSize": None,  "editionNum": None, "medium": "C-Type Print",    "substrate": "Fuji Crystal Archive Metallic",     "basePrice": 140},
    {"id": 9,  "title": "Rain Study No. 4",   "artist": "M. Okafor",   "seed": "art9",  "edition": "limited", "editionSize": 12,   "editionNum": 9,    "medium": "Fine Art Inkjet", "substrate": "Hahnemuhle Photo Rag 308gsm",       "basePrice": 190},
    {"id": 10, "title": "Winter Bloom",       "artist": "L. Fontaine", "seed": "art10", "edition": "limited", "editionSize": 20,   "editionNum": 4,    "medium": "Pigment Print",   "substrate": "Epson Ultra Premium Matte",         "basePrice": 175},
    {"id": 11, "title": "Dusk Over Mesa",     "artist": "A. Chen",     "seed": "art11", "edition": "open",    "editionSize": None,  "editionNum": None, "medium": "C-Type Print",    "substrate": "Fuji Crystal Archive Lustre",       "basePrice": 130},
    {"id": 12, "title": "Shoreline Abstract", "artist": "M. Okafor",   "seed": "art12", "edition": "limited", "editionSize": 8,    "editionNum": 2,    "medium": "Fine Art Inkjet", "substrate": "Canson Platine Fibre Rag 310gsm",   "basePrice": 320},
]

# ─── Auth helpers ─────────────────────────────────────────────────────────────

def _make_token(email: str) -> str:
    now     = int(time.time())
    payload = {"email": email, "iat": now, "exp": now + _JWT_TTL}
    return _jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALG)

def _decode_token(token: str) -> dict:
    return _jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALG])

def require_token(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Unauthorized -- missing Bearer token"}), 401
        try:
            payload = _decode_token(auth[7:])
            request.jwt_email = payload["email"]
        except _jwt.ExpiredSignatureError:
            return jsonify({"error": "Invalid or expired token"}), 401
        except Exception:
            return jsonify({"error": "Invalid or expired token"}), 401
        return f(*args, **kwargs)
    return wrapper

# ─── Routes ───────────────────────────────────────────────────────────────────

@art_store_bp.route("/prints", methods=["GET"])
def get_prints():
    """Return the full print catalog."""
    return jsonify({"prints": _PRINTS})


@art_store_bp.route("/login", methods=["POST"])
def login():
    """
    Accept any email + password (demo). Return a signed JWT.
    Body: { "email": "...", "password": "..." }
    """
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"error": "Email is required"}), 400
    token = _make_token(email)
    return jsonify({"token": token, "email": email})


@art_store_bp.route("/checkout", methods=["POST"])
@require_token
def checkout():
    """
    Require a valid JWT. Validate cart, return a mock order confirmation.
    Body: { "items": [...], "total": <number> }
    """
    data      = request.get_json(silent=True) or {}
    items     = data.get("items", [])
    total     = data.get("total", 0)
    order_id  = "MW-" + secrets.token_hex(4).upper()

    return jsonify({
        "orderId":   order_id,
        "status":    "confirmed",
        "email":     request.jwt_email,
        "total":     total,
        "itemCount": len(items),
        "shipping":  "3 to 5 business days",
        "message":   "Your order has been received. You will receive a confirmation email shortly.",
    })
