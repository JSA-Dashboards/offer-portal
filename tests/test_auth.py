from portal import auth


def _sent():
    box = []
    return box, lambda e, c: box.append((e, c))


def test_code_roundtrip(db):
    box, send = _sent()
    auth.request_code(db, "Farmer@A.com", "rcm", send)
    assert len(box) == 1
    u = auth.verify_code(db, "farmer@a.com", box[0][1], "rcm")
    assert u and u["user_id"] == 1
    assert auth.verify_code(db, "farmer@a.com", box[0][1], "rcm") is None   # single use


def test_unknown_email_and_wrong_tenant_send_nothing(db):
    box, send = _sent()
    auth.request_code(db, "nobody@x.com", "rcm", send)
    auth.request_code(db, "farmer@a.com", "dorans", send)       # rcm farmer on dorans page
    assert box == []


def test_platform_admin_signs_in_on_any_tenant(db):
    box, send = _sent()
    auth.request_code(db, "kpostin@jpsi.com", "dorans", send)
    assert auth.verify_code(db, "kpostin@jpsi.com", box[0][1], "dorans")["role"] == "platform_admin"


def test_lockout_after_wrong_guesses(db):
    box, send = _sent()
    auth.request_code(db, "farmer@a.com", "rcm", send)
    for _ in range(auth.MAX_ATTEMPTS):
        assert auth.verify_code(db, "farmer@a.com", "000000x", "rcm") is None
    assert auth.verify_code(db, "farmer@a.com", box[0][1], "rcm") is None


def test_request_rate_limit(db):
    box, send = _sent()
    for _ in range(auth.MAX_REQUESTS + 3):
        auth.request_code(db, "farmer@a.com", "rcm", send)
    assert len(box) == auth.MAX_REQUESTS
