"""JSA Offer Portal Management — white-label grain offer manager. One app; the tenant comes from ?t=<slug>.

Access by role (same portal, more or less in it): JSA platform admin (everything, every tenant) >
elevator admin (bids, scraped bids, hedging, auto-hit, people, everything staff do) >
elevator staff (offers, purchases, farmers; the bid board is read-only) >
farmer (the bid board read-only, their own offers, delayed futures, the USDA calendar).
"""
from __future__ import annotations

import html
import logging
import os
import pathlib
import re
import time
from datetime import date

import streamlit as st

# Streamlit Cloud secrets -> env, so portal/* (which reads os.environ) works unchanged.
try:
    for _k in st.secrets:
        if isinstance(st.secrets[_k], str) and not os.environ.get(_k):
            os.environ[_k] = st.secrets[_k]
except Exception:
    pass

import scrape_run
from portal import auth, contracts, sms, usda_calendar, data, farmers, futures, massive, notify, people, platform, purchases, recap, theme
from portal.db import connect

GRAINS = contracts.GRAINS
DEV = os.environ.get("OFFER_PORTAL_DEV") == "1"

_ICON = pathlib.Path(__file__).parent / "assets" / "jsa_favicon.png"
st.set_page_config(page_title="JSA Offer Portal Management", page_icon=str(_ICON) if _ICON.exists() else ":material/grain:",
                   layout="wide", initial_sidebar_state="expanded")


@st.cache_resource
def get_db():
    db = connect()
    db.actor_ttl = 15          # a page makes several role checks: reuse each for a few seconds (see data._actor)
    return db


def _kept(key: str, ttl: float, fn):
    """fn()'s result, kept in this session for ttl seconds. Every tab click reruns the whole script and each database
    round trip costs ~0.1-0.3 s, so things that rarely change (the tenant row, the JSA tenant list) are not re-read each time."""
    now = time.monotonic()
    hit = st.session_state.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    st.session_state[key] = (now, val)
    return val


def _markets(count: int = 6) -> list[str]:
    """Every commodity's next listed futures months as 'Corn|ZCZ26' choices: one pick names the market and the month."""
    return [f"{g}|{s}" for g in GRAINS for s in contracts.options(g, date.today(), count)]


def _market_label(choice: str) -> str:
    g, s = choice.split("|")
    return f"{g} · {contracts.futures_label(s)} ({s})"


def _tab_pages(labels: list[str], key: str, default: str | None = None):
    """Tab pages that only run the open tab: yields each label whose tab is selected, inside that tab.
    (A plain st.tabs runs every tab on every click; with on_change="rerun" only the selected one is computed.)"""
    seen = f"_tab_{key}"                                          # the open tab survives reruns and role switches
    default = default or st.session_state.get(seen)
    tabs = st.tabs(labels, key=key, on_change="rerun", default=default if default in labels else None)
    for label, tab in zip(labels, tabs):
        with tab:
            if tab.open:
                st.session_state[seen] = label
                yield label


def _months(n: int = 24) -> list[str]:
    t, out = date.today(), []
    y, m = t.year, t.month
    for _ in range(n):
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _brand(tenant: dict | None) -> None:
    """The COT-style theme, with this tenant's colors, plus the header (tenant logo/name left, JSA 'Powered by' right)."""
    st.session_state["_futures_shown"] = False             # views set this when they draw a futures price
    st.markdown(theme.css(tenant), unsafe_allow_html=True)
    st.markdown(theme.header_html(tenant), unsafe_allow_html=True)


def _end(tenant: dict | None = None) -> None:
    """The compliance footer (standard JSA disclaimer; adds the delayed-quote notice on a page that showed
    futures). Called at the end of every page, including the sign-in and error pages."""
    st.markdown(theme.footer_html(tenant, delayed=bool(st.session_state.get("_futures_shown"))),
                unsafe_allow_html=True)


def _stop(tenant: dict | None = None) -> None:
    _end(tenant)
    st.stop()


def _may_see_futures(tenant: dict | None, user: dict) -> bool:
    """Delayed futures are always visible to elevator staff; farmers see them only when JSA has switched
    that on for the tenant (market-data licensing)."""
    return user["role"] != "farmer" or bool((tenant or {}).get("show_futures"))


def _send_code(tenant: dict | None):
    name = tenant["name"] if tenant else "JSA Offer Portal Management"

    def send(email: str, code: str) -> None:
        if auth.graph_configured():
            auth.send_via_graph(
                email, f"Your {name} sign-in code",
                f"<p>Your sign-in code is <b>{code}</b>. It expires in 10 minutes.</p>")
        elif DEV:
            st.session_state["_dev_code"] = code          # local development only
        else:
            raise RuntimeError("Email is not configured.")
    return send


def sms_settings(db, user: dict, tenant: dict) -> None:
    """Sidebar: a person's own text-alert consent. Shown only where texting is on for the elevator."""
    tid = tenant["tenant_id"]
    cfg = _kept(f"_k_sms_{tid}", 60, lambda: notify.tenant_sms(db, tid))
    if not cfg:
        return
    me = _kept(f"_k_smsu_{user['user_id']}", 60, lambda: db.query(
        "SELECT phone, sms_opt_in_at FROM users WHERE user_id = %s", (user["user_id"],))[0])
    on = bool(me["sms_opt_in_at"])
    with st.expander("Text alerts" + (" (on)" if on else "")):
        if user["role"] == "farmer":
            st.caption("Texts when your offer is hit, completed, cancelled or expires.")
        else:
            st.caption("Texts when a new offer comes in and when an offer is hit.")
        phone = st.text_input("Mobile number", sms.pretty(me["phone"]), key="sms_phone", placeholder="(555) 123-4567")
        agree = st.checkbox(f"I agree to receive text messages from {cfg['name']} about offers. Message and data rates may "
                            "apply. Reply STOP to end, HELP for help.", value=on, key="sms_agree")
        if st.button("Save text settings", key="sms_save"):
            try:
                notify.set_sms_optin(db, user["user_id"], phone, agree)
                st.session_state.pop(f"_k_smsu_{user['user_id']}", None)
                _flush_mail()
                st.session_state["sms_msg"] = ("Text alerts are on. A confirmation text is on its way." if agree
                                               else "Text alerts are off.")
                st.rerun()                                   # so the header above reflects the new state
            except data.PortalError as e:
                st.error(str(e))
        if msg := st.session_state.pop("sms_msg", None):
            st.success(msg)


def password_form(db, user: dict, key: str, current_needed: bool = False) -> bool:
    """New-password fields (and the current password when one is set and nothing else proved who they are).
    True once saved."""
    with st.form(key, clear_on_submit=True):
        cur = st.text_input("Current password", type="password") if current_needed else None
        new = st.text_input(f"New password (at least {auth.MIN_PASSWORD} characters)", type="password")
        again = st.text_input("New password again", type="password")
        go = st.form_submit_button("Save password", type="primary")
    if not go:
        return False
    try:
        if current_needed and not auth.verify_password(db, user["email"], cur or "", user["tenant_id"]):
            st.error("The current password isn't right.")
            return False
        if new != again:
            st.error("The two passwords don't match.")
            return False
        auth.set_password(db, user["user_id"], new)
    except data.PortalError as e:
        st.error(str(e))
        return False
    return True


def set_password_view(db, user: dict) -> None:
    """Right after an email-code sign-in: offer to set a password so next time is one step."""
    st.subheader("Set a password for next time")
    st.caption("You just signed in with an email code. With a password you can sign in with your email and password "
               "from now on. If you already have one and forgot it, this replaces it.")
    if password_form(db, user, "set_pw"):
        st.session_state.pop("pw_offer", None)
        st.success("Password saved. Use it next time.")
        st.rerun()
    if st.button("Skip for now", key="skip_pw"):
        st.session_state.pop("pw_offer", None)
        st.rerun()


def jsa_sidebar(db, real: dict, tenant: dict) -> dict:
    """JSA super admin: jump to any tenant and look at the portal as an elevator admin, staff or a farmer.
    Returns the user the page should be drawn for. Admin/staff previews keep your own identity (anything you do is
    recorded as you); a farmer preview shows that farmer's own pages, read-only."""
    tenants = _kept("_k_tenants", 60, lambda: platform.list_tenants(db, real["user_id"]))
    slugs = [t["tenant_id"] for t in tenants]
    names = {t["tenant_id"]: t["name"] for t in tenants}
    with st.sidebar:
        st.markdown("**JSA tools**")
        pick = st.selectbox("Tenant", slugs, index=slugs.index(tenant["tenant_id"]) if tenant["tenant_id"] in slugs else 0,
                            format_func=names.get, key="jsa_tenant")
        if pick != tenant["tenant_id"]:
            st.query_params["t"] = pick
            st.session_state.pop("view_as", None)
            st.rerun()
        lens = st.radio("View as", ["JSA admin (me)", "Elevator admin", "Elevator staff", "Farmer"], key="view_as")
        farmer = None
        if lens == "Farmer":
            fl = _kept(f"_k_pf_{tenant['tenant_id']}", 60, lambda: platform.preview_farmers(db, real["user_id"], tenant["tenant_id"]))
            if fl:
                farmer = st.selectbox("Which farmer", fl, format_func=lambda u: u["farmer_name"], key="view_as_farmer")
            else:
                st.caption("No farmer here has a login yet, so there is nothing to show as a farmer.")
    if lens == "Elevator admin":
        return {**real, "role": "elevator_admin", "tenant_id": tenant["tenant_id"], "_preview": "elevator admin"}
    if lens == "Elevator staff":
        return {**real, "role": "elevator_staff", "tenant_id": tenant["tenant_id"], "_preview": "elevator staff"}
    if lens == "Farmer":
        if farmer:
            return {**farmer, "_preview": f"farmer {farmer['farmer_name']} (read-only)"}
        st.info("Pick a farmer with a login to preview the farmer pages.")
        _stop(tenant)
    return real


def console_view(db, real: dict) -> None:
    """JSA super admin signed in without a tenant link: pick a tenant, or run the platform pages."""
    st.subheader("JSA Offer Portal Management")
    tenants = platform.list_tenants(db, real["user_id"])
    for t in tenants:
        c1, c2 = st.columns([4, 1])
        c1.markdown(f"**{t['name']}** · `{t['tenant_id']}` · {t['users']} users · {t['status']}")
        if c2.button("Open", key=f"open_{t['tenant_id']}"):          # same tab: a link would start a new, signed-out session
            st.query_params["t"] = t["tenant_id"]
            st.rerun()
    platform_view(db, real)
    with st.sidebar:
        st.caption(f"{real['email']} · platform admin")
        if st.button("Sign out"):
            for k in ("user", "tenant_id", "login_step"):
                st.session_state.pop(k, None)
            st.rerun()


