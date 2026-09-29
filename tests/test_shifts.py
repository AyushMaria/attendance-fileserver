"""Shifts: each person counts as on the shift whose start is closest to their
first punch, and is late after that shift's start + grace."""

from datetime import date, datetime

from attendance import StoreData


def setup(make, conn):
    make.store("mall", start="09:30", grace=10, close="21:00")
    make.user("owner", "owner")
    for name, a, b, g in (("Morning", "09:30", "18:30", 10), ("Evening", "13:00", "22:00", 5)):
        conn.execute("INSERT INTO shifts (store, name, start_time, end_time, grace_minutes) VALUES ('mall',?,?,?,?)",
                     (name, a, b, g))
    conn.commit()


def cell(conn, now, uid, day="2026-10-06"):
    d = date.fromisoformat(day)
    return StoreData(conn, "mall", d, d, now.value).cell(uid, d)


def test_morning_on_time_and_late(conn, make, now):
    setup(make, conn)
    make.punch("mall", 1, "2026-10-06 09:40:00")
    make.punch("mall", 2, "2026-10-06 09:41:00")
    assert cell(conn, now, 1).code == "P" and cell(conn, now, 1).shift.name == "Morning"
    assert cell(conn, now, 2).code == "LT"


def test_evening_arrival_is_not_late(conn, make, now):
    setup(make, conn)
    make.punch("mall", 3, "2026-10-06 12:58:00")
    make.punch("mall", 4, "2026-10-06 13:06:00")
    c3, c4 = cell(conn, now, 3), cell(conn, now, 4)
    assert (c3.code, c3.shift.name) == ("P", "Evening")
    assert (c4.code, c4.shift.name) == ("LT", "Evening")          # 5 min grace


def test_nearest_start_decides(conn, make, now):
    setup(make, conn)
    make.punch("mall", 5, "2026-10-06 11:14:00")    # 104 min after 09:30, 106 before 13:00
    make.punch("mall", 6, "2026-10-06 11:16:00")    # closer to 13:00
    assert cell(conn, now, 5).shift.name == "Morning" and cell(conn, now, 5).code == "LT"
    assert cell(conn, now, 6).shift.name == "Evening" and cell(conn, now, 6).code == "P"


def test_not_in_yet_until_last_shift_ends(conn, make, now):
    setup(make, conn)
    make.punch("mall", 1, "2026-10-07 09:30:00")
    now.set(datetime(2026, 10, 7, 21, 30))           # after store close 21:00, before 22:00
    assert cell(conn, now, 2, "2026-10-07").code == "NOT_IN_YET"
    now.set(datetime(2026, 10, 7, 22, 5))
    assert cell(conn, now, 2, "2026-10-07").code == "A"


def test_no_shifts_uses_store_times(conn, make, now):
    make.store("mall", start="09:30", grace=10)
    make.user("owner", "owner")
    make.punch("mall", 1, "2026-10-06 13:00:00")
    assert cell(conn, now, 1).code == "LT"


def test_owner_manages_shifts(app, conn, make, as_user):
    make.store("mall")
    owner = make.user("owner", "owner")
    mgr = make.user("mgr", "manager", "mall", 2, covers=["mall"])
    c = as_user(owner)
    form = {"name": "Morning", "start_time": "09:30", "end_time": "18:30", "grace_minutes": "10"}
    assert c.post("/stores/mall/shifts/add", data=form).status_code == 302
    assert c.post("/stores/mall/shifts/add", data=dict(form, name="Dup")).status_code == 302   # same start
    c.post("/stores/mall/shifts/add", data=dict(form, name="Bad", start_time="20:00", end_time="10:00"))
    rows = conn.execute("SELECT name FROM shifts").fetchall()
    assert [r[0] for r in rows] == ["Morning"]
    sid = conn.execute("SELECT id FROM shifts").fetchone()[0]
    c.post(f"/shifts/{sid}/edit", data=dict(form, grace_minutes="15"))
    assert conn.execute("SELECT grace_minutes FROM shifts").fetchone()[0] == 15
    assert as_user(mgr).post(f"/shifts/{sid}/delete").status_code == 404
    c.post(f"/shifts/{sid}/delete")
    assert conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] == 0
    assert "Add a shift" in c.get("/stores").data.decode()


def test_day_view_groups_by_shift(app, conn, make, as_user, now):
    setup(make, conn)
    make.punch("mall", 1, "2026-10-06 09:31:00")
    make.punch("mall", 2, "2026-10-06 13:02:00")
    body = as_user(make._any_user()).get("/day/mall/2026-10-06").data.decode()
    assert "Morning shift · 09:30–18:30" in body and "Evening shift · 13:00–22:00" in body
    assert body.index("Morning shift") < body.index("Evening shift")
