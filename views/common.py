"""Shared helpers for the page views."""

from datetime import date, datetime, timedelta, timezone

from flask import abort, g

import auth
import clock
import db
from attendance import StoreRules

SYNC_STALE_MINUTES = 60


def store_row(store):
    row = db.get_db().execute("SELECT * FROM stores WHERE store=?", (store,)).fetchone()
    if row is None:
        abort(404)
    return row


def require_store(store):
    """404 unless the signed-in person may see this store's calendars."""
    if store not in auth.stores_for(g.user):
        abort(404)
    return store_row(store)


def parse_day(text):
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        abort(404)


def parse_month(text):
    try:
        y, m = (int(x) for x in text.split("-"))
        return date(y, m, 1)
    except (AttributeError, TypeError, ValueError):
        abort(404)


def shift_month(first, delta):
    y, m = first.year, first.month + delta
    while m < 1:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return date(y, m, 1)


def pending_for(user, conn=None):
    """Pending leave requests this person may decide on."""
    conn = conn or db.get_db()
    if user is None or user["role"] == "cro":
        return []
    rows = conn.execute(
        "SELECT l.*, e.name AS emp_name, u.role AS emp_role FROM leaves l "
        "LEFT JOIN employees e ON e.store=l.store AND e.user_id=l.user_id "
        "LEFT JOIN users u ON u.emp_store=l.store AND u.emp_user_id=l.user_id "
        "WHERE l.status='pending' ORDER BY l.start_date, l.id").fetchall()
    return [r for r in rows if auth.can_act_for(user, r["store"], r["user_id"], conn)]


def sync_state(row, now_local=None):
    """(stale, message) for a store's last sync."""
    now_local = now_local or clock.now_local()
    rules = StoreRules.from_row(row)
    open_now = rules.start_time <= now_local.time() <= rules.close_time
    if not row["last_sync_at"]:
        return True, "never synced"
    last = datetime.strptime(row["last_sync_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - last
    stale = open_now and age > timedelta(minutes=SYNC_STALE_MINUTES)
    return stale, ""


def header_context():
    user = g.get("user")
    if user is None:
        return {"me": None}
    conn = db.get_db()
    ctx = {"me": user, "pending_count": 0, "sync_warnings": []}
    if user["role"] in ("owner", "manager"):
        ctx["pending_count"] = len(pending_for(user, conn))
        mine = auth.stores_for(user, conn)
        now = clock.now_local()
        for row in conn.execute("SELECT * FROM stores ORDER BY store"):
            if row["store"] not in mine:
                continue
            stale, _ = sync_state(row, now)
            if stale:
                ctx["sync_warnings"].append(
                    (row["store"], f"{row['display_name']} hasn't synced since "
                                   f"{_ago(row['last_sync_at'])}. The PC may be off, offline, "
                                   f"or unable to reach the device."))
            if row["clock_warning"]:
                ctx["sync_warnings"].append((row["store"], f"{row['display_name']}: {row['clock_warning']}"))
    return ctx


def _ago(value):
    if not value:
        return "it was set up"
    dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.astimezone(clock.REPORT_TZ).strftime("%d %b, %H:%M")


def employee(store, user_id):
    row = db.get_db().execute("SELECT * FROM employees WHERE store=? AND user_id=?",
                              (store, str(user_id))).fetchone()
    return row


def person_label(store, user_id, name=None):
    if name is None:
        e = employee(store, user_id)
        name = e["name"] if e else ""
    return f"{user_id} {name}".strip()
