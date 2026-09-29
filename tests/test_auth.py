"""Signing in, lock-out, sessions, the first Owner."""

import db


def sign_in(client, username, password="password123", remember=False):
    data = {"username": username, "password": password}
    if remember:
        data["remember"] = "on"
    return client.post("/login", data=data)


def test_wrong_password_and_unknown_user_same_message(app, make):
    make.user("aditi", "owner")
    c = app.test_client()
    a = sign_in(c, "aditi", "wrong-password").data
    b = sign_in(c, "nobody", "wrong-password").data
    assert b"Wrong username or password." in a and b"Wrong username or password." in b


def test_success_signs_in_and_resets_count(app, conn, make):
    uid = make.user("aditi", "owner")
    c = app.test_client()
    for _ in range(3):
        sign_in(c, "aditi", "bad-password")
    assert conn.execute("SELECT failed_logins FROM users WHERE id=?", (uid,)).fetchone()[0] == 3
    r = sign_in(c, "aditi")
    assert r.status_code == 302
    assert conn.execute("SELECT failed_logins FROM users WHERE id=?", (uid,)).fetchone()[0] == 0
    assert c.get("/").status_code in (200, 302)


def test_five_failures_lock_for_fifteen_minutes(app, conn, make):
    uid = make.user("aditi", "owner")
    c = app.test_client()
    for _ in range(5):
        sign_in(c, "aditi", "bad-password")
    r = sign_in(c, "aditi")                       # right password, but locked
    assert b"locked" in r.data and r.status_code == 200
    # unlock by moving the lock into the past
    conn.execute("UPDATE users SET locked_until='2000-01-01T00:00:00Z' WHERE id=?", (uid,))
    conn.commit()
    assert sign_in(c, "aditi").status_code == 302


def test_deactivated_cannot_sign_in_and_sessions_end(app, conn, make, as_user):
    owner = make.user("boss", "owner")
    make.store("mall")
    uid = make.user("aditi", "cro", "mall", 101)
    c = as_user(uid)
    assert c.get("/leave").status_code == 200
    as_user(owner).post(f"/users/{uid}/deactivate")
    assert c.get("/leave").status_code == 302          # bounced to sign-in
    assert b"Wrong username or password." in sign_in(app.test_client(), "aditi").data


def test_reset_password_signs_out(app, conn, make, as_user):
    owner = make.user("boss", "owner")
    make.store("mall")
    uid = make.user("aditi", "cro", "mall", 101)
    c = as_user(uid)
    assert c.get("/leave").status_code == 200
    r = as_user(owner).post(f"/users/{uid}/reset", data={"password": "a-new-password"})
    assert b"Password reset" in r.data
    assert c.get("/leave").status_code == 302
    assert sign_in(app.test_client(), "aditi", "a-new-password").status_code == 302


def test_last_owner_protected(app, conn, make, as_user):
    owner = make.user("boss", "owner")
    c = as_user(owner)
    c.post(f"/users/{owner}/deactivate")
    c.post(f"/users/{owner}/delete")
    make.store("mall")
    make.emp("mall", 1)
    c.post(f"/users/{owner}/edit", data={"role": "cro", "display_name": "B", "person": "mall:1"})
    row = conn.execute("SELECT role, active FROM users WHERE id=?", (owner,)).fetchone()
    assert (row["role"], row["active"]) == ("owner", 1)


def test_bootstrap_only_when_empty(conn):
    import auth
    assert auth.bootstrap_owner(conn, "first", "a-long-password", log=lambda *_: None)
    assert not auth.bootstrap_owner(conn, "second", "a-long-password", log=lambda *_: None)
    assert [r[0] for r in conn.execute("SELECT username FROM users")] == ["first"]


def test_bootstrap_skipped_without_variables(conn):
    import auth
    assert not auth.bootstrap_owner(conn, "", "", log=lambda *_: None)


def test_change_own_password_signs_out_elsewhere(app, conn, make, as_user):
    uid = make.user("boss", "owner")
    here, elsewhere = as_user(uid), as_user(uid)
    r = here.post("/account", data={"action": "password", "current": "password123",
                                    "new": "another-pass", "again": "another-pass"})
    assert r.status_code == 302
    assert here.get("/account").status_code == 200
    assert elsewhere.get("/account").status_code == 302


def test_password_rules(app, make, as_user):
    uid = make.user("boss", "owner")
    c = as_user(uid)
    r = c.post("/account", data={"action": "password", "current": "password123",
                                 "new": "short", "again": "short"}, follow_redirects=True)
    assert b"at least 8" in r.data
    r = c.post("/account", data={"action": "password", "current": "password123",
                                 "new": "BossBoss", "again": "BossBoss"}, follow_redirects=True)
    assert b"at least 8" in r.data or b"same as the username" in r.data


def test_logins_are_logged(app, conn, make):
    make.user("aditi", "owner")
    c = app.test_client()
    sign_in(c, "aditi", "bad-password")
    sign_in(c, "aditi")
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions == ["login.fail", "login.ok"]


def test_next_parameter_cannot_leave_the_site(app, make):
    make.user("aditi", "owner")
    c = app.test_client()
    r = c.post("/login?next=//evil.example/x", data={"username": "aditi", "password": "password123"})
    assert r.headers["Location"] == "/"
