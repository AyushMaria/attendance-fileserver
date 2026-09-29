"""The store's rules on top of day_status: late, not in yet, no data,
future days, weekly-off history, the IST 'today' and comp-offs in the
calendar."""

from datetime import date, datetime, timezone

import clock
from attendance import StoreData


def cell(conn, now, uid, day, store="mall"):
    day = date.fromisoformat(day)
    return StoreData(conn, store, day, day, now.value).cell(uid, day)


def setup(make):
    make.store("mall", start="09:00", grace=10, close="21:00")
    make.user("owner", "owner")
    for uid in (101, 102, 103):
        make.emp("mall", uid)
        make.weekly_off("mall", uid, "6")        # Sunday


def test_late_after_grace(conn, make, now):
    setup(make)
    make.punch("mall", 101, "2026-10-05 09:10:59")
    make.punch("mall", 102, "2026-10-05 09:11:00")
    assert cell(conn, now, 101, "2026-10-05").code == "P"
    assert cell(conn, now, 102, "2026-10-05").code == "LT"


def test_not_in_yet_before_close_absent_after(conn, make, now):
    setup(make)
    make.punch("mall", 102, "2026-10-07 09:00:00")
    now.set(datetime(2026, 10, 7, 11, 0))
    assert cell(conn, now, 101, "2026-10-07").code == "NOT_IN_YET"
    now.set(datetime(2026, 10, 7, 22, 0))
    assert cell(conn, now, 101, "2026-10-07").code == "A"


def test_today_before_anyone_punched_is_not_in_yet(conn, make, now):
    setup(make)
    now.set(datetime(2026, 10, 7, 8, 30))
    assert cell(conn, now, 101, "2026-10-07").code == "NOT_IN_YET"


def test_nobody_punched_is_no_data(conn, make, now):
    setup(make)
    assert cell(conn, now, 101, "2026-10-05").code == "NODATA"
    make.punch("mall", 102, "2026-10-05 09:00:00")
    assert cell(conn, now, 101, "2026-10-05").code == "A"


def test_future_is_blank(conn, make, now):
    setup(make)
    assert cell(conn, now, 101, "2026-10-08").code is None


def test_single_punch_is_present_with_no_checkout(conn, make, now):
    setup(make)
    make.punch("mall", 101, "2026-10-05 09:00:00")
    c = cell(conn, now, 101, "2026-10-05")
    assert c.code == "P" and c.no_checkout and c.last is None


def test_weekly_off_changing_over_time(conn, make, now):
    make.store("mall")
    make.user("owner", "owner")
    make.weekly_off("mall", 101, "6", "2026-01-01")
    make.weekly_off("mall", 101, "1", "2026-10-01")
    for day in ("2026-09-27", "2026-10-04", "2026-10-06"):
        make.punch("mall", 999, f"{day} 09:00:00")     # the store was open
    assert cell(conn, now, 101, "2026-09-27").code == "WO"   # Sunday, old setting
    assert cell(conn, now, 101, "2026-10-04").code == "A"    # Sunday, no longer off
    assert cell(conn, now, 101, "2026-10-06").code == "WO"   # Tuesday, new setting


def test_today_uses_india_time(monkeypatch):
    # 02:00 IST on 29 Sep is 20:30 UTC on 28 Sep: today must be 29 Sep
    fixed = datetime(2026, 9, 28, 20, 30, tzinfo=timezone.utc)

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(clock, "datetime", FakeDateTime)
    assert clock.today() == date(2026, 9, 29)
    assert clock.now_local().hour == 2


# ----------------------------------------------------------------- comp-offs

def comp_setup(make):
    setup(make)
    make.setting("comp_from", "2026-09-01")
    # store open every day from 20 Sep to 7 Oct
    for n in range(20, 31):
        make.punch("mall", 103, f"2026-09-{n} 09:00:00")
    for n in range(1, 8):
        make.punch("mall", 103, f"2026-10-0{n} 09:00:00")


def worked_sunday(make, uid=101, day="2026-09-27", hours=(9, 18)):
    make.punch("mall", uid, f"{day} {hours[0]:02d}:00:00")
    make.punch("mall", uid, f"{day} {hours[1]:02d}:00:00")


