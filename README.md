# JSA Offer Portal Management

White-label grain offer manager. JSA runs the platform; each elevator company is a **tenant** that
manages its own bids, farmers and offers. One Streamlit app serves every tenant (`?t=<slug>`).

## What it does
- **Bids:** staff post a bid board; scrapers (AgriCharts, Bushel) stage suggestions that an admin confirms.
  Posting a new bid replaces the live one for that slot and re-prices outstanding flat-price offers
  (flat price - basis = implied futures).
- **Offers:** farmers (or staff on their behalf) make offers. Lifecycle: **Outstanding -> Hit -> Completed**
  (completing requires a contract number and archives the offer), or Declined / Withdrawn / Expired.
  There are no counter-offers.
- **Expiry:** an offer expires the day after the last day of its delivery period (its own dates, else the bid's range, else the
  end of its start month). Scraped bids need a clear delivery range; when the source doesn't give one, staff must enter it before approving.
- **Summary:** offers by commodity and delivery period, filterable by entity, individual, commodity, period.
- **Auto-hit (admin only):** rules that mark an outstanding offer hit automatically when the posted bid reaches it
  (scope by location, commodity, contract type, largest offer). No active rules = never automatic.
- **Hit -> acknowledge -> contract #:** a hit offer must be acknowledged by staff, then given a contract number to complete.
  An offer cancelled after a hit emails admins to adjust the hedge and sits on the Hedge page until they confirm.
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
