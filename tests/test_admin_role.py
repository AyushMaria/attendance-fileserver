"""The Admin role: every location's attendance, view only."""

import sqlite3

import pytest

import db


@pytest.fixture
def world(make, now):
    make.store("mall")
    make.store("school")
    w = {"owner": make.user("owner", "owner"), "admin": make.user("audit", "admin"),
         "cro": make.user("cro", "cro", "mall", 101)}
    make.punch("mall", 101, "2026-10-05 09:00:00")
    make.punch("school", 7, "2026-10-05 09:00:00")
    w["leave"] = make.leave("mall", 101, "2026-10-10", "2026-10-10", status="pending")
    return w


VIEW = ["/", "/calendar/mall/2026-10", "/calendar/school/2026-10", "/day/mall/2026-10-05",
        "/day/school/2026-10-05", "/person/mall/101/2026-10", "/person/school/7/2026-10", "/account",
        "/calendar/mall", "/day/school"]
NO = ["/approvals", "/stores", "/users", "/users/new", "/audit", "/archive", "/backup",
      "/stores/school/tally", "/stores/mall/names", "/leave"]


def test_admin_sees_every_calendar(world, as_user):
    c = as_user(world["admin"])
    for path in VIEW:
        assert c.get(path).status_code in (200, 302), path
    assert c.get("/calendar/mall/2026-10").status_code == 200
    home = c.get("/").data.decode()
    assert "View only" in home and "Mall" in home and "School" in home


def test_admin_shut_out_of_everything_else(world, as_user):
    c = as_user(world["admin"])
    for path in NO:
        assert c.get(path).status_code == 404, path
    posts = [("/approvals/%d/decide" % world["leave"], {"decision": "approve"}),
             ("/approvals/record", {"person": "mall:101", "from": "2026-10-11", "to": "2026-10-11"}),
             ("/mark", {"person": "mall:101", "day": "2026-10-05", "code": "A", "note": "x"}),
             ("/comp/grant", {"person": "mall:101", "day": "2026-10-04", "note": "x"}),
             ("/staff/weekly-off", {"person": "mall:101", "days": ["6"], "from": "2026-10-01"}),
             ("/stores/mall/edit", {"display_name": "X", "start_time": "09:00", "grace_minutes": "5",
                                    "close_time": "21:00"}),
             ("/stores/mall/policy", {}), ("/stores/mall/key", {"action": "create"}),
             ("/users/bulk-cro", {}), ("/people/hide", {"person": "mall:101"}),
             ("/stores/add", {"store": "x"})]
    for path, data in posts:
        assert c.post(path, data=data).status_code == 404, path


def test_admin_page_has_no_edit_controls(world, as_user):
    c = as_user(world["admin"])
    month = c.get("/calendar/mall/2026-10").data.decode()
    assert "&#34;edit&#34;: true" not in month
    nav = c.get("/").data.decode()
    for word in (">Approvals", ">Stores", ">Accounts", ">Activity", ">Archive"):
        assert word not in nav
    assert "Mark present" not in c.get("/day/mall/2026-10-05").data.decode()


def test_owner_creates_admin_without_a_device_person(world, as_user, conn):
    r = as_user(world["owner"]).post("/users/new", data={
        "username": "accountant", "display_name": "Accountant", "role": "admin", "password": "a-long-pass"})
    assert b"Account created" in r.data
    row = conn.execute("SELECT role, emp_user_id FROM users WHERE username='accountant'").fetchone()
    assert tuple(row) == ("admin", None)


FIRST_RELEASE_USERS = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('owner','manager','cro')),
    emp_store TEXT, emp_user_id TEXT, active INTEGER NOT NULL DEFAULT 1,
    session_version INTEGER NOT NULL DEFAULT 1, failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT, created_at TEXT NOT NULL, last_login_at TEXT, last_login_ip TEXT,
    password_changed_at TEXT NOT NULL,
    CHECK ((role = 'owner') = (emp_user_id IS NULL)), UNIQUE (emp_store, emp_user_id));
