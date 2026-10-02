"""Owner and Manager admin pages: stores, accounts, weekly offs, activity
log, the old reports archive and the database backup."""

import json
import os
import re
import secrets
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path

from flask import (Blueprint, abort, after_this_request, current_app, flash, g, redirect,
                   render_template, request, send_file, send_from_directory, url_for)
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

import auth
import clock
import db
from attendance import WEEKDAY_NAMES, CompConfig, StoreData, load_shifts, parse_hhmm
from views.api import hash_key
from views.common import store_row, sync_state

bp = Blueprint("admin", __name__)

STORE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
USERNAME = re.compile(r"^[A-Za-z0-9._-]{2,40}$")
MIN_KEY = 20


def ip():
    return auth.client_ip()


# ----------------------------------------------------------------- stores

def store_form():
    """(values, problem) from the store form."""
    name = request.form.get("display_name", "").strip()
    try:
        start = parse_hhmm(request.form.get("start_time", ""))
        close = parse_hhmm(request.form.get("close_time", ""))
        grace = int(request.form.get("grace_minutes", "0"))
    except (ValueError, TypeError):
        return None, "Times look like 09:00, and grace is a number of minutes."
    if not name:
        return None, "Give the store a name."
    if not 0 <= grace <= 120:
        return None, "Grace must be between 0 and 120 minutes."
    if close <= start:
        return None, "Closing time must be after the start time."
    return {"display_name": name, "start_time": start.strftime("%H:%M"),
            "grace_minutes": grace, "close_time": close.strftime("%H:%M")}, None


def stores_page(new_key=None):
    conn = db.get_db()
    now = clock.now_local()
    rows = []
    for r in conn.execute("SELECT * FROM stores ORDER BY store"):
        staff = conn.execute("SELECT COUNT(*) FROM employees WHERE store=? AND on_device=1 AND hidden=0",
                             (r["store"],)).fetchone()[0]
        stale, _ = sync_state(r, now)
        rows.append({"row": r, "staff": staff, "stale": stale,
                     "shifts": load_shifts(conn, r["store"])})
    cfg = CompConfig.load(conn)
    return render_template("stores.html", stores=rows, cfg=cfg, new_key=new_key,
                           today=now.date())


@bp.get("/stores")
@auth.require_role("owner")
def stores():
    return stores_page()


@bp.post("/stores/add")
@auth.require_role("owner")
def add_store():
    conn = db.get_db()
    store = request.form.get("store", "").strip().lower()
    values, problem = store_form()
    if not STORE_ID.match(store):
        problem = "The store ID is lower-case letters, numbers, - or _ (e.g. mall)."
    elif conn.execute("SELECT 1 FROM stores WHERE store=?", (store,)).fetchone():
        problem = "There's already a store with that ID."
    if problem:
        flash(problem, "error")
        return redirect(url_for("admin.stores"))
    conn.execute("INSERT INTO stores (store, display_name, start_time, grace_minutes, close_time) "
                 "VALUES (?,?,?,?,?)", (store, values["display_name"], values["start_time"],
                                        values["grace_minutes"], values["close_time"]))
    db.audit(conn, g.user["id"], "store.add", store, values, ip())
    conn.commit()
    flash(f"Added {store}. Now enter its sync key.", "ok")
    return redirect(url_for("admin.stores"))


@bp.post("/stores/<store>/edit")
@auth.require_role("owner")
def edit_store(store):
    conn = db.get_db()
    store_row(store)
    values, problem = store_form()
    if problem:
        flash(problem, "error")
        return redirect(url_for("admin.stores"))
    conn.execute("UPDATE stores SET display_name=?, start_time=?, grace_minutes=?, close_time=? "
                 "WHERE store=?", (values["display_name"], values["start_time"],
                                   values["grace_minutes"], values["close_time"], store))
    db.audit(conn, g.user["id"], "store.edit", store, values, ip())
    conn.commit()
    flash(f"Saved {store}.", "ok")
    return redirect(url_for("admin.stores"))


