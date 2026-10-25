# Scraper schedule on the Droplet

`scrape_run.py` pulls each tenant's public cash bids (RCM = AgriCharts, Dorans = Bushel) into the
`bid_suggestions` staging table. It never publishes a bid; elevator admins confirm each one in the portal.
The Droplet runs only this job (no Streamlit), from a git clone at `/opt/offer-portal`, like the other apps.

## State
**Live since 2026-10-25.** Snowflake scripts 00-03 were run, the smoke test staged RCM (16) and Dorans (8) bids as
`OFFER_PORTAL_SVC`, nothing was published, and the cron line is active (weekdays 06:15 and 12:15 CT). If the
database is ever rebuilt, comment the line out first or every run fails and pages `cron-alert`.

## Setup record (already done)
1. In Snowsight, as ACCOUNTADMIN, run `sql/00_database.sql`, `01_schema.sql`, `02_seed.sql`.
2. Paste the public key printed in step 3 below into `sql/03_service_user.sql` and run it.
3. Public key (safe to share; the private half never leaves the box):
   `ssh root@<droplet> "openssl pkey -in /opt/offer-portal/offer_portal_svc.p8 -pubout | sed '1d;\$d' | tr -d '\n'; echo"`
4. Smoke test once by hand, then check the log:
   `ssh root@<droplet> /opt/offer-portal/deploy/run_scrape.sh && tail -n 20 /opt/offer-portal/logs/scrape_*.log`
   Expect `ok staged=... unchanged=... unmatched=0` for `rcm` and `dorans`. `unmatched` > 0 means a feed
   location name does not match a row in `locations`.
5. Uncomment the crontab line (`crontab -e`; back it up first to `/opt/alerting/crontab.backup.<date>`):

   `15 6,12 * * 1-5 /opt/alerting/cron-alert "Offer portal bid scrape" "/opt/offer-portal/logs/scrape_*.log" /opt/offer-portal/deploy/run_scrape.sh`

6. Optional heartbeat: create a healthchecks.io check `offer-portal-bid-scrape`, schedule `15 6,12 * * 1-5`
   (America/Chicago, 2 h grace). An auto-made check defaults to a 1-day period and false-alarms on weekends.

## Other jobs (NOT installed yet: add after sql/06 is applied and the app is deployed)
Each runs through `cron-alert` like the scraper. They need `GRAPH_ENV_FILE=/opt/basis-tracker/.env` in `/opt/offer-portal/.env`
(that file holds the Graph settings) so the droplet can send email.
- `*/5 * * * *`      `notify_run.py`  expire offers whose last delivery day has passed, then flush queued emails (retries too)
- `0 17 * * 1-5`     `recap_run.py`   end-of-day recap emails, 5:00 pm Central (once per person per day, safe to re-run)
- `*/5 7-15 * * 1-5` `futures_run.py` delayed futures quotes + auto-hit check (only once `MASSIVE_API_KEY` is set)
Pull new code with the SQL already applied: `git pull` before `sql/06` is run would reference columns that do not exist yet.

## Deploy / update
`git -C /opt/offer-portal pull --ff-only` and, if `deploy/requirements-droplet.txt` changed,
`/opt/offer-portal/.venv/bin/pip install -r /opt/offer-portal/deploy/requirements-droplet.txt`.
`run_scrape.sh` must stay executable (`git update-index --chmod=+x deploy/run_scrape.sh`): files written on
Windows commit as 100644 and cron then fails with exit 126.

## Files on the box (none are committed)
`.env` (no secrets), `offer_portal_svc.p8` (mode 600, gitignored), `.venv/`, `logs/`.
