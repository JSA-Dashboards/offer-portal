"""theme.py — the portal's look, taken from the CFTC COT dashboard (sage palette, EB Garamond headings,
tiles, uppercase section headers), plus the compliance footer every JSA dashboard carries.

White-label: every elevator tenant gets its own **header slot** (its logo, name and colors from the
`tenants` row) and its own **support line** in the footer. JSA's logo stays in the header as
"Powered by" and in the footer; `JSA_BRANDING` below is the one switch for hiding it later.
The builders are pure strings (testable without Streamlit); `apply()` puts them on the page.
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

# ── palette (matches cftc-cot-dashboard / livestock-portal COT page) ────────────
JSA_GREEN = "#5e7164"
BG, SURFACE, BORDER = "#f6f8f7", "#ffffff", "#d7e2dc"
TEXT, MUTED = "#32373c", "#5f7267"
JSA_BLUE = "#0693e3"
POS, NEG = "#16a34a", "#dc2626"
DB_DEFAULT_PRIMARY = "#32373c"        # the tenants.primary_color default = "not customised"

JSA_LOGO_URL = "https://www.jpsi.com/wp-content/themes/gate39media/img/logo-full.png"
JSA_BRANDING = True                   # white-label switch: False drops the JSA logo / "Powered by" lines

# The standard JSA compliance disclaimer, word for word as on the River FOB, Livestock and other dashboards.
DISCLAIMER = (
    "Trading commodity futures, options on futures, cash commodities, and "
    "over-the-counter derivative products involves substantial risk of loss and "
    "may not be suitable for all investors. This communication is provided for "
    "informational purposes only and does not constitute investment advice, a "
    "recommendation, or an offer or solicitation to buy or sell any futures, "
    "options, cash commodities, or derivative products. John Stewart &amp; "
    "Associates, Inc. does not accept orders to buy or sell any financial "
    "instruments via email. The information contained herein has been obtained "
    "from sources believed to be reliable; however, its accuracy and completeness "
    "are not guaranteed. Any opinions expressed are solely those of the author, "
    "are subject to change without notice, and should not be relied upon as a "
    "basis for investment decisions. Past performance is not indicative of future "
    "results. This message may contain confidential or proprietary information "
    "intended solely for the use of the designated recipient.")

# Shown wherever a futures price appears, and in the footer of any page that shows one.
DELAYED_NOTICE = (
    "Futures prices, and the flat bids calculated from them, are based on quotes delayed at least 10 minutes. They are not real-time, "
    "may differ from the market, and are shown for reference only. A flat bid is the futures price plus the posted "
    "basis, so it moves with futures; the basis stays as posted until an elevator administrator updates and "
    "confirms it.")

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _year() -> int:
    return datetime.now(ZoneInfo("America/Chicago")).year


def _color(v, fallback: str) -> str:
    return v if isinstance(v, str) and _HEX.match(v) else fallback


def _https(url) -> str | None:
    return url if isinstance(url, str) and url.startswith("https://") else None


def colors(tenant: dict | None) -> dict:
    """Header / tile accent and button color for a tenant; a tenant still on the default gets JSA's palette."""
    t = tenant or {}
    primary = _color(t.get("primary_color"), JSA_GREEN)
    if primary.lower() == DB_DEFAULT_PRIMARY:
        primary = JSA_GREEN
    return {"primary": primary, "accent": _color(t.get("accent_color"), JSA_BLUE)}


def css(tenant: dict | None = None) -> str:
    c = colors(tenant)
    return f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=EB+Garamond:wght@500;600&family=Source+Sans+3:wght@400;600;700&display=swap');
