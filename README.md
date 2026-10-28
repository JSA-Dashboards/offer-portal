# JSA Offer Portal Management

White-label grain offer manager. JSA runs the platform; each elevator company is a **tenant** that
manages its own bids, farmers and offers. One Streamlit app serves every tenant (`?t=<slug>`).

## What it does
- **Bids:** staff post a bid board; scrapers (AgriCharts, Bushel) stage suggestions that an admin confirms.
  Posting a new bid replaces the live one for that slot and re-prices outstanding flat-price offers
  (flat price - basis = implied futures).
- **Contract types** (they decide what an offer is made of): **Cash** = a futures price plus a basis (the flat price is their sum),
  **Futures Only (HTA)** = a futures price (the basis is greyed out and set later), **Basis Only** = a basis (the futures price is
  greyed out and set later). An offer names a **futures month**, not a delivery window: against a bid it carries the bid's window,
  otherwise it runs through that futures month.
- **Offers:** farmers (or staff on their behalf) make offers. Flow: **Outstanding** (entered, not accepted) -> staff press
  **Accept Offer** -> **Working Offers** (accepted, price not reached) -> the bid reaches the price and the offer moves to
  **Filled Offers** by itself -> staff press **Confirm Purchase** with the contract number (required) -> **Completed** (today)
  and, from tomorrow, **History**. Or Declined / Withdrawn / Expired / Cancelled (the Closed tab). There are no counter-offers.
  An outstanding offer the bid has already reached is highlighted; accepting it fills it at once.
- **Expiry:** an offer expires the day after the last day of its delivery period (its own dates, else the bid's range, else the
  end of its start month). Scraped bids need a clear delivery range; when the source doesn't give one, staff must enter it before approving.
- **Summary:** offers by commodity and delivery period, filterable by entity, individual, commodity, period.
- **Confirm Purchase:** the elevator takes the grain on: the offer is added to that day's purchases (and so the recap and the
  summary), the contract number is recorded and the offer completes. A futures-only offer is a price on the futures
  month itself (no basis, no bid): once accepted it fills when the delayed futures quote reaches it, then acts like any other
  offer. Accepting an offer emails the farmer ("we are working it"); **Mark Filled** is the manual fallback. An offer cancelled after it was filled voids its purchase, emails admins to adjust the hedge
  and sits on the Hedge page until they confirm.
- **Email:** an outbox (`portal/notify.py`) emails farmers, staff and admins on offer events (the app flushes it after an
  action; `notify_run.py` on the droplet covers the rest). Everyone can switch their own notifications off.
- **Purchases + daily recap:** staff log what they bought; each staff member gets their own end-of-day recap, admins the
  whole company's (`recap_run.py`).
- **Hedge (admin only):** contracts to hedge and at what prices (implied futures / basis), by futures month.
- **Delayed futures (placeholder):** `portal/massive.py` + `futures_run.py` keep ONE latest delayed Massive quote per futures month (needs
  `MASSIVE_API_KEY`; never run against the live API yet). The sticky part of a bid is its BASIS; the flat bid is futures + basis and moves
  with every quote, and offers are compared with it to decide whether they were hit. No price history is stored: only a hit keeps the price
  that triggered it. Farmers see the flat bid; the futures column only if the tenant's `show_futures` switch is on. Delayed-quote notice everywhere.
- **Theme:** CFTC-COT look (`portal/theme.py`), standard JSA disclaimer at the bottom of every page, white-label header/footer slots
  (tenant logo, name, colors, support email) with JSA "Powered by" branding (`JSA_BRANDING` switch).
- **Farmers:** customer records (client ID, entity, individual) that exist with or without a portal login.
- **Platform console (JSA):** create tenants, branding, locations, scrape sources, usage report for billing.

## Layout
| Path | Purpose |
|---|---|
| `app.py` | Streamlit UI |
| `portal/data.py` | Tenant-scoped data layer (offers, bids, lifecycle, summary, hedge) |
| `portal/auth.py`, `people.py`, `farmers.py`, `platform.py` | Sign-in codes, invites/users, farmer records, platform admin |
| `portal/scrapers.py`, `scrape_run.py` | Public cash-bid scrapers -> `bid_suggestions` staging |
| `portal/db.py` | The only module that talks to a database (Snowflake; sqlite only inside tests) |
| `sql/` | `00_database`, `01_schema`, `02_seed` (run in that order in the JSA Analytics account) |
| `tests/` | pytest suite (sqlite mirror of the schema); `tests/integration/snowflake_scenario.py` = real-Snowflake scenario, only against a `*_TEST` clone schema |

## Run
```
pip install -r requirements-dev.txt   # requirements.txt is the runtime set Streamlit Cloud installs
python -m pytest -q tests
streamlit run app.py        # needs SNOWFLAKE_* (key-pair auth) in .env or Streamlit secrets
```
Settings (env or Streamlit secrets): `SNOWFLAKE_ACCOUNT/USER/ROLE/WAREHOUSE/DATABASE/SCHEMA`,
`SNOWFLAKE_PRIVATE_KEY_PATH` or `SNOWFLAKE_PRIVATE_KEY`, `GRAPH_TENANT_ID/CLIENT_ID/CLIENT_SECRET/SENDER`
(email), `OFFER_PORTAL_URL` (base URL for invite links), `AUTH_PEPPER`, and `OFFER_PORTAL_DEV=1`
(local only: shows sign-in codes on screen).

## Not built yet
Futures job schedule + Massive key, notifications, e-signed contracts, ERP/position export, QST hedge bridge (phase 2).

## Text messages (built, off until JSA registers its number)

Texts ride the same events as email: farmers on hit / completed / cancelled / expired; staff and admins on new offer / hit.
The daily recap stays email-only. Only people who opted in (their own consent, in the sidebar: number + agreement, time recorded)
are texted; every text says how to stop; a STOP reply (Twilio error 21610) clears the person's opt-in automatically.

To switch on:
1. Create a Twilio account; register the **brand and campaign (A2P 10DLC)** with US carriers (days to weeks) and buy a number.
2. Run `sql/12_sms.sql` (opt-in columns, per-elevator switch/number, the `sms_queue` outbox). The app works without it.
3. Add the secrets: `SMS_ENABLED = "1"`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_FROM` (JSA's shared number) or
   `TWILIO_MESSAGING_SERVICE_SID`. Put the same on the droplet's `.env` so `notify_run.py` can send texts too.
4. Platform -> Tenants -> an elevator -> **Text alerts on** (JSA Test Elevator first). Optional: that elevator's own number
   (its own 10DLC registration) in the field below; blank uses JSA's shared number.
