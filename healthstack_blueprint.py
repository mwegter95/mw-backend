"""
HealthStack Patient Portal -- Flask Blueprint
Mounts under /healthstack on the mw-backend server.

Backs the healthstack-patient-portal demo at
https://michaelwegter.com/demos/healthstack-patient-portal/.

Endpoints:
  POST /healthstack/login               Credential check, returns JWT with role claim
  GET  /healthstack/services            Public provider catalog (no auth)
  POST /healthstack/book                Book a provider slot (patient only)
  POST /healthstack/checkout            Mock Stripe payment (patient only)
  GET  /healthstack/dashboard/patient   Patient's own bookings (patient only)
  GET  /healthstack/dashboard/admin     All bookings + audit log (admin only)
  POST /healthstack/upload              Stub file upload (patient only)

Auth: Role-bearing JWT, per-boot secret (demo only, no DB).
CORS: handled globally by server.py -- no per-blueprint CORS config needed.
Storage: all data is in-memory; resets on server restart.
"""

import secrets
import datetime

import jwt as _jwt
from flask import Blueprint, request, jsonify
from functools import wraps

# ─── Blueprint ────────────────────────────────────────────────────────────────

healthstack_bp = Blueprint("healthstack", __name__, url_prefix="/healthstack")

# Per-boot ephemeral secret -- fine for a demo.
_JWT_SECRET = secrets.token_hex(32)
_JWT_ALG    = "HS256"
_JWT_TTL    = datetime.timedelta(hours=8)


# ─── Auth helpers ─────────────────────────────────────────────────────────────

def _make_token(email: str, role: str) -> str:
    payload = {
        "email": email,
        "role":  role,
        "exp":   datetime.datetime.utcnow() + _JWT_TTL,
    }
    return _jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALG)


def _decode_token(token: str) -> dict:
    return _jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALG])


