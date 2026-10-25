import pytest
from portal import auth, people
from portal.data import PortalError


def test_invite_accept_creates_user_once(db):
    tok = people.create_invite(db, "rcm", 2, "New.Farmer@X.com")
    assert people.invite_preview(db, tok, "rcm") == {"email": "new.farmer@x.com", "role": "farmer"}
    assert people.invite_preview(db, tok, "dorans") is None              # wrong tenant's page
    u = people.accept_invite(db, tok, "rcm", "Pat Farmer")
    assert (u["tenant_id"], u["role"], u["name"]) == ("rcm", "farmer", "Pat Farmer")
    with pytest.raises(PortalError):
        people.accept_invite(db, tok, "rcm", "again")                    # single use
    sent = []                                                            # and the new user can sign in
    auth.request_code(db, "new.farmer@x.com", "rcm", lambda e, c: sent.append(c))
    assert auth.verify_code(db, "new.farmer@x.com", sent[0], "rcm")["user_id"] == u["user_id"]


def test_expired_invite_rejected(db):
    tok = people.create_invite(db, "rcm", 2, "late@x.com")
    db.execute("UPDATE invites SET expires_at = '2000-01-01 00:00:00'")
    with pytest.raises(PortalError):
        people.accept_invite(db, tok, "rcm", "x")


def test_reinvite_voids_the_old_link(db):
    old = people.create_invite(db, "rcm", 2, "dup@x.com")
    new = people.create_invite(db, "rcm", 2, "dup@x.com")
    assert people.invite_preview(db, old, "rcm") is None
    assert people.invite_preview(db, new, "rcm")


def test_invite_permissions(db):
    db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (20, 'rcm', 's@rcm.com', 'elevator_staff')")
    people.create_invite(db, "rcm", 20, "f1@x.com", "farmer")            # staff may invite farmers
    with pytest.raises(PortalError):
        people.create_invite(db, "rcm", 20, "s2@x.com", "elevator_staff")   # ...but not staff
    with pytest.raises(PortalError):
        people.create_invite(db, "rcm", 1, "f2@x.com")                   # farmer can't invite
    with pytest.raises(PortalError):
        people.create_invite(db, "rcm", 4, "f3@x.com")                   # another tenant's admin can't
    with pytest.raises(PortalError):
        people.create_invite(db, "rcm", 2, "farmer@a.com")               # existing account
    with pytest.raises(PortalError):
        people.create_invite(db, "rcm", 2, "not-an-email")
    people.create_invite(db, "rcm", 2, "s3@x.com", "elevator_staff")     # admin may


def test_user_management_is_admin_only_and_tenant_scoped(db):
    people.set_user_active(db, "rcm", 2, 1, False)                       # admin deactivates farmer
    sent = []
    auth.request_code(db, "farmer@a.com", "rcm", lambda e, c: sent.append(c))
    assert sent == []                                                    # deactivated users get no code
    people.set_user_active(db, "rcm", 2, 1, True)
    for bad in (lambda: people.set_user_active(db, "rcm", 2, 2, False),      # yourself
                lambda: people.set_user_active(db, "rcm", 2, 3, False),      # another tenant's user
                lambda: people.set_user_active(db, "rcm", 2, 5, False),      # platform admin
                lambda: people.set_user_active(db, "rcm", 4, 1, False),      # other tenant's admin
                lambda: people.set_user_role(db, "rcm", 2, 1, "platform_admin")):
        with pytest.raises(PortalError):
            bad()
    people.set_user_role(db, "rcm", 2, 1, "elevator_staff")
    assert [u["role"] for u in people.list_users(db, "rcm", 2) if u["user_id"] == 1] == ["elevator_staff"]
    with pytest.raises(PortalError):
        people.list_users(db, "dorans", 3)                               # farmers can't list users