:root {{ --tenant-primary:{c['primary']}; --tenant-accent:{c['accent']}; }}
html, body, [data-testid="stAppViewContainer"] {{ background-color:{BG}; color:{TEXT}; }}
html, body, p, span, div, label, button, td, th {{ font-family:'Source Sans 3','Source Sans Pro',sans-serif; }}
h1, h2, h3 {{ font-family:'EB Garamond',Georgia,serif; color:{TEXT}; font-weight:600; }}
.block-container {{ padding-top: 3.2rem; max-width: 1400px; }}
.dash-header {{ display:flex; align-items:center; justify-content:space-between; gap:18px; padding-bottom:10px;
    border-bottom:1px solid {BORDER}; margin-bottom:14px; flex-wrap:wrap; }}
.dash-header .slot-tenant {{ display:flex; align-items:center; gap:16px; }}
.dash-header .slot-tenant img {{ height:40px; max-width:200px; object-fit:contain; }}
.dash-header .t b {{ font-family:'EB Garamond',Georgia,serif; font-size:1.7rem; color:{TEXT}; font-weight:600; }}
.dash-header .t div {{ font-size:.85rem; color:{MUTED}; }}
.dash-header .slot-powered {{ display:flex; align-items:center; gap:10px; color:{MUTED}; font-size:.72rem;
    text-transform:uppercase; letter-spacing:.09em; }}
.dash-header .slot-powered img {{ height:30px; }}
@media (max-width: 640px) {{ .dash-header .slot-powered {{ display:none; }} }}
.tile {{ background:{SURFACE}; border:1px solid {BORDER}; border-top:3px solid var(--tenant-primary);
    border-radius:10px; padding:16px 20px; text-align:center; height:100%; }}
.tile-label {{ color:{MUTED}; font-size:0.68rem; text-transform:uppercase; letter-spacing:0.09em; margin-bottom:6px; }}
.tile-value {{ color:{TEXT}; font-size:1.55rem; font-weight:700; line-height:1.1; }}
.tile-sub {{ color:{MUTED}; font-size:0.72rem; margin-top:5px; }}
.sec-header {{ color:{MUTED}; font-size:0.7rem; text-transform:uppercase; letter-spacing:0.1em;
    padding:10px 0 4px; border-bottom:1px solid {BORDER}; margin-bottom:12px; }}
.asof {{ background:{SURFACE}; border:1px solid {BORDER}; border-radius:8px; padding:10px 16px;
    color:{MUTED}; font-size:0.82rem; margin-bottom:12px; }}
.asof b {{ color:{TEXT}; }}
.delayed {{ background:#fff8e6; border:1px solid #f0d9a0; border-radius:8px; padding:8px 14px; color:#6b5a2a;
    font-size:0.8rem; margin:6px 0 12px; }}
.stButton>button[kind="primary"], .stFormSubmitButton>button[kind="primary"] {{
    background:var(--tenant-accent); border-color:var(--tenant-accent); }}