def require_role(allowed_roles):
    """Decorator factory: allow only tokens whose role is in allowed_roles."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return jsonify({"error": "Unauthorized"}), 401
            try:
                payload = _decode_token(auth[7:])
                if payload.get("role") not in allowed_roles:
                    return jsonify({"error": "Forbidden"}), 403
                request.hs_user = payload
            except _jwt.ExpiredSignatureError:
                return jsonify({"error": "Token expired"}), 401
            except Exception:
                return jsonify({"error": "Invalid token"}), 401
            return f(*args, **kwargs)
        return wrapper
    return decorator


# ─── Seeded in-memory data ────────────────────────────────────────────────────

_USERS = {
    "patient@healthstack.demo": {
        "password": "DemoPatient123!",
        "role":     "patient",
        "name":     "Jane D.",
    },
    "admin@healthstack.demo": {
        "password": "DemoAdmin123!",
        "role":     "admin",
        "name":     "Admin",
    },
}

_PROVIDERS = [
    {
        "id":        1,
        "name":      "Dr. Sarah Chen",
        "specialty": "Family Medicine",
        "price":     85,
        "rating":    4.9,
        "reviews":   48,
        "slots":     ["Mon 9:00 AM", "Mon 11:00 AM", "Tue 9:00 AM", "Tue 2:00 PM", "Wed 10:00 AM", "Thu 11:00 AM", "Fri 9:00 AM"],
    },
    {
        "id":        2,
        "name":      "Dr. Marcus Webb",
        "specialty": "Internal Medicine",
        "price":     95,
        "rating":    4.8,
        "reviews":   31,
        "slots":     ["Tue 10:00 AM", "Tue 1:00 PM", "Thu 10:00 AM", "Thu 3:00 PM"],
    },
    {
        "id":        3,
        "name":      "Dr. Priya Nair",
        "specialty": "Pediatrics",
        "price":     75,
        "rating":    4.9,
        "reviews":   62,
        "slots":     ["Mon 8:00 AM", "Wed 8:00 AM", "Fri 8:00 AM", "Mon 11:00 AM", "Wed 12:00 PM"],
    },
    {
        "id":        4,
        "name":      "NP Jordan Kim",
        "specialty": "Urgent Care",
        "price":     65,
        "rating":    4.7,
        "reviews":   19,
        "slots":     ["Mon 7:00 AM", "Tue 7:00 AM", "Wed 7:00 AM", "Thu 7:00 AM", "Fri 7:00 AM", "Sat 9:00 AM", "Sat 11:00 AM"],
    },
]

# Pre-seeded booking so the patient dashboard has data on first login.
_BOOKINGS = [
    {
        "id":             "bk_001",
        "patient_email":  "patient@healthstack.demo",
        "patient_name":   "Jane D.",
        "provider_id":    1,
        "provider_name":  "Dr. Sarah Chen",
        "specialty":      "Family Medicine",
        "slot":           "Next Tuesday 10:00 AM",
        "price":          85,
        "status":         "Confirmed",
        "payment_intent": "pi_demo_abc123",
        "booked_at":      "2026-06-13T09:30:00",
    }
]

_AUDIT_LOG = [
    {"ts": "Today 14:03",      "event": "Patient Jane D. viewed their dashboard"},
    {"ts": "Today 11:42",      "event": "Patient Mike T. booked with Dr. Webb"},
    {"ts": "Today 09:15",      "event": "Admin admin@healthstack.demo accessed roster"},
    {"ts": "Yesterday 16:30",  "event": "Patient Jane D. uploaded intake form"},
    {"ts": "Yesterday 14:22",  "event": "Patient Alex R. completed checkout"},
]


# ─── Routes ───────────────────────────────────────────────────────────────────

@healthstack_bp.route("/login", methods=["POST"])
def login():
    """
    Validate credentials against _USERS; return a role-bearing JWT.
    Body: { "email": "...", "password": "..." }
    """
    data     = request.get_json(silent=True) or {}
    email    = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()

    user = _USERS.get(email)
    if not user or user["password"] != password:
        return jsonify({"error": "Invalid credentials"}), 401

    token = _make_token(email, user["role"])
    return jsonify({"token": token, "role": user["role"], "name": user["name"]})


@healthstack_bp.route("/services", methods=["GET"])
def get_services():
    """Return the provider catalog. No auth required."""
    return jsonify({"providers": _PROVIDERS})


@healthstack_bp.route("/book", methods=["POST"])
@require_role(["patient"])
def book():
    """
    Book a provider slot for the authenticated patient.
    Body: { "provider_id": <int>, "slot": "<string>" }
    """
    data        = request.get_json(silent=True) or {}
    provider_id = int(data.get("provider_id", 0))
    slot        = (data.get("slot") or "").strip()

    provider = next((p for p in _PROVIDERS if p["id"] == provider_id), None)
    if not provider:
        return jsonify({"error": "Provider not found"}), 404
    if not slot:
        return jsonify({"error": "Slot is required"}), 400

    booking_id = "bk_" + secrets.token_hex(4)
    booking = {
        "id":             booking_id,
        "patient_email":  request.hs_user["email"],
        "patient_name":   request.hs_user.get("name", "Patient"),
        "provider_id":    provider_id,
        "provider_name":  provider["name"],
        "specialty":      provider["specialty"],
        "slot":           slot,
        "price":          provider["price"],
        "status":         "Confirmed",
        "payment_intent": None,
        "booked_at":      datetime.datetime.utcnow().isoformat(),
    }
    _BOOKINGS.append(booking)

    _AUDIT_LOG.insert(0, {
        "ts":    "Just now",
        "event": f"Patient {request.hs_user['email']} booked with {provider['name']}",
    })

    return jsonify({
        "booking_id":    booking_id,
        "provider_name": provider["name"],
        "specialty":     provider["specialty"],
        "slot":          slot,
        "price":         provider["price"],
        "status":        "Confirmed",
    })


@healthstack_bp.route("/checkout", methods=["POST"])
@require_role(["patient"])
def checkout():
    """
    Mock Stripe payment for a booking.
    Body: { "booking_id": "...", "card_last4": "..." }
    """
    data       = request.get_json(silent=True) or {}
    booking_id = (data.get("booking_id") or "").strip()
    card_last4 = (data.get("card_last4") or "4242").strip()

    booking = next((b for b in _BOOKINGS if b["id"] == booking_id), None)
    if not booking:
        return jsonify({"error": "Booking not found"}), 404
    if booking["patient_email"] != request.hs_user["email"]:
        return jsonify({"error": "Forbidden"}), 403

    pi_id = "pi_demo_" + secrets.token_hex(6)
    booking["status"]         = "Paid"
    booking["payment_intent"] = pi_id

    _AUDIT_LOG.insert(0, {
        "ts":    "Just now",
        "event": f"Patient {request.hs_user['email']} completed checkout (card ending {card_last4})",
    })

    return jsonify({
        "payment_intent_id": pi_id,
        "amount":            booking["price"],
        "currency":          "usd",
        "status":            "succeeded",
        "card_last4":        card_last4,
        "confirmation":      booking_id,
        "provider_name":     booking["provider_name"],
        "slot":              booking["slot"],
    })


@healthstack_bp.route("/dashboard/patient", methods=["GET"])
@require_role(["patient"])
def patient_dashboard():
    """Return the authenticated patient's own bookings."""
    email    = request.hs_user["email"]
    bookings = [b for b in _BOOKINGS if b["patient_email"] == email]
    return jsonify({
        "user_name": request.hs_user.get("email", email),
        "bookings":  bookings,
    })


@healthstack_bp.route("/dashboard/admin", methods=["GET"])
@require_role(["admin"])
def admin_dashboard():
    """Return all bookings, audit log, and a PHI-minimized patient roster."""
    # Build patient roster from bookings: initials only (PHI minimized)
    seen    = {}
    for b in _BOOKINGS:
        e = b["patient_email"]
        if e not in seen:
            parts    = b["patient_name"].split()
            initials = ".".join(p[0] for p in parts) + "." if parts else "?."
            seen[e]  = {
                "initials":   initials,
                "specialty":  b["specialty"],
                "last_visit": b["booked_at"][:10] if b.get("booked_at") else "N/A",
            }
    patients = list(seen.values())

    unique_patients = len(set(b["patient_email"] for b in _BOOKINGS))

    return jsonify({
        "total_bookings": len(_BOOKINGS),
        "total_patients": unique_patients,
        "bookings":       _BOOKINGS,
        "audit_log":      _AUDIT_LOG[:20],
        "patients":       patients,
    })


@healthstack_bp.route("/upload", methods=["POST"])
@require_role(["patient"])
def upload():
    """
    Stub file upload endpoint. Records the action; no actual storage.
    Accepts multipart/form-data with a 'file' field.
    """
    filename = "intake_form"
    if "file" in request.files:
        filename = request.files["file"].filename or filename

    file_id = "file_" + secrets.token_hex(4)

    _AUDIT_LOG.insert(0, {
        "ts":    "Just now",
        "event": f"Patient {request.hs_user['email']} uploaded {filename}",
    })

    return jsonify({"success": True, "file_id": file_id, "filename": filename})
