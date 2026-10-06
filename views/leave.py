"""Leave requests (/leave), approvals (/approvals) and comp-off checks."""

from datetime import date, timedelta

from flask import (Blueprint, abort, flash, g, jsonify, redirect, render_template, request,
                   url_for)

import auth
import clock
import db
from attendance import StoreData, daterange, fmt_day, leave_from_row, working_days
from views.common import pending_for

bp = Blueprint("leave", __name__)

MAX_SPAN_DAYS = 31
MAX_COMMENT = 300


def leave_problem(conn, store, user_id, start, end, comment):
    """Why this leave can't be saved, or None."""
    if start is None or end is None:
        return "Choose both dates."
    today = clock.today()
    if not (today - timedelta(days=366) <= start and end <= today + timedelta(days=366)):
        return "Leave can only be for dates within a year of today - check the year."
    if end < start:
        return "The end date is before the start date."
    if (end - start).days + 1 > MAX_SPAN_DAYS:
        return f"A single request can cover at most {MAX_SPAN_DAYS} days - check the year."
    if len(comment) > MAX_COMMENT:
        return f"The reason can be at most {MAX_COMMENT} characters."
    clash = conn.execute(
        "SELECT start_date, end_date, status FROM leaves WHERE store=? AND user_id=? "
        "AND status IN ('pending','approved') AND start_date <= ? AND end_date >= ?",
        (store, str(user_id), end.isoformat(), start.isoformat())).fetchone()
    if clash:
        return (f"This overlaps a {clash['status']} leave "
                f"({clash['start_date']} to {clash['end_date']}).")
    return None


def form_date(name):
    try:
        return date.fromisoformat(request.form.get(name, "").strip())
    except ValueError:
        return None


def preview_text(conn, store, user_id, start, end):
    """'2 working days - 1 covered by comp-off (worked Sun 27 Sep), 1 leave'."""
    now = clock.now_local()
    data = StoreData(conn, store, start, end, now, user_ids=[user_id])
    days, covered = data.leave_preview(user_id, start, end)
    n = len(days)
    text = f"{n} working day{'s' if n != 1 else ''}"
    if covered:
        worked = ", ".join(f"worked {fmt_day(c)}" for _d, c in covered)
        rest = n - len(covered)
        text += f" - {len(covered)} covered by comp-off ({worked}), {rest} leave"
    a = data.allowance(user_id)
    if a is not None:
        text += f" · {max(a.left, 0)} of {a.allowance} leaves left this year"
    return text, n, covered


# ----------------------------------------------------------------- own leave

def own_person():
    # an external Manager (not on any device) has no leave of their own
    if g.user["role"] not in ("cro", "manager") or g.user["emp_user_id"] is None:
        abort(404)
    return g.user["emp_store"], g.user["emp_user_id"]


@bp.route("/leave", methods=["GET", "POST"])
@auth.require_role("cro", "manager")
def my_leave():
    conn = db.get_db()
    store, user_id = own_person()       # always the signed-in person, never the form's
    error = None
    form = {"from": "", "to": "", "reason": ""}
    if request.method == "POST":
        start, end = form_date("from"), form_date("to")
        reason = request.form.get("reason", "").strip()
        form = {"from": request.form.get("from", ""), "to": request.form.get("to", ""),
                "reason": reason}
        error = leave_problem(conn, store, user_id, start, end, reason)
        if error is None:
            cur = conn.execute(
                "INSERT INTO leaves (store, user_id, start_date, end_date, staff_comment, status, "
                "created_by, created_at) VALUES (?,?,?,?,?,'pending',?,?)",
                (store, user_id, start.isoformat(), end.isoformat(), reason, g.user["id"],
                 db.utc_now_iso()))
            db.audit(conn, g.user["id"], "leave.request", f"leave:{cur.lastrowid}",
                     {"store": store, "user_id": user_id, "from": start.isoformat(),
                      "to": end.isoformat()}, auth.client_ip())
            conn.commit()
            flash("Leave request sent.", "ok")
            return redirect(url_for("leave.my_leave"))

    now = clock.now_local()
    data = StoreData(conn, store, now.date(), now.date(), now, user_ids=[user_id])
    credits = [c for c in data.credits(user_id) if c.status == "available"]
    allow = data.allowance(user_id)
    rows = conn.execute(
        "SELECT l.*, u.display_name AS decided_by_name FROM leaves l "
        "LEFT JOIN users u ON u.id=l.decided_by WHERE l.store=? AND l.user_id=? "
        "ORDER BY l.start_date DESC, l.id DESC LIMIT 50", (store, user_id)).fetchall()
    requests_ = []
    wo = data.wo_history(user_id)
    for r in rows:
        lv = leave_from_row(r)
        requests_.append((r, len(working_days(lv.start, lv.end, wo))))
    return render_template("leave.html", error=error, form=form, requests=requests_,
                           credits=credits, today=now.date(), max_comment=MAX_COMMENT,
                           allow=allow)


