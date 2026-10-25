"""platform.py — JSA platform-admin operations: tenants, branding, locations, scrape
sources. Every function requires a platform_admin actor (re-checked in the database,
not trusted from the UI) and writes an audit row.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

from . import people
from .data import PortalError, _audit, _insert, usage_by_tenant
from .db import Db

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
RESERVED = {"admin", "api", "www", "platform", "jsa", "static", "login"}
SOURCE_KINDS = ("agricharts", "bushelsites")


def _admin(db: Db, actor_id: int) -> dict:
    r = db.query("SELECT * FROM users WHERE user_id = %s AND active AND role = 'platform_admin'", (actor_id,))
    if not r:
        raise PortalError("Platform admin only.")
    return r[0]


def _https_url(url: str, what: str) -> str:
    u = urlparse((url or "").strip())
    host = u.hostname or ""
    if u.scheme != "https" or "." not in host:
        raise PortalError(f"{what} must be an https:// address.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return u.geturl()
    raise PortalError(f"{what} must use a domain name, not an IP address.")


def _tenant(db: Db, tenant_id: str) -> dict:
    r = db.query("SELECT * FROM tenants WHERE tenant_id = %s", (tenant_id,))
    if not r:
        raise PortalError("Unknown tenant.")
    return r[0]


# ── Tenants ─────────────────────────────────────────────────────────────────
def list_tenants(db: Db, actor_id: int) -> list[dict]:
    _admin(db, actor_id)
    return db.query("SELECT t.*, (SELECT COUNT(*) FROM users u WHERE u.tenant_id = t.tenant_id AND u.active) "
                    "AS users FROM tenants t ORDER BY t.name")


def create_tenant(db: Db, actor_id: int, slug: str, name: str, first_admin_email: str,
                  locations: list[tuple[str, str | None]]) -> str:
    """Creates the tenant, its locations, and an elevator_admin invite. Returns the invite token."""
    _admin(db, actor_id)
    slug, name = slug.strip().lower(), name.strip()
    if not _SLUG.match(slug) or slug in RESERVED:
        raise PortalError("Slug: 2-31 characters, lowercase letters, digits and hyphens, not reserved.")
    if not name:
        raise PortalError("Company name is required.")
    if db.query("SELECT 1 AS x FROM tenants WHERE tenant_id = %s", (slug,)):
        raise PortalError("That slug is already taken.")
    locs = [(n.strip(), (s or "").strip().upper() or None) for n, s in locations if n.strip()]
    if not locs:
        raise PortalError("Add at least one location.")
    _insert(db, "tenants", dict(tenant_id=slug, name=name, from_name=name))
    for n, st in locs:
        _insert(db, "locations", dict(tenant_id=slug, name=n, state=st))
    _audit(db, slug, actor_id, "tenant_created", dict(name=name))
    return people.create_invite(db, slug, actor_id, first_admin_email, "elevator_admin")


def update_tenant(db: Db, actor_id: int, tenant_id: str, *, name: str, logo_url: str | None,
                  primary_color: str, accent_color: str, support_email: str | None,
                  from_name: str | None, open_pricing: bool, hedging_enabled: bool) -> None:
    _admin(db, actor_id)
    _tenant(db, tenant_id)
    if not name.strip():
        raise PortalError("Company name is required.")
    if not (_HEX.match(primary_color or "") and _HEX.match(accent_color or "")):
        raise PortalError("Colors must look like #32373c.")
    logo = _https_url(logo_url, "Logo URL") if (logo_url or "").strip() else None
    email = (support_email or "").strip() or None
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise PortalError("Support email isn't valid.")
    db.execute("UPDATE tenants SET name = %s, logo_url = %s, primary_color = %s, accent_color = %s, "
               "support_email = %s, from_name = %s, open_pricing = %s, hedging_enabled = %s "
               "WHERE tenant_id = %s",
               (name.strip(), logo, primary_color, accent_color, email, (from_name or "").strip() or None,
                open_pricing, hedging_enabled, tenant_id))
    _audit(db, tenant_id, actor_id, "tenant_updated", dict(open_pricing=open_pricing,
                                                           hedging_enabled=hedging_enabled))


def set_tenant_status(db: Db, actor_id: int, tenant_id: str, active: bool) -> None:
    _admin(db, actor_id)
    _tenant(db, tenant_id)
    db.execute("UPDATE tenants SET status = %s WHERE tenant_id = %s",
               ("active" if active else "suspended", tenant_id))
    _audit(db, tenant_id, actor_id, "tenant_reactivated" if active else "tenant_suspended")


# ── Locations ───────────────────────────────────────────────────────────────
def add_location(db: Db, actor_id: int, tenant_id: str, name: str, state: str | None) -> None:
    _admin(db, actor_id)
    _tenant(db, tenant_id)
    name = name.strip()
    if not name:
        raise PortalError("Location name is required.")
    if db.query("SELECT 1 AS x FROM locations WHERE tenant_id = %s AND LOWER(name) = %s", (tenant_id, name.lower())):
        raise PortalError("That location already exists.")
    _insert(db, "locations", dict(tenant_id=tenant_id, name=name, state=(state or "").strip().upper() or None))
    _audit(db, tenant_id, actor_id, "location_added", dict(name=name))


# ── Scrape sources ──────────────────────────────────────────────────────────
def list_sources(db: Db, actor_id: int, tenant_id: str) -> list[dict]:
    _admin(db, actor_id)
    return db.query("SELECT * FROM scrape_sources WHERE tenant_id = %s ORDER BY source_id", (tenant_id,))


def add_source(db: Db, actor_id: int, tenant_id: str, kind: str, url: str, location_name: str | None) -> None:
    _admin(db, actor_id)
    _tenant(db, tenant_id)
    if kind not in SOURCE_KINDS:
        raise PortalError("Unknown source type.")
    url = _https_url(url, "Source URL")
    loc = (location_name or "").strip() or None
    if kind == "bushelsites":
        names = {l["name"].lower() for l in db.query("SELECT name FROM locations WHERE tenant_id = %s", (tenant_id,))}
        if not loc or loc.lower() not in names:
            raise PortalError("A Bushel source needs the name of one of this tenant's locations.")
    _insert(db, "scrape_sources", dict(tenant_id=tenant_id, kind=kind, url=url, location_name=loc))
    _audit(db, tenant_id, actor_id, "source_added", dict(kind=kind, url=url))


def set_source_active(db: Db, actor_id: int, tenant_id: str, source_id: int, active: bool) -> None:
    _admin(db, actor_id)
    db.execute("UPDATE scrape_sources SET active = %s WHERE source_id = %s AND tenant_id = %s",
               (active, source_id, tenant_id))
    _audit(db, tenant_id, actor_id, "source_enabled" if active else "source_disabled", dict(source_id=source_id))


# ── Usage / billing ─────────────────────────────────────────────────────────
def usage_table(db: Db, actor_id: int, month: str) -> list[dict]:
    """One row per tenant for the month: a column per usage event type, plus active users."""
    _admin(db, actor_id)
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
        raise PortalError("Month must look like 2026-10.")
    rows = usage_by_tenant(db, {"role": "platform_admin"}, month)
    table: dict[str, dict] = {}
    for t in db.query("SELECT tenant_id, name FROM tenants ORDER BY name"):
        table[t["tenant_id"]] = {"Tenant": t["name"], "offer_submitted": 0, "offer_hit": 0, "offer_completed": 0,
                                 "bid_published": 0, "user_joined": 0}
    for r in rows:
        if r["tenant_id"] in table:
            table[r["tenant_id"]][r["event_type"]] = int(r["qty"])
    for r in db.query("SELECT tenant_id, COUNT(*) AS n FROM users WHERE active AND role = 'farmer' "
                      "GROUP BY tenant_id"):
        if r["tenant_id"] in table:
            table[r["tenant_id"]]["active_farmers"] = int(r["n"])
    for v in table.values():
        v.setdefault("active_farmers", 0)
    return list(table.values())