def login_view(db, tenant: dict | None) -> None:
    """Password sign-in for everyday use; the emailed code for the first time, a forgotten password, or no password yet."""
    tid = tenant["tenant_id"] if tenant else None
    if auth.passwords_ready(db) and st.session_state.get("login_step", "email") == "email":
        how = st.segmented_control("Sign in with", ["Password", "Email code"], default="Password", key="login_how",
                                   label_visibility="collapsed") or "Password"
        if how == "Password":
            st.caption("Sign in with your email and password. First time, or forgot it? Choose **Email code**.")
            with st.form("password_form"):
                email = st.text_input("Email")
                pw = st.text_input("Password", type="password")
                go = st.form_submit_button("Sign in", type="primary")
            if go:
                u = auth.verify_password(db, email, pw, tid)
                if u:
                    st.session_state.update(user=u, tenant_id=tid)
                    st.rerun()
                st.error("That email and password didn't match, or password sign-in is paused for a few minutes. "
                         "You can always sign in with an email code instead.")
            return
    st.caption("Sign in with your email — we'll send you a one-time code.")
    if not DEV and not auth.graph_configured():
        st.warning("Email isn't set up on this deployment, so sign-in codes can't be sent. "
                   "The four GRAPH_* settings must be in the app's secrets above any [section] heading.")
    step = st.session_state.get("login_step", "email")
    if step == "email":
        with st.form("email_form"):
            email = st.text_input("Email")
            go = st.form_submit_button("Send code", type="primary")
        if go:
            try:
                auth.request_code(db, email, tenant["tenant_id"] if tenant else None, _send_code(tenant))
            except Exception as exc:
                logging.getLogger("offer_portal").exception("sign-in code could not be sent")
                # The exception's type (never its message, which could carry settings) helps support find the cause.
                st.error(f"We couldn't send the code ({type(exc).__name__}). Please try again shortly.")
                return
            st.session_state.update(login_step="code", login_email=email.strip().lower())
            st.rerun()
    else:
        st.info("If that email is registered with this elevator, a code is on its way.")
        if DEV and st.session_state.get("_dev_code"):
            st.warning(f"Dev mode code: {st.session_state['_dev_code']}")
        with st.form("code_form"):
            code = st.text_input("6-digit code")
            go = st.form_submit_button("Sign in", type="primary")
        if go:
            u = auth.verify_code(db, st.session_state["login_email"], code, tenant["tenant_id"] if tenant else None)
            if u:
                st.session_state.update(user=u, tenant_id=tenant["tenant_id"] if tenant else None,
                                        pw_offer=auth.passwords_ready(db))      # just proved the email: offer a password
                st.session_state.pop("login_step", None)
                st.session_state.pop("_dev_code", None)
                st.rerun()
            st.error("That code didn't work. Check it, or request a new one.")
        if st.button("Use a different email"):
            st.session_state.pop("login_step", None)
            st.rerun()


def _md(text: str) -> str:
    """Escape dollar signs for st.markdown: two of them in one string turn the text between into LaTeX math."""
    return str(text).replace("$", "\\$")


def _flush_mail() -> None:
    """Send whatever the action just queued (staff and farmers get the email within seconds). Best effort: a mail
    problem never blocks the page; notify_run.py on the droplet retries anything left over."""
    try:
        db = get_db()
        if db.query("SELECT 1 AS x FROM notifications WHERE status IN ('queued', 'failed') AND attempts < %s LIMIT 1",
                    (notify.MAX_ATTEMPTS,)):
            notify.send_pending(db, limit=10)
        if sms.ready() and notify.sms_ready(db) and db.query(
                "SELECT 1 AS x FROM sms_queue WHERE status IN ('queued', 'failed') AND attempts < %s LIMIT 1",
                (notify.MAX_ATTEMPTS,)):
            notify.send_pending_sms(db, limit=10)
    except Exception:
        pass


def _refresh() -> None:
    """Rerun just the calling fragment; a full rerun when not in a fragment run (e.g. AppTest)."""
    _flush_mail()
    for k in [k for k in st.session_state if str(k).startswith(("_k_pf_", "_k_tenant", "_k_locs_", "_k_sms"))]:
        st.session_state.pop(k, None)                  # people / tenants may have just changed
    try:
        get_db().__dict__.get("_actor_cache", {}).clear()   # so a role change just made takes effect now
    except Exception:
        pass
    try:
        st.rerun(scope="fragment")
    except st.errors.StreamlitAPIException:
        st.rerun()


def _loc_names(db, tenant_id: str) -> dict:
    return _kept(f"_k_locs_{tenant_id}", 60, lambda: {l["location_id"]: l["name"] for l in data.list_locations(db, tenant_id)})


def _repriced_msg(n: int) -> str:
    msg = "Bid posted."
    if n:
        msg += (f" {int(n)} outstanding flat-price offer{'s' if n != 1 else ''} had the implied futures "
                "price recalculated for the new basis.")
    if hit := getattr(n, "auto_hit", 0):
        msg += f" {hit} offer{'s were' if hit != 1 else ' was'} hit automatically."
    return msg


_FLAT_SOURCE = {"futures": "futures + basis (delayed)", "posted": "as posted", "stale": "futures quote is stale"}


def _bid_rows(bids: list[dict], locs: dict, show_futures: bool = False) -> list[dict]:
    """Basis is the posted (sticky) part; the flat bid is futures + basis and moves with the delayed quote."""
    rows = []
    for b in bids:
        ws, we = b.get("window_start"), b.get("window_end")
        window = f"{ws:%b %d} – {we:%b %d, %Y}" if ws and we else ""
        row = {"Location": locs.get(b["location_id"], "—"), "Grain": b["grain"], "Delivery": b["delivery_month"],
               "Delivery window": window,
               "Futures month": b["futures_symbol"] or "", "Basis (¢)": b["basis_cents"],
               "Flat bid ($/bu)": b["flat_price"], "Flat bid from": _FLAT_SOURCE.get(b["flat_source"], ""),
               "Bushel cap": b["bushel_cap"]}
        if show_futures:
            row["Futures, delayed ($/bu)"] = b["quote_price"]
        rows.append(row)
    return rows


def _futures_banner(quotes: dict) -> None:
    st.markdown(theme.delayed_banner_html(futures.latest_stamp(quotes)), unsafe_allow_html=True)
    st.session_state["_futures_shown"] = True


