"""people.py — invites and user management, always scoped to one tenant.

Who may do what:
  * farmer invites           — any elevator staff of the tenant (or platform admin)
  * elevator_staff / admin   — elevator_admin of the tenant (or platform admin) only
  * deactivate / role change — elevator_admin / platform admin, never on yourself,
                               never on a platform admin or another tenant's user
Invite links carry a random single-use token; only its SHA-256 is stored.
"""
from __future__ import annotations

import hashlib
import re
import secrets as _secrets
from datetime import datetime, timedelta, timezone

from .auth import _as_utc, _ts
from .data import ELEVATOR_ROLES, PortalError, _audit, _insert, _usage, ensure_farmer_for_user
from .db import Db

INVITE_TTL = timedelta(days=7)
INVITABLE = ("farmer", "elevator_staff", "elevator_admin")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _actor(db: Db, tenant_id: str, actor_id: int) -> dict:
    r = db.query("SELECT * FROM users WHERE user_id = %s AND active", (actor_id,))
    if not r or not (r[0]["role"] == "platform_admin"
                     or (r[0]["tenant_id"] == tenant_id and r[0]["role"] in ELEVATOR_ROLES)):
        raise PortalError("Elevator staff only.")
    return r[0]


def _is_admin(a: dict) -> bool:
    return a["role"] in ("elevator_admin", "platform_admin")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_invite(db: Db, tenant_id: str, actor_id: int, email: str, role: str = "farmer",
                  farmer_id: int | None = None) -> str:
    """Returns the raw token (put it in the link; it is never stored).

    farmer_id: invite an existing farmer record (staff added them to place offers); on accept the
    new login is linked to it, so the farmer sees the offers staff already placed for them.
    """
    a = _actor(db, tenant_id, actor_id)
    if farmer_id is not None:
        f = db.query("SELECT user_id FROM farmers WHERE farmer_id = %s AND tenant_id = %s AND active",
                     (farmer_id, tenant_id))
        if not f or f[0]["user_id"] is not None or role != "farmer":
            raise PortalError("That farmer record can't be invited (not found, or already has a login).")
    email = email.strip().lower()
    if not _EMAIL.match(email):
        raise PortalError("Enter a valid email address.")
    if role not in INVITABLE:
        raise PortalError("Unknown role.")
    if role != "farmer" and not _is_admin(a):
        raise PortalError("Only an elevator admin can invite staff.")
    if db.query("SELECT 1 AS x FROM users WHERE LOWER(email) = %s", (email,)):
        raise PortalError("That email already has an account.")
    db.execute("UPDATE invites SET used = TRUE WHERE tenant_id = %s AND LOWER(email) = %s AND NOT used",
               (tenant_id, email))                      # a re-invite voids the earlier link
    token = _secrets.token_urlsafe(24)
    _insert(db, "invites", dict(token_hash=_hash(token), tenant_id=tenant_id, email=email, role=role,
                                invited_by=actor_id, farmer_id=farmer_id,
                                expires_at=_ts(datetime.now(timezone.utc) + INVITE_TTL)))
    _audit(db, tenant_id, actor_id, "invite_created", dict(email=email, role=role))
    return token


def _live_invite(db: Db, token: str, tenant_id: str) -> dict | None:
    r = db.query("SELECT * FROM invites WHERE token_hash = %s AND tenant_id = %s AND NOT used",
                 (_hash(token), tenant_id))
    if r and _as_utc(r[0]["expires_at"]) > datetime.now(timezone.utc):
        return r[0]
    return None


def invite_preview(db: Db, token: str, tenant_id: str) -> dict | None:
    """What the join page may show: the invited email and role (None if invalid/expired/used)."""
    inv = _live_invite(db, token, tenant_id)
    return {"email": inv["email"], "role": inv["role"]} if inv else None


