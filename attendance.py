"""
Works out each person's code for each day, from punches + weekly offs +
leaves + the store's hours. Nothing is stored: every page works it out
fresh, so approving a leave or changing a weekly off shows up everywhere,
past months included.

Two layers:
  * pure rules (day_status, allocate_comp_offs, earned_credits) with no
    database code, so they are easy to test;
  * StoreData, which loads everything a page needs for one store in a small
    fixed number of queries and builds the calendar cells in Python.

Codes: P present, LT late, WO weekly off, L approved leave, CO leave covered
by a comp-off, A* absent with leave pending, A absent, NOT_IN_YET (today,
before closing), NODATA (nobody at the store punched), None (future).
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import db

PRIORITY = {"approved": 0, "pending": 1, "rejected": 2}   # cancelled is ignored

CODE_LABELS = {
    "P": "Present", "LT": "Late", "WO": "Weekly off", "L": "Leave",
    "CO": "Comp-off", "A*": "Leave pending", "A": "Absent",
    "NOT_IN_YET": "Not in yet", "NODATA": "No punches",
}
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ----------------------------------------------------------------- data

@dataclass
class Leave:
    start: date
    end: date
    status: str
    staff_comment: str = ""
    manager_comment: str = ""
    id: int = 0
    user_id: str = ""
    decided_by_name: str = ""
    created_by: int = 0
    decided_by: int = 0

    def covers(self, day):
        return self.start <= day <= self.end


def _plus_minutes(t, minutes):
    return (datetime.combine(date(2000, 1, 1), t) + timedelta(minutes=minutes)).time()


def _minutes(t):
    return t.hour * 60 + t.minute


@dataclass
class Shift:
    name: str
    start: time
    end: time
    grace_minutes: int
    id: int = 0

    @property
    def late_after(self):
        """The last on-time minute: start + grace."""
        return _plus_minutes(self.start, self.grace_minutes)

    @property
    def label(self):
        return f"{self.name} {self.start:%H:%M}–{self.end:%H:%M}" if self.name else ""


@dataclass
class StoreRules:
    start_time: time
    grace_minutes: int
    close_time: time
    shifts: list = field(default_factory=list)     # [Shift], sorted by start

    @property
    def late_after(self):
        """The last on-time minute: start + grace (store-wide times)."""
        return _plus_minutes(self.start_time, self.grace_minutes)

    @property
    def day_end(self):
        """When 'not in yet' turns into absent: closing time, or the end of
        the last shift if that is later."""
        return max([self.close_time] + [s.end for s in self.shifts])

    def shift_for(self, first_punch):
        """The shift someone is counted on: the one whose start is closest
        to their first punch (the earlier one on a tie). With no shifts
        set, the store's own start time and grace."""
        if not self.shifts:
            return Shift("", self.start_time, self.close_time, self.grace_minutes)
        at = first_punch.hour * 60 + first_punch.minute
        return min(self.shifts, key=lambda s: (abs(at - _minutes(s.start)), _minutes(s.start)))

    def is_late(self, first_punch):
        shift = self.shift_for(first_punch)
        return first_punch.time().replace(second=0, microsecond=0) > shift.late_after, shift

    @classmethod
    def from_row(cls, row, shifts=()):
        return cls(parse_hhmm(row["start_time"]), int(row["grace_minutes"]), parse_hhmm(row["close_time"]),
                   sorted(shifts, key=lambda s: (s.start, s.id)))


def load_shifts(conn, store):
    return [Shift(r["name"], parse_hhmm(r["start_time"]), parse_hhmm(r["end_time"]),
                  int(r["grace_minutes"]), r["id"])
            for r in conn.execute("SELECT * FROM shifts WHERE store=? ORDER BY start_time, id", (store,))]


@dataclass
class CompConfig:
    min_hours: float = 4.0
    window_days: int = 30
    comp_from: date | None = None      # None = comp-offs switched off

    @classmethod
    def load(cls, conn):
        raw_from = db.get_setting(conn, "comp_from")
        return cls(float(db.get_setting(conn, "comp_min_hours")),
                   int(db.get_setting(conn, "comp_window_days")),
                   date.fromisoformat(raw_from) if raw_from else None)


