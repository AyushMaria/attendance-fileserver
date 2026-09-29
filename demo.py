"""
Sample data for the preview site only (DEMO_DATA=1). Never set on the real
website.

Fills two stores with made-up staff and six weeks of punches up to today,
weekly offs, a few leaves (approved, pending, rejected), a worked weekly off
that earns a comp-off, and demo Manager and CRO logins that share the
DEMO_PASSWORD variable. Runs only when there are no stores yet.
"""

import os
import random
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

import clock
import db

STAFF = {
    "mall": [("101", "Aditi Sharma"), ("102", "Rahul Mehta"), ("103", "Karan Singh"),
             ("104", "Neha Verma"), ("105", "Vikas Rao"), ("106", "Meena Iyer"),
             ("107", "Sunil Gupta"), ("108", "Pooja Nair"), ("109", "Arjun Das"),
             ("110", "Kavya Reddy")],
    "nirala": [("201", "Priya Joshi"), ("202", "Ravi Kumar"), ("203", "Sana Khan"),
               ("204", "Deepak Patel"), ("205", "Anita Bose"), ("206", "Manoj Pillai"),
               ("207", "Ritu Kapoor"), ("208", "Imran Ali")],
}
HOURS = {"mall": ("10:00", 10, "21:30"), "nirala": ("09:30", 10, "21:00")}


