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

PBKDF2_ROUNDS = 600_000                      # OWASP's 2023 figure for PBKDF2-HMAC-SHA256
MIN_PASSWORD = 10
MAX_FAILS, LOCK_FOR = 5, timedelta(minutes=15)
_SECRET_COLS = ("password_hash", "failed_logins", "locked_until", "password_set_at")
_COMMON = {"password", "password1", "password123", "1234567890", "qwertyuiop", "iloveyou12", "letmein123",
           "welcome123", "changeme123", "admin12345", "0123456789", "abcdefghij"}

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


def _public(u: dict) -> dict:
    """The user as the app keeps it in the session: never the password hash or its lockout counters."""
    return {k: v for k, v in u.items() if k not in _SECRET_COLS}


def _allowed_user(db: Db, email: str, tenant_id: str | None) -> dict | None:
    r = db.query("SELECT * FROM users WHERE LOWER(email) = %s AND active", (email,))
    if not r:
        return None
    u = r[0]
    if u["role"] == "platform_admin" or u["tenant_id"] == tenant_id:
        return _public(u)
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


# ── Passwords ───────────────────────────────────────────────────────────────
# A password is the everyday sign-in; the emailed code stays for the first sign-in (it proves the email is theirs),
# for a forgotten password, and for anyone who has not set one. Hashes are salted PBKDF2 (stdlib, no extra package).
def passwords_ready(db: Db) -> bool:
    """True once the users table has the password columns (sql/11). Until then the app runs on email codes alone,
    so deploying the code before the SQL never breaks sign-in. A miss is re-checked after a minute."""
    import time
    now = time.monotonic()
    state = getattr(db, "_pw_ready", None)
    if state and (state[0] or now - state[1] < 60):
        return state[0]
    try:
        db.query("SELECT password_hash, failed_logins, locked_until FROM users LIMIT 1")
        ok = True
    except Exception:
        ok = False
    db._pw_ready = (ok, now)
    return ok


def hash_password(password: str, rounds: int = PBKDF2_ROUNDS) -> str:
    salt = _secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", (_pepper() + password).encode(), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${dk.hex()}"


def _matches(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, want = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", (_pepper() + password).encode(), bytes.fromhex(salt), int(rounds))
        return algo == "pbkdf2_sha256" and hmac.compare_digest(dk.hex(), want)
    except (ValueError, AttributeError):
        return False


_DUMMY = None


def check_policy(password: str, email: str = "") -> str | None:
    """Why this password is not acceptable, or None if it is."""
    pw = password or ""
    if len(pw) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters. A few words strung together is easy to remember and hard to guess."
    if pw.lower() in _COMMON or len(set(pw)) < 4:
        return "That password is too easy to guess."
    if email and (pw.lower() == email.lower() or pw.lower() == email.split("@")[0].lower()):
        return "Your password can't be your email address."
    return None


def has_password(db: Db, user_id: int) -> bool:
    if not passwords_ready(db):
        return False
    r = db.query("SELECT password_hash FROM users WHERE user_id = %s", (user_id,))
    return bool(r and r[0]["password_hash"])


def set_password(db: Db, user_id: int, new_password: str) -> None:
    """Sets (or replaces) a user's password. Callers prove who the person is first: a code sign-in, an accepted
    invite, or the current password."""
    from .data import PortalError, _audit
    if not passwords_ready(db):
        raise PortalError("Passwords aren't switched on yet. Please use an email code.")
    u = db.query("SELECT user_id, tenant_id, email FROM users WHERE user_id = %s AND active", (user_id,))
    if not u:
        raise PortalError("Account not found.")
    problem = check_policy(new_password, u[0]["email"])
    if problem:
        raise PortalError(problem)
    db.execute("UPDATE users SET password_hash = %s, password_set_at = CURRENT_TIMESTAMP(), failed_logins = 0, "
               "locked_until = NULL WHERE user_id = %s", (hash_password(new_password), user_id))
    _audit(db, u[0]["tenant_id"], user_id, "password_set", {})


def verify_password(db: Db, email: str, password: str, tenant_id: str | None) -> dict | None:
    """The user if the email and password match and they may sign in on this page, else None. All failures look the
    same (wrong password, unknown email, no password set, locked): a stranger learns nothing. Five wrong passwords in
    a row pause password sign-in for that account for 15 minutes (the emailed code still works)."""
    global _DUMMY
    email, password = (email or "").strip().lower(), password or ""
    rows = db.query("SELECT * FROM users WHERE LOWER(email) = %s AND active", (email,)) if passwords_ready(db) else []
    u = rows[0] if rows else None
    stored = (u or {}).get("password_hash")
    if not stored:                                       # spend the same time as a real check
        _DUMMY = _DUMMY or hash_password("not-a-real-password", PBKDF2_ROUNDS)
        _matches(password, _DUMMY)
        return None
    now = datetime.now(timezone.utc)
    if u.get("locked_until") and _as_utc(u["locked_until"]) > now:
        _matches(password, stored)
        return None
    if not _matches(password, stored):
        fails = int(u.get("failed_logins") or 0) + 1
        if fails >= MAX_FAILS:
            db.execute("UPDATE users SET failed_logins = 0, locked_until = %s WHERE user_id = %s",
                       (_ts(now + LOCK_FOR), u["user_id"]))
        else:
            db.execute("UPDATE users SET failed_logins = %s WHERE user_id = %s", (fails, u["user_id"]))
        return None
    if u["role"] != "platform_admin" and u["tenant_id"] != tenant_id:
        return None                                      # right password, wrong elevator's page
    if u.get("failed_logins") or u.get("locked_until"):
        db.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE user_id = %s", (u["user_id"],))
    return _public(u)
