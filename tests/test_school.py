"""The school: no weekly offs, no comp-offs, 7 leaves a year (June to May),
a hand tally up to 30 Sep, absences counted as leave oldest first."""

from datetime import date, timedelta

import pytest

from attendance import StoreData


@pytest.fixture
def school(make, conn):
    make.store("school", start="09:00", close="17:00")
    conn.execute("UPDATE stores SET uses_weekly_offs=0, uses_comp_offs=0, leave_allowance=7, "
                 "leave_year_start_month=6, leave_count_from='2026-09-01', tally_as_of='2026-09-30' "
                 "WHERE store='school'")
    conn.commit()
    owner = make.user("owner", "owner")
    # the school is open Monday to Saturday from 1 Sep to 7 Oct (someone punches);
    # Sundays nobody punches
    d = date(2026, 9, 1)
    while d <= date(2026, 10, 7):
        if d.weekday() != 6:
            make.punch("school", 999, f"{d.isoformat()} 08:55:00")
        d += timedelta(days=1)
    for uid in range(1, 6):
        make.emp("school", uid)
    return owner


def present_except(make, uid, absent):
    d = date(2026, 9, 1)
    while d <= date(2026, 10, 7):
        if d.weekday() != 6 and d.isoformat() not in absent:
            make.punch("school", uid, f"{d.isoformat()} 08:58:00")
        d += timedelta(days=1)


def tally(conn, uid, used):
    conn.execute("INSERT INTO leave_tally (store, user_id, year_start, used, set_at) VALUES "
                 "('school', ?, '2026-06-01', ?, 'x')", (str(uid), used))
    conn.commit()


def data(conn, now, first="2026-09-01", last="2026-10-31"):
    return StoreData(conn, "school", date.fromisoformat(first), date.fromisoformat(last), now.value)


def code(sd, uid, day):
    return sd.cell(str(uid), date.fromisoformat(day)).code


def test_tally_placed_on_september_absences_then_october_until_used_up(school, make, conn, now):
    present_except(make, 1, {"2026-09-03", "2026-09-10", "2026-09-17", "2026-10-01", "2026-10-02", "2026-10-05"})
    tally(conn, 1, 5)
    sd = data(conn, now)
    assert [code(sd, 1, d) for d in ("2026-09-03", "2026-09-10", "2026-09-17")] == ["L", "L", "L"]
    assert code(sd, 1, "2026-10-01") == "L" and code(sd, 1, "2026-10-02") == "L"
    c = sd.cell("1", date(2026, 10, 5))
    assert c.code == "A" and "beyond the 7 leaves" in c.note
    a = sd.allowance("1")
    assert (a.before_tracking, a.used, a.left) == (2, 7, 0)
    assert "7 of 7" in sd.cell("1", date(2026, 10, 2)).note


def test_september_absence_beyond_tally_stays_absent(school, make, conn, now):
    present_except(make, 2, {"2026-09-03", "2026-09-10", "2026-10-01"})
    tally(conn, 2, 1)
    sd = data(conn, now)
    assert code(sd, 2, "2026-09-03") == "L"            # oldest first
    assert code(sd, 2, "2026-09-10") == "A"            # the tally says only one leave
    assert code(sd, 2, "2026-10-01") == "L"            # after the tally: from what's left
    a = sd.allowance("2")
    assert (a.before_tracking, a.used, a.left) == (0, 2, 5)


def test_already_exhausted(school, make, conn, now):
    present_except(make, 3, {"2026-10-01"})
    tally(conn, 3, 7)
    sd = data(conn, now)
    assert code(sd, 3, "2026-10-01") == "A"
    assert sd.allowance("3").left == 0


def test_approved_leave_always_counts_and_can_go_over(school, make, conn, now):
    present_except(make, 4, {"2026-10-06"})
    tally(conn, 4, 7)
    make.leave("school", 4, "2026-10-06", "2026-10-06", comment="Wedding")
    sd = data(conn, now)
    assert code(sd, 4, "2026-10-06") == "L"
    assert sd.allowance("4").left == -1


def test_pending_request_not_counted(school, make, conn, now):
    present_except(make, 5, {"2026-10-01"})
    make.leave("school", 5, "2026-10-01", "2026-10-01", status="pending")
    sd = data(conn, now)
    assert code(sd, 5, "2026-10-01") == "A*"
    assert sd.allowance("5").used == 0


def test_no_tally_counts_from_count_from_date(school, make, conn, now):
    present_except(make, 1, {"2026-09-03"})
    sd = data(conn, now)
    assert code(sd, 1, "2026-09-03") == "A"          # tally period, tally is 0
    conn.execute("UPDATE stores SET tally_as_of=NULL WHERE store='school'")
    conn.commit()
    sd = data(conn, now)
    assert code(sd, 1, "2026-09-03") == "L"          # no tally: counted from 1 Sep
    assert sd.allowance("1").used == 1