def seed(conn, log=print):
    # both server processes start at once: take the write lock before
    # checking, so only one of them seeds
    conn.execute("BEGIN IMMEDIATE")
    if conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]:
        conn.rollback()
        return False
    rng = random.Random(42)
    now = clock.now_local()
    today = now.date()
    stamp = db.utc_now_iso()

    owner = conn.execute("SELECT id FROM users WHERE role='owner' ORDER BY id LIMIT 1").fetchone()
    if owner is None:
        conn.rollback()
        return False
    owner = owner["id"]

    for store, (start, grace, close) in HOURS.items():
        conn.execute("INSERT INTO stores (store, display_name, start_time, grace_minutes, close_time, "
                     "last_sync_at, last_sync_note) VALUES (?,?,?,?,?,?,?)",
                     (store, store.title(), start, grace, close, stamp, "demo data"))
        for name, a, b in (("Morning", start, "19:00"), ("Evening", "13:00", close)):
            conn.execute("INSERT INTO shifts (store, name, start_time, end_time, grace_minutes) "
                         "VALUES (?,?,?,?,?)", (store, name, a, b, grace))
        for uid, name in STAFF[store]:
            conn.execute("INSERT INTO employees (store, user_id, name, on_device, first_seen, last_seen) "
                         "VALUES (?,?,?,?,?,?)", (store, uid, name, 1, stamp, stamp))

    first_day = today - timedelta(days=45)
    db.set_setting(conn, "comp_from", first_day.isoformat())

    punches = []
    wo_of = {}
    for store, people in STAFF.items():
        for i, (uid, _name) in enumerate(people):
            # every fourth person works the evening shift
            start_h, start_m = (13, 0) if i % 4 == 3 else (int(x) for x in HOURS[store][0].split(":"))
            wo = 6 if i % 3 else (i % 7)                     # mostly Sunday, some others
            wo_of[(store, uid)] = wo
            if uid != "110":                                  # one person left unset
                conn.execute("INSERT INTO weekly_offs (store, user_id, weekdays, effective_from, set_by, set_at) "
                             "VALUES (?,?,?,?,?,?)", (store, uid, str(wo), first_day.isoformat(), owner, stamp))
            d = first_day
            while d <= today:
                if d == today - timedelta(days=17) and store == "mall":
                    d += timedelta(days=1)                    # a day the device was off
                    continue
                on_wo = d.weekday() == wo
                worked_off = on_wo and rng.random() < 0.12
                absent = (not on_wo) and rng.random() < 0.05
                if (on_wo and not worked_off) or absent:
                    d += timedelta(days=1)
                    continue
                late = rng.random() < 0.15
                arrive = datetime(d.year, d.month, d.day, start_h, start_m) + timedelta(
                    minutes=rng.randint(12, 55) if late else rng.randint(-25, 9))
                leave_at = arrive + timedelta(hours=rng.uniform(9.5, 11.5))
                if d == today:
                    if arrive > now:
                        d += timedelta(days=1)
                        continue
                    stamps = [arrive]
                    if now - arrive > timedelta(hours=4):
                        stamps.append(arrive + timedelta(hours=3, minutes=rng.randint(0, 40)))
                else:
                    stamps = [arrive, arrive + timedelta(hours=4, minutes=rng.randint(0, 30)),
                              arrive + timedelta(hours=4, minutes=rng.randint(35, 60)), leave_at]
                    if rng.random() < 0.04:
                        stamps = [arrive]                     # forgot to punch out
                for ts in stamps:
                    punches.append((store, uid, ts.strftime("%Y-%m-%d"),
                                    ts.strftime("%Y-%m-%d %H:%M:%S"), 0, stamp))
                d += timedelta(days=1)

    # a clearly worked weekly off for Aditi, 10 days ago-ish (her off day)
    aditi_wo = wo_of[("mall", "101")]
    worked = today - timedelta(days=(today.weekday() - aditi_wo) % 7 or 7)
    for hh in ("10:05:00", "19:40:00"):
        punches.append(("mall", "101", worked.isoformat(), f"{worked.isoformat()} {hh}", 0, stamp))
    conn.executemany("INSERT OR IGNORE INTO punches VALUES (?,?,?,?,?,?)", punches)

    password = os.environ.get("DEMO_PASSWORD", "")
    ids = {}
    if password:
        h = generate_password_hash(password)
        for username, display, role, store, uid in (
                ("manager.demo", "Rahul (demo Manager)", "manager", "mall", "102"),
                ("cro.demo", "Aditi (demo CRO)", "cro", "mall", "101")):
            cur = conn.execute(
                "INSERT INTO users (username, display_name, password_hash, role, emp_store, emp_user_id, "
                "created_at, password_changed_at) VALUES (?,?,?,?,?,?,?,?)",
                (username, display, h, role, store, uid, stamp, stamp))
            ids[username] = cur.lastrowid
        conn.execute("INSERT INTO user_stores (user_id, store) VALUES (?, 'mall')", (ids["manager.demo"],))
    cro = ids.get("cro.demo", owner)
    mgr = ids.get("manager.demo", owner)

    def leave(store, uid, a, b, status, comment, mcomment="", by=owner, created_by=None):
        conn.execute(
            "INSERT INTO leaves (store, user_id, start_date, end_date, staff_comment, status, "
            "manager_comment, created_by, created_at, decided_by, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (store, uid, a.isoformat(), b.isoformat(), comment, status, mcomment, created_by or by, stamp,
             by if status in ("approved", "rejected") else None,
             stamp if status in ("approved", "rejected") else None))

    # Aditi: an approved leave a few days after her worked weekly off -> comp-off
    lv_day = worked + timedelta(days=2)
    if lv_day.weekday() == aditi_wo:
        lv_day += timedelta(days=1)
    conn.execute("DELETE FROM punches WHERE store='mall' AND user_id='101' AND day=?", (lv_day.isoformat(),))
    leave("mall", "101", lv_day, lv_day, "approved", "Doctor's appointment", "Get well soon", by=mgr, created_by=cro)
    # Aditi: a pending request next week
    nxt = today + timedelta(days=5)
    leave("mall", "101", nxt, nxt + timedelta(days=1), "pending", "Sister's wedding", created_by=cro)
    # others
    for store, uid, back, length, status, comment in (
            ("mall", "105", 12, 3, "approved", "Family function"),
            ("mall", "107", 6, 1, "rejected", "Trip"),
            ("mall", "104", 3, 1, "pending", "Fever"),
            ("nirala", "203", 9, 2, "approved", "Wedding"),
            ("nirala", "206", -3, 1, "pending", "Bank work")):
        a = today - timedelta(days=back)
        b = a + timedelta(days=length - 1)
        if status == "approved" or (status == "rejected" and back > 0):
            conn.execute("DELETE FROM punches WHERE store=? AND user_id=? AND day BETWEEN ? AND ?",
                         (store, uid, a.isoformat(), b.isoformat()))
        leave(store, uid, a, b, status, comment, "Stock count that day" if status == "rejected" else "")
    # the manager's own request, which only the Owner can approve
    leave("mall", "102", today + timedelta(days=9), today + timedelta(days=9), "pending",
          "Family function", created_by=mgr)

    db.audit(conn, None, "demo.seed", "", {"punches": len(punches)})
    conn.commit()
    log(f"Demo data: 2 stores, {sum(len(v) for v in STAFF.values())} people, {len(punches)} punches.")
    return True
