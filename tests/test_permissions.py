"""Every row of the guide's section 3 table, with one Owner, a Manager for
mall only, a Manager for mall and nirala, and a CRO at each store."""

import pytest


@pytest.fixture
def world(make, now):
    make.store("mall")
    make.store("nirala")
    w = {
        "owner": make.user("owner", "owner"),
        "mgr_mall": make.user("mgrmall", "manager", "mall", 102, covers=["mall"]),
        "mgr_both": make.user("mgrboth", "manager", "nirala", 202, covers=["mall", "nirala"]),
        "cro_mall": make.user("cromall", "cro", "mall", 101),
        "cro_nirala": make.user("cronirala", "cro", "nirala", 201),
    }
    make.emp("mall", 103)            # staff with no login
    make.punch("mall", 101, "2026-10-05 09:00:00")
    make.punch("nirala", 201, "2026-10-05 09:00:00")
    return w


PAGES = {
    "/calendar/mall/2026-10": {"owner": 200, "mgr_mall": 200, "mgr_both": 200, "cro_mall": 404, "cro_nirala": 404},
    "/calendar/nirala/2026-10": {"owner": 200, "mgr_mall": 404, "mgr_both": 200, "cro_mall": 404, "cro_nirala": 404},
    "/day/mall/2026-10-05": {"owner": 200, "mgr_mall": 200, "mgr_both": 200, "cro_mall": 404, "cro_nirala": 404},
    "/day/nirala/2026-10-05": {"owner": 200, "mgr_mall": 404, "mgr_both": 200, "cro_mall": 404, "cro_nirala": 404},
    "/person/mall/101/2026-10": {"owner": 200, "mgr_mall": 200, "mgr_both": 200, "cro_mall": 200, "cro_nirala": 404},
    "/person/nirala/201/2026-10": {"owner": 200, "mgr_mall": 404, "mgr_both": 200, "cro_mall": 404, "cro_nirala": 200},
    "/person/mall/103/2026-10": {"owner": 200, "mgr_mall": 200, "mgr_both": 200, "cro_mall": 404, "cro_nirala": 404},
    "/person/nirala/202/2026-10": {"owner": 200, "mgr_mall": 404, "mgr_both": 200, "cro_mall": 404, "cro_nirala": 404},
    "/stores": {"owner": 200, "mgr_mall": 404, "mgr_both": 404, "cro_mall": 404, "cro_nirala": 404},
    "/users": {"owner": 200, "mgr_mall": 404, "mgr_both": 404, "cro_mall": 404, "cro_nirala": 404},
    "/users/new": {"owner": 200, "mgr_mall": 404, "mgr_both": 404, "cro_mall": 404, "cro_nirala": 404},
    "/audit": {"owner": 200, "mgr_mall": 404, "mgr_both": 404, "cro_mall": 404, "cro_nirala": 404},
    "/archive": {"owner": 200, "mgr_mall": 404, "mgr_both": 404, "cro_mall": 404, "cro_nirala": 404},
    "/backup": {"owner": 200, "mgr_mall": 404, "mgr_both": 404, "cro_mall": 404, "cro_nirala": 404},
    "/staff": {"owner": 200, "mgr_mall": 200, "mgr_both": 200, "cro_mall": 404, "cro_nirala": 404},
    "/approvals": {"owner": 200, "mgr_mall": 200, "mgr_both": 200, "cro_mall": 404, "cro_nirala": 404},
    "/leave": {"owner": 404, "mgr_mall": 200, "mgr_both": 200, "cro_mall": 200, "cro_nirala": 200},
    "/account": {"owner": 200, "mgr_mall": 200, "mgr_both": 200, "cro_mall": 200, "cro_nirala": 200},
}


@pytest.mark.parametrize("path", list(PAGES))
def test_page_access(world, as_user, path):
    for who, expected in PAGES[path].items():
        got = as_user(world[who]).get(path).status_code
        assert got == expected, f"{who} {path}: {got}"


def test_signed_out_goes_to_sign_in(app, world):
    for path in PAGES:
        r = app.test_client().get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], path