.portal-footer {{ font-family:inherit; margin-top:1.5rem; padding-top:0.7rem; border-top:1px solid #e0e0e0;
    font-size:inherit; line-height:1.4; color:#8a9199; text-align:justify; }}
.portal-footer .foot-brand {{ display:flex; align-items:center; justify-content:space-between; gap:14px;
    margin-bottom:8px; font-size:.8rem; }}
.portal-footer .foot-brand img {{ height:22px; }}
hr {{ border-color:{BORDER}; }}
#MainMenu, footer {{ visibility:hidden; }}
.stDeployButton {{ display:none; }}
</style>"""


def header_html(tenant: dict | None = None, subtitle: str | None = None) -> str:
    """The page header. LEFT = white-label slot (tenant logo + name). RIGHT = JSA 'Powered by'."""
    t = tenant or {}
    logo = _https(t.get("logo_url"))
    name = html.escape(t.get("name") or "JSA Offer Portal Management")
    sub = html.escape(subtitle or ("Grain offers and bids" if t.get("name") else
                                   "Grain offers and bids · John Stewart & Associates"))
    left_logo = f'<img src="{html.escape(logo)}" alt="{name}">' if logo else ""
    if not t.get("name") and JSA_BRANDING:               # no tenant yet (landing page): JSA's own header
        left_logo = f'<img src="{JSA_LOGO_URL}" alt="John Stewart &amp; Associates">'
    powered = (f'<div class="slot-powered">Powered by <img src="{JSA_LOGO_URL}" alt="John Stewart &amp; Associates">'
               f'</div>') if (JSA_BRANDING and t.get("name")) else ""
    return (f'<div class="dash-header"><div class="slot-tenant">{left_logo}'
            f'<div class="t"><b>{name}</b><div>{sub}</div></div></div>{powered}</div>')


def _foot_line(t: dict, support: str) -> str:
    """'RCM Coop · Questions about your account: ...' (either part may be missing)."""
    return " · ".join(x for x in (html.escape(t.get("name") or ""), support) if x)


def footer_html(tenant: dict | None = None, delayed: bool = False) -> str:
    """The compliance footer: the standard JSA disclaimer (+ the delayed-quote notice on pages that show
    futures), the white-label support line, the JSA mark and the copyright."""
    t = tenant or {}
    support = ""
    if t.get("support_email"):
        support = f'Questions about your account: <b>{html.escape(t["support_email"])}</b>.'
    brand = ""
    if JSA_BRANDING:
        brand = (f'<div class="foot-brand"><span>{_foot_line(t, support)}</span>'
                 f'<img src="{JSA_LOGO_URL}" alt="John Stewart &amp; Associates"></div>')
    elif support:
        brand = f'<div class="foot-brand"><span>{_foot_line(t, support)}</span></div>'
    delayed_html = f'<p><b>Delayed quotes.</b> {html.escape(DELAYED_NOTICE)}</p>' if delayed else ""
    return (f'<div class="portal-footer">{brand}{delayed_html}'
            f'{DISCLAIMER} &copy; John Stewart &amp; Associates, Inc. {_year()}</div>')


def delayed_banner_html(as_of: str | None = None) -> str:
    stamp = f" Latest quote: {html.escape(as_of)}." if as_of else ""
    return f'<div class="delayed"><b>Delayed futures.</b> {html.escape(DELAYED_NOTICE)}{stamp}</div>'


def tile_html(label: str, value: str, sub: str = "") -> str:
    s = f'<div class="tile-sub">{html.escape(sub)}</div>' if sub else ""
    return (f'<div class="tile"><div class="tile-label">{html.escape(label)}</div>'
            f'<div class="tile-value">{html.escape(value)}</div>{s}</div>')


def email_html(tenant: dict | None, body_html: str) -> str:
    """A simple, client-safe HTML email: tenant header, the message, the standard JSA disclaimer (as on every page).
    `body_html` must already be escaped / trusted."""
    t = tenant or {}
    name = html.escape(t.get("name") or "JSA Offer Portal Management")
    logo = _https(t.get("logo_url"))
    head_logo = f'<img src="{html.escape(logo)}" alt="" style="height:34px;vertical-align:middle;margin-right:10px">' if logo else ""
    powered = ('<div style="margin-top:6px;font-size:11px;color:#8a9199">Powered by John Stewart &amp; Associates</div>'
               if JSA_BRANDING else "")
    support = (f'<div style="font-size:12px;color:#5f7267">Questions: {html.escape(t["support_email"])}</div>'
               if t.get("support_email") else "")
    return (f'<div style="font-family:Segoe UI,Arial,sans-serif;max-width:640px;color:{TEXT}">'
            f'<div style="border-bottom:3px solid {colors(t)["primary"]};padding-bottom:8px;margin-bottom:14px">'
            f'{head_logo}<span style="font-family:Georgia,serif;font-size:20px;font-weight:600">{name}</span>{powered}</div>'
            f'{body_html}{support}'
            f'<div style="margin-top:22px;padding-top:10px;border-top:1px solid #e0e0e0;font-size:11px;line-height:1.4;'
            f'color:#8a9199;text-align:justify">{DISCLAIMER} &copy; John Stewart &amp; Associates, Inc. {_year()}</div></div>')
