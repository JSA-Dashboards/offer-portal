"""recap_run.py — end-of-day recap emails, then flush the notification queue.

    python recap_run.py            # today (Central), every active tenant

Each staff member gets their own recap and each admin the whole company's, once per person per day (safe to re-run).
Run it from cron on the droplet at the end of the business day; notify_run.py flushes the queue between runs.
"""
from __future__ import annotations

import json
import logging

from portal import data, notify, recap
from portal.db import connect

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("snowflake").setLevel(logging.WARNING)
    db = connect()
    data.expire_all_tenants(db)                          # so the recap's 'working' list is current
    queued = recap.send_daily_recaps(db)
    print(json.dumps(dict(recap=queued, sent=notify.send_pending(db, limit=200)), default=str))