@bp.post("/stores/<store>/key")
@auth.require_role("owner")
def store_key(store):
    conn = db.get_db()
    store_row(store)
    action = request.form.get("action")
    if action == "enter":
        key = request.form.get("key", "").strip()
        if len(key) < MIN_KEY:
            flash(f"That key is too short - paste the whole sync_key from the store's "
                  f"settings file (at least {MIN_KEY} characters).", "error")
            return redirect(url_for("admin.stores"))
        shown = None
    elif action == "create":
        key = secrets.token_urlsafe(32)
        shown = (store, key)
    else:
        abort(400)
    hashed = hash_key(key)
    clash = conn.execute("SELECT store FROM stores WHERE sync_key_hash=? AND store!=?",
                         (hashed, store)).fetchone()
    if clash:
        flash(f"That key already belongs to {clash['store']}. Each store needs its own.", "error")
        return redirect(url_for("admin.stores"))
    conn.execute("UPDATE stores SET sync_key_hash=? WHERE store=?", (hashed, store))
    db.audit(conn, g.user["id"], f"store.key.{action}", store, ip=ip())
    conn.commit()
    if shown:
        # shown once, on this page only - never stored or logged
        return stores_page(new_key=shown)
    flash(f"Key saved for {store}. Its next sync will be accepted.", "ok")
    return redirect(url_for("admin.stores"))


@bp.post("/stores/<store>/policy")
@auth.require_role("owner")
def store_policy(store):
    """Which rules a store uses: weekly offs, comp-offs, a yearly leave
    allowance."""
    conn = db.get_db()
    store_row(store)
    uses_wo = 1 if request.form.get("uses_weekly_offs") == "on" else 0
    uses_co = 1 if request.form.get("uses_comp_offs") == "on" else 0
    raw_allow = request.form.get("leave_allowance", "").strip()
    raw_from = request.form.get("leave_count_from", "").strip()
    try:
        allowance = int(raw_allow) if raw_allow else None
        month = int(request.form.get("leave_year_start_month", "6"))
        count_from = date.fromisoformat(raw_from) if raw_from else None
    except ValueError:
        flash("The allowance is a number of days, and the dates must be real dates.", "error")
        return redirect(url_for("admin.stores") + f"#s-{store}")
    if allowance is not None and not 0 <= allowance <= 366:
        flash("The allowance must be between 0 and 366 days.", "error")
        return redirect(url_for("admin.stores") + f"#s-{store}")
    if not 1 <= month <= 12 or (count_from and not (2020 <= count_from.year <= clock.today().year + 1)):
        flash("Choose a real month and a sensible 'count from' date.", "error")
        return redirect(url_for("admin.stores") + f"#s-{store}")
    values = {"uses_weekly_offs": uses_wo, "uses_comp_offs": uses_co, "leave_allowance": allowance,
              "leave_year_start_month": month,
              "leave_count_from": count_from.isoformat() if count_from else None}
    conn.execute("UPDATE stores SET uses_weekly_offs=?, uses_comp_offs=?, leave_allowance=?, "
                 "leave_year_start_month=?, leave_count_from=? WHERE store=?",
                 (uses_wo, uses_co, allowance, month, values["leave_count_from"], store))
    db.audit(conn, g.user["id"], "store.policy", store, values, ip())
    conn.commit()
    flash(f"Saved the leave rules for {store}.", "ok")
    return redirect(url_for("admin.stores") + f"#s-{store}")


