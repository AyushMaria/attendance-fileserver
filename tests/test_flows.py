"""End-to-end flows through the pages, speed, and two writers at once."""

import multiprocessing
import time
from datetime import date, datetime, timedelta

import db


def test_owner_sets_up_store_and_accounts(app, conn, make, as_user):
    owner = make.user("boss", "owner")
    c = as_user(owner)
    r = c.post("/stores/add", data={"store": "mall", "display_name": "Mall", "start_time": "10:00",
                                    "grace_minutes": "10", "close_time": "22:00"})
    assert r.status_code == 302
    make.emp("mall", 101, "Aditi")
    make.emp("mall", 102, "Rahul")
    r = c.post("/users/new", data={"username": "rahul.m", "display_name": "Rahul", "role": "manager",
                                   "person": "mall:102", "stores": ["mall"], "password": ""})
    assert b"shown only this once" in r.data
    r = c.post("/users/new", data={"username": "aditi", "display_name": "Aditi", "role": "cro",
                                   "person": "mall:101", "password": "aditi-pass-1"})
    assert b"Account created" in r.data
    # one login per person
    r = c.post("/users/new", data={"username": "aditi2", "display_name": "A", "role": "cro",
                                   "person": "mall:101", "password": "aditi-pass-1"})
    assert b"already has a login" in r.data
    rows = conn.execute("SELECT username, role, emp_store, emp_user_id FROM users ORDER BY id").fetchall()
    assert [tuple(r) for r in rows][1:] == [("rahul.m", "manager", "mall", "102"),
                                            ("aditi", "cro", "mall", "101")]
    assert [r[0] for r in conn.execute("SELECT store FROM user_stores")] == ["mall"]


def test_leave_request_to_approval_to_calendar(app, conn, make, as_user, now):
    make.store("mall")
    owner = make.user("boss", "owner")
    mgr = make.user("rahul", "manager", "mall", 102, covers=["mall"])
    cro = make.user("aditi", "cro", "mall", 101)
    make.setting("comp_from", "2026-09-01")
    make.weekly_off("mall", 101, "6")
    for n in range(1, 8):
        make.punch("mall", 102, f"2026-10-0{n} 09:00:00")
    make.punch("mall", 101, "2026-10-04 10:00:00")      # worked Sunday 4 Oct (weekly off)
    make.punch("mall", 101, "2026-10-04 18:00:00")

    c = as_user(cro)
    r = c.get("/leave/preview?from=2026-10-05&to=2026-10-06")
    assert "2 working days - 1 covered by comp-off (worked Sun 4 Oct), 1 leave" in r.get_json()["text"]
    r = c.post("/leave", data={"from": "2026-10-05", "to": "2026-10-06", "reason": "Sister's wedding"})
    assert r.status_code == 302
    r = c.post("/leave", data={"from": "2026-10-06", "to": "2026-10-06", "reason": "again"})
    assert b"overlaps a pending leave" in r.data

    m = as_user(mgr)
    page = m.get("/approvals").data.decode()
    assert "Sister&#39;s wedding" in page and "1 covered by comp-off" in page
    lid = conn.execute("SELECT id FROM leaves").fetchone()[0]
    m.post(f"/approvals/{lid}/decide", data={"decision": "approve", "comment": "Enjoy"})

    month = m.get("/calendar/mall/2026-10").data.decode()
    assert "k-CO" in month and "k-L" in month
    person = c.get("/person/mall/101/2026-10").data.decode()
    assert "used for leave on Mon 5 Oct" in person
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert "leave.request" in actions and "leave.approve" in actions


def test_leave_request_checks(app, conn, make, as_user):
    make.store("mall")
    cro = make.user("aditi", "cro", "mall", 101)
    c = as_user(cro)
    assert b"before the start date" in c.post("/leave", data={"from": "2026-10-06", "to": "2026-10-05", "reason": "x"}).data
    assert b"at most 31 days" in c.post("/leave", data={"from": "2026-10-01", "to": "2027-10-01", "reason": "x"}).data
    assert b"at most 300" in c.post("/leave", data={"from": "2026-10-01", "to": "2026-10-01", "reason": "x" * 301}).data
    # past dates are fine
    assert c.post("/leave", data={"from": "2026-09-01", "to": "2026-09-01", "reason": "fever"}).status_code == 302


