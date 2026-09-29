"""Regression tests for the independent review's findings."""

from datetime import datetime

import pytest

KEY = "mall-key-" + "x" * 30


@pytest.fixture
def world(make, now):
    make.store("mall", key=KEY)
    return {"owner": make.user("owner", "owner"),
            "mgr": make.user("mgr", "manager", "mall", 102, covers=["mall"]),
            "cro": make.user("cro", "cro", "mall", 101)}


def test_far_future_leave_refused(world, as_user, conn):
    c = as_user(world["cro"])
    r = c.post("/leave", data={"from": "9999-12-25", "to": "9999-12-31", "reason": "x"})
    assert b"within a year" in r.data
    assert conn.execute("SELECT COUNT(*) FROM leaves").fetchone()[0] == 0
    assert c.get("/leave/preview?from=9999-12-01&to=9999-12-31").get_json() == {"text": ""}
    assert as_user(world["mgr"]).get("/approvals").status_code == 200


def test_impossible_punch_dates_skipped_not_stored(app, conn, world):
    body = {"staff": [{"user_id": "101", "name": "A"}],
            "punches": [{"user_id": "101", "ts": "0999-10-05 09:00:00"},
                        {"user_id": "101", "ts": "2031-01-01 09:00:00"},
                        {"user_id": "101", "ts": "2026-10-05 09:00:00"}]}
    r = app.test_client().post("/api/sync", json=body, headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200 and r.get_json()["new_punches"] == 1
    assert "impossible dates were skipped" in conn.execute("SELECT clock_warning FROM stores").fetchone()[0]


def test_huge_punch_code_refused_cleanly(app, world):
    for code in (1e400, 10 ** 30):
        body = {"punches": [{"user_id": "101", "ts": "2026-10-05 09:00:00", "punch": code}]}
        r = app.test_client().post("/api/sync", data=__import__("json").dumps(body).replace("Infinity", "1e400"),
                                   content_type="application/json",
                                   headers={"Authorization": f"Bearer {KEY}"})
        assert r.status_code == 400


def test_ancient_comp_grant_refused(world, as_user, conn):
    r = as_user(world["mgr"]).post("/comp/grant", data={"person": "mall:101", "day": "0001-01-02", "note": "x"})
    assert r.status_code == 302
    assert conn.execute("SELECT COUNT(*) FROM comp_adjustments").fetchone()[0] == 0
    assert as_user(world["owner"]).get("/calendar/mall/2026-10").status_code == 200


def test_ancient_comp_start_date_refused(world, as_user, conn):
    import db
    as_user(world["owner"]).post("/settings/comp", data={"comp_min_hours": "4", "comp_window_days": "30",
                                                         "comp_from": "0001-01-01"})
    assert db.get_setting(conn, "comp_from") == ""


@pytest.mark.parametrize("path", ["/calendar/mall/9999-12", "/calendar/mall/0001-01", "/day/mall/0001-01-01",
                                  "/day/mall/9999-12-31", "/person/mall/101/9999-12"])
def test_extreme_dates_in_addresses_are_404(world, as_user, path):
    assert as_user(world["owner"]).get(path).status_code == 404


def test_comp_off_not_used_on_no_data_today(conn, make, now):
    from datetime import date
    from attendance import StoreData
    make.store("mall", close="21:00")
    make.user("owner", "owner")
    make.weekly_off("mall", 101, "6")
    make.setting("comp_from", "2026-09-01")
    make.punch("mall", 101, "2026-10-04 09:00:00")      # worked Sunday
    make.punch("mall", 101, "2026-10-04 18:00:00")
    make.leave("mall", 101, "2026-10-07", "2026-10-07")
    now.set(datetime(2026, 10, 7, 22, 0))                # after closing; nobody punched today
    data = StoreData(conn, "mall", date(2026, 10, 7), date(2026, 10, 7), now.value)
    assert data.cell("101", date(2026, 10, 7)).code == "NODATA"
    assert [c.status for c in data.credits("101")] == ["available"]


def test_failed_logins_counted_in_database(app, conn, make):
    uid = make.user("aditi", "owner")
    conn.execute("UPDATE users SET failed_logins=3 WHERE id=?", (uid,))
    conn.commit()
    app.test_client().post("/login", data={"username": "aditi", "password": "nope-nope"})
    assert conn.execute("SELECT failed_logins FROM users WHERE id=?", (uid,)).fetchone()[0] == 4
