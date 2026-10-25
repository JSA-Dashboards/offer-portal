#!/usr/bin/env bash
# run_scrape.sh — pull every tenant's public cash bids into the bid_suggestions staging table.
# Weekdays by cron on the Droplet (see DROPLET.md). Nothing here publishes a bid: an elevator
# admin confirms each suggestion in the portal. flock prevents overlapping runs; a failure exits
# non-zero so /opt/alerting/cron-alert emails it. Mirrors yield-portal's deploy/run_*.sh.
set -uo pipefail

APP_DIR="/opt/offer-portal"
VENV="$APP_DIR/.venv"
LOG_DIR="$APP_DIR/logs"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/scrape_$(date +%Y%m%d_%H%M%S).log"

cd "$APP_DIR" || { echo "APP_DIR $APP_DIR missing" >&2; exit 1; }

exec 9>"$LOG_DIR/.scrape.lock"
if ! flock -n 9; then
    echo "$(date -Is) another scrape run is in progress — skipping" >>"$LOG"
    exit 0
fi

rc=0
{
    echo "=== scrape start $(date -Is) ==="
    "$VENV/bin/python" scrape_run.py "$@"
    rc=$?    # capture immediately — must precede any other command (e.g. date)
    echo "=== scrape finished $(date -Is) rc=$rc ==="
} >>"$LOG" 2>&1

find "$LOG_DIR" -name 'scrape_*.log' -mtime +60 -delete 2>/dev/null || true

exit "$rc"
