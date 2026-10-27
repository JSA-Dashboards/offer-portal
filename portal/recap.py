"""recap.py — the end-of-day email: offers that hit, offers still working, and what staff bought.

Staff get THEIR information (the offers they entered or acknowledged, and their own purchases) plus the company-wide
to-do lists (hit offers waiting to be acknowledged, acknowledged offers waiting for a contract number). Admins get
ALL the company's information, plus the hedge position and any cancelled offers whose hedge still needs adjusting.
`send_daily_recaps` queues one email per person per day (idempotent); notify.send_pending delivers them.
"""
from __future__ import annotations

import html
from datetime import date, datetime, timezone

from . import notify, purchases, theme
from .data import CONTRACT_BUSHELS, CT, HELD, _require_admin, get_tenant, hedge_summary, list_offers, today_ct
from .db import Db

TH = "style='text-align:left;padding:3px 10px 3px 0;color:#5f7267;font-size:12px;border-bottom:1px solid #d7e2dc'"
TD = "style='padding:3px 10px 3px 0;font-size:13px;vertical-align:top'"


def _ct_date(v) -> date | None:
    """The Central-time calendar day of a stored timestamp (a datetime from Snowflake, or text from sqlite)."""
    if v is None:
        return None
    if isinstance(v, str):
        v = datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).astimezone(CT).date()


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def _px(o: dict) -> str:
    return f"${o['price']:.2f}" if o["price_type"] == "FIXED" else f"{o['basis_cents']:+d}¢ basis"