def test_covered_leave_is_co_and_worked_off_is_plus(conn, make, now):
    comp_setup(make)
    worked_sunday(make)
    make.leave("mall", 101, "2026-10-02", "2026-10-02", comment="Doctor")
    c = cell(conn, now, 101, "2026-10-02")
    assert c.code == "CO" and "Sun 27 Sep" in c.comp_note
    w = cell(conn, now, 101, "2026-09-27")
    assert w.code == "P" and w.comp_earned and "covers leave on Fri 2 Oct" in w.comp_note


def test_pending_leave_never_uses_comp_off(conn, make, now):
    comp_setup(make)
    worked_sunday(make)
    make.leave("mall", 101, "2026-10-02", "2026-10-02", status="pending")
    assert cell(conn, now, 101, "2026-10-02").code == "A*"
    assert "available until" in cell(conn, now, 101, "2026-09-27").comp_note


def test_unapproved_absence_never_covered(conn, make, now):
    comp_setup(make)
    worked_sunday(make)
    assert cell(conn, now, 101, "2026-10-02").code == "A"


def test_withdrawn_leave_returns_comp_off(conn, make, now):
    comp_setup(make)
    worked_sunday(make)
    lid = make.leave("mall", 101, "2026-10-02", "2026-10-02")
    assert cell(conn, now, 101, "2026-10-02").code == "CO"
    conn.execute("UPDATE leaves SET status='rejected' WHERE id=?", (lid,))
    conn.commit()
    assert cell(conn, now, 101, "2026-10-02").code == "A"
    assert "available" in cell(conn, now, 101, "2026-09-27").comp_note


def test_cancelled_comp_off_turns_co_back_to_l_or_moves(conn, make, now):
    comp_setup(make)
    worked_sunday(make, day="2026-09-20")
    worked_sunday(make, day="2026-09-27")
    make.leave("mall", 101, "2026-10-02", "2026-10-02")
    assert "Sun 20 Sep" in cell(conn, now, 101, "2026-10-02").comp_note
    make.adjust("mall", 101, "2026-09-20", "cancel")
    c = cell(conn, now, 101, "2026-10-02")
    assert c.code == "CO" and "Sun 27 Sep" in c.comp_note          # moved to the other one
    make.adjust("mall", 101, "2026-09-27", "cancel")
    assert cell(conn, now, 101, "2026-10-02").code == "L"          # none left


def test_weekly_off_change_changes_past_earning(conn, make, now):
    comp_setup(make)
    worked_sunday(make, day="2026-09-29", hours=(9, 18))          # a Tuesday
    make.leave("mall", 101, "2026-10-02", "2026-10-02")
    assert cell(conn, now, 101, "2026-10-02").code == "L"
    make.weekly_off("mall", 101, "1", "2026-09-28")              # Tuesday off from 28 Sep
    assert cell(conn, now, 101, "2026-10-02").code == "CO"


def test_leave_on_weekly_off_is_wo_and_not_counted(conn, make, now):
    comp_setup(make)
    worked_sunday(make)
    make.leave("mall", 101, "2026-10-03", "2026-10-04")            # Sat + Sun
    assert cell(conn, now, 101, "2026-10-04").code == "WO"
    assert cell(conn, now, 101, "2026-10-03").code == "CO"


def test_leave_preview(conn, make, now):
    comp_setup(make)
    worked_sunday(make)
    data = StoreData(conn, "mall", date(2026, 10, 2), date(2026, 10, 3), now.value, user_ids=["101"])
    days, covered = data.leave_preview("101", date(2026, 10, 2), date(2026, 10, 3))
    assert len(days) == 2
    assert covered == [(date(2026, 10, 2), date(2026, 9, 27))]


def test_credits_listing(conn, make, now):
    comp_setup(make)
    worked_sunday(make, day="2026-09-20")
    worked_sunday(make, day="2026-09-27")
    make.leave("mall", 101, "2026-10-02", "2026-10-02")
    data = StoreData(conn, "mall", date(2026, 10, 1), date(2026, 10, 31), now.value)
    credits = data.credits("101")
    assert [(c.day, c.status) for c in credits] == [
        (date(2026, 9, 20), "used"), (date(2026, 9, 27), "available")]
