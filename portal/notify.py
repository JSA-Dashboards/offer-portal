"""notify.py — email notifications for admins, staff and farmers.

Events (portal/data.py `_notify`) queue a row in `notifications`; a sender delivers it through Microsoft Graph.
The app flushes the queue right after a user acts, and `notify_run.py` flushes it on the droplet (that covers
offers hit automatically by the futures job, and the daily recaps). Nothing here can fail an offer action: the
caller wraps `offer_event` in a try/except.

Who hears about what:
  offer_submitted  staff + admins (not the person who entered it)      "New offer"
  offer_hit        the farmer, staff, admins                           "confirm the purchase, then enter the contract #"
  offer_completed  the farmer                                          contract number
  offer_declined / offer_expired   the farmer
  offer_withdrawn  staff + admins (when the FARMER withdrew)
  offer_cancelled  admins ("adjust the hedge") and the farmer
  daily_recap      each staff member (own info) and each admin (company info)  see recap.py
A farmer is emailed at the login's address, or at the farmer record's email when there is no login, unless that
person turned notifications off.
"""
from __future__ import annotations

import html
import re

from . import theme
from .data import CONTRACT_BUSHELS, PortalError, _insert, new_id
from .db import Db

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_ATTEMPTS = 3


# ── the outbox ──────────────────────────────────────────────────────────────────
def queue(db: Db, tenant_id: str, kind: str, to: dict, subject: str, body_html: str, *, offer_id=None,
          dedupe_key: str | None = None) -> bool:
    """Queue one email. Returns False (and queues nothing) for a bad address or a duplicate (same key + address)."""
    email = (to.get("email") or "").strip().lower()
    if not _EMAIL.match(email):
        return False
    if dedupe_key and db.query("SELECT 1 AS x FROM notifications WHERE tenant_id = %s AND dedupe_key = %s AND to_email = %s",
                               (tenant_id, dedupe_key, email)):
        return False
    _insert(db, "notifications", dict(notification_id=new_id(), tenant_id=tenant_id, kind=kind, to_email=email,
                                      to_name=to.get("name"), user_id=to.get("user_id"), offer_id=offer_id,
                                      dedupe_key=dedupe_key, subject=subject[:250], body_html=body_html))
    return True


def send_pending(db: Db, send=None, limit: int = 25) -> dict:
    """Deliver queued (and previously failed, under MAX_ATTEMPTS) emails. `send(to, subject, html)` defaults to Graph;
    without Graph settings nothing is sent and the queue is left alone."""
    from . import auth
    if send is None:
        if not auth.graph_configured():
            return dict(sent=0, failed=0, skipped="Graph is not configured; emails stay queued")
        send = auth.send_via_graph
    rows = db.query("SELECT notification_id, to_email, subject, body_html, attempts FROM notifications "
                    "WHERE status IN ('queued', 'failed') AND attempts < %s ORDER BY notification_id", (MAX_ATTEMPTS,))[:limit]
    sent = failed = 0
    for r in rows:
        try:
            send(r["to_email"], r["subject"], r["body_html"])
            db.execute("UPDATE notifications SET status = 'sent', sent_at = CURRENT_TIMESTAMP(), attempts = attempts + 1, "
                       "error = NULL WHERE notification_id = %s", (r["notification_id"],))
            sent += 1
        except Exception as exc:                          # one bad address must not stop the rest
            db.execute("UPDATE notifications SET status = 'failed', attempts = attempts + 1, error = %s "
                       "WHERE notification_id = %s", (f"{type(exc).__name__}: {exc}"[:400], r["notification_id"]))
            failed += 1
    return dict(sent=sent, failed=failed)


def set_notify(db: Db, user_id: int, on: bool) -> None:
    """A portal user turns their own email notifications on or off."""
    db.execute("UPDATE users SET notify_email = %s WHERE user_id = %s", (bool(on), user_id))


def set_farmer_notify(db: Db, tenant_id: str, staff_id: int, farmer_id: int, on: bool) -> None:
    from .data import _require_staff
    _require_staff(db, tenant_id, staff_id)
    db.execute("UPDATE farmers SET notify_email = %s WHERE farmer_id = %s AND tenant_id = %s", (bool(on), farmer_id, tenant_id))