def _table(headers: list[str], rows: list[list], empty: str) -> str:
    if not rows:
        return f"<p style='color:#8a9199;font-size:13px'>{_e(empty)}</p>"
    head = "".join(f"<th {TH}>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td {TD}>{_e(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table style='border-collapse:collapse'><tr>{head}</tr>{body}</table>"


def _h(text: str) -> str:
    return (f"<div style='margin:18px 0 6px;font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:#5f7267;"
            f"border-bottom:1px solid #d7e2dc;padding-bottom:3px'>{_e(text)}</div>")


def _offer_row(o: dict) -> list:
    return [o["farmer_name"], f"{o['bushels']:,}", o["grain"], _px(o), o["futures_symbol"], o["delivery_period"],
            o.get("farmer_client_id") or "", data_label(o["status"])]


def data_label(status: str) -> str:
    from .data import STATUS_LABELS
    return STATUS_LABELS.get(status, status)


OFFER_HEAD = ["Farmer", "Bushels", "Commodity", "Price / basis", "Futures month", "Delivery period", "Client ID", "Stage"]


def build_recap(db: Db, tenant_id: str, user: dict, day: date | None = None) -> dict:
    """{'subject', 'html', 'counts'} for one person. `user` needs user_id and role."""
    day = day or today_ct()
    admin = user["role"] in ("elevator_admin", "platform_admin")
    tenant = get_tenant(db, tenant_id) or {"tenant_id": tenant_id, "name": tenant_id}
    viewer = {"role": "elevator_admin", "user_id": user["user_id"], "tenant_id": tenant_id}
    offers = list_offers(db, tenant_id, viewer)

    def mine(o):                                   # staff: offers they entered or acknowledged
        return admin or o.get("placed_by") == user["user_id"] or o.get("acknowledged_by") == user["user_id"]
    hit_today = [o for o in offers if _ct_date(o.get("hit_at")) == day and mine(o)]
    working = [o for o in offers if o["status"] == "submitted" and mine(o)]
    need_ack = [o for o in offers if o["status"] == "hit"]
    need_contract = [o for o in offers if o["status"] == "acknowledged"]
    buys = purchases.list_purchases(db, tenant_id, user, day)
    buy_groups = purchases.summarize(buys)

    parts = [f"<p style='font-size:15px'>Daily recap for <b>{_e(day.strftime('%A, %B %d, %Y'))}</b> · "
             f"{'all company activity' if admin else 'your activity'}.</p>"]
    if need_ack or need_contract:
        parts.append(_h("To do"))
        if need_ack:
            parts.append(f"<p><b>{len(need_ack)}</b> hit offer{'s' if len(need_ack) != 1 else ''} waiting to be acknowledged.</p>")
            parts.append(_table(OFFER_HEAD, [_offer_row(o) for o in need_ack], ""))
        if need_contract:
            parts.append(f"<p><b>{len(need_contract)}</b> acknowledged offer{'s' if len(need_contract) != 1 else ''} "
                         f"waiting for a contract number.</p>")
            parts.append(_table(OFFER_HEAD, [_offer_row(o) for o in need_contract], ""))
    parts.append(_h("Offers that hit today"))
    parts.append(_table(OFFER_HEAD, [_offer_row(o) for o in hit_today], "No offers hit today."))
    parts.append(_h("Offers still working"))
    work_rows = sorted(working, key=lambda o: (o["grain"], o["delivery_period"], o["farmer_name"] or ""))
    parts.append(_table(OFFER_HEAD, [_offer_row(o) for o in work_rows], "No outstanding offers."))
    parts.append(_h("Purchases made today" + ("" if admin else " (yours)") + " (includes offers acknowledged today)"))
    if not buy_groups:
        parts.append("<p style='color:#8a9199;font-size:13px'>No purchases were logged today.</p>")
    for g in buy_groups:
        avg = " · ".join(x for x in (f"avg ${g['avg_price']:.4f}" if g["avg_price"] is not None else "",
                                      f"avg basis {g['avg_basis_cents']:+.1f}¢" if g["avg_basis_cents"] is not None else "") if x)
        parts.append(f"<p style='margin:10px 0 4px'><b>{_e(g['grain'])}</b>: {g['bushels']:,} bu in {g['entries']} "
                     f"purchase{'s' if g['entries'] != 1 else ''}{' · ' + avg if avg else ''}</p>")
        head = ["Bought from", "Bushels", "Price", "Basis", "Futures month", "Delivery location", "Delivery period", "Contract #",
                "Source"]
        if admin:
            head.append("Logged by")
        rows = []
        for p in g["rows"]:
            r = [p["counterparty"], f"{p['bushels']:,}", f"${p['price']:.4f}" if p["price"] is not None else "",
                 f"{p['basis_cents']:+d}¢" if p["basis_cents"] is not None else "", p["futures_symbol"] or "",
                 p["delivery_location"] or "", p["delivery_period"] or "", p["contract_ref"] or "",
                 "Offer" if p.get("offer_id") else "Entered"]
            if admin:
                r.append(p.get("staff_name") or p.get("staff_email") or "")
            rows.append(r)
        parts.append(_table(head, rows, ""))
    if admin:
        h = hedge_summary(db, tenant_id, user["user_id"])
        parts.append(_h("Hedge position (admin)"))
        parts.append(_table(["Commodity", "Futures month", "Offers", "Bushels", "Contracts", "Whole", "Left over (bu)"],
                            [[t["grain"], t["futures_symbol"], t["offers"], f"{t['bushels']:,}", t["contracts"],
                              t["whole_contracts"], f"{t['remainder_bushels']:,}"] for t in h["now_totals"]],
                            "No hit offers waiting to be hedged."))
        if h["adjustments"]:
            parts.append(f"<p style='color:#b42318'><b>{len(h['adjustments'])}</b> cancelled offer"
                         f"{'s' if len(h['adjustments']) != 1 else ''} still need a hedge adjustment.</p>")
    link = ""
    from .data import portal_link
    if portal_link(tenant_id):
        link = f"<p><a href=\"{_e(portal_link(tenant_id))}\">Open the portal</a></p>"
    body = theme.email_html(tenant, "".join(parts) + link)
    return dict(subject=f"{tenant.get('name', tenant_id)} daily recap: {day.strftime('%a %b %d')}", html=body,
                counts=dict(hit_today=len(hit_today), working=len(working), need_ack=len(need_ack),
                            need_contract=len(need_contract), purchases=len(buys)))


def send_daily_recaps(db: Db, day: date | None = None, tenant_id: str | None = None) -> dict:
    """Queue today's recap for every staff member and admin who has email on (once per person per day)."""
    day = day or today_ct()
    sql, params = "SELECT tenant_id FROM tenants WHERE status = 'active'", ()
    if tenant_id:
        sql, params = sql + " AND tenant_id = %s", (tenant_id,)
    queued = 0
    for t in db.query(sql + " ORDER BY tenant_id", params):
        for u in notify.staff_recipients(db, t["tenant_id"]):
            r = build_recap(db, t["tenant_id"], dict(user_id=u["user_id"], role=u["role"], email=u["email"]), day)
            queued += notify.queue(db, t["tenant_id"], "daily_recap", u, r["subject"], r["html"],
                                   dedupe_key=f"daily_recap:{day.isoformat()}")
    return dict(day=day.isoformat(), queued=queued)
