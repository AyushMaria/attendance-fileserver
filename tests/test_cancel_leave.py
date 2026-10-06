"""The Owner cancels a leave that was approved earlier."""

from datetime import date

import pytest

from attendance import StoreData


@pytest.fixture
def world(make):
    make.store("mall")
    w = {"owner": make.user("owner", "owner"),
         "mgr": make.user("mgr", "manager", "mall", 1, covers=("mall",)),
         "cro": make.user("cro", "cro", "mall", 101)}
    make.punch("mall", 1, "2026-08-03 09:00:00")         # the store was open
    # approved long ago (older than the 30 days Approvals lists) by the manager
    w["leave"] = make.leave("mall", 101, "2026-08-03", "2026-08-04", by=w["mgr"])
    return w


def code(conn, now, day):
    d = date.fromisoformat(day)
    return StoreData(conn, "mall", d, d, now.value).cell("101", d).code


def cancel(c, leave_id, reason="approved by mistake", back="/person/mall/101/2026-08"):
    return c.post(f"/leave/{leave_id}/revoke", data={"reason": reason, "back": back})


def test_owner_cancels_an_old_approved_leave(world, as_user, conn, now):
    assert code(conn, now, "2026-08-03") == "L"
    c = as_user(world["owner"])
    assert "Cancel leave" in c.get("/person/mall/101/2026-08").data.decode()
    r = cancel(c, world["leave"])
    assert r.status_code == 302 and r.headers["Location"].endswith("/person/mall/101/2026-08")
    row = conn.execute("SELECT status, manager_comment FROM leaves WHERE id=?", (world["leave"],)).fetchone()
    assert tuple(row) == ("cancelled", "cancelled: approved by mistake")
    assert code(conn, now, "2026-08-03") == "A"           # counted from the punches again
    assert conn.execute("SELECT action FROM audit_log ORDER BY id DESC").fetchone()[0] == "leave.revoke"
    page = c.get("/person/mall/101/2026-08").data.decode()
    assert "Cancelled" in page and "Cancel leave" not in page
    # the days are free again for a new leave
    assert c.post("/approvals/record", data={"person": "mall:101", "from": "2026-08-03",
                                             "to": "2026-08-03", "note": "x"}).status_code == 302
    assert code(conn, now, "2026-08-03") == "L"


def test_reason_required_and_only_approved(world, as_user, conn, make):
    c = as_user(world["owner"])
    cancel(c, world["leave"], reason="")
    assert conn.execute("SELECT status FROM leaves WHERE id=?", (world["leave"],)).fetchone()[0] == "approved"
    pending = make.leave("mall", 101, "2026-08-10", "2026-08-10", status="pending")
    cancel(c, pending)
    assert conn.execute("SELECT status FROM leaves WHERE id=?", (pending,)).fetchone()[0] == "pending"


def test_only_the_owner(world, as_user, conn):
    for who in ("mgr", "cro"):
        c = as_user(world[who])
        assert cancel(c, world["leave"]).status_code == 404
        assert "Cancel leave" not in c.get("/person/mall/101/2026-08").data.decode()
    assert conn.execute("SELECT status FROM leaves WHERE id=?", (world["leave"],)).fetchone()[0] == "approved"


def test_no_open_redirect(world, as_user):
    r = cancel(as_user(world["owner"]), world["leave"], back="//evil.example")
    assert r.headers["Location"].endswith("/approvals")


def test_approvals_page_offers_cancel_to_owner_withdraw_to_manager(world, as_user, make, conn):
    recent = make.leave("mall", 101, "2026-10-08", "2026-10-08", by=world["mgr"])
    conn.execute("UPDATE leaves SET decided_at=datetime('now') WHERE id=?", (recent,))
    conn.commit()
    owner = as_user(world["owner"]).get("/approvals").data.decode()
    assert "Cancel leave" in owner and "Withdraw leave" not in owner
    mgr = as_user(world["mgr"]).get("/approvals").data.decode()
    assert "Withdraw leave" in mgr and "Cancel leave" not in mgr