# ── recipients ──────────────────────────────────────────────────────────────────
def staff_recipients(db: Db, tenant_id: str, admins_only: bool = False) -> list[dict]:
    roles = "('elevator_admin')" if admins_only else "('elevator_admin', 'elevator_staff')"
    rows = db.query(f"SELECT user_id, email, name, role FROM users WHERE tenant_id = %s AND active "
                    f"AND role IN {roles} AND COALESCE(notify_email, TRUE) ORDER BY user_id", (tenant_id,))
    return [dict(user_id=r["user_id"], email=r["email"], name=r["name"], role=r["role"]) for r in rows]


def farmer_recipient(db: Db, tenant_id: str, farmer_id) -> dict | None:
    r = db.query("SELECT f.farmer_id, f.name, f.email AS f_email, f.user_id, f.notify_email AS f_on, "
                 "u.email AS u_email, u.notify_email AS u_on FROM farmers f LEFT JOIN users u ON u.user_id = f.user_id "
                 "WHERE f.farmer_id = %s AND f.tenant_id = %s", (farmer_id, tenant_id))
    if not r:
        return None
    f = r[0]
    if f["user_id"]:                                      # a login: their own address and their own switch
        return dict(user_id=f["user_id"], email=f["u_email"], name=f["name"]) if f["u_on"] in (None, 1, True) else None
    return dict(user_id=None, email=f["f_email"], name=f["name"]) if f["f_on"] in (None, 1, True) else None


# ── content ─────────────────────────────────────────────────────────────────────
def _terms_text(o: dict) -> str:
    px = f"${o['price']:.2f}/bu" if o["price_type"] == "FIXED" else f"{o['basis_cents']:+d}¢ on {o['futures_symbol']}"
    return f"{o['bushels']:,} bu {o['grain']} · {o['contract_type']} · {px} · {o['futures_symbol']}"


def _offer_table(o: dict) -> str:
    rows = [("Farmer", o.get("farmer_name") or ""), ("Client ID", o.get("farmer_client_id") or ""),
            ("Terms", _terms_text(o)), ("Delivery", f"{o['delivery_start'] or ''} {('to ' + str(o['delivery_end'])) if o.get('delivery_end') else ''}".strip()),
            ("Contract #", o.get("contract_ref") or "")]
    body = "".join(f"<tr><td style='padding:2px 14px 2px 0;color:#5f7267'>{html.escape(k)}</td><td>{html.escape(str(v))}</td></tr>"
                   for k, v in rows if v)
    return f"<table style='border-collapse:collapse;font-size:14px'>{body}</table>"


def _msg(tenant: dict | None, lead: str, offer: dict | None = None, extra: str = "") -> str:
    from .data import portal_link
    link = portal_link((tenant or {}).get("tenant_id") or "")
    table = _offer_table(offer) if offer else ""
    open_link = f"<p><a href=\"{html.escape(link)}\">Open the portal</a></p>" if link else ""
    return theme.email_html(tenant, f"<p>{lead}</p>{table}{extra}{open_link}")


