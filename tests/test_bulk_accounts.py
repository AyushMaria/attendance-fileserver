"""One-click CRO logins and the first-sign-in password change."""

import sqlite3

import db


def people(make):
    make.store("mall")
    make.store("nirala")
    make.emp("mall", 101, "Aditi Sharma")
    make.emp("mall", 102, "Rahul Mehta")
    make.emp("nirala", 101, "Aditi Rao")            # same first name and number, other store
    make.emp("nirala", 205)
    make.conn.execute("UPDATE employees SET name='' WHERE user_id='205'")   # no name on the device
    make.conn.commit()
    make.emp("mall", 150, "Gone Person", on_device=0)


def test_bulk_creates_cro_logins(app, conn, make, as_user):
    people(make)
    owner = make.user("boss", "owner")
    make.user("rahul", "manager", "mall", 102, covers=["mall"])
    r = as_user(owner).post("/users/bulk-cro", data={"password": "tmpl@2026"})
    body = r.data.decode()
    assert "3 CRO logins created" in body and "tmpl@2026" in body
    rows = conn.execute("SELECT username, role, emp_store, emp_user_id, must_change_password FROM users "
                        "WHERE role='cro' ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("aditi101", "cro", "mall", "101", 1),
                                        ("aditi101.nirala", "cro", "nirala", "101", 1),
                                        ("staff205", "cro", "nirala", "205", 1)]
    # pressing it again adds nobody
    r = as_user(owner).post("/users/bulk-cro", data={"password": "tmpl@2026"})
    assert "0 CRO logins created" in r.data.decode()


def test_bulk_is_owner_only(app, make, as_user):
    people(make)
    mgr = make.user("rahul", "manager", "mall", 102, covers=["mall"])
    assert as_user(mgr).post("/users/bulk-cro", data={"password": "tmpl@2026"}).status_code == 404


def test_standard_password_signs_in_then_must_change(app, conn, make, as_user):
    people(make)
    owner = make.user("boss", "owner")
    as_user(owner).post("/users/bulk-cro", data={"password": "tmpl@2026"})
    c = app.test_client()
    r = c.post("/login", data={"username": "aditi101", "password": "tmpl@2026"})
    assert r.status_code == 302
    for path in ("/", "/leave", "/person/mall/101/2026-10"):
        r = c.get(path)
        assert r.status_code == 302 and r.headers["Location"].endswith("/account"), path
    assert "Choose your own password first" in c.get("/account").data.decode()
    # can't keep the same one
    c.post("/account", data={"action": "password", "current": "tmpl@2026", "new": "tmpl@2026", "again": "tmpl@2026"})
    assert c.get("/leave").status_code == 302
    c.post("/account", data={"action": "password", "current": "tmpl@2026", "new": "aditi-own-pw", "again": "aditi-own-pw"})
    assert c.get("/leave").status_code == 200
    assert conn.execute("SELECT must_change_password FROM users WHERE username='aditi101'").fetchone()[0] == 0


def test_owner_turns_cro_into_manager(app, conn, make, as_user):
    people(make)
    owner = make.user("boss", "owner")
    as_user(owner).post("/users/bulk-cro", data={"password": "tmpl@2026"})
    uid = conn.execute("SELECT id FROM users WHERE username='aditi101'").fetchone()[0]
    r = as_user(owner).post(f"/users/{uid}/edit", data={"role": "manager", "display_name": "Aditi",
                                                         "person": "mall:101", "stores": ["mall", "nirala"]})
    assert r.status_code == 302
    assert conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()[0] == "manager"
    assert {r[0] for r in conn.execute("SELECT store FROM user_stores WHERE user_id=?", (uid,))} == {"mall", "nirala"}


def test_reset_can_require_change(app, conn, make, as_user):
    make.store("mall")
    owner = make.user("boss", "owner")
    uid = make.user("aditi", "cro", "mall", 101)
    as_user(owner).post(f"/users/{uid}/reset", data={"password": "tmpl@2026", "must_change": "on"})
    assert conn.execute("SELECT must_change_password FROM users WHERE id=?", (uid,)).fetchone()[0] == 1


def test_accounts_page_offers_button(app, make, as_user):
    people(make)
    owner = make.user("boss", "owner")
    body = as_user(owner).get("/users").data.decode()
    assert "4 people on the devices have no login yet" in body and 'value="tmpl@2026"' in body


def test_old_database_gets_new_column(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    conn.commit()
    conn.close()
    db.init_db(str(path))
    db.init_db(str(path))              # running twice is fine
    conn = db.connect(str(path))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    assert "must_change_password" in cols
