"""scrape_run.py — run every active scrape source into the bid_suggestions staging table.

    python scrape_run.py            # all tenants
    python scrape_run.py rcm        # one tenant

Run on a schedule (droplet cron, weekday mornings). Failures are logged per source and
never stop the others; nothing here publishes a bid.
"""
from __future__ import annotations

import logging
import sys

from portal import data, scrapers
from portal.db import connect

log = logging.getLogger("scrape_run")


def run(db, only_tenant: str | None = None, fetch=scrapers.fetch_source) -> dict:
    sql = "SELECT * FROM scrape_sources WHERE active"
    params: tuple = ()
    if only_tenant:
        sql, params = sql + " AND tenant_id = %s", (only_tenant,)
    results = {}
    for src in db.query(sql, params):
        try:
            items = fetch(src)
            if not items:
                raise RuntimeError("source returned no bids")
            res = data.sync_suggestions(db, src["tenant_id"], f"{src['kind']}:{src['url']}", items)
            status = "ok " + " ".join(f"{k}={v}" for k, v in res.items())
        except Exception as exc:                      # one bad site must not stop the rest
            res, status = None, f"error: {type(exc).__name__}: {exc}"[:500]
            log.error("%s %s: %s", src["tenant_id"], src["kind"], status)
        db.execute("UPDATE scrape_sources SET last_run_at = CURRENT_TIMESTAMP(), last_status = %s "
                   "WHERE source_id = %s", (status, src["source_id"]))
        results[(src["tenant_id"], src["kind"])] = res if res is not None else status
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = run(connect(), sys.argv[1] if len(sys.argv) > 1 else None)
    for k, v in out.items():
        print(k, v)
