"""futures_run.py — fetch delayed futures quotes (Massive API, PLACEHOLDER) and stage cash-price suggestions.

    python futures_run.py

Needs MASSIVE_API_KEY; without it this does nothing and says so. It NEVER changes a posted bid: quotes are
stored for display, and tenants with no scrape source get "indicated cash" suggestions that an elevator admin
must edit and confirm in the portal. Not scheduled anywhere yet.
"""
from __future__ import annotations

import json
import logging
import sys

from portal import futures
from portal.db import connect

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("snowflake").setLevel(logging.WARNING)
    out = futures.run_update(connect())
    print(json.dumps(out, default=str, indent=2))
    sys.exit(0 if str(out.get("status", "")).startswith(("ok", "not configured", "no futures")) else 1)