@bp.route("/stores/<store>/tally", methods=["GET", "POST"])
@auth.require_role("owner")
def leave_tally(store):
    """The hand tally: leaves each person had already taken this leave year
    when the website started counting."""
    from attendance import LeavePolicy
    conn = db.get_db()
    row = store_row(store)
    policy = LeavePolicy.from_row(row)
    if policy.allowance is None:
        flash("Set a yearly leave allowance for this store first.", "error")
        return redirect(url_for("admin.stores") + f"#s-{store}")
    today = clock.today()
    if request.method == "POST":
        try:
            as_of = date.fromisoformat(request.form.get("tally_as_of", "").strip())
        except ValueError:
            as_of = None
        if as_of is None or as_of > today or as_of < today - timedelta(days=366):
            flash("The tally date must be a real date in the last year, not in the future.", "error")
            return redirect(url_for("admin.leave_tally", store=store))
        year_start, _ = policy.year_of(as_of)
        people = conn.execute("SELECT user_id FROM employees WHERE store=? AND hidden=0",
                              (store,)).fetchall()
        saved = {}
        for p in people:
            raw = request.form.get(f"used_{p['user_id']}", "").strip()
            if raw == "":
                conn.execute("DELETE FROM leave_tally WHERE store=? AND user_id=? AND year_start=?",
                             (store, p["user_id"], year_start.isoformat()))
                continue
            try:
                used = int(raw)
            except ValueError:
                flash(f"#{p['user_id']}: '{raw}' isn't a whole number.", "error")
                return redirect(url_for("admin.leave_tally", store=store))
            if not 0 <= used <= 366:
                flash(f"#{p['user_id']}: the number must be between 0 and 366.", "error")
                return redirect(url_for("admin.leave_tally", store=store))
            conn.execute("INSERT INTO leave_tally (store, user_id, year_start, used, set_by, set_at) "
                         "VALUES (?,?,?,?,?,?) ON CONFLICT(store, user_id, year_start) DO UPDATE SET "
                         "used=excluded.used, set_by=excluded.set_by, set_at=excluded.set_at",
                         (store, p["user_id"], year_start.isoformat(), used, g.user["id"],
                          db.utc_now_iso()))
            saved[p["user_id"]] = used
        conn.execute("UPDATE stores SET tally_as_of=? WHERE store=?", (as_of.isoformat(), store))
        db.audit(conn, g.user["id"], "leave.tally", store,
                 {"as_of": as_of.isoformat(), "used": saved}, ip())
        conn.commit()
        flash("Tally saved. The calendars now count from it.", "ok")
        return redirect(url_for("admin.leave_tally", store=store))

    as_of = policy.tally_as_of or today
    year_start, _ = policy.year_of(as_of)
    tallies = {r["user_id"]: r["used"] for r in conn.execute(
        "SELECT user_id, used FROM leave_tally WHERE store=? AND year_start=?",
        (store, year_start.isoformat()))}
    now = clock.now_local()
    data = StoreData(conn, store, now.date(), now.date(), now)
    rows = []
    for p in data.people:
        a = data.allowance(p["user_id"])
        rows.append({"p": p, "tally": tallies.get(p["user_id"]), "a": a})
    return render_template("tally.html", store=row, rows=rows, as_of=as_of, policy=policy,
                           year_start=year_start, today=today)


BULK_LINE = re.compile(r"^\s*#?\s*([A-Za-z0-9]+)\s*[,;:\t=\-–]+\s*(.+?)\s*$")


@bp.route("/stores/<store>/names", methods=["GET", "POST"])
@auth.require_role("owner")
def staff_names(store):
    """Names for the people on a device - for a device with no names stored
    on it (the office). A name set here is never overwritten by the sync."""
    conn = db.get_db()
    row = store_row(store)
    people = conn.execute("SELECT * FROM employees WHERE store=? AND hidden=0 "
                          "ORDER BY CAST(user_id AS INTEGER), user_id", (store,)).fetchall()
    known = {p["user_id"]: p for p in people}
    if request.method == "POST":
        wanted = {}
        for uid in known:
            if f"name_{uid}" in request.form:
                wanted[uid] = request.form.get(f"name_{uid}", "").strip()[:100]
        unknown = []
        for line in request.form.get("bulk", "").splitlines():
            if not line.strip():
                continue
            m = BULK_LINE.match(line)
            if not m or m.group(1) not in known:
                unknown.append(line.strip()[:60])
                continue
            wanted[m.group(1)] = m.group(2).strip()[:100]
        changed = 0
        for uid, name in wanted.items():
            p = known[uid]
            if name == (p["name"] or ""):
                continue
            conn.execute("UPDATE employees SET name=?, name_locked=? WHERE store=? AND user_id=?",
                         (name, 1 if name else 0, store, uid))
            changed += 1
        db.audit(conn, g.user["id"], "staff.names", store, {"changed": changed}, ip())
        conn.commit()
        flash(f"Saved {changed} name{'s' if changed != 1 else ''}.", "ok")
        if unknown:
            flash("Not understood or no such number on the device: " + "; ".join(unknown[:10])
                  + (" …" if len(unknown) > 10 else ""), "error")
        return redirect(url_for("admin.staff_names", store=store))
    missing = sum(1 for p in people if not p["name"])
    return render_template("names.html", store=row, people=people, missing=missing)


MAX_SHIFTS = 6


def shift_form():
    """(values, problem) from a shift form."""
    name = request.form.get("name", "").strip()
    try:
        start = parse_hhmm(request.form.get("start_time", ""))
        end = parse_hhmm(request.form.get("end_time", ""))
        grace = int(request.form.get("grace_minutes", "0"))
    except (ValueError, TypeError):
        return None, "Shift times look like 09:30, and grace is a number of minutes."
    if not name or len(name) > 30:
        return None, "Give the shift a short name, e.g. Morning."
    if end <= start:
        return None, "A shift must end after it starts (on the same day)."
    if not 0 <= grace <= 120:
        return None, "Grace must be between 0 and 120 minutes."
    return {"name": name, "start_time": start.strftime("%H:%M"), "end_time": end.strftime("%H:%M"),
            "grace_minutes": grace}, None