def test_august_punches_and_absences_ignored(school, make, conn, now):
    make.punch("school", 999, "2026-08-20 09:00:00")      # school open in August
    sd = data(conn, now, "2026-08-01", "2026-08-31")
    assert code(sd, 1, "2026-08-20") == "A"               # shown, but not counted
    a = sd.allowance("1", date(2026, 8, 31))
    assert a.used_by(date(2026, 8, 31)) == 0
    assert all(d >= date(2026, 9, 1) for d in a.counted)


def test_weekly_offs_ignored_and_sunday_is_no_data(school, make, conn, now):
    make.weekly_off("school", 1, "0")                      # Monday - ignored at the school
    present_except(make, 1, {"2026-10-05"})                # absent Monday
    sd = data(conn, now)
    assert code(sd, 1, "2026-10-05") == "L"                # not WO: counted as leave
    assert code(sd, 1, "2026-10-04") == "NODATA"           # Sunday, nobody punched


def test_no_comp_offs_at_school(school, make, conn, now):
    make.setting("comp_from", "2026-09-01")
    make.adjust("school", 1, "2026-10-04", "grant")
    sd = data(conn, now)
    assert sd.credits("1") == []


def test_tally_page_and_month_columns(school, make, conn, now, as_user):
    present_except(make, 1, {"2026-09-03"})
    c = as_user(school)
    page = c.get("/stores/school/tally").data.decode()
    assert "Leave tally" in page and "P1" in page
    r = c.post("/stores/school/tally", data={"tally_as_of": "2026-09-30", "used_1": "4", "used_2": ""})
    assert r.status_code == 302
    assert conn.execute("SELECT used FROM leave_tally WHERE user_id='1'").fetchone()[0] == 4
    month = c.get("/calendar/school/2026-09").data.decode()
    assert "Used/7" in month and ">WO<" not in month
    person = c.get("/person/school/1/2026-10").data.decode()
    assert "4</b> of 7 used" in person and "Comp-offs" not in person
    assert "doesn&#39;t use weekly offs" in c.get("/staff").data.decode() or \
        "doesn't use weekly offs" in c.get("/staff").data.decode()


def test_policy_form(school, make, conn, as_user):
    make.store("mall")
    c = as_user(school)
    c.post("/stores/mall/policy", data={"uses_weekly_offs": "on", "leave_allowance": "12",
                                        "leave_year_start_month": "4", "leave_count_from": "2026-10-01"})
    row = conn.execute("SELECT * FROM stores WHERE store='mall'").fetchone()
    assert (row["uses_weekly_offs"], row["uses_comp_offs"], row["leave_allowance"],
            row["leave_year_start_month"], row["leave_count_from"]) == (1, 0, 12, 4, "2026-10-01")


def test_stores_without_policy_unchanged(make, conn, now):
    """Mall and Nirala: no allowance, so absences stay absences."""
    make.store("mall")
    make.user("owner", "owner")
    make.punch("mall", 999, "2026-10-05 09:00:00")
    sd = StoreData(conn, "mall", date(2026, 10, 5), date(2026, 10, 5), now.value)
    assert sd.cell("1", date(2026, 10, 5)).code == "A" and sd.allowance("1") is None


def test_tally_over_seven_latest_days_stay_absent(school, make, conn, now):
    """Tally 9 with 5 September absences: 4 were before September, so the
    September ones are leaves 5, 6, 7 and then two absences beyond 7."""
    days = ["2026-09-03", "2026-09-10", "2026-09-17", "2026-09-22", "2026-09-24"]
    present_except(make, 1, set(days))
    tally(conn, 1, 9)
    sd = data(conn, now)
    assert [code(sd, 1, d) for d in days] == ["L", "L", "L", "A", "A"]
    assert "beyond the 7 leaves" in sd.cell("1", date(2026, 9, 24)).note
    a = sd.allowance("1")
    assert (a.before_tracking, a.used, a.left) == (4, 7, 0)


def test_hand_marked_absent_stays_absent_and_counts_in_tally(school, make, conn, now, as_user):
    days = ["2026-09-03", "2026-09-10", "2026-09-17", "2026-09-22", "2026-09-24"]
    present_except(make, 1, set(days))
    tally(conn, 1, 9)
    c = as_user(school)
    for d in ("2026-09-10", "2026-09-24"):
        c.post("/mark", data={"person": "school:1", "day": d, "code": "A", "note": "over the 7"})
    sd = data(conn, now)
    # the two marked stay A; they are 2 of the 9, so the other three are leave
    assert [code(sd, 1, d) for d in days] == ["L", "A", "L", "L", "A"]
    a = sd.allowance("1")
    assert (a.before_tracking, a.used, a.left) == (4, 7, 0)


def test_hand_marked_absent_after_tally_never_uses_a_leave(school, make, conn, now, as_user):
    present_except(make, 2, {"2026-10-01"})
    as_user(school).post("/mark", data={"person": "school:2", "day": "2026-10-01", "code": "A",
                                        "note": "unpaid"})
    sd = data(conn, now)
    assert code(sd, 2, "2026-10-01") == "A" and sd.allowance("2").used == 0