def test_cro_home_is_own_calendar(world, as_user):
    r = as_user(world["cro_mall"]).get("/")
    assert r.status_code == 302 and "/person/mall/101/" in r.headers["Location"]


def test_staff_page_lists_only_own_stores(world, as_user):
    body = as_user(world["mgr_mall"]).get("/staff").data.decode()
    assert "P101" in body and "P201" not in body


# ----------------------------------------------------------------- leave

def test_cro_request_always_own_person(world, as_user, conn):
    as_user(world["cro_mall"]).post("/leave", data={
        "from": "2026-10-10", "to": "2026-10-10", "reason": "x",
        "store": "nirala", "user_id": "201", "person": "nirala:201"})
    rows = conn.execute("SELECT store, user_id FROM leaves").fetchall()
    assert [tuple(r) for r in rows] == [("mall", "101")]


def test_cro_cannot_cancel_someone_elses_leave(world, as_user, make, conn):
    lid = make.leave("mall", 103, "2026-10-10", "2026-10-10", status="pending")
    r = as_user(world["cro_mall"]).post(f"/leave/{lid}/cancel")
    assert r.status_code == 404
    assert conn.execute("SELECT status FROM leaves WHERE id=?", (lid,)).fetchone()[0] == "pending"


def test_cro_can_cancel_own_pending_only(world, as_user, make, conn):
    pending = make.leave("mall", 101, "2026-10-10", "2026-10-10", status="pending")
    approved = make.leave("mall", 101, "2026-10-12", "2026-10-12", status="approved")
    c = as_user(world["cro_mall"])
    c.post(f"/leave/{pending}/cancel")
    c.post(f"/leave/{approved}/cancel")
    got = dict(conn.execute("SELECT id, status FROM leaves").fetchall())
    assert got == {pending: "cancelled", approved: "approved"}


def decide(client, lid, decision="approve"):
    return client.post(f"/approvals/{lid}/decide", data={"decision": decision})


def test_manager_cannot_approve_own_leave(world, as_user, make, conn):
    lid = make.leave("mall", 102, "2026-10-10", "2026-10-10", status="pending")
    assert decide(as_user(world["mgr_mall"]), lid).status_code == 404
    assert decide(as_user(world["mgr_both"]), lid).status_code == 404     # another manager
    assert decide(as_user(world["owner"]), lid).status_code == 302
    assert conn.execute("SELECT status FROM leaves WHERE id=?", (lid,)).fetchone()[0] == "approved"


def test_manager_approves_cro_in_own_store_only(world, as_user, make, conn):
    mall = make.leave("mall", 101, "2026-10-10", "2026-10-10", status="pending")
    nirala = make.leave("nirala", 201, "2026-10-10", "2026-10-10", status="pending")
    c = as_user(world["mgr_mall"])
    assert decide(c, mall).status_code == 302
    assert decide(c, nirala).status_code == 404
    assert decide(as_user(world["cro_nirala"]), nirala).status_code == 404


def test_pending_list_excludes_own_and_other_stores(world, as_user, make):
    make.leave("mall", 102, "2026-10-10", "2026-10-10", status="pending", comment="MINE")
    make.leave("nirala", 201, "2026-10-10", "2026-10-10", status="pending", comment="NIRALA")
    make.leave("mall", 101, "2026-10-10", "2026-10-10", status="pending", comment="CRO-MALL")
    body = as_user(world["mgr_mall"]).get("/approvals").data.decode()
    assert "CRO-MALL" in body and "MINE" not in body and "NIRALA" not in body
    owner = as_user(world["owner"]).get("/approvals").data.decode()
    assert all(x in owner for x in ("MINE", "NIRALA", "CRO-MALL"))


def test_manager_cannot_record_own_leave_or_other_store(world, as_user, conn):
    c = as_user(world["mgr_mall"])
    data = {"from": "2026-10-10", "to": "2026-10-10", "note": "x"}
    assert c.post("/approvals/record", data=dict(data, person="mall:102")).status_code == 404
    assert c.post("/approvals/record", data=dict(data, person="nirala:201")).status_code == 404
    assert c.post("/approvals/record", data=dict(data, person="mall:103")).status_code == 302
    row = conn.execute("SELECT status, created_by, decided_by FROM leaves").fetchone()
    assert tuple(row) == ("approved", world["mgr_mall"], world["mgr_mall"])