@dataclass
class Cell:
    day: date
    code: str | None
    note: str = ""
    punches: list = field(default_factory=list)
    leave: Leave | None = None
    comp_earned: bool = False       # the P+ marker: a worked weekly off that earned a comp-off
    comp_note: str = ""              # "covers leave on 2 Oct" / "available until 27 Oct" / ...
    weekly_off: bool = False
    shift: "Shift | None" = None     # the shift they were counted on (days they came in)

    @property
    def first(self):
        return self.punches[0] if self.punches else None

    @property
    def last(self):
        return self.punches[-1] if len(self.punches) > 1 else None

    @property
    def no_checkout(self):
        return len(self.punches) == 1


@dataclass
class LeavePolicy:
    """A store's leave allowance (the school: 7 a year, June to May)."""
    allowance: int | None = None          # None = no allowance at this store
    year_start_month: int = 6
    count_from: date | None = None        # absences count as leave from this day
    tally_as_of: date | None = None       # the hand tally covers up to this day

    @classmethod
    def from_row(cls, row):
        keys = row.keys()

        def d(k):
            v = row[k] if k in keys else None
            return date.fromisoformat(v) if v else None
        return cls(row["leave_allowance"] if "leave_allowance" in keys else None,
                   (row["leave_year_start_month"] if "leave_year_start_month" in keys else 6) or 6,
                   d("leave_count_from"), d("tally_as_of"))

    def year_of(self, day):
        """(first, last) day of the leave year containing `day`."""
        m = self.year_start_month
        y = day.year if day.month >= m else day.year - 1
        start = date(y, m, 1)
        return start, date(y + 1, m, 1) - timedelta(days=1)


@dataclass
class Allowance:
    """One person's leave year: which absences were counted as leave, and
    the totals."""
    year_start: date
    year_end: date
    allowance: int
    tally: int = 0                 # from the hand tally
    before_tracking: int = 0       # part of the tally from before tracking began
    auto: dict = field(default_factory=dict)       # day -> "n of 7" for absences counted as leave
    exhausted: set = field(default_factory=set)    # absences after the allowance ran out
    counted: dict = field(default_factory=dict)    # day -> running total, every leave day counted
    used: int = 0

    @property
    def left(self):
        return self.allowance - self.used

    def used_by(self, day):
        """Leaves used up to and including `day`."""
        total = self.before_tracking
        for d, _n in self.counted.items():
            if d <= day:
                total += 1
        return total


@dataclass
class Credit:
    day: date
    status: str          # available / used / expired
    covers: date | None
    until: date
    granted: bool = False
    hours: float = 0.0


# ----------------------------------------------------------------- helpers

def parse_hhmm(text):
    hh, mm = (int(p) for p in str(text).split(":")[:2])
    return time(hh, mm)


