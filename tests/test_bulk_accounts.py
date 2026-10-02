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
    assert "2 CRO logins created" in body and "tmpl@2026" in body and "1 skipped" in body
    rows = conn.execute("SELECT username, role, emp_store, emp_user_id, must_change_password FROM users "
                        "WHERE role='cro' ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("aditi101", "cro", "mall", "101", 1),
                                        ("aditi101.nirala", "cro", "nirala", "101", 1)]
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


def test_hidden_people_left_out_everywhere(app, conn, make, as_user, now):
    from datetime import date
    from attendance import StoreData
    make.store("mall")
    owner = make.user("boss", "owner")
    make.emp("mall", 1, "Ayush")
    make.emp("mall", 2, "Aditi Sharma")
    make.punch("mall", 1, "2026-10-04 11:00:00")      # only the Owner came in on Sunday
    make.punch("mall", 2, "2026-10-05 09:00:00")
    c = as_user(owner)
    c.post("/people/hide", data={"person": "mall:1"})
    sd = StoreData(conn, "mall", date(2026, 10, 1), date(2026, 10, 31), now.value)
    assert [p["user_id"] for p in sd.people] == ["2"]
    assert sd.cell("2", date(2026, 10, 4)).code == "NODATA"      # the Owner alone doesn't open the store
    assert "Ayush" not in c.get("/calendar/mall/2026-10").data.decode()
    assert "Ayush" not in c.get("/staff").data.decode()
    body = c.get("/users").data.decode()
    assert "1 people on the devices have no login yet" in body
    r = c.post("/users/bulk-cro", data={"password": "tmpl@2026"}).data.decode()
    assert "aditi2" in r and "ayush1" not in r
    assert conn.execute("SELECT COUNT(*) FROM users WHERE emp_user_id='1'").fetchone()[0] == 0
    c.post("/people/show", data={"person": "mall:1"})
    assert "Ayush" in c.get("/calendar/mall/2026-10").data.decode()


def test_ayush_hidden_once_when_upgrading(tmp_path):
    import sqlite3
    import db
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE employees (store TEXT, user_id TEXT, name TEXT, on_device INTEGER,
                                first_seen TEXT, last_seen TEXT, PRIMARY KEY (store, user_id));
        INSERT INTO employees VALUES ('mall','1','Ayush',1,'x','x'), ('nirala','2',' ayush ',1,'x','x'),
                                     ('mall','3','Ayushi',1,'x','x');""")
    conn.commit()
    conn.close()
    db.init_db(str(path))
    conn = db.connect(str(path))
    got = {r["user_id"]: r["hidden"] for r in conn.execute("SELECT * FROM employees")}
    assert got == {"1": 1, "2": 1, "3": 0}
    conn.execute("UPDATE employees SET hidden=0")
    conn.commit()
    db.init_db(str(path))                              # never again after the first time
    assert conn.execute("SELECT SUM(hidden) FROM employees").fetchone()[0] == 0


def test_nn_placeholders_ignored_and_typed_names_kept(app, conn, make, as_user):
    from views.api import hash_key
    make.store("office", key="office-key-" + "x" * 30)
    owner = make.user("boss", "owner")
    c = app.test_client()
    h = {"Authorization": "Bearer office-key-" + "x" * 30}
    c.post("/api/sync", json={"staff": [{"user_id": "12", "name": "NN-12"},
                                        {"user_id": "17", "name": "NN-17"}], "punches": []}, headers=h)
    assert {r[0]: r[1] for r in conn.execute("SELECT user_id, name FROM employees")} == {"12": "", "17": ""}
    o = as_user(owner)
    o.post("/stores/office/names", data={"name_12": "Ravi Kumar", "bulk": "17, Sana Khan\n99, Nobody"})
    names = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT user_id, name, name_locked FROM employees")}
    assert names == {"12": ("Ravi Kumar", 1), "17": ("Sana Khan", 1)}
    # the next sync, still with placeholders - or even a device name - keeps them
    c.post("/api/sync", json={"staff": [{"user_id": "12", "name": "NN-12"},
                                        {"user_id": "17", "name": "SANA"}], "punches": []}, headers=h)
    assert {r[0]: r[1] for r in conn.execute("SELECT user_id, name FROM employees")} == \
        {"12": "Ravi Kumar", "17": "Sana Khan"}
    assert "Ravi Kumar" in o.get("/stores/office/names").data.decode()


def test_bulk_skips_people_without_names(app, conn, make, as_user):
    make.store("office")
    owner = make.user("boss", "owner")
    make.emp("office", 12, "Ravi Kumar")
    make.emp("office", 13)
    conn.execute("UPDATE employees SET name='' WHERE user_id='13'")
    conn.commit()
    body = as_user(owner).post("/users/bulk-cro", data={"password": "tmpl@2026"}).data.decode()
    assert "ravi12" in body and "1 skipped" in body
    assert conn.execute("SELECT COUNT(*) FROM users WHERE emp_user_id='13'").fetchone()[0] == 0


def test_old_nn_names_cleared_on_upgrade(tmp_path):
    import sqlite3
    import db
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE employees (store TEXT, user_id TEXT, name TEXT, on_device INTEGER,
                                first_seen TEXT, last_seen TEXT, PRIMARY KEY (store, user_id));
        INSERT INTO employees VALUES ('office','12','NN-12',1,'x','x'), ('mall','3','Imran',1,'x','x');""")
    conn.commit()
    conn.close()
    db.init_db(str(path))
    conn = db.connect(str(path))
    assert {r["user_id"]: r["name"] for r in conn.execute("SELECT * FROM employees")} == {"12": "", "3": "Imran"}
