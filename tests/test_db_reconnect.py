import pytest

from portal.db import Db


class ReauthenticationRequest(Exception):          # the connector's exception, by name
    pass


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.description = conn, [("N",)]

    def execute(self, sql, params=()):
        self.conn.calls.append(sql)
        if self.conn.dead:
            raise ReauthenticationRequest()

    def fetchall(self):
        return [(1,)]

    def close(self):
        pass


class FakeConn:
    def __init__(self, dead=False):
        self.dead, self.calls, self.closed = dead, [], False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def test_an_expired_session_reconnects_and_the_statement_runs_once_more():
    fresh = FakeConn()
    old = FakeConn(dead=True)
    db = Db(old, "snowflake", reconnect=lambda: fresh)
    assert db.query("SELECT 1") == [{"n": 1}]
    assert db.conn is fresh and old.closed and fresh.calls == ["SELECT 1"]
    db.execute("UPDATE t SET x = 1")                       # and the new connection keeps being used
    assert fresh.calls[-1] == "UPDATE t SET x = 1" and len(old.calls) == 1


def test_other_errors_are_not_retried_and_a_second_failure_surfaces():
    class Boom(Exception):
        pass

    class BoomConn(FakeConn):
        def cursor(self):
            raise Boom("syntax error")

    calls = []
    db = Db(BoomConn(), "snowflake", reconnect=lambda: calls.append(1))
    with pytest.raises(Boom):
        db.query("SELECT 1")
    assert not calls
    stuck = Db(FakeConn(dead=True), "snowflake", reconnect=lambda: FakeConn(dead=True))
    with pytest.raises(ReauthenticationRequest):
        stuck.query("SELECT 1")


def test_role_checks_are_reused_briefly_but_misses_and_changes_are_not_stuck(db):
    from portal import data
    db.actor_ttl = 60
    seen = []
    real = Db.query
    Db.query = lambda self, sql, params=(): (seen.append(sql), real(self, sql, params))[1]
    try:
        for _ in range(3):
            data._require_admin(db, "rcm", 2)                    # admin of rcm
        assert sum("FROM users" in q for q in seen) == 1         # one lookup, reused
        db.execute("UPDATE users SET role = 'farmer' WHERE user_id = 2")
        data._require_admin(db, "rcm", 2)                        # still the cached row inside the window...
        db.__dict__["_actor_cache"].clear()                      # ...and the app clears it on every action
        with pytest.raises(data.PortalError):
            data._require_admin(db, "rcm", 2)
        with pytest.raises(data.PortalError):                    # an unknown user is never cached as allowed
            data._require_admin(db, "rcm", 999)
        db.actor_ttl = 0
        db.execute("UPDATE users SET role = 'elevator_admin' WHERE user_id = 2")
        data._require_admin(db, "rcm", 2)
    finally:
        Db.query = real