@bp.post("/stores/<store>/shifts/add")
@auth.require_role("owner")
def add_shift(store):
    conn = db.get_db()
    store_row(store)
    values, problem = shift_form()
    if problem is None and conn.execute("SELECT COUNT(*) FROM shifts WHERE store=?",
                                        (store,)).fetchone()[0] >= MAX_SHIFTS:
        problem = f"A store can have at most {MAX_SHIFTS} shifts."
    if problem is None and conn.execute("SELECT 1 FROM shifts WHERE store=? AND start_time=?",
                                        (store, values["start_time"])).fetchone():
        problem = "There's already a shift starting at that time."
    if problem:
        flash(problem, "error")
        return redirect(url_for("admin.stores") + f"#s-{store}")
    conn.execute("INSERT INTO shifts (store, name, start_time, end_time, grace_minutes) VALUES (?,?,?,?,?)",
                 (store, values["name"], values["start_time"], values["end_time"], values["grace_minutes"]))
    db.audit(conn, g.user["id"], "shift.add", store, values, ip())
    conn.commit()
    flash(f"Added the {values['name']} shift to {store}.", "ok")
    return redirect(url_for("admin.stores") + f"#s-{store}")


@bp.post("/shifts/<int:shift_id>/<action>")
@auth.require_role("owner")
def shift_action(shift_id, action):
    conn = db.get_db()
    row = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
    if row is None or action not in ("edit", "delete"):
        abort(404)
    store = row["store"]
    if action == "delete":
        conn.execute("DELETE FROM shifts WHERE id=?", (shift_id,))
        db.audit(conn, g.user["id"], "shift.delete", store, {"name": row["name"]}, ip())
        conn.commit()
        flash(f"Removed the {row['name']} shift.", "ok")
        return redirect(url_for("admin.stores") + f"#s-{store}")
    values, problem = shift_form()
    if problem is None and conn.execute("SELECT 1 FROM shifts WHERE store=? AND start_time=? AND id!=?",
                                        (store, values["start_time"], shift_id)).fetchone():
        problem = "There's already a shift starting at that time."
    if problem:
        flash(problem, "error")
        return redirect(url_for("admin.stores") + f"#s-{store}")
    conn.execute("UPDATE shifts SET name=?, start_time=?, end_time=?, grace_minutes=? WHERE id=?",
                 (values["name"], values["start_time"], values["end_time"], values["grace_minutes"], shift_id))
    db.audit(conn, g.user["id"], "shift.edit", store, values, ip())
    conn.commit()
    flash(f"Saved the {values['name']} shift.", "ok")
    return redirect(url_for("admin.stores") + f"#s-{store}")


@bp.post("/settings/comp")
@auth.require_role("owner")
def comp_settings():
    conn = db.get_db()
    try:
        hours = float(request.form.get("comp_min_hours", ""))
        window = int(request.form.get("comp_window_days", ""))
    except ValueError:
        flash("Hours and days must be numbers.", "error")
        return redirect(url_for("admin.stores"))
    raw_from = request.form.get("comp_from", "").strip()
    if raw_from:
        try:
            chosen = date.fromisoformat(raw_from)
        except ValueError:
            chosen = None
        if chosen is None or chosen.year < 2020 or chosen > clock.today() + timedelta(days=366):
            flash("The start date must be a real date between 2020 and a year from now.", "error")
            return redirect(url_for("admin.stores"))
    if not (0.5 <= hours <= 24 and 0 <= window <= 365):
        flash("Hours must be between 0.5 and 24, and days between 0 and 365.", "error")
        return redirect(url_for("admin.stores"))
    before = {k: db.get_setting(conn, k) for k in db.SETTING_DEFAULTS}
    db.set_setting(conn, "comp_min_hours", f"{hours:g}")
    db.set_setting(conn, "comp_window_days", str(window))
    db.set_setting(conn, "comp_from", raw_from)
    after = {k: db.get_setting(conn, k) for k in db.SETTING_DEFAULTS}
    db.audit(conn, g.user["id"], "settings.comp", "", {"before": before, "after": after}, ip())
    conn.commit()
    flash("Comp-off settings saved.", "ok")
    return redirect(url_for("admin.stores"))


# ----------------------------------------------------------------- accounts

def active_owner_count(conn):
    return conn.execute("SELECT COUNT(*) FROM users WHERE role='owner' AND active=1").fetchone()[0]