def accept_invite(db: Db, token: str, tenant_id: str, name: str) -> dict:
    inv = _live_invite(db, token, tenant_id)
    if not inv:
        raise PortalError("This invite link is invalid, expired, or already used.")
    if db.query("SELECT 1 AS x FROM users WHERE LOWER(email) = %s", (inv["email"],)):
        raise PortalError("That email already has an account.")
    _insert(db, "users", dict(tenant_id=tenant_id, email=inv["email"], name=(name or "").strip() or None,
                              role=inv["role"], invited_by=inv["invited_by"]))
    db.execute("UPDATE invites SET used = TRUE WHERE token_hash = %s", (inv["token_hash"],))
    user = db.query("SELECT * FROM users WHERE LOWER(email) = %s", (inv["email"],))[0]
    if inv["role"] == "farmer":
        fid = inv.get("farmer_id")
        linked = fid is not None and db.query(
            "SELECT 1 AS x FROM farmers WHERE farmer_id = %s AND tenant_id = %s AND user_id IS NULL",
            (fid, tenant_id))
        if linked:
            db.execute("UPDATE farmers SET user_id = %s, email = COALESCE(email, %s) "
                       "WHERE farmer_id = %s AND tenant_id = %s", (user["user_id"], user["email"], fid, tenant_id))
            db.execute("UPDATE offers SET farmer_user_id = %s WHERE farmer_id = %s AND tenant_id = %s",
                       (user["user_id"], fid, tenant_id))
        else:
            ensure_farmer_for_user(db, tenant_id, user)
    _usage(db, tenant_id, "user_joined", user["user_id"])
    _audit(db, tenant_id, user["user_id"], "invite_accepted", dict(role=inv["role"]))
    return user


def pending_invites(db: Db, tenant_id: str, actor_id: int) -> list[dict]:
    _actor(db, tenant_id, actor_id)
    now = datetime.now(timezone.utc)
    rows = db.query("SELECT token_hash, email, role, expires_at FROM invites WHERE tenant_id = %s AND NOT used "
                    "ORDER BY created_at DESC", (tenant_id,))
    return [r for r in rows if _as_utc(r["expires_at"]) > now]


def revoke_invite(db: Db, tenant_id: str, actor_id: int, token_hash: str) -> None:
    _actor(db, tenant_id, actor_id)
    db.execute("UPDATE invites SET used = TRUE WHERE token_hash = %s AND tenant_id = %s", (token_hash, tenant_id))
    _audit(db, tenant_id, actor_id, "invite_revoked")


def list_users(db: Db, tenant_id: str, actor_id: int) -> list[dict]:
    _actor(db, tenant_id, actor_id)
    return db.query("SELECT user_id, email, name, role, active FROM users WHERE tenant_id = %s "
                    "ORDER BY role, email", (tenant_id,))


def _manage(db: Db, tenant_id: str, actor_id: int, user_id: int) -> dict:
    a = _actor(db, tenant_id, actor_id)
    if not _is_admin(a):
        raise PortalError("Only an elevator admin can manage users.")
    if user_id == actor_id:
        raise PortalError("You can't change your own account.")
    r = db.query("SELECT * FROM users WHERE user_id = %s AND tenant_id = %s", (user_id, tenant_id))
    if not r or r[0]["role"] == "platform_admin":
        raise PortalError("User not found for this tenant.")
    return r[0]


def set_user_active(db: Db, tenant_id: str, actor_id: int, user_id: int, active: bool) -> None:
    _manage(db, tenant_id, actor_id, user_id)
    db.execute("UPDATE users SET active = %s WHERE user_id = %s AND tenant_id = %s", (active, user_id, tenant_id))
    _audit(db, tenant_id, actor_id, "user_reactivated" if active else "user_deactivated", dict(user_id=user_id))


def set_user_role(db: Db, tenant_id: str, actor_id: int, user_id: int, role: str) -> None:
    _manage(db, tenant_id, actor_id, user_id)
    if role not in INVITABLE:
        raise PortalError("Unknown role.")
    db.execute("UPDATE users SET role = %s WHERE user_id = %s AND tenant_id = %s", (role, user_id, tenant_id))
    _audit(db, tenant_id, actor_id, "user_role_changed", dict(user_id=user_id, role=role))