CREATE TABLE user_stores (user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    store TEXT NOT NULL, PRIMARY KEY (user_id, store));
CREATE TABLE leaves (id INTEGER PRIMARY KEY, store TEXT NOT NULL, user_id TEXT NOT NULL,
    start_date TEXT NOT NULL, end_date TEXT NOT NULL, staff_comment TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, manager_comment TEXT NOT NULL DEFAULT '',
    created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
    decided_by INTEGER REFERENCES users(id), decided_at TEXT);
INSERT INTO users VALUES (1,'ayush','Ayush','h1','owner',NULL,NULL,1,3,0,NULL,'x',NULL,NULL,'x');
INSERT INTO users VALUES (7,'imran3','Imran','h7','manager','mall','3',1,1,0,NULL,'x',NULL,NULL,'x');
INSERT INTO users VALUES (9,'saif4','Saif','h9','cro','mall','4',1,1,0,NULL,'x',NULL,NULL,'x');
INSERT INTO user_stores VALUES (7,'mall');
INSERT INTO leaves VALUES (1,'mall','4','2026-10-01','2026-10-01','x','approved','',9,'x',7,'x');
"""


def test_old_users_table_rebuilt_keeping_everything(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript(FIRST_RELEASE_USERS)
    c.commit()
    c.close()
    db.init_db(str(path))
    db.init_db(str(path))                                   # a second start does nothing
    conn = db.connect(str(path))
    rows = [tuple(r) for r in conn.execute(
        "SELECT id, username, password_hash, role, emp_store, emp_user_id, session_version, "
        "must_change_password FROM users ORDER BY id")]
    assert rows == [(1, "ayush", "h1", "owner", None, None, 3, 0),
                    (7, "imran3", "h7", "manager", "mall", "3", 1, 0),
                    (9, "saif4", "h9", "cro", "mall", "4", 1, 0)]
    assert [tuple(r) for r in conn.execute("SELECT * FROM user_stores")] == [(7, "mall")]
    assert conn.execute("SELECT created_by, decided_by FROM leaves").fetchone()[:] == (9, 7)
    conn.execute("INSERT INTO users (username, display_name, password_hash, role, created_at, "
                 "password_changed_at) VALUES ('acc','Acc','h','admin','x','x')")
    conn.commit()
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError):              # a CRO still needs a device person
        conn.execute("INSERT INTO users (username, display_name, password_hash, role, created_at, "
                     "password_changed_at) VALUES ('x','X','h','cro','x','x')")


def test_external_manager(world, as_user, conn, make):
    make.store("nirala")
    r = as_user(world["owner"]).post("/users/new", data={
        "username": "omkar", "display_name": "Omkar", "role": "manager", "person": "",
        "stores": ["mall", "nirala"], "password": "a-long-pass"})
    assert b"Account created" in r.data
    row = conn.execute("SELECT id, role, emp_user_id FROM users WHERE username='omkar'").fetchone()
    assert (row["role"], row["emp_user_id"]) == ("manager", None)
    c = as_user(row["id"])
    assert c.get("/leave").status_code == 404                     # no leave of his own
    assert ">My leave" not in c.get("/calendar/mall/2026-10").data.decode()
    assert c.get("/calendar/nirala/2026-10").status_code == 200
    assert c.get("/calendar/school/2026-10").status_code == 404   # only his stores
    assert c.post(f"/approvals/{world['leave']}/decide", data={"decision": "approve"}).status_code == 302
    assert conn.execute("SELECT status FROM leaves WHERE id=?", (world["leave"],)).fetchone()[0] == "approved"
    # a CRO still needs a device person
    r = as_user(world["owner"]).post("/users/new", data={
        "username": "nobody", "display_name": "N", "role": "cro", "person": "", "password": "a-long-pass"})
    assert b"Choose the person" in r.data


def test_admin_era_table_upgraded_for_external_managers(tmp_path):
    path = tmp_path / "v2.db"
    c = sqlite3.connect(path)
    c.executescript(FIRST_RELEASE_USERS.replace(
        "CHECK (role IN ('owner','manager','cro'))", "CHECK (role IN ('owner','admin','manager','cro'))").replace(
        "CHECK ((role = 'owner') = (emp_user_id IS NULL))", "CHECK ((role IN ('owner','admin')) = (emp_user_id IS NULL))"))
    c.commit()
    c.close()
    db.init_db(str(path))
    conn = db.connect(str(path))
    assert [r[0] for r in conn.execute("SELECT username FROM users ORDER BY id")] == ["ayush", "imran3", "saif4"]
    conn.execute("INSERT INTO users (username, display_name, password_hash, role, created_at, "
                 "password_changed_at) VALUES ('omkar','Omkar','h','manager','x','x')")
    conn.commit()
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_admin_limited_to_ticked_stores(world, as_user, conn, make):
    """e.g. Omkar: an area manager who only watches Mall and Nirala."""
    make.store("nirala")
    r = as_user(world["owner"]).post("/users/new", data={
        "username": "omkar", "display_name": "Omkar", "role": "admin", "person": "",
        "stores": ["mall", "nirala"], "password": "a-long-pass"})
    assert b"Account created" in r.data
    row = conn.execute("SELECT id, role, emp_user_id FROM users WHERE username='omkar'").fetchone()
    assert (row["role"], row["emp_user_id"]) == ("admin", None)
    c = as_user(row["id"])
    assert c.get("/calendar/mall/2026-10").status_code == 200
    assert c.get("/calendar/nirala/2026-10").status_code == 200
    assert c.get("/calendar/school/2026-10").status_code == 404
    assert c.get("/person/school/7/2026-10").status_code == 404
    home = c.get("/").data.decode()
    assert "Mall" in home and "School" not in home
    for path in ("/approvals", "/leave"):
        assert c.get(path).status_code == 404, path
    for path, data in [("/approvals/%d/decide" % world["leave"], {"decision": "approve"}),
                       ("/approvals/record", {"person": "mall:101", "from": "2026-10-11", "to": "2026-10-11"}),
                       ("/comp/grant", {"person": "mall:101", "day": "2026-10-04", "note": "x"}),
                       ("/staff/weekly-off", {"person": "mall:101", "days": ["6"], "from": "2026-10-01"})]:
        assert c.post(path, data=data).status_code == 404, path
    assert conn.execute("SELECT status FROM leaves WHERE id=?", (world["leave"],)).fetchone()[0] == "pending"
    # an Admin with no stores ticked still sees everything
    assert as_user(world["admin"]).get("/calendar/school/2026-10").status_code == 200


def test_manager_switched_to_admin(world, as_user, conn, make):
    make.store("nirala")
    o = as_user(world["owner"])
    o.post("/users/new", data={"username": "omkar", "display_name": "Omkar", "role": "manager",
                               "person": "", "stores": ["mall", "nirala"], "password": "a-long-pass"})
    uid = conn.execute("SELECT id FROM users WHERE username='omkar'").fetchone()[0]
    assert o.post(f"/users/{uid}/edit", data={"display_name": "Omkar", "role": "admin", "person": "",
                                              "stores": ["mall", "nirala"]}).status_code == 302
    assert conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()[0] == "admin"
    assert sorted(r[0] for r in conn.execute("SELECT store FROM user_stores WHERE user_id=?", (uid,))) \
        == ["mall", "nirala"]
    assert as_user(uid).get("/approvals").status_code == 404


def test_admin_sees_weekly_offs_of_their_stores_only(world, as_user, conn, make):
    make.store("nirala")
    make.punch("nirala", 5, "2026-10-05 09:00:00")
    conn.execute("INSERT INTO weekly_offs (store, user_id, weekdays, effective_from, set_by, set_at) "
                 "VALUES ('mall','101','1','2026-09-01',?, 'x')", (world["owner"],))
    conn.execute("INSERT INTO user_stores (user_id, store) VALUES (?, 'mall')", (world["admin"],))
    conn.commit()
    c = as_user(world["admin"])
    page = c.get("/staff").data.decode()
    assert "Mall" in page and "Tue" in page and "Nirala" not in page and "School" not in page
    assert "/staff/weekly-off" not in page and "Change</summary>" not in page and "Owner sets this" not in page
    assert "Login</th>" not in page and ">Weekly offs" in c.get("/").data.decode()
    assert c.post("/staff/weekly-off", data={"person": "mall:101", "days": ["6"],
                                             "from": "2026-10-01"}).status_code == 404