def user_or_404(conn, uid):
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if row is None:
        abort(404)
    return row


def people_without_login(conn, keep=None):
    rows = conn.execute(
        "SELECT e.* FROM employees e LEFT JOIN users u "
        "ON u.emp_store=e.store AND u.emp_user_id=e.user_id "
        "WHERE (u.id IS NULL AND e.hidden=0) OR (u.id = ?) "
        "ORDER BY e.store, CAST(e.user_id AS INTEGER), e.user_id",
        (keep or -1,)).fetchall()
    return rows


@bp.get("/users")
@auth.require_role("owner")
def users():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT u.*, e.name AS emp_name, "
        "(SELECT GROUP_CONCAT(store, ', ') FROM user_stores s WHERE s.user_id=u.id) AS covers "
        "FROM users u LEFT JOIN employees e ON e.store=u.emp_store AND e.user_id=u.emp_user_id "
        "ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'manager' THEN 1 ELSE 2 END, "
        "active DESC, username").fetchall()
    without = conn.execute(
        "SELECT COUNT(*) FROM employees e LEFT JOIN users u ON u.emp_store=e.store "
        "AND u.emp_user_id=e.user_id WHERE u.id IS NULL AND e.on_device=1 AND e.hidden=0").fetchone()[0]
    everyone = conn.execute("SELECT * FROM employees ORDER BY store, CAST(user_id AS INTEGER), "
                            "user_id").fetchall()
    return render_template("users.html", users=rows, now_iso=db.utc_now_iso(),
                           without_login=without, standard_password=STANDARD_PASSWORD,
                           hidden=[e for e in everyone if e["hidden"]],
                           visible=[e for e in everyone if not e["hidden"]])


@bp.post("/people/<action>")
@auth.require_role("owner")
def hide_person(action):
    """Hide someone on a device who isn't staff (e.g. the Owner, enrolled as
    the device admin) from every calendar, count and account creation - or
    show them again. Their punches stay stored."""
    if action not in ("hide", "show"):
        abort(404)
    conn = db.get_db()
    store, _, user_id = request.form.get("person", "").partition(":")
    if not conn.execute("SELECT 1 FROM employees WHERE store=? AND user_id=?",
                        (store, user_id)).fetchone():
        abort(404)
    conn.execute("UPDATE employees SET hidden=? WHERE store=? AND user_id=?",
                 (1 if action == "hide" else 0, store, user_id))
    db.audit(conn, g.user["id"], f"person.{action}", f"{store}:{user_id}", ip=ip())
    conn.commit()
    flash("Hidden from the calendars." if action == "hide" else "Shown on the calendars again.", "ok")
    return redirect(url_for("admin.users") + "#hidden")


STANDARD_PASSWORD = "tmpl@2026"


def username_for(conn, first_name, store, user_id, taken):
    """firstname + device number, e.g. aditi101; with the store added if
    that's taken (the same number can exist at both stores)."""
    base = re.sub(r"[^a-z]", "", (first_name or "").lower())[:20] or "staff"
    uid = re.sub(r"[^A-Za-z0-9]", "", str(user_id))[:10]
    for candidate in (f"{base}{uid}", f"{base}{uid}.{store}"):
        if candidate.lower() not in taken and USERNAME.match(candidate):
            return candidate
    return None


@bp.post("/users/bulk-cro")
@auth.require_role("owner")
def bulk_cro():
    """A CRO login for everyone on the devices who hasn't got one, all with
    the same starting password, which each must change at first sign-in."""
    conn = db.get_db()
    password = request.form.get("password", "").strip() or STANDARD_PASSWORD
    if len(password) < auth.MIN_PASSWORD:
        flash(f"The starting password must be at least {auth.MIN_PASSWORD} characters.", "error")
        return redirect(url_for("admin.users"))
    taken = {r[0].lower() for r in conn.execute("SELECT username FROM users")}
    people = conn.execute(
        "SELECT e.* FROM employees e LEFT JOIN users u ON u.emp_store=e.store AND u.emp_user_id=e.user_id "
        "WHERE u.id IS NULL AND e.on_device=1 AND e.hidden=0 "
        "ORDER BY e.store, CAST(e.user_id AS INTEGER), e.user_id").fetchall()
    hashed = generate_password_hash(password)     # one hash for all: same password
    now = db.utc_now_iso()
    created, skipped = [], []
    for e in people:
        if not (e["name"] or "").strip():
            skipped.append(e)            # name them first (Stores > names)
            continue
        first = (e["name"] or "").split()[0] if (e["name"] or "").split() else ""
        username = username_for(conn, first, e["store"], e["user_id"], taken)
        if username is None:
            skipped.append(e)
            continue
        taken.add(username.lower())
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, role, emp_store, emp_user_id, "
            "created_at, password_changed_at, must_change_password) VALUES (?,?,?,?,?,?,?,?,1)",
            (username, e["name"] or f"Staff {e['user_id']}", hashed, "cro", e["store"], e["user_id"],
             now, now))
        created.append((e, username))
    db.audit(conn, g.user["id"], "user.bulk_create", "", {"created": len(created),
                                                          "skipped": len(skipped)}, ip())
    conn.commit()
    return render_template("bulk_created.html", created=created, skipped=skipped,
                           password=password)


