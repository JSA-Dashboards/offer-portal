"""farmers.py — a tenant's farmer customer records, managed by elevator staff.

A farmer record is the elevator's customer: an optional client ID (the elevator's own account
number for the entity), an entity name (farm / partnership / LLC) and an individual name, with
at least one of the two names. Phone and email are optional. It exists independently of a portal
login so staff can place offers for farmers who call in or stop by; inviting the farmer later
(people.create_invite with farmer_id) links a login to the same record.
"""
from __future__ import annotations

import re

from .data import PortalError, _audit, _insert, _require_staff, new_id
from .db import Db

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def display_name(entity_name: str | None, individual_name: str | None) -> str:
    """How the farmer appears in lists: 'Entity (Individual)', or whichever one exists."""
    if entity_name and individual_name:
        return f"{entity_name} ({individual_name})"
    return entity_name or individual_name or ""


def _clean(individual_name, phone, email, entity_name, client_id) -> dict:
    ind = (individual_name or "").strip() or None
    ent = (entity_name or "").strip() or None
    if not (ind or ent):
        raise PortalError("Enter an entity name or an individual name.")
    email = (email or "").strip().lower() or None
    if email and not _EMAIL.match(email):
        raise PortalError("That email address isn't valid.")
    cid = (client_id or "").strip() or None
    if cid and len(cid) > 40:
        raise PortalError("Client ID is too long (40 characters max).")
    return dict(individual_name=ind, entity_name=ent, name=display_name(ent, ind),
                phone=(phone or "").strip() or None, email=email, client_id=cid)


def _client_id_taken(db: Db, tenant_id: str, client_id: str | None, except_id: int | None = None) -> bool:
    if not client_id:
        return False
    rows = db.query("SELECT farmer_id FROM farmers WHERE tenant_id = %s AND LOWER(client_id) = %s",
                    (tenant_id, client_id.lower()))
    return any(r["farmer_id"] != except_id for r in rows)


def create_farmer(db: Db, tenant_id: str, staff_id: int, individual_name: str = "", phone: str | None = None,
                  email: str | None = None, *, entity_name: str | None = None,
                  client_id: str | None = None) -> int:
    _require_staff(db, tenant_id, staff_id)
    c = _clean(individual_name, phone, email, entity_name, client_id)
    if _client_id_taken(db, tenant_id, c["client_id"]):
        raise PortalError("That client ID is already used by another farmer.")
    dup = db.query("SELECT 1 AS x FROM farmers WHERE tenant_id = %s AND LOWER(name) = %s AND active "
                   "AND COALESCE(phone, '') = %s", (tenant_id, c["name"].lower(), c["phone"] or ""))
    if dup:
        raise PortalError("A farmer with that name and phone already exists.")
    fid = new_id()
    _insert(db, "farmers", dict(farmer_id=fid, tenant_id=tenant_id, created_by=staff_id, **c))
    _audit(db, tenant_id, staff_id, "farmer_created", dict(farmer_id=fid, name=c["name"], client_id=c["client_id"]))
    return fid


def update_farmer(db: Db, tenant_id: str, staff_id: int, farmer_id: int, individual_name: str,
                  phone: str | None, email: str | None, *, entity_name: str | None = None,
                  client_id: str | None = None) -> None:
    _require_staff(db, tenant_id, staff_id)
    c = _clean(individual_name, phone, email, entity_name, client_id)
    if not db.query("SELECT 1 AS x FROM farmers WHERE farmer_id = %s AND tenant_id = %s", (farmer_id, tenant_id)):
        raise PortalError("Farmer not found for this tenant.")
    if _client_id_taken(db, tenant_id, c["client_id"], except_id=farmer_id):
        raise PortalError("That client ID is already used by another farmer.")
    db.execute("UPDATE farmers SET name = %s, individual_name = %s, entity_name = %s, client_id = %s, "
               "phone = %s, email = %s WHERE farmer_id = %s AND tenant_id = %s",
               (c["name"], c["individual_name"], c["entity_name"], c["client_id"], c["phone"], c["email"],
                farmer_id, tenant_id))
    _audit(db, tenant_id, staff_id, "farmer_updated", dict(farmer_id=farmer_id))


def set_farmer_active(db: Db, tenant_id: str, staff_id: int, farmer_id: int, active: bool) -> None:
    _require_staff(db, tenant_id, staff_id)
    db.execute("UPDATE farmers SET active = %s WHERE farmer_id = %s AND tenant_id = %s",
               (active, farmer_id, tenant_id))
    _audit(db, tenant_id, staff_id, "farmer_reactivated" if active else "farmer_deactivated",
           dict(farmer_id=farmer_id))


def list_farmers(db: Db, tenant_id: str, staff_id: int, include_inactive: bool = False) -> list[dict]:
    """Each farmer with whether they have a portal login and how many offers are still open."""
    _require_staff(db, tenant_id, staff_id)
    sql = ("SELECT f.farmer_id, f.name, f.individual_name, f.entity_name, f.client_id, f.phone, f.email, "
           "f.user_id, f.active, "
           "(SELECT COUNT(*) FROM offers o WHERE o.farmer_id = f.farmer_id AND o.tenant_id = f.tenant_id "
           "AND o.status = 'submitted') AS open_offers "
           "FROM farmers f WHERE f.tenant_id = %s")
    if not include_inactive:
        sql += " AND f.active"
    return db.query(sql + " ORDER BY f.name", (tenant_id,))
