"""The pure rules: day status, comp-off earning and matching."""

import itertools
import random
from datetime import date, datetime, timedelta

import pytest

from attendance import (CompConfig, Leave, WeeklyOffHistory, allocate_comp_offs, day_status,
                        earned_credits, working_days)

D = date(2026, 10, 6)          # a Tuesday


def lv(status, staff="staff note", mgr="mgr note"):
    return Leave(D, D, status, staff, mgr)


@pytest.mark.parametrize("punched, wo, leaves, expected", [
    (True, False, [], ("P", "")),
    (True, True, [], ("P", "worked on weekly off")),
    (True, False, ["approved"], ("P", "came in during approved leave")),
    (False, True, ["approved"], ("WO", "")),
    (False, False, ["approved"], ("L", "staff note")),
    (False, False, ["pending"], ("A*", "leave requested: staff note")),
    (False, False, ["rejected"], ("A", "leave rejected: mgr note")),
    (False, False, ["cancelled"], ("A", "")),
    (False, False, ["rejected", "approved"], ("L", "staff note")),
    (False, False, [], ("A", "")),
])
def test_day_status_table(punched, wo, leaves, expected):
    wo_days = {D.weekday()} if wo else set()
    assert day_status(D, punched, wo_days, [lv(s) for s in leaves]) == expected


# ----------------------------------------------------------------- matching

def d(text):
    return date.fromisoformat(text)


def test_credit_covers_leave_after():
    assert allocate_comp_offs({d("2026-09-27")}, {d("2026-10-03")}, 30) == {d("2026-10-03"): d("2026-09-27")}


def test_credit_covers_earlier_leave():
    assert allocate_comp_offs({d("2026-09-13")}, {d("2026-09-01")}, 30) == {d("2026-09-01"): d("2026-09-13")}


def test_credit_too_far_away():
    assert allocate_comp_offs({d("2026-08-01")}, {d("2026-10-03")}, 30) == {}


def test_one_credit_two_leave_days_covers_earlier():
    got = allocate_comp_offs({d("2026-09-27")}, {d("2026-10-02"), d("2026-10-03")}, 30)
    assert got == {d("2026-10-02"): d("2026-09-27")}


def best_possible(credits, leaves, window):
    """Brute force: the most leave days any matching could cover."""
    credits, leaves = sorted(credits), sorted(leaves)
    best = 0
    for k in range(min(len(credits), len(leaves)), 0, -1):
        for chosen in itertools.combinations(leaves, k):
            for perm in itertools.permutations(credits, k):
                if all(abs((a - b).days) <= window for a, b in zip(chosen, perm)):
                    return k
    return best


def test_fairness_random():
    rng = random.Random(7)
    base = date(2026, 1, 1)
    for _ in range(3000):
        credits = {base + timedelta(days=rng.randrange(90)) for _ in range(rng.randrange(0, 5))}
        leaves = {base + timedelta(days=rng.randrange(90)) for _ in range(rng.randrange(0, 5))}
        window = rng.choice([3, 7, 14, 30])
        got = allocate_comp_offs(credits, leaves, window)
        assert len(set(got.values())) == len(got)                   # no credit used twice
        assert all(abs((l - c).days) <= window for l, c in got.items())
        assert set(got) <= leaves and set(got.values()) <= credits
        assert len(got) == best_possible(credits, leaves, window)


# ----------------------------------------------------------------- earning

SUN = date(2026, 9, 27)
SUN_OFF = WeeklyOffHistory([(date(2026, 1, 1), {6}, 1)])
CFG = CompConfig(4, 30, date(2026, 9, 1))


def span(day, h1, m1, h2, m2):
    return {day: (datetime.combine(day, datetime.min.time()).replace(hour=h1, minute=m1),
                  datetime.combine(day, datetime.min.time()).replace(hour=h2, minute=m2))}


def test_four_hours_earns():
    assert earned_credits(span(SUN, 10, 0, 14, 0), SUN_OFF, CFG) == {SUN}


def test_just_under_four_hours_does_not():
    assert earned_credits(span(SUN, 10, 0, 13, 59), SUN_OFF, CFG) == set()


def test_single_punch_never_earns():
    assert earned_credits(span(SUN, 10, 0, 10, 0), SUN_OFF, CFG) == set()


def test_before_start_date_does_not_earn():
    early = date(2026, 8, 30)
    assert earned_credits(span(early, 9, 0, 18, 0), SUN_OFF, CFG) == set()


def test_cancelled_day_stops_counting():
    assert earned_credits(span(SUN, 9, 0, 18, 0), SUN_OFF, CFG, cancels={SUN}) == set()


def test_granted_day_counts_without_punches():
    assert earned_credits({}, SUN_OFF, CFG, grants={SUN}) == {SUN}


def test_not_weekly_off_never_earns():
    mon = SUN + timedelta(days=1)
    assert earned_credits(span(mon, 9, 0, 18, 0), SUN_OFF, CFG) == set()


def test_switched_off_earns_nothing():
    assert earned_credits(span(SUN, 9, 0, 18, 0), SUN_OFF, CompConfig(4, 30, None)) == set()


def test_working_days_skip_weekly_off():
    # Fri 2 - Sun 4 Oct with Sunday off: 2 working days
    assert len(working_days(d("2026-10-02"), d("2026-10-04"), SUN_OFF)) == 2


def test_weekly_off_history_changes_on_date():
    h = WeeklyOffHistory([(d("2026-01-01"), {6}, 1), (d("2026-10-01"), {1}, 2)])
    assert h.days_on(d("2026-09-27")) == {6}
    assert h.days_on(d("2026-10-04")) == {1}
    assert h.days_on(d("2025-12-31")) == set()
