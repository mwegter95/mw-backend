"""
Stage-2 growyard seed: backfills the `image` / `thumb` fields onto each plant
row for users who were already seeded by v1 (before per-plant photos existed).

Idempotency model
-----------------
A separate `yard_seed_versions` table records which one-shot migrations have
been applied to each owner. `seed_v2_for_owner` checks that table; if the
('v2_images', owner) row already exists, it returns False immediately. Once
it runs successfully it stamps the row so subsequent calls are no-ops.

This runs from `/yard/state` (covers existing users on next page load) and
from `/auth/login` + `/auth/register` (covers fresh logins / signups).

For *new* users the v1 seed in `yard_seed.py` already includes `image` /
`thumb` since those fields are patched onto PLANTS at module-load time, so
v2 essentially short-circuits for them after the first run.
"""
from __future__ import annotations

import json

from yard_seed import PLANTS, _copy_default_photos

# Convention: image filename derives from plant id. Keeping the mapping
# explicit here so future v3+ migrations can target specific ids if the
# naming convention changes.
IMAGES = {p["id"]: {"image": p["image"], "thumb": p["thumb"]} for p in PLANTS}

SEED_VERSION = "v2_images"


def ensure_versions_table(db) -> None:
    """Make sure the yard_seed_versions tracking table exists.
    Safe to call repeatedly — `CREATE TABLE IF NOT EXISTS`."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS yard_seed_versions (
            owner_type TEXT NOT NULL,
            owner_id   TEXT NOT NULL,
            version    TEXT NOT NULL,
            applied_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (owner_type, owner_id, version)
        )
        """
    )


def seed_v2_for_owner(db, owner_type: str, owner_id: str) -> bool:
    """Patch image/thumb fields onto every yard_plants row for this owner.

    Returns True if work was done, False if the v2 migration was already
    applied for this user (or the user has no plants to patch — e.g. a
    user that never triggered the v1 seed for some reason).
    """
    ensure_versions_table(db)

    already = db.execute(
        "SELECT 1 FROM yard_seed_versions "
        "WHERE owner_type=? AND owner_id=? AND version=?",
        (owner_type, owner_id, SEED_VERSION),
    ).fetchone()
    if already:
        return False

    rows = db.execute(
        "SELECT id, data FROM yard_plants WHERE owner_type=? AND owner_id=?",
        (owner_type, owner_id),
    ).fetchall()
    if not rows:
        # Nothing to patch yet. Don't stamp the version — the next call (after
        # v1 has seeded the user) should still run.
        return False

    updates = 0
    for row in rows:
        plant_id = row["id"]
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        info = IMAGES.get(plant_id)
        if not info:
            continue  # unknown id; leave alone for forward-compat with custom plants
        # Only write if a field is actually missing or different — keeps
        # updated_at honest for users who got images via v1 already.
        if data.get("image") == info["image"] and data.get("thumb") == info["thumb"]:
            continue
        data["image"] = info["image"]
        data["thumb"] = info["thumb"]
        db.execute(
            "UPDATE yard_plants SET data=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND owner_type=? AND owner_id=?",
            (json.dumps(data), plant_id, owner_type, owner_id),
        )
        updates += 1

    db.execute(
        "INSERT INTO yard_seed_versions (owner_type, owner_id, version) VALUES (?,?,?)",
        (owner_type, owner_id, SEED_VERSION),
    )
    db.commit()
    # Also ensure default photos are on disk for this user (no-op if already copied).
    _copy_default_photos(owner_id)
    return updates > 0 or True


def _cli() -> None:
    """Standalone runner: `python -m yard_seed_v2 --email zweetztuph@gmail.com`.
    Useful if you want to backfill without waiting for the user to hit the
    server. Mirrors the v1 CLI in yard_seed.py."""
    import argparse
    import sqlite3
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Backfill yard plant images for a user by email.")
    parser.add_argument("--email", required=True)
    args = parser.parse_args()

    db_path = Path(__file__).parent / "data" / "mw.db"
    if not db_path.exists():
        print(f"Database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    user = conn.execute("SELECT id FROM users WHERE email=?", (args.email.lower(),)).fetchone()
    if not user:
        print(f"No user with email={args.email}", file=sys.stderr)
        sys.exit(2)

    did = seed_v2_for_owner(conn, "user", str(user["id"]))
    if did:
        print(f"✓ v2 images applied for user_id={user['id']} ({args.email}).")
    else:
        print(f"✓ v2 already applied (or no plants yet) for {args.email} — no-op.")
    conn.close()


if __name__ == "__main__":
    _cli()
