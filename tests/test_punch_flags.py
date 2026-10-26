"""The month view's punch flags: ! for a check-in with no check-out, a
number for more than two punches. Double taps count once."""

from datetime import date, datetime

from attendance import StoreData


def cell(conn, now, uid, day):
    d = date.fromisoformat(day)
    return StoreData(conn, "mall", d, d, now.value).cell(uid, d)


def setup(make):
    make.store("mall", start="09:00", grace=10, close="21:00")
    make.user("owner", "owner")


def test_flags(conn, make, now):
    setup(make)
    day = "2026-10-05"
    for uid, times in {101: ["09:00"], 102: ["09:00", "18:00"], 103: ["09:00", "13:00", "14:00", "18:00"],
                       104: ["09:00", "13:00", "14:00"], 105: ["09:00", "09:02", "18:00"],
                       106: ["09:00", "09:01"]}.items():
        for t in times:
            make.punch("mall", uid, f"{day} {t}:00")
    flags = {u: cell(conn, now, u, day).punch_flag for u in range(101, 107)}
    assert flags == {101: ("single", 1), 102: None, 103: ("multi", 4), 104: ("multi", 3),
                     105: None,                 # a double tap at the machine
                     106: ("single", 1)}        # a double tap is still only a check-in
    assert "no check-out" in cell(conn, now, 101, day).punch_flag_text
    assert "may be missing" in cell(conn, now, 104, day).punch_flag_text
    assert "came back" in cell(conn, now, 103, day).punch_flag_text


def test_today_single_punch_waits_for_closing(conn, make, now):
    setup(make)
    make.punch("mall", 101, "2026-10-07 09:00:00")
    now.set(datetime(2026, 10, 7, 12, 0))
    assert cell(conn, now, 101, "2026-10-07").punch_flag is None     # still at work
    now.set(datetime(2026, 10, 7, 23, 0))
    assert cell(conn, now, 101, "2026-10-07").punch_flag == ("single", 1)


def test_month_page_shows_the_marks(conn, make, now, as_user):
    setup(make)
    owner = conn.execute("SELECT id FROM users WHERE username='owner'").fetchone()[0]
    make.punch("mall", 101, "2026-10-05 09:00:00")
    for t in ("09:00", "13:00", "14:00", "18:00"):
        make.punch("mall", 102, f"2026-10-05 {t}:00")
    page = as_user(owner).get("/calendar/mall/2026-10").data.decode()
    assert 'class="pf pf-single"' in page and '>!</sub>' in page
    assert 'class="pf pf-multi" title="4 punches' in page
    assert "checked in, no check-out" in page