def offer_event(db: Db, tenant_id: str, kind: str, offer_id: int, **ctx) -> int:
    """Queue the emails for one offer event. Returns how many were queued."""
    from .data import get_tenant, list_offers
    tenant = get_tenant(db, tenant_id) or {"tenant_id": tenant_id, "name": tenant_id}
    viewer = {"role": "elevator_admin", "user_id": 0, "tenant_id": tenant_id}
    o = next((x for x in list_offers(db, tenant_id, viewer) if x["offer_id"] == offer_id), None)
    if not o:
        return 0
    farmer = farmer_recipient(db, tenant_id, o["farmer_id"])
    staff, admins = staff_recipients(db, tenant_id), staff_recipients(db, tenant_id, admins_only=True)
    who = html.escape(o.get("farmer_name") or "A farmer")
    short = html.escape(_terms_text(o))
    plan: list[tuple[list, str, str]] = []                 # (recipients, subject, html)
    if kind == "offer_submitted":
        rcpt = [s for s in staff if s["user_id"] != o.get("placed_by")]
        plan.append((rcpt, f"New offer: {o['bushels']:,} bu {o['grain']} from {o.get('farmer_name') or 'a farmer'}",
                     _msg(tenant, f"{who} has an offer outstanding: {short}.", o)))
    elif kind == "offer_hit":
        how = "automatically when the bid reached its price" if ctx.get("auto") else "by staff"
        plan.append((staff, f"HIT: {o['bushels']:,} bu {o['grain']} ({o.get('farmer_name') or 'farmer'})",
                     _msg(tenant, f"An offer was hit {how}. <b>Confirm the purchase in the portal, then enter its contract number.</b>", o)))
        if farmer:
            plan.append(([farmer], f"Your offer was hit: {o['bushels']:,} bu {o['grain']}",
                         _msg(tenant, "Good news: your offer was hit. The elevator will confirm your contract shortly.", o)))
    elif kind == "offer_completed":
        if farmer:
            plan.append(([farmer], f"Contract confirmed: {o['bushels']:,} bu {o['grain']} (#{o.get('contract_ref') or ''})",
                         _msg(tenant, "Your offer is complete. Your contract number is below.", o)))
    elif kind in ("offer_declined", "offer_expired"):
        if farmer:
            word = "declined" if kind == "offer_declined" else "expired"
            plan.append(([farmer], f"Your offer was {word}: {o['bushels']:,} bu {o['grain']}",
                         _msg(tenant, f"Your offer was {word} without being filled.", o)))
    elif kind == "offer_withdrawn" and not ctx.get("on_behalf"):
        plan.append((staff, f"Offer withdrawn: {o['bushels']:,} bu {o['grain']} ({o.get('farmer_name') or 'farmer'})",
                     _msg(tenant, f"{who} withdrew an outstanding offer.", o)))
    elif kind == "offer_cancelled":
        contracts = round(o["bushels"] / CONTRACT_BUSHELS.get(o["grain"], 5000), 2)
        reason = html.escape(o.get("cancel_reason") or "")
        extra = (f"<p><b>Reason:</b> {reason}</p><p><b>{contracts} contracts of {html.escape(o['futures_symbol'] or '')}: "
                 f"adjust the hedge, then confirm it on the Hedge page.</b></p>")
        plan.append((admins, f"ADJUST HEDGE: offer cancelled, {o['bushels']:,} bu {o['grain']} ({o.get('farmer_name') or 'farmer'})",
                     _msg(tenant, "An offer that had been hit was cancelled.", o, extra)))
        if farmer:
            plan.append(([farmer], f"Your offer was cancelled: {o['bushels']:,} bu {o['grain']}",
                         _msg(tenant, "Your offer was cancelled by the elevator. Contact them with any questions.", o)))
    n = 0
    for rcpts, subject, body in plan:
        for r in rcpts:
            n += queue(db, tenant_id, kind, r, subject, body, offer_id=offer_id, dedupe_key=f"{kind}:{offer_id}")
    try:
        sms_offer_event(db, tenant, kind, o, farmer, ctx)       # texts ride along; never at the cost of the emails
    except Exception:
        pass
    return n


# ── text messages ───────────────────────────────────────────────────────────────
# Who is texted about what: farmers on hit / completed / cancelled / expired; staff and admins on new offer / hit.
# Only people who opted in (their own consent, in the sidebar), only for elevators switched on in Platform, only while
# SMS_ENABLED=1 and the Twilio settings exist. The daily recap stays email-only.
SMS_FARMER = ("offer_hit", "offer_completed", "offer_cancelled", "offer_expired")
SMS_STAFF = ("offer_submitted", "offer_hit")
STOP_LINE = " Reply STOP to end texts."


def sms_ready(db: Db) -> bool:
    """True once sql/12 has run (the opt-in columns and the outbox exist). A miss is re-checked after a minute, so the
    code can be deployed before the SQL without breaking anything."""
    import time
    now = time.monotonic()
    state = getattr(db, "_sms_ready", None)
    if state and (state[0] or now - state[1] < 60):
        return state[0]
    try:
        db.query("SELECT phone, sms_opt_in_at FROM users LIMIT 1")
        db.query("SELECT sms_enabled, sms_from FROM tenants LIMIT 1")
        db.query("SELECT sms_id FROM sms_queue LIMIT 1")
        ok = True
    except Exception:
        ok = False
    db._sms_ready = (ok, now)
    return ok