@bp.get("/leave/preview")
@auth.require_role("cro", "manager")
def my_leave_preview():
    store, user_id = own_person()
    try:
        start = date.fromisoformat(request.args.get("from", ""))
        end = date.fromisoformat(request.args.get("to", ""))
    except ValueError:
        return jsonify({"text": ""})
    if leave_problem(db.get_db(), store, "-", start, end, "") is not None:
        return jsonify({"text": ""})
    text, _n, _covered = preview_text(db.get_db(), store, user_id, start, end)
    return jsonify({"text": text + (" if approved" if _covered else "")})


@bp.post("/leave/<int:leave_id>/cancel")
@auth.require_role("cro", "manager")
def cancel_leave(leave_id):
    conn = db.get_db()
    store, user_id = own_person()
    row = conn.execute("SELECT * FROM leaves WHERE id=?", (leave_id,)).fetchone()
    # only their own, and only while pending; anything else looks like it doesn't exist
    if row is None or row["store"] != store or row["user_id"] != str(user_id):
        abort(404)
    if row["status"] != "pending":
        flash("Only a pending request can be cancelled.", "error")
        return redirect(url_for("leave.my_leave"))
    conn.execute("UPDATE leaves SET status='cancelled' WHERE id=?", (leave_id,))
    db.audit(conn, g.user["id"], "leave.cancel", f"leave:{leave_id}", ip=auth.client_ip())
    conn.commit()
    flash("Request cancelled.", "ok")
    return redirect(url_for("leave.my_leave"))


# ----------------------------------------------------------------- approvals

def actable_people(conn):
    """(store, user_id, name) for everyone this person may act for."""
    out = []
    mine = auth.stores_for(g.user, conn)
    for e in conn.execute("SELECT * FROM employees WHERE hidden=0 "
                          "ORDER BY store, CAST(user_id AS INTEGER), user_id"):
        if e["store"] in mine and auth.can_act_for(g.user, e["store"], e["user_id"], conn):
            out.append(e)
    return out


def comp_checks(conn):
    """Every weekly off worked in the last 30 days by people this person may
    act for, with hours and where the comp-off went."""
    now = clock.now_local()
    start = now.date() - timedelta(days=30)
    out = []
    people = actable_people(conn)
    by_store = {}
    for e in people:
        by_store.setdefault(e["store"], []).append(e)
    for store, emps in by_store.items():
        data = StoreData(conn, store, start, now.date(), now)
        for e in emps:
            uid = e["user_id"]
            wo = data.wo_history(uid)
            credits = {c.day: c for c in data.credits(uid)}
            spans = data.spans.get(uid, {})
            days = set()
            for d in daterange(start, now.date()):
                if (d in spans and d.weekday() in wo.days_on(d)) or d in credits \
                        or d in data.cancels.get(uid, set()):
                    days.add(d)
            for d in sorted(days, reverse=True):
                span = spans.get(d)
                hours = (span[1] - span[0]).total_seconds() / 3600 if span else 0
                c = credits.get(d)
                cancelled = d in data.cancels.get(uid, set())
                if c and c.status == "used":
                    where = f"covers {fmt_day(c.covers)}"
                elif c and c.status == "available":
                    where = f"available until {fmt_day(c.until)}"
                elif c:
                    where = "expired"
                elif cancelled:
                    where = "cancelled"
                elif data.cfg.comp_from is None:
                    where = "comp-offs not switched on"
                elif d < data.cfg.comp_from:
                    where = "before the comp-off start date"
                else:
                    where = f"below {data.cfg.min_hours:g} h - none"
                out.append({"emp": e, "day": d, "span": span, "hours": hours,
                            "credit": c, "granted": bool(c and c.granted),
                            "cancelled": cancelled, "where": where})
    out.sort(key=lambda r: r["day"], reverse=True)
    return out


@bp.get("/approvals")
@auth.require_role("owner", "manager")
def approvals():
    conn = db.get_db()
    pending = []
    for r in pending_for(g.user, conn):
        text, _n, _c = preview_text(conn, r["store"], r["user_id"],
                                    date.fromisoformat(r["start_date"]),
                                    date.fromisoformat(r["end_date"]))
        pending.append((r, text))
    since = (clock.now_local() - timedelta(days=30)).strftime("%Y-%m-%d")
    decided = [r for r in conn.execute(
        "SELECT l.*, e.name AS emp_name, u.display_name AS decided_by_name FROM leaves l "
        "LEFT JOIN employees e ON e.store=l.store AND e.user_id=l.user_id "
        "LEFT JOIN users u ON u.id=l.decided_by "
        "WHERE l.status IN ('approved','rejected') AND l.decided_at >= ? "
        "ORDER BY l.decided_at DESC LIMIT 100", (since,)).fetchall()
        if auth.can_act_for(g.user, r["store"], r["user_id"], conn)]
    return render_template("approvals.html", pending=pending, decided=decided,
                           people=actable_people(conn), checks=comp_checks(conn),
                           today=clock.today(), max_comment=MAX_COMMENT)


