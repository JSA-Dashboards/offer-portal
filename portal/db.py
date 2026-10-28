"""db.py — the only module that talks to a database.

Snowflake in production (key-pair auth, same env names as the basis tracker);
sqlite only for the test suite, and only when passed in explicitly — there is
no silent fallback, so a missing SNOWFLAKE_* never writes to a local file.
Keeping all SQL access behind `Db` also makes the move to Streamlit in
Snowflake a one-class change (swap `connect()` for the session connection).
"""
from __future__ import annotations

import copy
import os
import pathlib
import time

PROJ = pathlib.Path(__file__).resolve().parent.parent


def _private_key_der():
    path = (os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH") or "").strip()
    pem = os.environ.get("SNOWFLAKE_PRIVATE_KEY") or ""
    if not path and not pem.strip():
        return None
    from cryptography.hazmat.primitives import serialization
    data = open(path, "rb").read() if path else pem.replace("\\n", "\n").encode()
    pwd = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PWD") or None
    key = serialization.load_pem_private_key(data, password=pwd.encode() if pwd else None)
    return key.private_bytes(encoding=serialization.Encoding.DER,
                             format=serialization.PrivateFormat.PKCS8,
                             encryption_algorithm=serialization.NoEncryption())


class Db:
    """Thin wrapper: `%s` placeholders everywhere, lowercase result keys."""

    def __init__(self, conn, dialect: str, reconnect=None):
        self.conn = conn
        self.dialect = dialect                      # 'snowflake' | 'sqlite'
        self._reconnect = reconnect                 # opens a fresh connection (Snowflake sessions expire when idle)
        self.read_ttl = 0                           # seconds a cached read is reused (the app sets this; 0 = never cache)
        self._reads: dict = {}
        self._writes = 0                            # bumped by every write: a cached read never outlives a change
        self.json_fn = "PARSE_JSON(%s)" if dialect == "snowflake" else "%s"

    def _sql(self, sql: str) -> str:
        if self.dialect == "sqlite":
            return sql.replace("%s", "?").replace("CURRENT_TIMESTAMP()", "CURRENT_TIMESTAMP")
        return sql

    @staticmethod
    def _session_expired(exc: Exception) -> bool:
        """Snowflake drops an idle session (the app keeps one connection for hours); the next statement fails before it
        runs, so it is safe to reconnect and send it again."""
        msg = str(exc).lower()
        return (type(exc).__name__ == "ReauthenticationRequest" or getattr(exc, "errno", None) in (390114, 390111, 390112)
                or "session no longer exists" in msg or "authentication token has expired" in msg)

    def _with_cursor(self, work):
        for attempt in (1, 2):
            cur = self.conn.cursor()
            try:
                return work(cur)
            except Exception as exc:
                if attempt == 1 and self._reconnect and self._session_expired(exc):
                    try:
                        self.conn.close()
                    except Exception:
                        pass
                    self.conn = self._reconnect()
                    continue
                raise
            finally:
                try:
                    cur.close()
                except Exception:
                    pass

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        def work(cur):
            cur.execute(self._sql(sql), params)
            cols = [d[0].lower() for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        return self._with_cursor(work)

    def cached(self, key, fn, ttl: float | None = None):
        """fn()'s result, reused for `ttl` seconds (default read_ttl) unless anything has been written since. Every tab click
        reruns the page and the same lists (bids, offers, quotes) are read again and again at ~0.1-0.3 s a round trip. Writes
        through this object (any session of this app) drop the cache at once; changes made by another process (the droplet's
        jobs) show up within the ttl. Callers get their own copy, so changing a returned row never alters the cache."""
        ttl = self.read_ttl if ttl is None else ttl
        if not ttl:
            return fn()
        hit = self._reads.get(key)
        if hit and hit[2] == self._writes and time.monotonic() - hit[0] < ttl:
            return copy.deepcopy(hit[1])
        written = self._writes
        val = fn()
        if len(self._reads) > 400:
            self._reads.clear()
        self._reads[key] = (time.monotonic(), copy.deepcopy(val), written)
        return val

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._writes += 1
        self._with_cursor(lambda cur: cur.execute(self._sql(sql), params))
        if self.dialect == "sqlite":
            self.conn.commit()


def connect() -> Db:
    """Snowflake connection from the environment; raises if not configured."""
    import snowflake.connector as sc
    from dotenv import load_dotenv
    load_dotenv(PROJ / ".env", override=False)
    kw = dict(account=os.environ["SNOWFLAKE_ACCOUNT"], user=os.environ["SNOWFLAKE_USER"],
              role=os.environ.get("SNOWFLAKE_ROLE") or None,
              warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE") or None,
              database=os.environ.get("SNOWFLAKE_DATABASE") or "OFFER_PORTAL",
              schema=os.environ.get("SNOWFLAKE_SCHEMA") or "OFFER_PORTAL",
              login_timeout=30, network_timeout=30,
              # The app writes UTC timestamp strings (code / invite expiry, offer expiry). Snowflake reads a
              # tz-less string in the SESSION time zone (default America/Los_Angeles), which would shift every
              # expiry by hours. Pin the session to UTC so a string means what we wrote.
              session_parameters={"TIMEZONE": "UTC"})
    pkey = _private_key_der()
    if pkey is None:
        raise RuntimeError("Set SNOWFLAKE_PRIVATE_KEY_PATH or SNOWFLAKE_PRIVATE_KEY (key-pair auth).")
    kw["private_key"] = pkey
    def _open():
        return sc.connect(**{k: v for k, v in kw.items() if v is not None})

    return Db(_open(), "snowflake", reconnect=_open)