def tenant_sms(db: Db, tenant_id: str) -> dict | None:
    """{'from': this elevator's own number or None, 'name': display name} when texting is on for it, else None."""
    from . import sms
    if not (sms.ready() and sms_ready(db)):
        return None
    r = db.query("SELECT name, from_name, sms_enabled, sms_from FROM tenants WHERE tenant_id = %s", (tenant_id,))
    if not r or r[0]["sms_enabled"] not in (1, True):
        return None
    return dict(name=(r[0]["from_name"] or r[0]["name"] or tenant_id), **{"from": r[0]["sms_from"] or None})


def queue_sms(db: Db, tenant_id: str, kind: str, to: dict, body: str, *, offer_id=None, dedupe_key: str | None = None,
              from_number: str | None = None) -> bool:
    """Queue one text. False (and nothing queued) for an unusable number or a duplicate (same key + number)."""
    from . import sms
    phone = sms.normalize_phone(to.get("phone"))
    if not phone:
        return False
    if dedupe_key and db.query("SELECT 1 AS x FROM sms_queue WHERE tenant_id = %s AND dedupe_key = %s AND to_phone = %s",
                               (tenant_id, dedupe_key, phone)):
        return False
    _insert(db, "sms_queue", dict(sms_id=new_id(), tenant_id=tenant_id, kind=kind, to_phone=phone, to_name=to.get("name"),
                                  user_id=to.get("user_id"), offer_id=offer_id, dedupe_key=dedupe_key,
                                  body=body[:600], from_number=from_number))
    return True


def _optin_users(db: Db, tenant_id: str, roles: tuple[str, ...]) -> list[dict]:
    marks = ", ".join(["%s"] * len(roles))
    return db.query(f"SELECT user_id, name, phone, role FROM users WHERE tenant_id = %s AND active AND role IN ({marks}) "
                    f"AND sms_opt_in_at IS NOT NULL AND phone IS NOT NULL ORDER BY user_id", (tenant_id, *roles))


def _sms_text(name: str, kind: str, o: dict) -> str:
    bu, grain = f"{o['bushels']:,} bu {o['grain']}", o["grain"]
    farmer = o.get("farmer_name") or "a farmer"
    if kind == "offer_submitted":
        px = f"${o['price']:.2f}" if o["price_type"] == "FIXED" else f"{o['basis_cents']:+d}c {o['futures_symbol']}"
        return f"{name}: new offer, {farmer}, {bu} at {px}."
    if kind == "offer_hit":
        return f"{name}: HIT, {farmer}, {bu}. Confirm the purchase in the portal."
    return ""


def _farmer_sms_text(name: str, kind: str, o: dict) -> str:
    bu = f"{o['bushels']:,} bu {o['grain']}"
    return {"offer_hit": f"{name}: your offer was hit, {bu}. We'll confirm your contract shortly.",
            "offer_completed": f"{name}: contract confirmed, {bu}" + (f", #{o['contract_ref']}." if o.get("contract_ref") else "."),
            "offer_cancelled": f"{name}: your offer ({bu}) was cancelled. Please contact us.",
            "offer_expired": f"{name}: your offer ({bu}) expired unfilled."}.get(kind, "")


def sms_offer_event(db: Db, tenant: dict, kind: str, o: dict, farmer: dict | None, ctx: dict) -> int:
    """Queue the texts for one offer event (see the rules above). Returns how many were queued."""
    tid = tenant["tenant_id"]
    cfg = tenant_sms(db, tid)
    if not cfg:
        return 0
    n, fr, label = 0, cfg["from"], f"{kind}:{o['offer_id']}"
    if kind in SMS_STAFF:
        text = _sms_text(cfg["name"], kind, o) + STOP_LINE
        for u in _optin_users(db, tid, ("elevator_admin", "elevator_staff")):
            if kind == "offer_submitted" and u["user_id"] == o.get("placed_by"):
                continue                                    # the person who entered it knows
            n += queue_sms(db, tid, kind, u, text, offer_id=o["offer_id"], dedupe_key=label, from_number=fr)
    if kind in SMS_FARMER and farmer and farmer.get("user_id"):
        text = _farmer_sms_text(cfg["name"], kind, o) + STOP_LINE
        for u in _optin_users(db, tid, ("farmer",)):
            if u["user_id"] == farmer["user_id"]:
                n += queue_sms(db, tid, kind, u, text, offer_id=o["offer_id"], dedupe_key=label, from_number=fr)
    return n