def leave_for_action(conn, leave_id):
    row = conn.execute("SELECT * FROM leaves WHERE id=?", (leave_id,)).fetchone()
    if row is None or not auth.can_act_for(g.user, row["store"], row["user_id"], conn):
        abort(404)
    return row


@bp.post("/approvals/<int:leave_id>/decide")
@auth.require_role("owner", "manager")
def decide(leave_id):
    conn = db.get_db()
    row = leave_for_action(conn, leave_id)
    decision = request.form.get("decision")
    comment = request.form.get("comment", "").strip()[:MAX_COMMENT]
    if decision not in ("approve", "reject"):
        abort(400)
    if row["status"] != "pending":
        flash("That request has already been decided or cancelled.", "error")
        return redirect(url_for("leave.approvals"))
    status = "approved" if decision == "approve" else "rejected"
    conn.execute("UPDATE leaves SET status=?, manager_comment=?, decided_by=?, decided_at=? "
                 "WHERE id=? AND status='pending'",
                 (status, comment, g.user["id"], db.utc_now_iso(), leave_id))
    db.audit(conn, g.user["id"], f"leave.{decision}", f"leave:{leave_id}",
             {"store": row["store"], "user_id": row["user_id"], "comment": comment},
             auth.client_ip())
    conn.commit()
    flash(f"Leave {status}.", "ok")
    return redirect(url_for("leave.approvals"))


@bp.post("/approvals/<int:leave_id>/withdraw")
@auth.require_role("owner", "manager")
def withdraw(leave_id):
    conn = db.get_db()
    row = leave_for_action(conn, leave_id)
    reason = request.form.get("reason", "").strip()[:MAX_COMMENT]
    if row["status"] != "approved":
        flash("Only an approved leave can be withdrawn.", "error")
    elif not reason:
        flash("Give a reason for withdrawing the leave.", "error")
    else:
        conn.execute("UPDATE leaves SET status='rejected', manager_comment=?, decided_by=?, "
                     "decided_at=? WHERE id=?",
                     (f"withdrawn: {reason}", g.user["id"], db.utc_now_iso(), leave_id))
        db.audit(conn, g.user["id"], "leave.withdraw", f"leave:{leave_id}",
                 {"store": row["store"], "user_id": row["user_id"], "reason": reason},
                 auth.client_ip())
        conn.commit()
        flash("Leave withdrawn.", "ok")
    return redirect(url_for("leave.approvals"))


@bp.post("/leave/<int:leave_id>/revoke")
@auth.require_role("owner")
def revoke(leave_id):
    """The Owner cancels a leave that was approved (by anyone, at any time).
    The leave is then ignored, so its days count from the punches as if it
    had never been asked for. Its history stays in the activity log."""
    conn = db.get_db()
    row = leave_for_action(conn, leave_id)
    reason = request.form.get("reason", "").strip()[:MAX_COMMENT]
    back = request.form.get("back", "")
    if not back.startswith("/") or back.startswith("//"):
        back = url_for("leave.approvals")
    if row["status"] != "approved":
        flash("Only an approved leave can be cancelled.", "error")
    elif not reason:
        flash("Give a reason for cancelling the leave.", "error")
    else:
        conn.execute("UPDATE leaves SET status='cancelled', manager_comment=?, decided_by=?, "
                     "decided_at=? WHERE id=? AND status='approved'",
                     (f"cancelled: {reason}", g.user["id"], db.utc_now_iso(), leave_id))
        db.audit(conn, g.user["id"], "leave.revoke", f"leave:{leave_id}",
                 {"store": row["store"], "user_id": row["user_id"], "from": row["start_date"],
                  "to": row["end_date"], "approved_by": row["decided_by"], "reason": reason},
                 auth.client_ip())
        conn.commit()
        flash("Leave cancelled.", "ok")
    return redirect(back)


def form_person(conn):
    """The person chosen in a form, as (store, user_id) - checked."""
    raw = request.form.get("person", "")
    store, _, user_id = raw.partition(":")
    if not store or not user_id:
        abort(400)
    exists = conn.execute("SELECT 1 FROM employees WHERE store=? AND user_id=?",
                          (store, user_id)).fetchone()
    if not exists or not auth.can_act_for(g.user, store, user_id, conn):
        abort(404)
    return store, user_id


