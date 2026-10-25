"""auth.py — emailed one-time codes, scoped to a tenant.

Anti-enumeration: request_code() always behaves the same whether or not the
email is registered; a code is only generated and sent for a real, active user
who belongs to the tenant the login page is for (platform admins may sign in
on any tenant page). Codes are stored hashed, expire in 10 minutes, die after
5 wrong guesses, and are limited to 5 requests per email per 15 minutes.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets as _secrets
from datetime import datetime, timedelta, timezone

from .db import Db

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
MAX_REQUESTS, REQUEST_WINDOW = 5, timedelta(minutes=15)


def _pepper() -> str:
    return os.environ.get("AUTH_PEPPER", "")


def _hash(email: str, code: str) -> str:
    return hashlib.sha256(f"{_pepper()}|{email.lower()}|{code}".encode()).hexdigest()


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _as_utc(v) -> datetime:
    if isinstance(v, str):
        return datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def _allowed_user(db: Db, email: str, tenant_id: str | None) -> dict | None:
    r = db.query("SELECT * FROM users WHERE LOWER(email) = %s AND active", (email,))
    if not r:
        return None
    u = r[0]
    if u["role"] == "platform_admin" or u["tenant_id"] == tenant_id:
        return u
    return None


def request_code(db: Db, email: str, tenant_id: str | None, send) -> None:
    """`send(email, code)` delivers the code. Silent no-op for unknown/ineligible emails."""
    email = email.strip().lower()
    if not email or "@" not in email:
        return
    now = datetime.now(timezone.utc)
    recent = db.query("SELECT created_at FROM login_codes WHERE email = %s", (email,))
    if sum(1 for r in recent if now - _as_utc(r["created_at"]) < REQUEST_WINDOW) >= MAX_REQUESTS:
        return
    if not _allowed_user(db, email, tenant_id):
        return
    code = f"{_secrets.randbelow(10**6):06d}"
    db.execute("INSERT INTO login_codes (email, code_hash, expires_at) VALUES (%s, %s, %s)",
               (email, _hash(email, code), _ts(now + CODE_TTL)))
    send(email, code)


def verify_code(db: Db, email: str, code: str, tenant_id: str | None) -> dict | None:
    """Returns the user dict on success, else None."""
    email, code = email.strip().lower(), code.strip()
    rows = db.query("SELECT code_hash, expires_at, attempts FROM login_codes WHERE email = %s "
                    "AND NOT used ORDER BY created_at DESC", (email,))
    now = datetime.now(timezone.utc)
    live = [r for r in rows if _as_utc(r["expires_at"]) > now and r["attempts"] < MAX_ATTEMPTS]
    if not live:
        return None
    latest = live[0]
    if not hmac.compare_digest(latest["code_hash"], _hash(email, code)):
        db.execute("UPDATE login_codes SET attempts = attempts + 1 WHERE email = %s AND code_hash = %s",
                   (email, latest["code_hash"]))
        return None
    db.execute("UPDATE login_codes SET used = TRUE WHERE email = %s", (email,))
    return _allowed_user(db, email, tenant_id)


def _graph_env() -> None:
    """On the droplet the four GRAPH_* values live in the basis tracker's .env: GRAPH_ENV_FILE points there (as
    yield-portal does), so there is one copy to rotate. Only GRAPH_* keys are read, and only if not already set."""
    path = os.environ.get("GRAPH_ENV_FILE")
    if not path or not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        k, _, v = line.partition("=")
        k = k.strip()
        if k.startswith("GRAPH_") and not os.environ.get(k):
            os.environ[k] = v.strip().strip("\"'")


def send_via_graph(to: str, subject: str, html: str) -> None:
    """App-only Graph sendMail, same four GRAPH_* settings as the basis tracker."""
    _graph_env()
    import msal
    import requests
    app = msal.ConfidentialClientApplication(
        os.environ["GRAPH_CLIENT_ID"], authority=f"https://login.microsoftonline.com/{os.environ['GRAPH_TENANT_ID']}",
        client_credential=os.environ["GRAPH_CLIENT_SECRET"])
    tok = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in tok:
        raise RuntimeError("Graph token request failed")
    r = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{os.environ['GRAPH_SENDER']}/sendMail",
        headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=20,
        json={"message": {"subject": subject, "body": {"contentType": "HTML", "content": html},
                          "toRecipients": [{"emailAddress": {"address": to}}]}, "saveToSentItems": False})
    r.raise_for_status()


def graph_configured() -> bool:
    _graph_env()
    return all(os.environ.get(k) for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID",
                                           "GRAPH_CLIENT_SECRET", "GRAPH_SENDER"))