def test_withdraw_needs_reason_and_permission(world, as_user, make, conn):
    lid = make.leave("mall", 101, "2026-10-10", "2026-10-10")
    assert as_user(world["mgr_both"]).post(f"/approvals/{lid}/withdraw", data={"reason": ""}).status_code == 302
    assert conn.execute("SELECT status FROM leaves WHERE id=?", (lid,)).fetchone()[0] == "approved"
    as_user(world["mgr_both"]).post(f"/approvals/{lid}/withdraw", data={"reason": "stock count"})
    assert conn.execute("SELECT status FROM leaves WHERE id=?", (lid,)).fetchone()[0] == "rejected"
    assert as_user(world["cro_nirala"]).post(f"/approvals/{lid}/withdraw",
                                             data={"reason": "x"}).status_code == 404


# ----------------------------------------------------------------- comp-offs, weekly offs

def adjust(client, kind, person, day="2026-10-04"):
    return client.post(f"/comp/{kind}", data={"person": person, "day": day, "note": "why"})


def test_comp_adjust_permissions(world, as_user, conn):
    assert adjust(as_user(world["mgr_mall"]), "grant", "mall:102").status_code == 404   # self
    assert adjust(as_user(world["mgr_mall"]), "grant", "nirala:201").status_code == 404  # other store
    assert adjust(as_user(world["mgr_both"]), "grant", "mall:102").status_code == 404    # a manager
    assert adjust(as_user(world["cro_mall"]), "grant", "mall:101").status_code == 404    # CRO
    assert adjust(as_user(world["mgr_mall"]), "grant", "mall:101").status_code == 302
    assert adjust(as_user(world["owner"]), "grant", "mall:102").status_code == 302
    rows = conn.execute("SELECT store, user_id, kind FROM comp_adjustments ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("mall", "101", "grant"), ("mall", "102", "grant")]


def test_comp_adjust_needs_reason_and_past_day(world, as_user, conn):
    c = as_user(world["owner"])
    c.post("/comp/grant", data={"person": "mall:101", "day": "2026-10-04", "note": ""})
    c.post("/comp/grant", data={"person": "mall:101", "day": "2026-12-01", "note": "x"})
    assert conn.execute("SELECT COUNT(*) FROM comp_adjustments").fetchone()[0] == 0


def set_wo(client, person, days=("6",), eff="2026-10-01"):
    return client.post("/staff/weekly-off", data={"person": person, "days": list(days), "from": eff})


def test_weekly_off_permissions(world, as_user, conn):
    assert set_wo(as_user(world["mgr_mall"]), "mall:102").status_code == 404     # self
    assert set_wo(as_user(world["mgr_mall"]), "nirala:201").status_code == 404   # other store
    assert set_wo(as_user(world["cro_mall"]), "mall:101").status_code == 404
    assert set_wo(as_user(world["mgr_mall"]), "mall:101").status_code == 302
    assert set_wo(as_user(world["owner"]), "mall:102").status_code == 302
    assert conn.execute("SELECT COUNT(*) FROM weekly_offs").fetchone()[0] == 2


def test_weekly_off_none_and_history_kept(world, as_user, conn):
    c = as_user(world["owner"])
    set_wo(c, "mall:101", ("6",), "2026-09-01")
    c.post("/staff/weekly-off", data={"person": "mall:101", "none": "on", "from": "2026-10-01"})
    rows = conn.execute("SELECT weekdays, effective_from FROM weekly_offs ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("6", "2026-09-01"), ("", "2026-10-01")]


# ----------------------------------------------------------------- owner-only actions

def test_owner_only_posts(world, as_user):
    for who in ("mgr_mall", "cro_mall"):
        c = as_user(world[who])
        assert c.post("/stores/add", data={"store": "x"}).status_code == 404
        assert c.post("/stores/mall/key", data={"action": "create"}).status_code == 404
        assert c.post("/settings/comp", data={}).status_code == 404
        assert c.post(f"/users/{world['owner']}/deactivate").status_code == 404