def parse_ts(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


# Any date outside this range is refused wherever dates come in (forms,
# addresses, the store sync), so date arithmetic can never overflow.
MIN_DATE = date(2000, 1, 1)
MAX_DATE = date(2100, 12, 31)


def in_range(d):
    return MIN_DATE <= d <= MAX_DATE


def daterange(first, last):
    d = first
    while d <= last:
        yield d
        if d == date.max:
            return
        d += timedelta(days=1)


def month_bounds(year, month):
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def fmt_day(d):
    return f"{d:%a} {d.day} {d:%b}"


def parse_weekdays(text):
    return {int(x) for x in (text or "").split(",") if x.strip() != ""}


class WeeklyOffHistory:
    """Dated weekly-off settings for one person. A change applies from its
    'from' date until the next change, so past months never change."""

    def __init__(self, rows=()):
        # rows: (effective_from date, weekdays set, id)
        self.rows = sorted(rows, key=lambda r: (r[0], r[2]))

    def days_on(self, day):
        current = None
        for eff, days, _id in self.rows:
            if eff <= day:
                current = days
            else:
                break
        return current if current is not None else set()

    def is_set(self):
        return bool(self.rows)

    def current(self, day):
        """(weekdays, effective_from) in force on `day`, or (None, None)."""
        cur = (None, None)
        for eff, days, _id in self.rows:
            if eff <= day:
                cur = (days, eff)
        return cur


# ----------------------------------------------------------------- pure rules

def day_status(day, punched, weekly_off_days, leaves):
    """Return (code, note) for one employee on one day."""
    covering = [l for l in leaves if l.covers(day) and l.status in PRIORITY]
    leave = min(covering, key=lambda l: PRIORITY[l.status], default=None)
    on_wo = day.weekday() in weekly_off_days

    if punched:
        if on_wo:
            return "P", "worked on weekly off"
        if leave and leave.status == "approved":
            return "P", "came in during approved leave"
        return "P", ""
    if on_wo:
        return "WO", ""                       # weekly off beats leave
    if leave and leave.status == "approved":
        return "L", leave.staff_comment
    if leave and leave.status == "pending":
        return "A*", f"leave requested: {leave.staff_comment}".rstrip(": ")
    if leave and leave.status == "rejected":
        return "A", f"leave rejected: {leave.manager_comment}".rstrip(": ")
    return "A", ""


def active_leave(day, leaves):
    covering = [l for l in leaves if l.covers(day) and l.status in PRIORITY]
    return min(covering, key=lambda l: PRIORITY[l.status], default=None)


def allocate_comp_offs(credit_days, leave_days, window_days):
    """Match approved-leave days to worked weekly offs (comp-off credits).

    A credit can cover a leave day up to `window_days` before or after the
    day it was earned. Leave days are taken in date order, and each takes
    the earliest-earned credit that can still cover it. This covers the
    largest possible number of leave days.
    Returns {leave_day: credit_day}.
    """
    available = sorted(credit_days)
    used = {}
    for day in sorted(leave_days):
        for credit in available:
            if abs((day - credit).days) <= window_days:
                used[day] = credit
                available.remove(credit)
                break
    return used


def earned_credits(spans, wo_history, cfg, cancels=(), grants=()):
    """Which days earned a comp-off.

    spans: {day: (first_punch, last_punch)} for one person.
    A day counts if it is on/after the start date, it was their weekly off,
    and they were in at least the minimum hours. Cancelled days are removed;
    granted days are added (a grant is a deliberate decision, so it counts
    whatever the start date).
    """
    earned = set()
    if cfg.comp_from is not None:
        need = timedelta(hours=cfg.min_hours)
        for day, (first, last) in spans.items():
            if day < cfg.comp_from:
                continue
            if day.weekday() not in wo_history.days_on(day):
                continue
            if last - first >= need:
                earned.add(day)
    earned -= set(cancels)
    earned |= set(grants)
    return earned


def working_days(start, end, wo_history):
    return [d for d in daterange(start, end) if d.weekday() not in wo_history.days_on(d)]


# ----------------------------------------------------------------- loading

class StoreData:
    """Everything needed to draw one store's calendars for a date range,
    loaded in a fixed handful of queries, whatever the number of people."""

    def __init__(self, conn, store, first, last, now, user_ids=None):
        self.conn = conn
        self.first, self.last, self.now = first, last, now
        self.today = now.date()
        row = conn.execute("SELECT * FROM stores WHERE store=?", (store,)).fetchone()
        if row is None:
            raise KeyError(store)
        self.store_row = row
        self.store = store
        self.rules = StoreRules.from_row(row, load_shifts(conn, store))
        self.cfg = CompConfig.load(conn)
        keys = row.keys()
        self.uses_weekly_offs = bool(row["uses_weekly_offs"]) if "uses_weekly_offs" in keys else True
        self.uses_comp_offs = bool(row["uses_comp_offs"]) if "uses_comp_offs" in keys else True
        if not self.uses_comp_offs:
            self.cfg = CompConfig(self.cfg.min_hours, self.cfg.window_days, None)
        self.policy = LeavePolicy.from_row(row)

        only = ""
        args = [store]
        if user_ids is not None:
            user_ids = [str(u) for u in user_ids]
            only = f" AND user_id IN ({','.join('?' * len(user_ids))})" if user_ids else " AND 0"
            args += user_ids

        # 1. people
        self.people = conn.execute(
            f"SELECT * FROM employees WHERE store=?{only}", args).fetchall()
        self.people = sorted(self.people, key=lambda r: natural_key(r["user_id"]))

        # 2. every punch in the range shown
        self.punches = {}          # (user_id, day) -> [datetime]
        self.store_days = set()    # days on which anyone at the store punched
        for r in conn.execute(
                "SELECT user_id, day, ts FROM punches WHERE store=? AND day BETWEEN ? AND ? ORDER BY ts",
                (store, first.isoformat(), last.isoformat())):
            d = date.fromisoformat(r["day"])
            self.store_days.add(d)
            if user_ids is None or r["user_id"] in user_ids:
                self.punches.setdefault((r["user_id"], d), []).append(parse_ts(r["ts"]))

        # 3. comp-off exceptions (cancelled / granted by hand)
        self.cancels, self.grants = {}, {}
        for r in conn.execute(f"SELECT * FROM comp_adjustments WHERE store=?{only}", args):
            target = self.cancels if r["kind"] == "cancel" else self.grants
            target.setdefault(r["user_id"], set()).add(date.fromisoformat(r["day"]))
        if not self.uses_comp_offs:
            self.cancels, self.grants = {}, {}

        # 4. comp-off history: first/last punch per person-day, far enough
        #    back to cover every comp-off and every leave day one could
        #    cover, plus the store's working days over the same period.
        window = timedelta(days=self.cfg.window_days)
        starts = [first]
        if self.cfg.comp_from:
            starts.append(self.cfg.comp_from - window)
        starts += [min(days) - window for days in self.grants.values() if days]
        if self.policy.allowance is not None:
            # the whole leave year so far, from where absences start counting
            year_start, _ = self.policy.year_of(first)
            starts.append(max(year_start, self.policy.count_from or year_start))
        self.hist_from = min(starts)
        self.spans = {}            # user_id -> {day: (first, last)}
        for r in conn.execute(
                f"SELECT user_id, day, MIN(ts) AS a, MAX(ts) AS b FROM punches "
                f"WHERE store=? AND day >= ?{only} GROUP BY user_id, day",
                [store, self.hist_from.isoformat()] + (user_ids or [])):
            self.spans.setdefault(r["user_id"], {})[date.fromisoformat(r["day"])] = (
                parse_ts(r["a"]), parse_ts(r["b"]))
        self.hist_store_days = {date.fromisoformat(r["day"]) for r in conn.execute(
            "SELECT DISTINCT day FROM punches WHERE store=? AND day >= ?",
            (store, self.hist_from.isoformat()))}
        self.hist_store_days |= self.store_days

        # 5. weekly offs (all of them: they are dated)
        self.wo = {}
        wo_rows = {}
        for r in (conn.execute(f"SELECT * FROM weekly_offs WHERE store=?{only}", args)
                  if self.uses_weekly_offs else ()):
            wo_rows.setdefault(r["user_id"], []).append(
                (date.fromisoformat(r["effective_from"]), parse_weekdays(r["weekdays"]), r["id"]))
        for uid, rows in wo_rows.items():
            self.wo[uid] = WeeklyOffHistory(rows)

        # 6. leaves (not cancelled) that could matter: anything touching the
        #    range shown or the comp-off history.
        self.leaves = {}
        for r in conn.execute(
                f"SELECT l.*, u.display_name AS decided_by_name FROM leaves l "
                f"LEFT JOIN users u ON u.id = l.decided_by "
                f"WHERE l.store=? AND l.status != 'cancelled' AND l.end_date >= ?"
                f"{only.replace('user_id', 'l.user_id')}",
                [store, self.hist_from.isoformat()] + (user_ids or [])):
            self.leaves.setdefault(r["user_id"], []).append(leave_from_row(r))

        # 7. the hand tally of leaves already taken, per person and year
        self.tally = {}
        if self.policy.allowance is not None:
            for r in conn.execute(f"SELECT * FROM leave_tally WHERE store=?{only}", args):
                self.tally[(r["user_id"], r["year_start"])] = int(r["used"])

        self._comp_cache = {}
        self._allow_cache = {}

    # --------------------------------------------------------- per person

    def wo_history(self, user_id):
        return self.wo.get(str(user_id), WeeklyOffHistory())

    def leaves_for(self, user_id):
        return self.leaves.get(str(user_id), [])

    def store_open_on(self, day):
        """Did anyone at the store punch that day? Today, before closing,
        counts as open: the day is still going."""
        if day == self.today and self.now.time() < self.rules.day_end:
            return True
        return day in self.hist_store_days

    def comp(self, user_id, extra_leaves=()):
        """(credits set, {leave_day: credit_day}) over the whole history."""
        uid = str(user_id)
        key = (uid, tuple(id(l) for l in extra_leaves))
        if key in self._comp_cache:
            return self._comp_cache[key]
        wo = self.wo_history(uid)
        spans = self.spans.get(uid, {})
        credits = {d for d in earned_credits(spans, wo, self.cfg,
                                             self.cancels.get(uid, ()), self.grants.get(uid, ()))
                   if d <= self.today}
        if not credits:
            result = (credits, {})
            self._comp_cache[key] = result
            return result
        leaves = self.leaves_for(uid) + list(extra_leaves)
        # no comp-off can reach a leave day earlier than this
        lower = min(credits) - timedelta(days=self.cfg.window_days)
        leave_days = set()
        for lv in leaves:
            if lv.status != "approved":
                continue
            for d in daterange(max(lv.start, lower), lv.end):
                if d.weekday() in wo.days_on(d):
                    continue
                if d in spans:                        # came in: not a leave day
                    continue
                if d <= self.today and not self.store_open_on(d):
                    continue                          # nobody worked that day: shows as no data
                if active_leave(d, leaves) is not lv:
                    continue
                leave_days.add(d)
        result = (credits, allocate_comp_offs(credits, leave_days, self.cfg.window_days))
        self._comp_cache[key] = result
        return result

    def _base(self, uid, day):
        """(code, leave) for a past or current day, before any allowance:
        P, L, A, A*, WO, NODATA or None (today, still going)."""
        punches = self.spans.get(uid, {}).get(day) or self.punches.get((uid, day))
        wo = self.wo_history(uid)
        if not punches and not self.store_open_on(day):
            return "NODATA", None
        leaves = self.leaves_for(uid)
        code, _note = day_status(day, bool(punches), wo.days_on(day), leaves)
        if code in ("A", "A*") and day == self.today and self.now.time() < self.rules.day_end:
            return None, None
        return code, active_leave(day, leaves)

    def allowance(self, user_id, day=None):
        """The leave year containing `day` (default today) for a store with
        a leave allowance, or None.

        Up to the tally date, the hand tally is placed on the person's
        leave and absence days, oldest first; whatever doesn't fit counts as
        taken before tracking began. After it, each approved leave day or
        absence uses one of what's left, oldest first, until none are left.
        Absences with a pending or rejected request are never counted: they
        wait for, or already have, a decision."""
        pol = self.policy
        if pol.allowance is None:
            return None
        uid = str(user_id)
        ys, ye = pol.year_of(day or self.today)
        key = (uid, ys)
        if key in self._allow_cache:
            return self._allow_cache[key]
        a = Allowance(ys, ye, pol.allowance)
        tally_on = pol.tally_as_of is not None and ys <= pol.tally_as_of <= ye
        a.tally = self.tally.get((uid, ys.isoformat()), 0) if tally_on else 0
        budget = a.tally                 # tally still to place on days
        settled = not tally_on
        used = 0                         # leaves counted so far this year

        def settle():
            # whatever of the tally didn't land on a day was taken before
            # tracking began (June to August for the school)
            nonlocal budget, used, settled
            a.before_tracking = budget
            used += budget
            budget = 0
            settled = True

        start = max(ys, pol.count_from or ys)
        for d in daterange(start, min(ye, self.today)):
            if not settled and d > pol.tally_as_of:
                settle()
            code, lv = self._base(uid, d)
            explicit = code == "L"                  # an approved leave request
            absent = code == "A" and lv is None     # no request at all
            if not (explicit or absent):
                continue
            if not settled:                          # covered by the tally
                if budget > 0:
                    budget -= 1
                elif absent:
                    continue                         # the tally says this wasn't leave
                used += 1
                a.counted[d] = used
                if absent:
                    a.auto[d] = used
                continue
            if explicit:
                used += 1
                a.counted[d] = used
            elif used < pol.allowance:
                used += 1
                a.counted[d] = used
                a.auto[d] = used
            else:
                a.exhausted.add(d)
        if not settled:
            settle()
        a.used = used
        self._allow_cache[key] = a
        return a

    def cell(self, user_id, day):
        uid = str(user_id)
        punches = self.punches.get((uid, day), [])
        wo = self.wo_history(uid)
        on_wo = day.weekday() in wo.days_on(day)
        if day > self.today:
            return Cell(day, None, weekly_off=on_wo, leave=active_leave(day, self.leaves_for(uid)))
        credits, matches = self.comp(uid)
        if not punches and not self.store_open_on(day):
            return Cell(day, "NODATA", "no punches from anyone at the store", weekly_off=on_wo)
        leaves = self.leaves_for(uid)
        code, note = day_status(day, bool(punches), wo.days_on(day), leaves)
        c = Cell(day, code, note, punches, active_leave(day, leaves), weekly_off=on_wo)
        if code == "L" and day in matches:
            c.code = "CO"
            c.comp_note = f"covered by weekly off worked {fmt_day(matches[day])}"
        if punches:
            late, c.shift = self.rules.is_late(punches[0])
            if code == "P" and late:
                c.code = "LT"
        if c.code in ("A", "A*") and day == self.today and self.now.time() < self.rules.day_end:
            c.code = "NOT_IN_YET"
        if self.policy.allowance is not None and c.code in ("A", "L"):
            a = self.allowance(uid, day)
            if day in a.auto:
                c.code = "L"
                c.note = f"absence counted as leave ({a.used_by(day)} of {a.allowance} this year)"
            elif c.code == "L" and day in a.counted:
                c.note = (f"{c.note} · " if c.note else "") + \
                    f"leave {a.used_by(day)} of {a.allowance} this year"
            elif day in a.exhausted:
                c.note = f"all {a.allowance} leaves for the year already used"
        if day in credits:
            c.comp_earned = True
            c.comp_note = self._credit_note(day, matches)
        return c

    def _credit_note(self, credit_day, matches):
        for leave_day, cday in matches.items():
            if cday == credit_day:
                return f"comp-off: covers leave on {fmt_day(leave_day)}"
        until = credit_day + timedelta(days=self.cfg.window_days)
        if until < self.today:
            return f"comp-off: expired {fmt_day(until)}"
        return f"comp-off: available until {fmt_day(until)}"

    def row(self, user_id):
        return [self.cell(user_id, d) for d in daterange(self.first, self.last)]

    def credits(self, user_id):
        """Every comp-off this person has earned, with where it went."""
        uid = str(user_id)
        credits, matches = self.comp(uid)
        by_credit = {c: l for l, c in matches.items()}
        grants = self.grants.get(uid, set())
        spans = self.spans.get(uid, {})
        out = []
        for c in sorted(credits):
            until = c + timedelta(days=self.cfg.window_days)
            if c in by_credit:
                status = "used"
            elif until < self.today:
                status = "expired"
            else:
                status = "available"
            hours = 0.0
            if c in spans:
                hours = (spans[c][1] - spans[c][0]).total_seconds() / 3600
            out.append(Credit(c, status, by_credit.get(c), until, c in grants, hours))
        return out

    def leave_preview(self, user_id, start, end):
        """For a leave being requested or approved: (working days, [(day,
        credit_day)] that comp-offs would cover if it were approved)."""
        uid = str(user_id)
        wo = self.wo_history(uid)
        days = working_days(start, end, wo)
        probe = Leave(start, end, "approved", id=-1, user_id=uid)
        existing = [l for l in self.leaves_for(uid)
                    if not (l.status == "approved" and l.start == start and l.end == end)]
        # Work it out as though this leave were approved (and, if it already
        # is, without counting it twice).
        saved = self.leaves.get(uid)
        self.leaves[uid] = existing
        try:
            self._comp_cache.clear()
            _credits, matches = self.comp(uid, extra_leaves=(probe,))
        finally:
            if saved is None:
                self.leaves.pop(uid, None)
            else:
                self.leaves[uid] = saved
            self._comp_cache.clear()
        covered = sorted((d, c) for d, c in matches.items() if start <= d <= end)
        return days, covered


def leave_from_row(r):
    keys = r.keys()
    return Leave(date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"]),
                 r["status"], r["staff_comment"], r["manager_comment"], r["id"], r["user_id"],
                 (r["decided_by_name"] if "decided_by_name" in keys else "") or "",
                 r["created_by"], r["decided_by"] or 0)


def natural_key(user_id):
    try:
        return (0, int(user_id), "")
    except (TypeError, ValueError):
        return (1, 0, str(user_id))


def summarize(cells):
    """Totals for a row or a day, counting only up to today and ignoring
    days with no data."""
    out = {"P": 0, "LT": 0, "WO": 0, "L": 0, "CO": 0, "A*": 0, "A": 0, "NOT_IN_YET": 0}
    for c in cells:
        if c.code in out:
            out[c.code] += 1
    out["present"] = out["P"] + out["LT"]
    out["unapproved"] = out["A"] + out["A*"]
    return out
