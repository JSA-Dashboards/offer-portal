"""notify_run.py — deliver queued notification emails (offers hit by the futures job, recaps, retries).

    python notify_run.py

The app already flushes the queue right after a user acts; this covers everything that happens with nobody
logged in. Needs the Graph settings (GRAPH_ENV_FILE on the droplet points at the basis tracker's .env).
"""
from __future__ import annotations

import json
import logging

from portal import data, notify
from portal.db import connect

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("snowflake").setLevel(logging.WARNING)
    db = connect()
    expired = data.expire_all_tenants(db)               # offers past their last delivery day expire (and queue their email)
    print(json.dumps(dict(expired=expired, **notify.flush_all(db, limit=200)), default=str))
