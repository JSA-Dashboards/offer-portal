# Scraper schedule on the Droplet

`scrape_run.py` pulls each tenant's public cash bids (RCM = AgriCharts, Dorans = Bushel) into the
`bid_suggestions` staging table. It never publishes a bid; elevator admins confirm each one in the portal.
The Droplet runs only this job (no Streamlit), from a git clone at `/opt/offer-portal`, like the other apps.

## State on 2026-10-25
Installed and idle: clone, `.venv`, `.env`, the Snowflake key, and the cron line **commented out**.
It must stay off until the Snowflake side exists, otherwise every run fails and pages `cron-alert`.

## Turn it on (after the Snowflake scripts are run)
1. In Snowsight, as ACCOUNTADMIN, run `sql/00_database.sql`, `01_schema.sql`, `02_seed.sql`.
2. Paste the public key printed in step 3 below into `sql/03_service_user.sql` and run it.
3. Public key (safe to share; the private half never leaves the box):
   `ssh root@137.184.195.51 "openssl pkey -in /opt/offer-portal/offer_portal_svc.p8 -pubout | sed '1d;\$d' | tr -d '\n'; echo"`
4. Smoke test once by hand, then check the log:
   `ssh root@137.184.195.51 /opt/offer-portal/deploy/run_scrape.sh && tail -n 20 /opt/offer-portal/logs/scrape_*.log`
   Expect `ok staged=... unchanged=... unmatched=0` for `rcm` and `dorans`. `unmatched` > 0 means a feed
   location name does not match a row in `locations`.
5. Uncomment the crontab line (`crontab -e`; back it up first to `/opt/alerting/crontab.backup.<date>`):

   `15 6,12 * * 1-5 /opt/alerting/cron-alert "Offer portal bid scrape" "/opt/offer-portal/logs/scrape_*.log" /opt/offer-portal/deploy/run_scrape.sh`

6. Optional heartbeat: create a healthchecks.io check `offer-portal-bid-scrape`, schedule `15 6,12 * * 1-5`
   (America/Chicago, 2 h grace). An auto-made check defaults to a 1-day period and false-alarms on weekends.

## Deploy / update
`git -C /opt/offer-portal pull --ff-only` and, if `deploy/requirements-droplet.txt` changed,
`/opt/offer-portal/.venv/bin/pip install -r /opt/offer-portal/deploy/requirements-droplet.txt`.
`run_scrape.sh` must stay executable (`git update-index --chmod=+x deploy/run_scrape.sh`): files written on
Windows commit as 100644 and cron then fails with exit 126.

## Files on the box (none are committed)
`.env` (no secrets), `offer_portal_svc.p8` (mode 600, gitignored), `.venv/`, `logs/`.