def send_pending_sms(db: Db, send=None, limit: int = 25) -> dict:
    """Deliver queued (and failed, under MAX_ATTEMPTS) texts. `send(to, body, from_number)` defaults to Twilio; nothing
    is sent, and the queue is left alone, unless texting is switched on. A STOP reply (Twilio error 21610) clears that
    person's opt-in so they are never texted again until they opt in afresh."""
    from . import sms
    if send is None:
        if not (sms.ready() and sms_ready(db)):
            return dict(sent=0, failed=0, skipped="texting is not switched on; texts stay queued")
        send = sms.send
    elif not sms_ready(db):
        return dict(sent=0, failed=0, skipped="sql/12 has not been run")
    rows = db.query("SELECT sms_id, to_phone, body, from_number, user_id, attempts FROM sms_queue "
                    "WHERE status IN ('queued', 'failed') AND attempts < %s ORDER BY sms_id", (MAX_ATTEMPTS,))[:limit]
    sent = failed = 0
    for r in rows:
        try:
            pid = send(r["to_phone"], r["body"], r["from_number"])
            db.execute("UPDATE sms_queue SET status = 'sent', sent_at = CURRENT_TIMESTAMP(), attempts = attempts + 1, "
                       "error = NULL, provider_id = %s WHERE sms_id = %s", (pid or None, r["sms_id"]))
            sent += 1
        except sms.StopRequested as exc:
            if r["user_id"]:
                db.execute("UPDATE users SET sms_opt_in_at = NULL WHERE user_id = %s", (r["user_id"],))
            db.execute("UPDATE sms_queue SET status = 'opted_out', attempts = attempts + 1, error = %s WHERE sms_id = %s",
                       (str(exc)[:400], r["sms_id"]))
            failed += 1
        except Exception as exc:
            db.execute("UPDATE sms_queue SET status = 'failed', attempts = attempts + 1, error = %s WHERE sms_id = %s",
                       (f"{type(exc).__name__}: {exc}"[:400], r["sms_id"]))
            failed += 1
    return dict(sent=sent, failed=failed)


def set_sms_optin(db: Db, user_id: int, phone: str | None, on: bool) -> None:
    """A person turns their own text alerts on (with their number: this is their consent, recorded with the time) or off.
    Turning on queues a confirmation text that says how to stop."""
    from . import sms
    from .data import PortalError, _audit
    if not sms_ready(db):
        raise PortalError("Text alerts aren't switched on yet.")
    u = db.query("SELECT user_id, tenant_id, name FROM users WHERE user_id = %s AND active", (user_id,))
    if not u or not u[0]["tenant_id"]:
        raise PortalError("Text alerts are for elevator staff and farmers.")
    tid = u[0]["tenant_id"]
    if not on:
        db.execute("UPDATE users SET sms_opt_in_at = NULL WHERE user_id = %s", (user_id,))
        _audit(db, tid, user_id, "sms_opt_out", {})
        return
    cfg = tenant_sms(db, tid)
    if not cfg:
        raise PortalError("Text alerts aren't switched on for your elevator yet.")
    number = sms.normalize_phone(phone)
    if not number:
        raise PortalError("Enter a mobile number with its area code, like (555) 123-4567.")
    before = db.query("SELECT phone, sms_opt_in_at FROM users WHERE user_id = %s", (user_id,))[0]
    already = bool(before["sms_opt_in_at"]) and before["phone"] == number
    db.execute("UPDATE users SET phone = %s, sms_opt_in_at = CURRENT_TIMESTAMP() WHERE user_id = %s", (number, user_id))
    _audit(db, tid, user_id, "sms_opt_in", {})
    if not already:                                         # saving the same settings again does not text them again
        queue_sms(db, tid, "sms_optin", dict(phone=number, name=u[0]["name"], user_id=user_id),
                  f"{cfg['name']}: you're signed up for offer text alerts. Msg & data rates may apply. "
                  f"Reply STOP to end, HELP for help.", from_number=cfg["from"])


def flush_all(db: Db, limit: int = 25) -> dict:
    """Email and texts in one go (what the app and the droplet job call)."""
    out = send_pending(db, limit=limit)
    out["sms"] = send_pending_sms(db, limit=limit)
    return out
