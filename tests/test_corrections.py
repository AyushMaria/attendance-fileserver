"""Correcting a day's status by hand."""

from datetime import date

import pytest

from attendance import StoreData


@pytest.fixture
def world(make, now):
    make.store("mall")
    make.store("nirala")
    w = {"owner": make.user("owner", "owner"),
         "mgr": make.user("mgr", "manager", "mall", 102, covers=["mall"]),
         "cro": make.user("cro", "cro", "mall", 101)}
    make.emp("nirala", 201)
    make.punch("mall", 999, "2026-10-05 09:00:00")       # the store was open
    return w


def mark(client, person="mall:101", day="2026-10-05", code="P", note="forgot to punch"):
    return client.post("/mark", data={"person": person, "day": day, "code": code, "note": note,
                                      "back": "/calendar/mall/2026-10"})


def cell(conn, now, uid="101", day="2026-10-05", store="mall"):
    d = date.fromisoformat(day)
    return StoreData(conn, store, d, d, now.value).cell(uid, d)


def test_mark_present_and_undo(world, as_user, conn, now):
    assert cell(conn, now).code == "A"
    c = as_user(world["owner"])
    assert mark(c).status_code == 302
    got = cell(conn, now)
    assert got.code == "P" and got.manual and "forgot to punch" in got.note and "Owner" in got.note
    mark(c, code="clear", note="")
    assert cell(conn, now).code == "A"
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions[-2:] == ["day.mark", "day.clear"]


def test_mark_absent_overrides_punches(world, as_user, make, conn, now):
    make.punch("mall", 103, "2026-10-05 09:30:00")
    mark(as_user(world["owner"]), person="mall:103", code="A", note="punched for someone else")
    assert cell(conn, now, "103").code == "A"


def test_mark_on_no_data_day(world, as_user, conn, now):
    mark(as_user(world["owner"]), day="2026-10-04")        # nobody punched that day
    assert cell(conn, now, day="2026-10-04").code == "P"


def test_reason_required_and_no_future(world, as_user, conn):
    c = as_user(world["owner"])
    mark(c, note="")
    mark(c, day="2026-12-01")
    assert conn.execute("SELECT COUNT(*) FROM day_overrides").fetchone()[0] == 0


def test_permissions(world, as_user, conn):
    assert mark(as_user(world["mgr"]), person="mall:101").status_code == 404      # managers: never
    assert mark(as_user(world["mgr"]), person="mall:102").status_code == 404
    assert mark(as_user(world["cro"]), person="mall:101").status_code == 404      # CRO
    assert conn.execute("SELECT COUNT(*) FROM day_overrides").fetchone()[0] == 0
    assert mark(as_user(world["owner"]), person="mall:102").status_code == 302    # owner: anyone
    assert as_user(world["owner"]).post("/mark", data={"person": "mall:101", "day": "2026-10-05",
                                                      "code": "X", "note": "x"}).status_code == 400


def test_school_absence_marked_present_frees_a_leave(make, conn, now, as_user):
    make.store("school")
    conn.execute("UPDATE stores SET uses_weekly_offs=0, uses_comp_offs=0, leave_allowance=7, "
                 "leave_count_from='2026-10-01' WHERE store='school'")
    conn.commit()
    owner = make.user("owner", "owner")
    make.punch("school", 999, "2026-10-05 09:00:00")
    make.emp("school", 1)
    d = date(2026, 10, 5)
    sd = StoreData(conn, "school", d, d, now.value)
    assert sd.cell("1", d).code == "L" and sd.allowance("1").used == 1
    as_user(owner).post("/mark", data={"person": "school:1", "day": "2026-10-05", "code": "P",
                                       "note": "forgot to punch"})
    sd = StoreData(conn, "school", d, d, now.value)
    assert sd.cell("1", d).code == "P" and sd.allowance("1").used == 0


def test_pages_show_the_form_to_owner_only(world, as_user):
    m = as_user(world["mgr"])
    assert "&#34;edit&#34;: true" not in m.get("/calendar/mall/2026-10").data.decode()
    assert "Mark present" not in m.get("/day/mall/2026-10-05").data.decode()
    c = as_user(world["owner"])
    assert "&#34;edit&#34;: true" in c.get("/calendar/mall/2026-10").data.decode()
    assert "Mark present" in c.get("/day/mall/2026-10-05").data.decode()
    mark(c)
    assert "✎" in c.get("/calendar/mall/2026-10").data.decode()
    assert "✎" in m.get("/calendar/mall/2026-10").data.decode()      # managers still see it