def account_form(conn, editing=None):
    """(values, problem) from the new/edit account form."""
    role = request.form.get("role", "")
    display = request.form.get("display_name", "").strip()
    person = request.form.get("person", "")
    covers = sorted(set(request.form.getlist("stores")))
    if role not in ("owner", "manager", "cro"):
        return None, "Choose a role."
    if not display:
        return None, "Give a display name."
    values = {"role": role, "display_name": display, "emp_store": None, "emp_user_id": None,
              "stores": []}
    if role != "owner":
        store, _, uid = person.partition(":")
        emp = conn.execute("SELECT * FROM employees WHERE store=? AND user_id=?",
                           (store, uid)).fetchone()
        if emp is None:
            return None, "Choose the person on the device this login belongs to."
        taken = conn.execute("SELECT id FROM users WHERE emp_store=? AND emp_user_id=?",
                             (store, uid)).fetchone()
        if taken and (editing is None or taken["id"] != editing):
            return None, "That person already has a login."
        values["emp_store"], values["emp_user_id"] = store, uid
    if role == "manager":
        known = set(auth.all_stores(conn))
        if not covers or not set(covers) <= known:
            return None, "Tick the store(s) this Manager covers."
        values["stores"] = covers
    return values, None


def save_stores(conn, uid, stores):
    conn.execute("DELETE FROM user_stores WHERE user_id=?", (uid,))
    for s in stores:
        conn.execute("INSERT INTO user_stores (user_id, store) VALUES (?,?)", (uid, s))


@bp.route("/users/new", methods=["GET", "POST"])
@auth.require_role("owner")
def new_user():
    conn = db.get_db()
    error = None
    shown = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        values, error = account_form(conn)
        password = request.form.get("password", "")
        if request.form.get("generate") == "on" or not password:
            password = auth.generate_password()
            shown = password
        if error is None and not USERNAME.match(username):
            error = "Usernames are 2-40 letters, numbers, dots, dashes or underscores."
        if error is None and conn.execute("SELECT 1 FROM users WHERE username=?",
                                          (username,)).fetchone():
            error = "That username is taken."
        if error is None:
            error = auth.password_problem(password, username)
        if error is None:
            now = db.utc_now_iso()
            cur = conn.execute(
                "INSERT INTO users (username, display_name, password_hash, role, emp_store, "
                "emp_user_id, created_at, password_changed_at, must_change_password) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (username, values["display_name"], generate_password_hash(password),
                 values["role"], values["emp_store"], values["emp_user_id"], now, now,
                 1 if request.form.get("must_change") == "on" else 0))
            save_stores(conn, cur.lastrowid, values["stores"])
            db.audit(conn, g.user["id"], "user.create", username,
                     {k: v for k, v in values.items()}, ip())
            conn.commit()
            return render_template("password_shown.html", username=username,
                                   password=password if shown else None, created=True)
    return render_template("user_form.html", u=None, error=error, form=request.form,
                           people=people_without_login(conn), stores=auth.all_stores(conn),
                           covers=request.form.getlist("stores"))