@st.fragment
def published_view(db, tenant_id: str, user: dict, editable: bool, tenant: dict | None = None) -> None:
    if msg := st.session_state.pop("bid_msg", None):
        st.success(msg)
    locs = _loc_names(db, tenant_id)
    bids = data.list_bids(db, tenant_id)
    if not bids:
        st.info("No bids are posted right now.")
    else:
        live = [b for b in bids if b["flat_source"] in ("futures", "stale")]
        if live:                                     # a flat bid is derived from delayed futures: say so, to everyone
            _futures_banner(futures.list_quotes(db, {b["futures_symbol"] for b in live}))
        st.dataframe(_bid_rows(bids, locs, show_futures=_may_see_futures(tenant, user)), hide_index=True,
                     width="stretch")
        st.caption("Basis stays as posted until an elevator administrator updates and confirms it. "
                   "The flat bid is futures plus that basis.")
    if not editable:
        return
    if bids:
        with st.expander("Retire a bid"):
            pick = st.selectbox("Bid", bids, key="retire_pick", format_func=lambda b: (
                f"{locs.get(b['location_id'], '—')} · {b['grain']} · {b['delivery_month']}"))
            if st.button("Retire bid", key="retire_go"):
                data.retire_bid(db, tenant_id, user["user_id"], pick["bid_id"])
                _refresh()
    with st.expander("Post a bid", expanded=not bids):
        with st.form("post_bid", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            loc_id = c1.selectbox("Location", list(locs), format_func=locs.get)
            market = c2.selectbox("Commodity and futures month (what it hedges against)", _markets(),
                                  format_func=_market_label)
            month = c3.selectbox("Delivery month", _months())
            c4, c5, c6, c7 = st.columns(4)
            basis = c5.number_input("Basis (¢, e.g. -38)", value=None, step=1, format="%d")
            cash = c6.number_input("Cash price ($/bu)", value=None, step=0.01, format="%.2f")
            cap = c7.number_input("Bushel cap", value=None, min_value=0, step=1000, format="%d")
            w1, w2 = st.columns(2)
            wf = w1.date_input("Delivery from (blank = the whole month)", value=None)
            wt = w2.date_input("Delivery to (offers expire the day after this)", value=None)
            if st.form_submit_button("Post bid", type="primary"):
                try:
                    grain, fut = market.split("|")
                    n = data.publish_bid(db, tenant_id, user["user_id"], grain=grain, delivery_month=month,
                                         location_id=loc_id, futures_symbol=fut,
                                         basis_cents=basis, cash_price=cash, bushel_cap=cap,
                                         delivery_start=wf.isoformat() if wf else None,
                                         delivery_end=wt.isoformat() if wt else None)
                    st.session_state["bid_msg"] = _repriced_msg(n)
                    _refresh()
                except data.PortalError as e:
                    st.error(str(e))


@st.fragment
def _scrape_now(db, tenant_id: str) -> None:
    """Admin button: pull this elevator's public bid pages now (the droplet also does it at 6:15, 12:15 and 4:15 pm CT, weekdays)."""
    srcs = db.query("SELECT last_run_at, last_status FROM scrape_sources WHERE tenant_id = %s AND active", (tenant_id,))
    if not srcs:
        return
    ran = [auth._as_utc(x["last_run_at"]) for x in srcs if x["last_run_at"]]
    c1, c2 = st.columns([1, 3], vertical_alignment="center")
    if c1.button("Scrape bids now", key="scrape_now", icon=":material/refresh:"):
        with st.spinner("Fetching bids…"):
            out = scrape_run.run(db, tenant_id)
        failed = [v for v in out.values() if isinstance(v, str)]
        st.session_state["bid_msg"] = (f"Scrape finished with a problem: {failed[0]}" if failed else
                                       "Scrape finished. New and changed bids are below for your review.")
        _refresh()
    c2.caption(f"Last scraped: {max(ran).astimezone(data.CT):%a %b %d, %I:%M %p} CT" if ran else "Not scraped yet.")


def suggestions_view(db, tenant_id: str, user: dict) -> None:
    if msg := st.session_state.pop("bid_msg", None):
        st.success(msg)
    st.caption("Basis levels pulled from your public cash-bid page. Nothing reaches farmers until you approve it: "
               "a posted basis is sticky. The flat bid then follows futures (futures + basis).")
    locs = _loc_names(db, tenant_id)
    _scrape_now(db, tenant_id)
    sugg = data.list_suggestions(db, tenant_id)
    if not sugg:
        st.info("Nothing waiting for review.")
        return
    live = {(b["location_id"], b["grain"], b["delivery_month"], b["futures_symbol"]): b
            for b in data.list_bids(db, tenant_id)}
    for s in sugg:
        with st.container(border=True, horizontal=True, vertical_alignment="bottom"):
            posted = live.get((s["location_id"], s["grain"], s["delivery_month"], s["futures_symbol"]))
            note = ""
            if posted and posted["basis_cents"] is not None:
                note = f"  \nPosted now: basis {posted['basis_cents']:+d}¢ (stays until you approve)"
            s_start, s_end = data._to_date(s.get("delivery_start")), data._to_date(s.get("delivery_end"))
            note += (f"  \nDelivery {s_start:%b %d} – {s_end:%b %d, %Y}" if s_start and s_end else
                     "  \n:red[The source gave no clear delivery range. Enter one to approve: offers expire when it ends.]")
            st.markdown(_md(f"**{locs.get(s['location_id'], '—')}** · {s['grain']} · {s['delivery_month']}"
                            f"  \n{s['futures_symbol'] or ''} · source: {s['source']}{note}"))
            basis = st.number_input("Basis (¢)", value=s["basis_cents"], step=1, format="%d",
                                    key=f"sb_{s['suggestion_id']}")
            d_from = st.date_input("Delivery from", value=s_start, key=f"sds_{s['suggestion_id']}")
            d_to = st.date_input("Delivery to", value=s_end, key=f"sde_{s['suggestion_id']}")
            if st.button("Approve", key=f"ok_{s['suggestion_id']}", type="primary"):
                try:
                    n = data.review_suggestion(db, tenant_id, user["user_id"], s["suggestion_id"], True, basis_cents=basis,
                                               delivery_start=d_from.isoformat() if d_from else None,
                                               delivery_end=d_to.isoformat() if d_to else None)
                    st.session_state["bid_msg"] = _repriced_msg(n)
                    _refresh()
                except data.PortalError as e:
                    st.error(str(e))
            if st.button("Discard", key=f"no_{s['suggestion_id']}"):
                data.review_suggestion(db, tenant_id, user["user_id"], s["suggestion_id"], False)
                _refresh()


CONTRACT_LABELS = {"CASH": "Cash (deliver and price)", "HTA": "HTA (hedge-to-arrive)",
                   "FUTURES_ONLY": "Futures only"}
OPEN = "Open price (my own number)"


def _terms(o: dict) -> str:
    px = f"${o['price']:.2f}/bu" if o["price_type"] == "FIXED" else \
        f"{o['basis_cents']:+d}¢ on {o['futures_symbol']}"
    return f"{o['bushels']:,} bu {o['grain']} · {o['contract_type']} · {px}"


def _expiry(d: date | None):
    return f"{d.isoformat()} 23:59:59" if d else None


@st.fragment
def offer_form_view(db, tenant: dict, user: dict, farmer_id: int | None = None) -> None:
    """farmer_id=None: a farmer submits their own offer. Otherwise staff place it for that farmer."""
    tid = tenant["tenant_id"]
    kp = f"f{farmer_id}_" if farmer_id else ""
    locs = _loc_names(db, tid)
    bids = data.list_bids(db, tid)
    by_id = {b["bid_id"]: b for b in bids}
    choices = [None] * (1 if tenant["open_pricing"] else 0) + list(by_id)        # ids, so the box is simple to drive
    if not choices:
        st.info("This elevator isn't taking offers right now.")
        return
    if st.session_state.pop("offer_sent", False):
        st.success("Offer entered. Find it under Offers." if farmer_id else
                   "Offer sent. You can follow it under My offers.")

    def label(i):
        b = by_id.get(i)
        return OPEN if b is None else (f"{locs.get(b['location_id'], '—')} · {b['grain']} · "
                                       f"{b['delivery_month']} · "
                                       + (f"{b['basis_cents']:+d}¢" if b["basis_cents"] is not None
                                          else f"${b['cash_price']:.2f}"))
    sel = st.selectbox("Make an offer against", choices, format_func=label, key=f"{kp}offer_bid")
    bid = by_id.get(sel)
    # Everything below keys on the chosen bid so switching bids resets the defaults.
    k = bid["bid_id"] if bid else "open"
    with st.form(f"{kp}offer_form_{k}", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        if bid:
            grain, fut = bid["grain"], bid["futures_symbol"]
            c1.text_input("Commodity and futures month", f"{grain} · {contracts.futures_label(fut)} ({fut})", disabled=True)
        else:
            grain, fut = c1.selectbox("Commodity and futures month", _markets(), format_func=_market_label).split("|")
        contract = c2.selectbox("Contract type", list(data.CONTRACT_TYPES),
                                format_func=CONTRACT_LABELS.get)
        bushels = c3.number_input("Bushels", min_value=0, step=1000, value=5000, format="%d")
        c4, c5 = st.columns(2)
        ptype = c4.segmented_control("Price type", list(data.PRICE_TYPES), default="BASIS")
        basis = c5.number_input("Basis (¢)", value=bid["basis_cents"] if bid else None, step=1, format="%d")
        price = st.number_input("Fixed price ($/bu) — for fixed-price offers",
                                value=bid["cash_price"] if bid else None, step=0.01, format="%.2f")
        c7, c8, c9 = st.columns(3)
        start = c7.date_input("Delivery from (first day)", value=bid["window_start"] if bid else None,
                              key=f"{kp}dfrom_{k}")
        end = c8.date_input("Delivery to (last day: the offer expires after it)", value=bid["window_end"] if bid else None,
                            key=f"{kp}dto_{k}")
        good = c9.date_input("Offer good until", value=None, min_value=date.today(),
                             help="Leave blank and the offer stays good through the last day of the delivery period "
                                  "(it expires the next day). Pick an earlier date to end it sooner.")
        contract_ref = st.text_input("Contract reference # (optional)", max_chars=60)
        note = st.text_area("Notes" if farmer_id else "Notes to the elevator", max_chars=500)
        if st.form_submit_button("Enter offer" if farmer_id else "Send offer", type="primary"):
            if user.get("_preview") and user["role"] == "farmer":
                st.warning("You are previewing as a farmer: offers can't be sent from here.")
                return
            if bool(start) != bool(end) or (not bid and not (start and end)):
                st.error("Enter the delivery period (first and last day). The offer expires the day after the last day.")
                return
            if start and end and end < start:
                st.error("The delivery period ends before it starts.")
                return
            terms = dict(
                grain=grain, contract_type=contract, price_type=ptype or "BASIS",
                bushels=int(bushels), price=price if ptype == "FIXED" else None,
                basis_cents=basis if ptype != "FIXED" else None,
                futures_symbol=fut, bid_id=bid["bid_id"] if bid else None,
                location_id=bid["location_id"] if bid else None,
                delivery_start=start.isoformat() if start else None,
                delivery_end=end.isoformat() if end else None, note=note or None,
                contract_ref=contract_ref or None,
                expires_at=_expiry(good))
            try:
                if farmer_id:
                    data.place_offer_for_farmer(db, tid, user["user_id"], farmer_id, **terms)
                else:
                    data.create_offer(db, tid, user, **terms)
                st.session_state["offer_sent"] = True
                _refresh()
            except data.PortalError as e:
                st.error(str(e))


def _offer_card(db, tenant_id: str, user: dict, o: dict, staff: bool) -> None:
    with st.container(border=True):
        who = f" · {o['farmer_name'] or o['farmer_email']}" if staff else ""
        if staff and o.get("farmer_client_id"):
            who += f" · #{o['farmer_client_id']}"
        if staff and o.get("farmer_phone"):
            who += f" · {o['farmer_phone']}"
        if o.get("placed_by"):
            who += f" · entered by {o['placed_by_name'] or o['placed_by_email']}"
        line = [data.STATUS_LABELS.get(o["status"], o["status"]).upper()]
        flat = o["price_type"] == "FIXED" and o["contract_type"] != "FUTURES_ONLY"
        if flat and o.get("implied_futures") is not None:
            basis = f" ({o['basis_ref_cents']:+d}¢ basis)" if o.get("basis_ref_cents") is not None else ""
            line.append(f"implied futures {o['implied_futures']:.4f}{basis}")
        elif flat and staff:
            line.append("no basis reference yet")
        if data.bid_gap(o):
            line.append(data.bid_gap(o))
        if staff and o.get("hedged_at"):
            line.append("hedged")
        st.markdown(_md(f"**{_terms(o)}**{who}  \n:grey[{' · '.join(line)}]"))
        if staff and o.get("bid_reached"):
            st.markdown(":green[:material/bolt: The posted bid has reached this offer. Mark it hit when you fill it.]")
        meta = [x for x in ((f"Delivery {o['delivery_start']}" + (f" → {o['delivery_end']}" if o["delivery_end"] else "")) if o["delivery_start"] else "",
                            (f"Good through {data.offer_delivery_end(o):%b %d, %Y}"
                             if o["status"] == "submitted" and data.offer_delivery_end(o) else ""),
                            f"Good until {str(o['expires_at'])[:10]}" if o["expires_at"] else "",
                            (f"Hit automatically {str(o['hit_at'])[:10]}" if o.get("auto_hit") else
                             f"Hit {str(o['hit_at'])[:10]}") if o.get("hit_at") else "",
                            (f"Acknowledged {str(o['acknowledged_at'])[:10]} by "
                             f"{o.get('acknowledged_by_name') or o.get('acknowledged_by_email') or 'staff'}")
                            if o.get("acknowledged_at") else "",
                            (f"Cancelled {str(o['cancelled_at'])[:10]}: {o['cancel_reason']}"
                             + (" · hedge adjusted" if o.get("hedge_adjusted_at") else
                                (" · hedge adjustment pending" if o.get("hedge_adjust_needed") and staff else "")))
                            if o.get("cancelled_at") else "",
                            f"Completed {str(o['completed_at'])[:10]}" if o.get("completed_at") else "",
                            f"Contract # {o['contract_ref']}" if o.get("contract_ref") else "",
                            o["note"] or "") if x]
        if meta:
            st.caption(_md(" · ".join(meta)))
        if staff:
            with st.expander("Edit notes / contract #"):
                with st.form(f"details_{o['offer_id']}", border=False):
                    c1, c2 = st.columns([1, 3])
                    ref = c1.text_input("Contract reference #", o.get("contract_ref") or "", max_chars=60)
                    note = c2.text_area("Notes", o["note"] or "", max_chars=500)
                    if st.form_submit_button("Save"):
                        try:
                            data.update_offer_details(db, tenant_id, user["user_id"], o["offer_id"],
                                                      note=note, contract_ref=ref)
                            _refresh()
                        except data.PortalError as e:
                            st.error(str(e))
        key = str(o["offer_id"])
        if o["status"] == "submitted":
            # The elevator marks an offer hit (or declines it). A farmer can withdraw their own; staff can
            # record a withdrawal for a farmer who called in (flagged on_behalf in the offer history).
            acts = ([("Mark hit", "hit", "primary"), ("Decline", "decline", "secondary"),
                     ("Withdraw for farmer", "withdraw", "secondary")] if staff
                    else [("Withdraw", "withdraw", "secondary")])
            with st.container(horizontal=True):
                for lbl, act, kind in acts:
                    if st.button(lbl, key=f"{act}_{key}", type=kind):
                        if user.get("_preview") and user["role"] == "farmer":
                            st.warning("You are previewing as a farmer: offers can't be changed from here.")
                            continue
                        try:
                            data.act_on_offer(db, tenant_id, user, o["offer_id"], act,
                                              on_behalf=staff and act == "withdraw")
                            _refresh()
                        except data.PortalError as e:
                            st.error(str(e))
        elif o["status"] == "hit" and staff:
            if st.button("Acknowledge", key=f"ack_{key}", type="primary"):
                try:
                    data.acknowledge_offer(db, tenant_id, user["user_id"], o["offer_id"])
                    _refresh()
                except data.PortalError as e:
                    st.error(str(e))
        elif o["status"] == "acknowledged" and staff:
            with st.form(f"complete_{key}", border=False):
                c1, c2 = st.columns([2, 1], vertical_alignment="bottom")
                ref = c1.text_input("Contract number (required to complete)", o.get("contract_ref") or "",
                                    max_chars=60)
                if c2.form_submit_button("Complete and archive", type="primary"):
                    try:
                        data.complete_offer(db, tenant_id, user["user_id"], o["offer_id"], ref)
                        _refresh()
                    except data.PortalError as e:
                        st.error(str(e))
        admin = user["role"] in ("elevator_admin", "platform_admin")
        if staff and (o["status"] in ("hit", "acknowledged") or (o["status"] == "completed" and admin)):
            with st.expander("Cancel this offer"):
                with st.form(f"cancel_{key}", border=False):
                    c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
                    why = c1.text_input("Reason (required). Admins are told to adjust the hedge.", max_chars=200,
                                        key=f"why_{key}")
                    if c2.form_submit_button("Cancel offer"):
                        try:
                            data.cancel_offer(db, tenant_id, user["user_id"], o["offer_id"], why)
                            _refresh()
                        except data.PortalError as e:
                            st.error(str(e))


STAGES = {"Outstanding": ("submitted",), "Hit": ("hit",), "Acknowledged": ("acknowledged",),
          "Completed": ("completed",), "Closed": ("declined", "withdrawn", "expired", "cancelled")}


_CAL_COLORS = {"wasde": ("#fdecd9", "#a45a16"), "nass": ("#e3f1e4", "#2f6b3a"), "weekly": ("#e8edf1", "#4c5a66")}


def seasonals_view() -> None:
    """The Grain Futures Seasonals dashboard (seasonals/, generated from the standalone app by scripts/sync_seasonals.py).
    Imported here, not at the top, so a problem in that package can never take the rest of the portal down."""
    try:
        from seasonals import dashboard
    except Exception as exc:
        st.error(f"The seasonals dashboard could not be loaded ({type(exc).__name__}). The rest of the portal is unaffected.")
        return
    st.session_state["_futures_shown"] = True              # futures prices are on screen: the footer carries the delayed notice
    dashboard.render()


@st.fragment
def calendar_view() -> None:
    """A month calendar of USDA reports (portal/usda_calendar.py), with a filter and the month's list below."""
    today = data.today_ct()
    ym = st.session_state.setdefault("cal_ym", (today.year, today.month))
    c1, c2, c3, c4 = st.columns([1, 1, 1, 5], vertical_alignment="center")
    if c1.button("Previous", key="cal_prev", icon=":material/chevron_left:"):
        st.session_state["cal_ym"] = ym = (ym[0] - 1, 12) if ym[1] == 1 else (ym[0], ym[1] - 1)
    if c2.button("Today", key="cal_today"):
        st.session_state["cal_ym"] = ym = (today.year, today.month)
    if c3.button("Next", key="cal_next", icon=":material/chevron_right:", icon_position="right"):
        st.session_state["cal_ym"] = ym = (ym[0] + 1, 1) if ym[1] == 12 else (ym[0], ym[1] + 1)
    year, month = ym
    c4.subheader(f"{date(year, month, 1):%B %Y}")
    show = st.pills("Show", list(usda_calendar.KIND_LABELS), selection_mode="multi", key="cal_kinds",
                    default=list(usda_calendar.KIND_LABELS), format_func=usda_calendar.KIND_LABELS.get) or []
    css = "".join(f".cal-ev.{k}{{background:{bg};color:{fg};}}" for k, (bg, fg) in _CAL_COLORS.items())
    cells = []
    for wd in ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"):
        cells.append(f'<div class="cal-dow">{wd}</div>')
    for week in usda_calendar.month_grid(year, month):
        for d in week:
            chips = "".join(
                f'<div class="cal-ev {e["kind"]}" title="{html.escape(e["time"] + " CT: " + e["note"])}">'
                f'{html.escape(e["report"])}</div>' for e in d["events"] if e["kind"] in show)
            cls = "cal-day" + ("" if d["in_month"] else " out") + (" today" if d["date"] == today else "")
            cells.append(f'<div class="{cls}"><div class="cal-n">{d["date"].day}</div>{chips}</div>')
    st.markdown(
        f"<style>.cal{{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px;margin:6px 0 12px;}}"
        f".cal-dow{{font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;color:{theme.MUTED};padding:2px 4px;}}"
        f".cal-day{{background:{theme.SURFACE};border:1px solid {theme.BORDER};border-radius:8px;min-height:92px;padding:4px 6px;}}"
        f".cal-day.out{{background:transparent;opacity:.45;}}.cal-day.today{{border:2px solid {theme.JSA_BLUE};}}"
        f".cal-n{{font-size:.78rem;color:{theme.MUTED};text-align:right;}}"
        f".cal-ev{{font-size:.7rem;line-height:1.2;border-radius:4px;padding:2px 5px;margin-top:3px;}}"
        f"@media (max-width:640px){{.cal-day{{min-height:56px;padding:2px;}}.cal-ev{{font-size:.58rem;padding:1px 2px;}}}}"
        f"{css}</style><div class='cal'>{''.join(cells)}</div>", unsafe_allow_html=True)
    st.caption("Times are Central. Hover a report for its time and what it covers. Weekly reports can move a day in "
               "holiday weeks, and only dates USDA has published are shown for the monthly and quarterly reports: "
               "always confirm on usda.gov.")
    rows = [e for week in usda_calendar.month_grid(year, month) for d in week if d["in_month"] for e in d["events"]
            if e["kind"] in show and e["kind"] != "weekly"]
    st.markdown(f"<div class='sec-header'>Monthly and quarterly reports in {date(year, month, 1):%B}</div>",
                unsafe_allow_html=True)
    if rows:
        st.dataframe([{"Date": f"{r['weekday']} {r['date']:%b %d}", "Time (CT)": r["time"], "Report": r["report"],
                       "What it covers": r["note"]} for r in rows], hide_index=True, width="stretch")
    else:
        st.info("No monthly or quarterly report dates are published for this month yet.")


def offers_view(db, tenant_id: str, user: dict, staff: bool) -> None:
    _kept(f"_k_exp_{tenant_id}", 45, lambda: data.expire_stale(db, tenant_id))      # a sweep per 45 s is plenty; the jobs run it too
    rows = data.list_offers(db, tenant_id, user)
    if staff and st.toggle("Only offers I placed", False, key="offers_mine",
                           help="Offers you entered on a farmer's behalf. Off shows the whole company's."):
        rows = [o for o in rows if o["placed_by"] == user["user_id"]]
    counts = {k: sum(1 for o in rows if o["status"] in v) for k, v in STAGES.items()}
    names = {f"{k} ({counts[k]})": k for k in STAGES}          # the counts change, so remember the stage by its name
    remembered = st.session_state.get("offers_stage")
    for label in _tab_pages(list(names), key="offers_tabs", default=next((l for l, k in names.items() if k == remembered), None)):
        st.session_state["offers_stage"] = names[label]
        _offers_stage(db, tenant_id, user, staff, rows, names[label])


def _offers_stage(db, tenant_id: str, user: dict, staff: bool, rows: list[dict], stage: str) -> None:
    shown = [o for o in rows if o["status"] in STAGES[stage]]
    if stage == "Outstanding":
        shown.sort(key=lambda o: not o["bid_reached"])          # offers the bid has reached float to the top
    elif stage == "Hit" and staff:
        st.caption("These offers have been hit. Acknowledge each one to take it on; then enter its contract number "
                   "to complete it.")
    elif stage == "Acknowledged" and staff:
        st.caption("Acknowledged offers. Enter the contract number to complete one and move it to the archive.")
    elif stage == "Completed":
        st.caption("Archive: completed offers, each with its contract number.")
    if not shown:
        st.info("No offers here.")
    for o in shown:
        _offer_card(db, tenant_id, user, o, staff)


ROLE_LABELS = {"farmer": "Farmer", "elevator_staff": "Elevator staff", "elevator_admin": "Elevator admin"}


def _invite_link(tenant_id: str, token: str) -> str:
    base = os.environ.get("OFFER_PORTAL_URL", "").rstrip("/")
    return f"{base}/?t={tenant_id}&invite={token}"


def _email_invite(tenant: dict, email: str, link: str) -> bool:
    if not auth.graph_configured():
        return False
    name = html.escape(tenant["name"])
    try:
        auth.send_via_graph(email, f"You're invited to {tenant['name']}",
                            f"<p>{name} has invited you to its grain offer portal.</p>"
                            f"<p><a href=\"{html.escape(link)}\">Accept your invitation</a> "
                            f"(the link works once and expires in 7 days).</p>")
        return True
    except Exception:
        return False


def join_view(db, tenant: dict, token: str) -> None:
    inv = people.invite_preview(db, token, tenant["tenant_id"])
    if not inv:
        st.error("This invite link is invalid, expired, or already used. Ask your elevator for a new one.")
        return
    st.subheader("Accept your invitation")
    st.caption(f"You're joining as {ROLE_LABELS[inv['role']].lower()} using {inv['email']}.")
    ready = auth.passwords_ready(db)
    with st.form("join_form"):
        name = st.text_input("Your name")
        pw = st.text_input(f"Choose a password (at least {auth.MIN_PASSWORD} characters)", type="password") if ready else None
        again = st.text_input("Password again", type="password") if ready else None
        if st.form_submit_button("Join", type="primary"):
            try:
                if ready:
                    if pw != again:
                        raise data.PortalError("The two passwords don't match.")
                    problem = auth.check_policy(pw or "", inv["email"])
                    if problem:
                        raise data.PortalError(problem)
                u = people.accept_invite(db, token, tenant["tenant_id"], name)
                if ready:
                    auth.set_password(db, u["user_id"], pw)
            except data.PortalError as e:
                st.error(str(e))
                return
            st.session_state.update(user=auth._public(u), tenant_id=tenant["tenant_id"])
            st.query_params.pop("invite", None)
            st.rerun()


@st.fragment
def people_view(db, tenant: dict, user: dict) -> None:
    tid = tenant["tenant_id"]
    admin = user["role"] in ("elevator_admin", "platform_admin")
    if link := st.session_state.pop("new_invite", None):
        st.success(link["msg"])
        st.code(link["url"], language=None)
    st.subheader("Invite someone")
    with st.form("invite_form", clear_on_submit=True):
        c1, c2 = st.columns([3, 1])
        email = c1.text_input("Email")
        role = c2.selectbox("Role", list(ROLE_LABELS) if admin else ["farmer"], format_func=ROLE_LABELS.get)
        if st.form_submit_button("Create invite", type="primary"):
            try:
                url = _invite_link(tid, people.create_invite(db, tid, user["user_id"], email, role))
            except data.PortalError as e:
                st.error(str(e))
            else:
                sent = _email_invite(tenant, email.strip().lower(), url)
                st.session_state["new_invite"] = dict(url=url, msg=(
                    "Invite emailed. You can also share this link yourself:" if sent else
                    "Invite created. Email isn't set up here, so share this link yourself:"))
                _refresh()

    pend = people.pending_invites(db, tid, user["user_id"])
    if pend:
        st.subheader("Pending invites")
        for p in pend:
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown(f"{p['email']} · {ROLE_LABELS[p['role']]} · expires {str(p['expires_at'])[:10]}")
                if st.button("Revoke", key=f"rv_{p['token_hash'][:12]}"):
                    people.revoke_invite(db, tid, user["user_id"], p["token_hash"])
                    _refresh()

    st.subheader("People")
    for u in people.list_users(db, tid, user["user_id"]):
        with st.container(border=True, horizontal=True, vertical_alignment="center"):
            st.markdown(f"**{u['name'] or u['email']}**  \n:grey[{u['email']}"
                        f"{'' if u['active'] else ' · deactivated'}]")
            if admin and u["user_id"] != user["user_id"] and u["role"] != "platform_admin":
                new = st.selectbox("Role", list(ROLE_LABELS), index=list(ROLE_LABELS).index(u["role"]),
                                   format_func=ROLE_LABELS.get, key=f"role_{u['user_id']}",
                                   label_visibility="collapsed")
                if new != u["role"]:
                    people.set_user_role(db, tid, user["user_id"], u["user_id"], new)
                    _refresh()
                if st.button("Reactivate" if not u["active"] else "Deactivate", key=f"act_{u['user_id']}"):
                    people.set_user_active(db, tid, user["user_id"], u["user_id"], not u["active"])
                    _refresh()
            else:
                st.caption(ROLE_LABELS.get(u["role"], u["role"]))


def _tenant_editor(db, user: dict, t: dict) -> None:
    tid, uid = t["tenant_id"], user["user_id"]
    st.caption(f"Link: {_invite_link(tid, '').split('&invite=')[0]}")
    with st.form(f"brand_{tid}"):
        name = st.text_input("Company name", t["name"])
        c1, c2 = st.columns(2)
        primary = c1.color_picker("Heading color", t["primary_color"] or "#32373c")
        accent = c2.color_picker("Button color", t["accent_color"] or "#0693e3")
        logo = st.text_input("Logo URL (https)", t["logo_url"] or "")
        c3, c4 = st.columns(2)
        support = c3.text_input("Support email", t["support_email"] or "")
        from_name = c4.text_input("Email 'from' name", t["from_name"] or "")
        c5, c6 = st.columns(2)
        open_pricing = c5.toggle("Farmers may propose any price", bool(t["open_pricing"]))
        hedging = c6.toggle("Hedging enabled (phase 2)", bool(t["hedging_enabled"]))
        show_fut = st.toggle("Farmers can see delayed futures quotes (staff always can; check market-data licensing)",
                             bool(t.get("show_futures")))
        sms_on = sms_from = None
        if notify.sms_ready(db):
            c7, c8 = st.columns(2)
            sms_on = c7.toggle("Text alerts on for this elevator", bool(t.get("sms_enabled")),
                               help="Also needs JSA's texting set up (SMS_ENABLED and the Twilio settings). People still opt in themselves.")
            sms_from = c8.text_input("This elevator's own texting number (blank = JSA's shared number)",
                                     t.get("sms_from") or "", placeholder="+15551234567")
        if st.form_submit_button("Save settings", type="primary"):
            try:
                platform.update_tenant(db, uid, tid, name=name, logo_url=logo, primary_color=primary,
                                       accent_color=accent, support_email=support, from_name=from_name,
                                       open_pricing=open_pricing, hedging_enabled=hedging,
                                       show_futures=show_fut, sms_enabled=sms_on, sms_from=sms_from)
                st.success("Saved.")
            except data.PortalError as e:
                st.error(str(e))

    st.markdown("**Locations**")
    locs = data.list_locations(db, tid)
    st.write(", ".join(f"{l['name']} ({l['state'] or '—'})" for l in locs) or "None yet.")
    with st.form(f"loc_{tid}", clear_on_submit=True, border=False):
        c1, c2, c3 = st.columns([3, 1, 1], vertical_alignment="bottom")
        n, s = c1.text_input("New location"), c2.text_input("State", max_chars=2)
        if c3.form_submit_button("Add"):
            try:
                platform.add_location(db, uid, tid, n, s)
                _refresh()
            except data.PortalError as e:
                st.error(str(e))

    st.markdown("**Bid scrape sources**")
    for src in platform.list_sources(db, uid, tid):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown(f"{src['kind']} · {src['url']}  \n:grey[{src['location_name'] or 'all locations'} · "
                        f"last run: {src['last_status'] or 'never'}]")
            on = st.toggle("Active", bool(src["active"]), key=f"src_{src['source_id']}")
            if on != bool(src["active"]):
                platform.set_source_active(db, uid, tid, src["source_id"], on)
                _refresh()
    with st.form(f"srcadd_{tid}", clear_on_submit=True, border=False):
        c1, c2, c3 = st.columns([1, 3, 2], vertical_alignment="bottom")
        kind = c1.selectbox("Type", platform.SOURCE_KINDS)
        url = c2.text_input("Site or bids page (https)")
        loc = c3.selectbox("Location (Bushel only)", [""] + [l["name"] for l in locs])
        if st.form_submit_button("Add source"):
            try:
                platform.add_source(db, uid, tid, kind, url, loc)
                _refresh()
            except data.PortalError as e:
                st.error(str(e))
    if st.button("Run scrapers now", key=f"run_{tid}"):
        with st.spinner("Fetching bids…"):
            out = scrape_run.run(db, tid)
        st.info("\n\n".join(f"{k[1]}: {v}" for k, v in out.items()) or "No active sources.")

    st.markdown("**Status**")
    suspended = t["status"] != "active"
    if st.button("Reactivate tenant" if suspended else "Suspend tenant", key=f"susp_{tid}"):
        platform.set_tenant_status(db, uid, tid, suspended)
        _refresh()


@st.fragment
def platform_view(db, user: dict) -> None:
    uid = user["user_id"]
    for section in _tab_pages(["Tenants", "Usage Report", "JSA Admins"], key="platform_tabs"):
        _platform_section(db, user, section)


def _platform_section(db, user: dict, section: str) -> None:
    uid = user["user_id"]
    if section == "JSA Admins":
        st.caption("JSA super admins can open every tenant and preview each role. They sign in with an emailed code.")
        for a in platform.list_platform_admins(db, uid):
            c1, c2 = st.columns([4, 1])
            c1.markdown(f"{a['name'] or '—'} · {a['email']}" + ("" if a["active"] else " · *turned off*"))
            if a["user_id"] != uid and c2.button("Turn off" if a["active"] else "Turn on", key=f"pa_{a['user_id']}"):
                platform.set_platform_admin_active(db, uid, a["user_id"], not a["active"])
                _refresh()
        with st.form("add_jsa_admin", clear_on_submit=True):
            c1, c2 = st.columns(2)
            nm, em = c1.text_input("Name"), c2.text_input("Email (@jpsi.com)")
            if st.form_submit_button("Add JSA admin", type="primary"):
                try:
                    platform.add_platform_admin(db, uid, em, nm)
                except data.PortalError as e:
                    st.error(str(e))
                else:
                    st.success(f"{em.strip().lower()} can now sign in.")
        return
    if section == "Usage Report":
        month = st.text_input("Month (YYYY-MM)", data.today_ct().strftime("%Y-%m"))
        try:
            rows = platform.usage_table(db, uid, month)
        except data.PortalError as e:
            st.error(str(e))
            return
        st.dataframe(rows, hide_index=True, width="stretch", column_config={
            "offer_submitted": "Offers submitted", "offer_hit": "Offers hit", "offer_completed": "Offers completed",
            "bid_published": "Bids published", "user_joined": "Users joined",
            "active_farmers": "Active farmers"})
        csv = "Tenant,Offers submitted,Offers hit,Offers completed,Bids published,Users joined,Active farmers\n" + "\n".join(
            f"\"{r['Tenant']}\",{r['offer_submitted']},{r['offer_hit']},{r['offer_completed']},{r['bid_published']},"
            f"{r['user_joined']},{r['active_farmers']}" for r in rows)
        st.download_button("Download CSV", csv, file_name=f"usage-{month}.csv", mime="text/csv")
        return

    tenants = platform.list_tenants(db, uid)
    with st.expander("Create a tenant", expanded=not tenants):
        with st.form("new_tenant", clear_on_submit=True):
            c1, c2 = st.columns(2)
            name = c1.text_input("Company name")
            slug = c2.text_input("Slug (the ?t= in their link)", placeholder="rcm")
            admin_email = st.text_input("First elevator admin's email")
            locs = st.text_area("Locations — one per line, as Name, ST", placeholder="Riverton, IL")
            if st.form_submit_button("Create tenant", type="primary"):
                parsed = [tuple(p.strip() for p in (ln.rsplit(",", 1) + [""])[:2]) if "," in ln else (ln.strip(), "")
                          for ln in locs.splitlines() if ln.strip()]
                try:
                    tok = platform.create_tenant(db, uid, slug, name, admin_email, parsed)
                except data.PortalError as e:
                    st.error(str(e))
                else:
                    url = _invite_link(slug.strip().lower(), tok)
                    st.session_state["new_invite"] = dict(url=url, msg="Tenant created. Send this link to their first admin:")
                    _refresh()
    if link := st.session_state.pop("new_invite", None):
        st.success(link["msg"])
        st.code(link["url"], language=None)
    if not tenants:
        return
    pick = st.selectbox("Tenant", tenants, format_func=lambda t: f"{t['name']} ({t['tenant_id']})"
                        + ("" if t["status"] == "active" else " — suspended"), key="plat_pick")
    _tenant_editor(db, user, pick)


def _farmer_label(f: dict) -> str:
    return (f["name"] + (f" · #{f['client_id']}" if f["client_id"] else "")
            + (f" · {f['phone']}" if f["phone"] else "") + ("" if f["active"] else " (inactive)"))


def _farmer_fields(f: dict | None = None) -> dict:
    """The farmer inputs (call inside a form). Returns kwargs for farmers.create/update_farmer."""
    f = f or {}
    c1, c2 = st.columns(2)
    client_id = c1.text_input("Client ID (your account number)", f.get("client_id") or "")
    entity = c2.text_input("Entity name", f.get("entity_name") or "")
    c3, c4, c5 = st.columns(3)
    individual = c3.text_input("Individual name", f.get("individual_name") or "")
    phone = c4.text_input("Phone", f.get("phone") or "")
    email = c5.text_input("Email (optional)", f.get("email") or "")
    return dict(individual_name=individual, phone=phone, email=email, entity_name=entity, client_id=client_id)


@st.fragment
def place_offer_view(db, tenant: dict, user: dict) -> None:
    """Staff enter an offer for a farmer who called or stopped by."""
    tid = tenant["tenant_id"]
    st.caption("Enter an offer on a farmer's behalf. It goes to your Offers inbox like any other offer.")
    flist = farmers.list_farmers(db, tid, user["user_id"])
    NEW = {"farmer_id": None}
    if (pending := st.session_state.pop("place_farmer_new", None)) is not None:    # just added: preselect them
        st.session_state["place_farmer"] = next((f for f in flist if f["farmer_id"] == pending), NEW)
    pick = st.selectbox("Farmer", [NEW] + flist, key="place_farmer",
                        format_func=lambda f: "+ New farmer…" if f is NEW else _farmer_label(f))
    if pick is NEW:
        with st.form("quick_farmer", clear_on_submit=True):
            fields = _farmer_fields()
            if st.form_submit_button("Add farmer"):
                try:
                    st.session_state["place_farmer_new"] = farmers.create_farmer(
                        db, tid, user["user_id"], **fields)
                except data.PortalError as e:
                    st.error(str(e))
                else:
                    _refresh()
        return
    offer_form_view(db, tenant, user, farmer_id=pick["farmer_id"])


@st.fragment
def farmers_view(db, tenant: dict, user: dict) -> None:
    tid = tenant["tenant_id"]
    if link := st.session_state.pop("new_invite", None):
        st.success(link["msg"])
        st.code(link["url"], language=None)
    with st.expander("Add a farmer"):
        with st.form("add_farmer", clear_on_submit=True):
            fields = _farmer_fields()
            if st.form_submit_button("Add farmer", type="primary"):
                try:
                    farmers.create_farmer(db, tid, user["user_id"], **fields)
                    _refresh()
                except data.PortalError as e:
                    st.error(str(e))
    rows = farmers.list_farmers(db, tid, user["user_id"], include_inactive=True)
    if not rows:
        st.info("No farmers yet. Add one, or place an offer and add them there.")
    for f in rows:
        with st.container(border=True):
            top = st.container(horizontal=True, vertical_alignment="center")
            top.markdown(f"**{f['name']}**  \n:grey[{'#' + f['client_id'] if f['client_id'] else 'no client ID'} · "
                         f"{f['phone'] or 'no phone'} · {f['email'] or 'no email'} · "
                         f"{'portal login' if f['user_id'] else 'no login'} · {f['open_offers']} open offers"
                         f"{'' if f['active'] else ' · inactive'}]")
            with top:
                if not f["user_id"] and f["active"] and f["email"]:
                    if st.button("Invite to portal", key=f"finv_{f['farmer_id']}"):
                        try:
                            tok = people.create_invite(db, tid, user["user_id"], f["email"], "farmer",
                                                       farmer_id=f["farmer_id"])
                        except data.PortalError as e:
                            st.error(str(e))
                        else:
                            url = _invite_link(tid, tok)
                            sent = _email_invite(tenant, f["email"], url)
                            st.session_state["new_invite"] = dict(url=url, msg=(
                                "Invite emailed. You can also share this link yourself:" if sent else
                                "Invite created. Email isn't set up here, so share this link yourself:"))
                            _refresh()
                if st.button("Reactivate" if not f["active"] else "Deactivate", key=f"fact_{f['farmer_id']}"):
                    farmers.set_farmer_active(db, tid, user["user_id"], f["farmer_id"], not f["active"])
                    _refresh()
            with st.expander("Edit"):
                with st.form(f"edit_farmer_{f['farmer_id']}", border=False):
                    fields = _farmer_fields(f)
                    mail_on = st.toggle("Email this farmer about their offers (uses the email above)",
                                        f.get("notify_email") in (None, 1, True), key=f"fnotify_{f['farmer_id']}",
                                        disabled=bool(f["user_id"]),
                                        help="A farmer with a portal login manages this themselves, in the sidebar."
                                        if f["user_id"] else None)
                    if st.form_submit_button("Save"):
                        try:
                            farmers.update_farmer(db, tid, user["user_id"], f["farmer_id"], **fields)
                            if not f["user_id"]:
                                notify.set_farmer_notify(db, tid, user["user_id"], f["farmer_id"], mail_on)
                            _refresh()
                        except data.PortalError as e:
                            st.error(str(e))


@st.fragment
def summary_view(db, tenant_id: str, user: dict) -> None:
    """Offers by commodity and delivery period, with filters."""
    all_rows = data.list_offers(db, tenant_id, user)
    c1, c2, c3, c4 = st.columns(4)
    grains = c1.multiselect("Commodity", sorted({o["grain"] for o in all_rows}))
    periods = c2.multiselect("Delivery period", sorted({o["delivery_period"] for o in all_rows}))
    entity = c3.text_input("Entity name contains")
    indiv = c4.text_input("Individual name contains")
    stages = st.multiselect("Stage", list(STAGES), default=["Outstanding", "Hit"])
    statuses = tuple(x for k in stages for x in STAGES[k])
    res = data.offer_summary(db, tenant_id, user["user_id"], grains=grains, periods=periods, entity=entity,
                             individual=indiv, statuses=statuses)
    if not res["offers"]:
        st.info("No offers match these filters.")
        return
    m1, m2 = st.columns(2)
    m1.metric("Offers", len(res["offers"]))
    m2.metric("Bushels", f"{sum(o['bushels'] for o in res['offers']):,}")
    st.subheader("By commodity and delivery period")
    st.dataframe([{"Commodity": g["grain"], "Delivery period": g["period"], "Offers": g["offers"],
                   "Bushels": g["bushels"], "Avg flat price": g["avg_flat_price"],
                   "Avg implied futures": g["avg_implied_futures"], "Avg basis (¢)": g["avg_basis_cents"]}
                  for g in res["groups"]], hide_index=True, width="stretch",
                 column_config={"Bushels": st.column_config.NumberColumn(format="%d"),
                                "Avg flat price": st.column_config.NumberColumn(format="$%.4f"),
                                "Avg implied futures": st.column_config.NumberColumn(format="%.4f")})
    st.subheader("Offers")
    st.dataframe([{"Commodity": o["grain"], "Delivery period": o["delivery_period"],
                   "Entity": o["farmer_entity"] or "", "Individual": o["farmer_individual"] or "",
                   "Client ID": o["farmer_client_id"] or "", "Type": o["contract_type"],
                   "Flat price": o["price"] if o["price_type"] == "FIXED" else None,
                   "Basis (¢)": o["basis_cents"] if o["price_type"] == "BASIS" else None,
                   "Implied futures": o["implied_futures"], "Futures month": o["futures_symbol"],
                   "Bushels": o["bushels"], "Stage": data.STATUS_LABELS.get(o["status"], o["status"]),
                   "Contract #": o["contract_ref"] or ""} for o in res["offers"]],
                 hide_index=True, width="stretch",
                 column_config={"Bushels": st.column_config.NumberColumn(format="%d"),
                                "Flat price": st.column_config.NumberColumn(format="$%.4f"),
                                "Implied futures": st.column_config.NumberColumn(format="%.4f")})


def _level_label(b: dict) -> str:
    if b["level"] is None:
        return "no basis reference"
    return f"{int(b['level']):+d}¢ basis" if b["kind"] == "Basis (HTA)" else f"{b['level']:.4f}"


def _hedge_tables(totals: list[dict], lines: list[dict], quotes: dict | None = None) -> None:
    st.markdown("**Contracts by futures month**")
    st.dataframe([{"Commodity": t["grain"], "Futures month": f"{t['futures_month']} ({t['futures_symbol']})",
                   "Exchange": t["exchange"], "Offers": t["offers"],
                   "Bushels": t["bushels"], "Contracts": t["contracts"], "Whole contracts": t["whole_contracts"],
                   "Left over (bu)": t["remainder_bushels"],
                   **({"Futures, delayed ($/bu)": (quotes.get(t["futures_symbol"]) or {}).get("price")}
                      if quotes else {})} for t in totals],
                 hide_index=True, width="stretch",
                 column_config={"Bushels": st.column_config.NumberColumn(format="%d")})
    st.markdown("**By price level**")
    st.dataframe([{"Commodity": b["grain"], "Futures month": f"{b['futures_month']} ({b['futures_symbol']})", "Type": b["kind"],
                   "Price level": _level_label(b), "Offers": b["offers"], "Bushels": b["bushels"],
                   "Contracts": b["contracts"]} for b in lines],
                 hide_index=True, width="stretch",
                 column_config={"Bushels": st.column_config.NumberColumn(format="%d")})


@st.fragment
def hedge_view(db, tenant_id: str, user: dict) -> None:
    """Admin only: how many contracts to hedge, at what prices."""
    if user["role"] not in ("elevator_admin", "platform_admin"):
        st.error("This page is for elevator admins.")
        return
    h = data.hedge_summary(db, tenant_id, user["user_id"])
    quotes = futures.list_quotes(db, {t["futures_symbol"] for t in h["now_totals"] + h["potential_totals"]}) or None
    if quotes:
        _futures_banner(quotes)
    if h["adjustments"]:
        st.subheader("Cancelled offers: adjust your hedges")
        st.warning(f"{len(h['adjustments'])} offer{'s were' if len(h['adjustments']) != 1 else ' was'} cancelled after a hit. "
                   "Adjust the hedge, then confirm it here.")
        for o in h["adjustments"]:
            with st.container(border=True, horizontal=True, vertical_alignment="center"):
                lvl = (f"basis {o['basis_cents']:+d}¢" if o["price_type"] == "BASIS" else
                       (f"implied futures {o['implied_futures']:.4f}" if o["implied_futures"] is not None else "flat price"))
                st.markdown(_md(f"**{o['farmer_name']}** · {o['bushels']:,} bu {o['grain']} · {o['futures_symbol']} · "
                                f"{o['contracts']} contracts · {lvl}  \n:grey[Cancelled {str(o['cancelled_at'])[:10]} by "
                                f"{o.get('cancelled_by_name') or o.get('cancelled_by_email') or 'staff'}: {o['cancel_reason']}"
                                f" · {'was hedged: unwind it' if o['was_hedged'] else 'no hedge had been placed'}]"))
                if st.button("Hedge adjusted", key=f"hadj_{o['offer_id']}"):
                    data.mark_hedge_adjusted(db, tenant_id, user["user_id"], [o["offer_id"]])
                    _refresh()
    st.subheader("Hit offers: hedge now")
    if not h["now"]:
        st.info("No hit offers are waiting to be hedged.")
    else:
        _hedge_tables(h["now_totals"], h["now"], quotes)
        with st.expander("Mark offers as hedged"):
            picks = st.multiselect("Hit offers not yet hedged", h["now_offers"], key="hedge_pick",
                                   format_func=lambda o: f"{o['farmer_name']} · {o['bushels']:,} bu {o['grain']} "
                                                         f"{o['futures_symbol']} · {o['price_type'].lower()} offer")
            if st.button("Mark selected as hedged", key="hedge_go", disabled=not picks):
                data.mark_hedged(db, tenant_id, user["user_id"], [o["offer_id"] for o in picks])
                _refresh()
    st.subheader("Outstanding offers: if they are hit")
    if not h["potential"]:
        st.info("No outstanding offers.")
    else:
        _hedge_tables(h["potential_totals"], h["potential"], quotes)
    st.caption("Contracts = bushels divided by the contract size (5,000 bu for corn, soybeans and wheat). "
               "Flat-price offers show the implied futures price (flat price less the posted basis), and it moves "
               "when the basis does. Basis (HTA) offers have no futures price yet, so they show their basis.")


def _rule_text(r: dict, locs: dict) -> str:
    scope = [locs.get(r["location_id"], "unknown location") if r["location_id"] is not None else "any location",
             r["grain"] or "any commodity", r["contract_type"] or "cash and HTA"]
    return " · ".join(scope) + (f" · up to {r['max_bushels']:,} bu per offer" if r["max_bushels"] else " · any size")


@st.fragment
def auto_hit_view(db, tenant_id: str, user: dict) -> None:
    """Admin only: rules that mark an outstanding offer hit when the posted bid reaches it."""
    if user["role"] not in ("elevator_admin", "platform_admin"):
        st.error("This page is for elevator admins.")
        return
    uid = user["user_id"]
    if msg := st.session_state.pop("auto_hit_msg", None):
        st.success(msg)
    st.caption("When the posted bid reaches an outstanding offer, it is marked hit automatically if an active rule "
               "covers it. With no active rules nothing is ever hit automatically. A hit offer is executed on the "
               "farmer's own terms; the bid that triggered it is saved in the offer's history. Offers outside every "
               "rule (for example, larger than a rule's size limit) stay outstanding with a \"bid has reached\" flag "
               "for staff. Flat-price offers need the bid to carry a cash price.")
    locs = _loc_names(db, tenant_id)
    rules = data.list_auto_hit_rules(db, tenant_id, uid)
    st.subheader("Rules")
    if not rules:
        st.info("No rules yet. Offers are only hit when staff mark them.")
    for r in rules:
        with st.container(border=True, horizontal=True, vertical_alignment="center"):
            st.markdown(_rule_text(r, locs))
            on = st.toggle("Active", bool(r["active"]), key=f"ahr_{r['rule_id']}")
            if on != bool(r["active"]):
                n = data.set_auto_hit_rule_active(db, tenant_id, uid, r["rule_id"], on)
                st.session_state["auto_hit_msg"] = (f"Rule switched on. {n} outstanding offer{'s' if n != 1 else ''} "
                                                    "hit." if on else "Rule switched off.")
                _refresh()
    with st.expander("Add a rule", expanded=not rules):
        with st.form("auto_hit_rule", clear_on_submit=True):
            c1, c2, c3, c4 = st.columns(4)
            loc = c1.selectbox("Location", [None] + list(locs), format_func=lambda x: "Any location" if x is None else locs[x])
            grain = c2.selectbox("Commodity", [None] + list(GRAINS), format_func=lambda x: x or "Any commodity")
            ctype = c3.selectbox("Contract type", [None, "CASH", "HTA"], format_func=lambda x: x or "Cash and HTA")
            cap = c4.number_input("Largest offer (bu), blank = no limit", value=None, min_value=0, step=1000, format="%d")
            if st.form_submit_button("Add rule", type="primary"):
                try:
                    n = data.add_auto_hit_rule(db, tenant_id, uid, location_id=loc, grain=grain, contract_type=ctype,
                                               max_bushels=int(cap) if cap else None)
                    st.session_state["auto_hit_msg"] = (f"Rule added. {n} outstanding offer{'s' if n != 1 else ''} "
                                                        "already reached by the bid were hit.")
                    _refresh()
                except data.PortalError as e:
                    st.error(str(e))
    if st.button("Check outstanding offers now", key="auto_hit_now"):
        n = data.check_auto_hit_now(db, tenant_id, uid)
        st.session_state["auto_hit_msg"] = f"Checked. {n} offer{'s' if n != 1 else ''} hit."
        _refresh()
    st.subheader("Recently hit automatically")
    recent = data.list_auto_hits(db, tenant_id, uid)
    if not recent:
        st.info("Nothing has been hit automatically yet.")
    else:
        st.dataframe([{"Hit": str(o["hit_at"])[:16], "Farmer": o["farmer_name"], "Client ID": o["farmer_client_id"] or "",
                       "Commodity": o["grain"], "Futures month": o["futures_symbol"], "Bushels": o["bushels"],
                       "Price": o["price"] if o["price_type"] == "FIXED" else None,
                       "Basis (¢)": o["basis_cents"] if o["price_type"] == "BASIS" else None,
                       "Stage": data.STATUS_LABELS.get(o["status"], o["status"]), "Rule": o["auto_hit_rule_id"]}
                      for o in recent], hide_index=True, width="stretch",
                     column_config={"Bushels": st.column_config.NumberColumn(format="%d"),
                                    "Price": st.column_config.NumberColumn(format="$%.4f")})


@st.fragment
def purchases_view(db, tenant: dict, user: dict) -> None:
    """Staff log what they bought; the summary by commodity feeds the end-of-day recap."""
    tid = tenant["tenant_id"]
    admin = user["role"] in ("elevator_admin", "platform_admin")
    if msg := st.session_state.pop("buy_msg", None):
        st.success(msg)
    day = st.date_input("Day", value=data.today_ct(), max_value=data.today_ct(), key="buy_day")
    locs = _loc_names(db, tid)
    flist = farmers.list_farmers(db, tid, user["user_id"])
    with st.expander("Log a purchase", expanded=True):
        # Pick where and when first: the approved bids for that location and period fill in the market and basis.
        # (Each picker's key carries what it depends on, so a new choice upstream resets the ones below it.)
        bids = data.list_bids(db, tid)
        p1, p2, p3 = st.columns(3)
        loc = p1.selectbox("Delivery location", [None] + list(locs), key="buy_loc", index=1 if locs else 0,
                           format_func=lambda x: "—" if x is None else locs[x])
        at_loc = [b for b in bids if loc is None or b["location_id"] == loc]
        periods = sorted({b["delivery_month"] for b in at_loc})
        period = p2.selectbox("Delivery period", periods or _months(), key=f"buy_period_{loc}")
        match = [b for b in at_loc if b["delivery_month"] == period]
        bid = None
        if match:
            by_id = {b["bid_id"]: b for b in match}
            bid = by_id[p3.selectbox("Commodity and futures month", list(by_id), key=f"buy_bid_{loc}_{period}",
                                     format_func=lambda i: f"{by_id[i]['grain']} · {contracts.futures_label(by_id[i]['futures_symbol'])} "
                                                           f"({by_id[i]['futures_symbol']})")]
            grain, fut = bid["grain"], bid["futures_symbol"]
            posted = [f"basis {bid['basis_cents']:+d}¢" if bid["basis_cents"] is not None else None,
                      f"flat ${bid['flat_price']:.4f}" if bid.get("flat_price") is not None else None]
            st.caption("Posted bid (approved): " + ", ".join(x for x in posted if x) + ". The basis below is filled in "
                       "from it; change it if the price you paid was different.")
        else:
            grain, fut = p3.selectbox("Commodity and futures month", _markets(), key=f"buy_mkt_{loc}_{period}",
                                      format_func=_market_label).split("|")
            st.caption("No approved bid is posted for this location and period, so enter the basis by hand.")
        with st.form("log_purchase", clear_on_submit=True):
            c1, c2 = st.columns(2)
            fpick = c1.selectbox("Bought from (farmer record)", [None] + flist,
                                 format_func=lambda f: "Type a name instead…" if f is None else _farmer_label(f))
            typed = c2.text_input("…or type the entity or person")
            c4, c5, c6, c10 = st.columns(4)
            bushels = c4.number_input("Bushels", min_value=0, step=1000, value=None, format="%d")
            price = c5.number_input("Flat price ($/bu)", value=None, step=0.01, format="%.4f")
            basis = c6.number_input("Basis (¢)", value=bid["basis_cents"] if bid else None, step=1, format="%d",
                                    key=f"buy_basis_{bid['bid_id'] if bid else 'manual'}")
            ref = c10.text_input("Contract #")
            note = st.text_input("Note")
            if st.form_submit_button("Log purchase", type="primary"):
                try:
                    purchases.add_purchase(
                        db, tid, user["user_id"], grain=grain, bushels=int(bushels or 0), counterparty=typed or None,
                        farmer_id=fpick["farmer_id"] if fpick else None, price=price,
                        basis_cents=int(basis) if basis is not None else None, futures_symbol=fut or None,
                        location_id=loc, delivery_period=period or None, contract_ref=ref or None, note=note or None,
                        purchase_date=day)
                    st.session_state["buy_msg"] = "Purchase logged."
                    _refresh()
                except data.PortalError as e:
                    st.error(str(e))
    staff_id = None
    if admin:
        crew = [u for u in people.list_users(db, tid, user["user_id"]) if u["role"] in data.ELEVATOR_ROLES and u["active"]]
        pick = st.selectbox("Staff", [None] + crew, key="buy_staff",
                            format_func=lambda u: "Everyone" if u is None else (u["name"] or u["email"]))
        staff_id = pick["user_id"] if pick else None
    rows = purchases.list_purchases(db, tid, user, day, staff_id=staff_id)
    groups = purchases.summarize(rows)
    st.markdown(f"<div class='sec-header'>Purchases for {day:%A, %B %d}"
                f"{'' if admin else ' (yours)'}</div>", unsafe_allow_html=True)
    if not groups:
        st.info("No purchases logged for this day.")
    for g in groups:
        bits = [f"{g['bushels']:,} bu", f"{g['entries']} purchase{'s' if g['entries'] != 1 else ''}"]
        if g["avg_price"] is not None:
            bits.append(f"avg ${g['avg_price']:.4f}")
        if g["avg_basis_cents"] is not None:
            bits.append(f"avg basis {g['avg_basis_cents']:+.1f}¢")
        st.markdown(_md(f"**{g['grain']}** · " + " · ".join(bits)))
        st.dataframe([{"Bought from": p["counterparty"], "Bushels": p["bushels"], "Price ($/bu)": p["price"],
                       "Basis (¢)": p["basis_cents"], "Futures month": p["futures_symbol"] or "",
                       "Delivery location": p["delivery_location"] or "", "Delivery period": p["delivery_period"] or "",
                       "Contract #": p["contract_ref"] or "",
                       **({"Logged by": p.get("staff_name") or p.get("staff_email") or ""} if admin else {})}
                      for p in g["rows"]], hide_index=True, width="stretch",
                     column_config={"Bushels": st.column_config.NumberColumn(format="%d"),
                                    "Price ($/bu)": st.column_config.NumberColumn(format="$%.4f")})
    if rows:
        with st.expander("Void an entry"):
            pick = st.selectbox("Entry", rows, key="void_pick", format_func=lambda p: (
                f"{p['counterparty']} · {p['bushels']:,} bu {p['grain']}" + (f" · {p['staff_name']}" if admin else "")))
            if st.button("Void this purchase", key="void_go"):
                try:
                    purchases.void_purchase(db, tid, user, pick["purchase_id"])
                    _refresh()
                except data.PortalError as e:
                    st.error(str(e))


@st.fragment
def recap_view(db, tenant: dict, user: dict) -> None:
    """A preview of the end-of-day email (staff: their own; admins: the whole company) and a way to send it now."""
    tid = tenant["tenant_id"]
    admin = user["role"] in ("elevator_admin", "platform_admin")
    if msg := st.session_state.pop("recap_msg", None):
        st.success(msg)
    day = st.date_input("Recap for", value=data.today_ct(), max_value=data.today_ct(), key="recap_day")
    r = recap.build_recap(db, tid, user, day)
    c = r["counts"]
    st.caption(f"{c['hit_today']} hit today · {c['working']} working · {c['purchases']} purchase"
               f"{'s' if c['purchases'] != 1 else ''} · {c['need_ack']} to acknowledge · {c['need_contract']} need a contract #")
    b1, b2 = st.columns(2)
    if b1.button("Email me this recap", key="recap_me"):
        queued = notify.queue(db, tid, "daily_recap", dict(user_id=user["user_id"], email=user["email"], name=user.get("name")),
                              r["subject"], r["html"])
        st.session_state["recap_msg"] = "Sent to your email." if queued else "Couldn't queue an email for your address."
        _refresh()
    if admin and b2.button("Send everyone's recap now", key="recap_all"):
        out = recap.send_daily_recaps(db, day, tid)
        st.session_state["recap_msg"] = (f"Queued {out['queued']} recap email{'s' if out['queued'] != 1 else ''}."
                                         if out["queued"] else "Everyone has already been sent today's recap.")
        _refresh()
    st.html(r["html"])


def main() -> None:
    try:
        db = get_db()
    except Exception:
        _brand(None)
        st.error("The portal can't reach its database. Please contact JSA support.")
        _stop()
    slug = (st.query_params.get("t") or "").strip().lower()
    tenant = _kept(f"_k_tenant_{slug}", 30, lambda: data.get_tenant(db, slug)) if slug else None
    real = st.session_state.get("user")
    jsa = bool(real) and real["role"] == "platform_admin"
    if not tenant:
        _brand(None)
        if jsa:
            console_view(db, real)
            _stop()
        st.info("Please use the link your elevator gave you.")
        with st.expander("JSA staff sign in"):
            login_view(db, None)
        _stop()
    _brand(tenant)

    user = real
    if not user or (st.session_state.get("tenant_id") != tenant["tenant_id"] and not jsa):
        st.session_state.pop("user", None)
        if st.query_params.get("invite"):
            join_view(db, tenant, st.query_params["invite"])
        else:
            login_view(db, tenant)
        _stop(tenant)

    if st.session_state.get("pw_offer") and not user.get("_preview"):
        set_password_view(db, user)
        _stop(tenant)
    if jsa:
        user = jsa_sidebar(db, real, tenant)
    with st.sidebar:
        st.caption(f"{user['email']} · {user['role'].replace('_', ' ')}")
        if user["role"] != "platform_admin" and not user.get("_preview"):
            pref = f"_notify_{user['user_id']}"                   # read once per session, then it is the toggle's own state
            if pref not in st.session_state:
                row = db.query("SELECT notify_email FROM users WHERE user_id = %s", (user["user_id"],))
                st.session_state[pref] = (not row) or row[0]["notify_email"] in (None, 1, True)
            was = st.session_state[pref]
            now = st.toggle("Email me notifications", was, key="notify_pref",
                            help="Offer updates, and the end-of-day recap for staff and admins.")
            if now != was:
                notify.set_notify(db, user["user_id"], now)
                st.session_state[pref] = now
        if user["role"] != "platform_admin" and not user.get("_preview"):
            sms_settings(db, user, tenant)
        if auth.passwords_ready(db) and not user.get("_preview"):
            with st.expander("Password"):
                if password_form(db, real or user, "change_pw", current_needed=auth.has_password(db, user["user_id"])):
                    st.success("Password saved.")
        if st.button("Sign out"):
            for k in ("user", "tenant_id", "login_step", "pw_offer"):
                st.session_state.pop(k, None)
            st.rerun()

    staff = user["role"] in data.ELEVATOR_ROLES or user["role"] == "platform_admin"
    tid = tenant["tenant_id"]
    admin = user["role"] in ("elevator_admin", "platform_admin")
    seasonals = ["Grain Futures Seasonals"] if _may_see_futures(tenant, user) else []
    views = (["Offers", "Place Offer", "Summary"] + (["Hedge", "Auto-Hit"] if admin else []) +
             ["Purchases", "Recap", "Bid Board"] + seasonals + ["USDA Calendar"] +
             (["Review Scraped Bids"] if admin else []) + ["Farmers", "People"] if staff
             else ["Bid Board", "Make an Offer", "My Offers"] + seasonals + ["USDA Calendar"])
    if user["role"] == "platform_admin":
        views = views + ["Platform"]
    if user.get("_preview"):
        st.warning(f"Previewing as {user['_preview']}. Nothing here is saved as them; switch back in the sidebar.")
    for view in _tab_pages(views, key=f"main_tabs_{user['role']}"):
        _render_page(db, tenant, user, view, staff, admin)
    _end(tenant)


def _render_page(db, tenant: dict, user: dict, view: str, staff: bool, admin: bool) -> None:
    tid = tenant["tenant_id"]
    if view == "Platform":
        platform_view(db, user)
    elif view == "Summary":
        summary_view(db, tid, user)
    elif view == "Hedge":
        hedge_view(db, tid, user)
    elif view == "Auto-Hit":
        auto_hit_view(db, tid, user)
    elif view == "Purchases":
        purchases_view(db, tenant, user)
    elif view == "Recap":
        recap_view(db, tenant, user)
    elif view == "People":
        people_view(db, tenant, user)
    elif view == "Place Offer":
        place_offer_view(db, tenant, user)
    elif view == "Farmers":
        farmers_view(db, tenant, user)
    elif view == "Review Scraped Bids":
        suggestions_view(db, tid, user)
    elif view == "USDA Calendar":
        calendar_view()
    elif view == "Grain Futures Seasonals":
        seasonals_view()
    elif view == "Bid Board":
        published_view(db, tid, user, editable=admin, tenant=tenant)
    elif view == "Make an Offer":
        offer_form_view(db, tenant, user)
    else:
        offers_view(db, tid, user, staff)


main()
