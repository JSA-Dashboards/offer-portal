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


# ── cached reads: reused between clicks, dropped by any write ────────────────
def test_cached_reads_are_reused_until_a_write_and_callers_get_their_own_copy(db):
    calls = []

    def read():
        calls.append(1)
        return db.query("SELECT user_id, role FROM users ORDER BY user_id")

    assert db.cached("k", read) == db.query("SELECT user_id, role FROM users ORDER BY user_id") and len(calls) == 1
    db.read_ttl = 60
    a = db.cached("k", read)
    b = db.cached("k", read)
    assert len(calls) == 2 and a == b                                       # the second call came from the cache
    a[0]["role"] = "tampered"
    assert db.cached("k", read)[0]["role"] != "tampered"                    # changing a returned row cannot poison the cache
    db.execute("UPDATE users SET role = 'farmer' WHERE user_id = 2")        # any write drops every cached read
    assert [r["role"] for r in db.cached("k", read) if r["user_id"] == 2] == ["farmer"] and len(calls) == 3


def test_a_cached_read_expires_and_the_cache_is_off_unless_asked_for(db):
    n = []
    db.read_ttl = 0.05
    f = lambda: n.append(1) or [len(n)]
    assert db.cached("t", f) == [1] and db.cached("t", f) == [1]
    import time
    time.sleep(0.08)
    assert db.cached("t", f) == [2]                                         # past the ttl: read again
    db.read_ttl = 0
    assert db.cached("t", f) == [3] and db.cached("t", f) == [4]            # off: always reads


def test_the_lists_the_pages_share_follow_changes_made_through_the_app(db):
    from portal import data
    db.read_ttl = 60
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", basis_cents=-38, futures_symbol="ZCZ26")
    assert len(data.list_bids(db, "rcm")) == 1 and len(data.list_bids(db, "rcm")) == 1
    data.publish_bid(db, "rcm", 2, grain="Soybeans", delivery_month="2999-12", basis_cents=-50, futures_symbol="ZSX26")
    assert len(data.list_bids(db, "rcm")) == 2                                # a new bid shows at once
    from conftest import user
    oid = data.create_offer(db, "rcm", user(db, 1), grain="Corn", contract_type="HTA",
                            price_type="BASIS", bushels=5000, basis_cents=-30, futures_symbol="ZCZ26")
    staff = user(db, 2)
    assert [o["status"] for o in data.list_offers(db, "rcm", staff)] == ["submitted"]
    data.act_on_offer(db, "rcm", staff, oid, "accept")
    assert [o["status"] for o in data.list_offers(db, "rcm", staff)] == ["working"]      # and so does a status change