@bp.route("/users/<int:uid>/edit", methods=["GET", "POST"])
@auth.require_role("owner")
def edit_user(uid):
    conn = db.get_db()
    u = user_or_404(conn, uid)
    error = None
    if request.method == "POST":
        values, error = account_form(conn, editing=uid)
        if error is None and u["role"] == "owner" and values["role"] != "owner" \
                and u["active"] and active_owner_count(conn) <= 1:
            error = "This is the last active Owner - make someone else an Owner first."
        if error is None:
            conn.execute("UPDATE users SET display_name=?, role=?, emp_store=?, emp_user_id=?, "
                         "session_version=session_version+? WHERE id=?",
                         (values["display_name"], values["role"], values["emp_store"],
                          values["emp_user_id"], 1 if values["role"] != u["role"] else 0, uid))
            save_stores(conn, uid, values["stores"])
            db.audit(conn, g.user["id"], "user.edit", u["username"], values, ip())
            conn.commit()
            flash(f"Saved {u['username']}.", "ok")
            return redirect(url_for("admin.users"))
    covers = request.form.getlist("stores") if request.method == "POST" else [
        r["store"] for r in conn.execute("SELECT store FROM user_stores WHERE user_id=?", (uid,))]
    form = request.form if request.method == "POST" else {
        "role": u["role"], "display_name": u["display_name"],
        "person": f"{u['emp_store']}:{u['emp_user_id']}" if u["emp_user_id"] else ""}
    return render_template("user_form.html", u=u, error=error, form=form,
                           people=people_without_login(conn, keep=uid),
                           stores=auth.all_stores(conn), covers=covers)