@bp.post("/approvals/record")
@auth.require_role("owner", "manager")
def record():
    conn = db.get_db()
    store, user_id = form_person(conn)
    start, end = form_date("from"), form_date("to")
    note = request.form.get("note", "").strip()
    problem = leave_problem(conn, store, user_id, start, end, note)
    if problem:
        flash(problem, "error")
        return redirect(url_for("leave.approvals"))
    now = db.utc_now_iso()
    cur = conn.execute(
        "INSERT INTO leaves (store, user_id, start_date, end_date, staff_comment, status, "
        "created_by, created_at, decided_by, decided_at) VALUES (?,?,?,?,?,'approved',?,?,?,?)",
        (store, user_id, start.isoformat(), end.isoformat(), note, g.user["id"], now,
         g.user["id"], now))
    db.audit(conn, g.user["id"], "leave.record", f"leave:{cur.lastrowid}",
             {"store": store, "user_id": user_id, "from": start.isoformat(),
              "to": end.isoformat()}, auth.client_ip())
    conn.commit()
    flash("Leave recorded as approved.", "ok")
    return redirect(url_for("leave.approvals"))


MARK_CODES = {"P": "present", "LT": "late", "A": "absent", "WO": "weekly off"}


@bp.post("/mark")
@auth.require_role("owner")          # corrections are the Owner's alone
def mark_day():
    """Set, or clear, one person's status for one day by hand - for a
    forgotten punch, a punch made for someone else, a swapped day off."""
    conn = db.get_db()
    store, user_id = form_person(conn)
    day = form_date("day")
    code = request.form.get("code", "")
    note = request.form.get("note", "").strip()[:MAX_COMMENT]
    back = request.form.get("back") or url_for("main.home")
    if not back.startswith("/") or back.startswith("//"):
        back = url_for("main.home")
    if day is None or day > clock.today() or day < clock.today() - timedelta(days=366):
        flash("Choose a day in the last year that has already happened.", "error")
        return redirect(back)
    target = f"{store}:{user_id}:{day.isoformat()}"
    if code == "clear":
        removed = conn.execute("DELETE FROM day_overrides WHERE store=? AND user_id=? AND day=?",
                               (store, user_id, day.isoformat())).rowcount
        if removed:
            db.audit(conn, g.user["id"], "day.clear", target, {"note": note}, auth.client_ip())
            conn.commit()
            flash("Correction removed - the day follows the punches again.", "ok")
        return redirect(back)
    if code not in MARK_CODES:
        abort(400)
    if not note:
        flash("Give a reason (e.g. forgot to punch) - it's shown on the calendar and kept in the "
              "activity log.", "error")
        return redirect(back)
    conn.execute(
        "INSERT INTO day_overrides (store, user_id, day, code, note, set_by, set_at) VALUES "
        "(?,?,?,?,?,?,?) ON CONFLICT(store, user_id, day) DO UPDATE SET code=excluded.code, "
        "note=excluded.note, set_by=excluded.set_by, set_at=excluded.set_at",
        (store, user_id, day.isoformat(), code, note, g.user["id"], db.utc_now_iso()))
    db.audit(conn, g.user["id"], "day.mark", target, {"code": code, "note": note}, auth.client_ip())
    conn.commit()
    flash(f"Marked {MARK_CODES[code]} for {fmt_day(day)}.", "ok")
    return redirect(back)


@bp.post("/comp/<kind>")
@auth.require_role("owner", "manager")
def comp_adjust(kind):
    if kind not in ("cancel", "grant"):
        abort(404)
    conn = db.get_db()
    store, user_id = form_person(conn)
    day = form_date("day")
    note = request.form.get("note", "").strip()[:MAX_COMMENT]
    back = request.form.get("back") or url_for("leave.approvals")
    if not back.startswith("/") or back.startswith("//"):
        back = url_for("leave.approvals")
    if day is None or day > clock.today() or day < clock.today() - timedelta(days=366):
        flash("Choose a day in the last year that has already happened.", "error")
        return redirect(back)
    if not note:
        flash("Give a reason - it's kept in the activity log.", "error")
        return redirect(back)
    # a day is either cancelled or granted, never both
    conn.execute("DELETE FROM comp_adjustments WHERE store=? AND user_id=? AND day=?",
                 (store, user_id, day.isoformat()))
    conn.execute("INSERT INTO comp_adjustments (store, user_id, day, kind, note, set_by, set_at) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (store, user_id, day.isoformat(), kind, note, g.user["id"], db.utc_now_iso()))
    db.audit(conn, g.user["id"], f"comp.{kind}", f"{store}:{user_id}:{day.isoformat()}",
             {"note": note}, auth.client_ip())
    conn.commit()
    flash("Comp-off cancelled." if kind == "cancel" else "Comp-off granted.", "ok")
    return redirect(back)
