"""db.py — the only module that talks to a database.

Snowflake in production (key-pair auth, same env names as the basis tracker);
sqlite only for the test suite, and only when passed in explicitly — there is
no silent fallback, so a missing SNOWFLAKE_* never writes to a local file.
Keeping all SQL access behind `Db` also makes the move to Streamlit in
Snowflake a one-class change (swap `connect()` for the session connection).
"""
from __future__ import annotations

import os
import pathlib

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

    def __init__(self, conn, dialect: str):
        self.conn = conn
        self.dialect = dialect                      # 'snowflake' | 'sqlite'
        self.json_fn = "PARSE_JSON(%s)" if dialect == "snowflake" else "%s"

    def _sql(self, sql: str) -> str:
        if self.dialect == "sqlite":
            return sql.replace("%s", "?").replace("CURRENT_TIMESTAMP()", "CURRENT_TIMESTAMP")
        return sql

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self.conn.cursor()
        try:
            cur.execute(self._sql(sql), params)
            cols = [d[0].lower() for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            cur.close()

    def execute(self, sql: str, params: tuple = ()) -> None:
        cur = self.conn.cursor()
        try:
            cur.execute(self._sql(sql), params)
        finally:
            cur.close()
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
              login_timeout=30, network_timeout=30)
    pkey = _private_key_der()
    if pkey is None:
        raise RuntimeError("Set SNOWFLAKE_PRIVATE_KEY_PATH or SNOWFLAKE_PRIVATE_KEY (key-pair auth).")
    kw["private_key"] = pkey
    return Db(sc.connect(**{k: v for k, v in kw.items() if v is not None}), "snowflake")