@bp.post("/users/<int:uid>/<action>")
@auth.require_role("owner")
def user_action(uid, action):
    conn = db.get_db()
    u = user_or_404(conn, uid)
    name = u["username"]
    if action == "reset":
        password = request.form.get("password", "")
        generated = not password
        if generated:
            password = auth.generate_password()
        problem = auth.password_problem(password, name)
        if problem:
            flash(problem, "error")
            return redirect(url_for("admin.edit_user", uid=uid))
        conn.execute("UPDATE users SET password_hash=?, password_changed_at=?, failed_logins=0, "
                     "locked_until=NULL, session_version=session_version+1, must_change_password=? "
                     "WHERE id=?",
                     (generate_password_hash(password), db.utc_now_iso(),
                      1 if request.form.get("must_change") == "on" else 0, uid))
        db.audit(conn, g.user["id"], "user.reset_password", name, ip=ip())
        conn.commit()
        return render_template("password_shown.html", username=name,
                               password=password if generated else None, created=False)
    if action == "deactivate":
        if u["role"] == "owner" and u["active"] and active_owner_count(conn) <= 1:
            flash("This is the last active Owner and can't be deactivated.", "error")
            return redirect(url_for("admin.users"))
        conn.execute("UPDATE users SET active=0, session_version=session_version+1 WHERE id=?", (uid,))
    elif action == "reactivate":
        conn.execute("UPDATE users SET active=1, failed_logins=0, locked_until=NULL WHERE id=?", (uid,))
    elif action == "delete":
        if u["role"] == "owner" and active_owner_count(conn) <= 1 and u["active"]:
            flash("This is the last active Owner and can't be deleted.", "error")
            return redirect(url_for("admin.users"))
        used = conn.execute(
            "SELECT (SELECT COUNT(*) FROM leaves WHERE created_by=:i OR decided_by=:i "
            "        OR (store=:s AND user_id=:p)) "
            "     + (SELECT COUNT(*) FROM weekly_offs WHERE set_by=:i) "
            "     + (SELECT COUNT(*) FROM comp_adjustments WHERE set_by=:i)",
            {"i": uid, "s": u["emp_store"] or "", "p": u["emp_user_id"] or ""}).fetchone()[0]
        if used:
            flash("This account has leave or weekly-off history, so it can only be "
                  "deactivated. Delete is just for accounts made by mistake.", "error")
            return redirect(url_for("admin.users"))
        conn.execute("UPDATE audit_log SET actor=NULL WHERE actor=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
    else:
        abort(404)
    db.audit(conn, g.user["id"], f"user.{action}", name, ip=ip())
    conn.commit()
    if uid == g.user["id"] and action in ("deactivate", "delete"):
        return redirect(url_for("auth.login"))
    flash(f"{name}: {action}d.", "ok")
    return redirect(url_for("admin.users"))


# ----------------------------------------------------------------- weekly offs

@bp.get("/staff")
@auth.require_role("owner", "manager")
def staff():
    conn = db.get_db()
    now = clock.now_local()
    mine = sorted(auth.stores_for(g.user, conn))
    logins = {(r["emp_store"], r["emp_user_id"]): r for r in conn.execute(
        "SELECT * FROM users WHERE emp_user_id IS NOT NULL")}
    groups = []
    for store in mine:
        row = store_row(store)
        if not row["uses_weekly_offs"]:
            groups.append((row, None))          # this store has no weekly offs
            continue
        data = StoreData(conn, store, now.date(), now.date(), now)
        people = []
        for p in data.people:
            wo = data.wo_history(p["user_id"])
            days, eff = wo.current(now.date())
            upcoming = [(e, d) for e, d, _i in wo.rows if e > now.date()]
            people.append({"p": p, "login": logins.get((store, p["user_id"])),
                           "days": days, "from": eff, "upcoming": upcoming,
                           "can_act": auth.can_act_for(g.user, store, p["user_id"], conn)})
        groups.append((row, people))
    return render_template("staff.html", groups=groups, weekday_names=WEEKDAY_NAMES,
                           today=now.date())


@bp.post("/staff/weekly-off")
@auth.require_role("owner", "manager")
def set_weekly_off():
    conn = db.get_db()
    store, _, user_id = request.form.get("person", "").partition(":")
    if not conn.execute("SELECT 1 FROM employees WHERE store=? AND user_id=?",
                        (store, user_id)).fetchone() \
            or not auth.can_act_for(g.user, store, user_id, conn):
        abort(404)
    try:
        days = sorted({int(d) for d in request.form.getlist("days")})
        eff = date.fromisoformat(request.form.get("from", "").strip())
    except ValueError:
        flash("Choose the days and a valid 'from' date.", "error")
        return redirect(url_for("admin.staff"))
    if any(d < 0 or d > 6 for d in days):
        abort(400)
    if not days and request.form.get("none") != "on":
        flash("Tick the weekly off day(s), or tick 'No weekly off'.", "error")
        return redirect(url_for("admin.staff") + f"#p-{store}-{user_id}")
    weekdays = ",".join(str(d) for d in days)
    conn.execute("INSERT INTO weekly_offs (store, user_id, weekdays, effective_from, set_by, set_at) "
                 "VALUES (?,?,?,?,?,?)",
                 (store, user_id, weekdays, eff.isoformat(), g.user["id"], db.utc_now_iso()))
    db.audit(conn, g.user["id"], "weekly_off.set", f"{store}:{user_id}",
             {"weekdays": weekdays, "from": eff.isoformat()}, ip())
    conn.commit()
    flash("Weekly off saved.", "ok")
    return redirect(url_for("admin.staff") + f"#p-{store}-{user_id}")


# ----------------------------------------------------------------- activity log

@bp.get("/audit")
@auth.require_role("owner")
def audit_log():
    conn = db.get_db()
    kind = request.args.get("kind", "").strip()
    q = ("SELECT a.*, u.username FROM audit_log a LEFT JOIN users u ON u.id=a.actor")
    args = []
    if kind:
        q += " WHERE a.action LIKE ?"
        args.append(kind + "%")
    q += " ORDER BY a.id DESC LIMIT 300"
    rows = []
    for r in conn.execute(q, args):
        try:
            details = json.loads(r["details"]) if r["details"] else {}
        except ValueError:
            details = {"raw": r["details"]}
        rows.append((r, details))
    return render_template("audit.html", rows=rows, kind=kind)


# ----------------------------------------------------------------- archive

def archive_root():
    return Path(current_app.config["STORAGE_DIR"])


@bp.get("/archive")
@auth.require_role("owner")
def archive():
    root = archive_root()
    groups = []
    if root.is_dir():
        for folder in sorted(root.iterdir()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            files = sorted((p for p in folder.iterdir() if p.is_file()),
                           key=lambda p: p.name, reverse=True)
            groups.append((folder.name, [(p.name, p.stat().st_size) for p in files]))
    return render_template("archive.html", groups=groups)


@bp.get("/archive/<folder>/<name>")
@auth.require_role("owner")
def archive_file(folder, name):
    safe_dir, safe = secure_filename(folder), secure_filename(name)
    if not safe_dir or not safe or safe_dir != folder or safe != name or folder.startswith("."):
        abort(404)
    target = archive_root() / safe_dir / safe
    if not target.is_file():
        abort(404)
    return send_from_directory(archive_root() / safe_dir, safe, as_attachment=True)


# ----------------------------------------------------------------- backup

@bp.get("/backup")
@auth.require_role("owner")
def backup():
    """A consistent copy made with SQLite's own backup function - safe while
    the other server process is writing, unlike copying the file."""
    src = db.get_db()
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    dest = sqlite3.connect(tmp)
    try:
        src.backup(dest)
    finally:
        dest.close()
    db.audit(src, g.user["id"], "backup.download", "", ip=ip())
    src.commit()

    @after_this_request
    def _cleanup(response):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return response

    stamp = clock.now_local().strftime("%Y-%m-%d_%H%M")
    return send_file(tmp, as_attachment=True, download_name=f"attendance-backup-{stamp}.db")