def test_day_view_timeline_and_not_in(app, make, as_user, now):
    make.store("mall")
    owner = make.user("boss", "owner")
    make.emp("mall", 103, "Karan")
    make.punch("mall", 101, "2026-10-07 08:52:00")
    make.punch("mall", 101, "2026-10-07 11:05:00")
    make.punch("mall", 102, "2026-10-07 09:14:00")
    body = as_user(owner).get("/day/mall/2026-10-07").data.decode()
    assert "<svg" in body and "08:52 – 11:05" in body and "no check-out" in body
    assert "Karan" in body and "Not in yet" in body
    assert 'http-equiv="refresh"' in body


def test_stores_page_and_settings(app, conn, make, as_user):
    make.store("mall")
    owner = make.user("boss", "owner")
    c = as_user(owner)
    c.post("/settings/comp", data={"comp_min_hours": "5", "comp_window_days": "20", "comp_from": "2026-10-01"})
    assert db.get_setting(conn, "comp_from") == "2026-10-01"
    assert db.get_setting(conn, "comp_min_hours") == "5"
    c.post("/stores/mall/edit", data={"display_name": "Mall", "start_time": "10:00",
                                      "grace_minutes": "5", "close_time": "22:30"})
    row = conn.execute("SELECT * FROM stores").fetchone()
    assert (row["start_time"], row["grace_minutes"], row["close_time"]) == ("10:00", 5, "22:30")
    assert "no sync key yet" in c.get("/stores").data.decode()


def test_backup_is_a_database(app, make, as_user, tmp_path):
    owner = make.user("boss", "owner")
    r = as_user(owner).get("/backup")
    assert r.status_code == 200 and r.data[:15] == b"SQLite format 3"


def test_archive_lists_and_downloads(app, make, as_user, tmp_path):
    (tmp_path / "mall").mkdir()
    (tmp_path / "mall" / "attendance_2026-09-01.xlsx").write_bytes(b"xlsx")
    owner = make.user("boss", "owner")
    c = as_user(owner)
    page = c.get("/archive").data.decode()
    assert "attendance_2026-09-01.xlsx" in page and ".internal" not in page
    assert c.get("/archive/mall/attendance_2026-09-01.xlsx").data == b"xlsx"
    assert c.get("/archive/.internal/attendance.db").status_code == 404
    assert c.get("/archive/mall/..%2F.internal%2Fattendance.db").status_code == 404


def test_month_view_speed(app, conn, make, as_user, now):
    """40 people, a full month, about 4,000 punches: under half a second."""
    make.store("mall")
    owner = make.user("boss", "owner")
    make.setting("comp_from", "2026-06-01")
    rows = []
    for uid in range(1, 41):
        make.emp("mall", uid)
        make.weekly_off("mall", uid, str(uid % 7))
        for day in range(1, 31):
            for ts in ("09:0%d:00" % (uid % 10), "13:00:00", "14:00:00", "20:00:00"):
                rows.append(("mall", str(uid), f"2026-09-{day:02d}", f"2026-09-{day:02d} {ts}", 0, "x"))
    conn.executemany("INSERT OR IGNORE INTO punches VALUES (?,?,?,?,?,?)", rows)
    for uid in range(1, 41, 3):
        make.leave("mall", uid, "2026-09-10", "2026-09-11")
    conn.commit()
    now.set(datetime(2026, 9, 30, 21, 30))
    c = as_user(owner)
    c.get("/calendar/mall/2026-09")          # warm up templates
    start = time.perf_counter()
    r = c.get("/calendar/mall/2026-09")
    took = time.perf_counter() - start
    assert r.status_code == 200
    print(f"month view took {took:.3f}s")
    assert took < 0.5, f"month view took {took:.2f}s"


def _writer(path, store, n):
    conn = db.connect(path)
    for i in range(n):
        conn.execute("INSERT INTO punches VALUES (?,?,?,?,?,?)",
                     (store, "1", "2026-10-01", f"2026-10-01 09:{i // 60:02d}:{i % 60:02d}", 0, "x"))
        conn.commit()
    conn.close()


def test_two_processes_writing_at_once(app):
    path = app.config["DB_PATH"]
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_writer, args=(path, s, 300)) for s in ("a", "b")]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    assert all(p.exitcode == 0 for p in procs)
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM punches").fetchone()[0] == 600
