"""Home page and the three calendars (month, day, person)."""

from datetime import date, datetime, timedelta

from flask import Blueprint, abort, g, redirect, render_template, request, url_for

import auth
import clock
import db
from attendance import (StoreData, daterange, fmt_day, month_bounds, natural_key,
                        summarize)
from views.common import (employee, parse_day, parse_month, pending_for, require_store,
                          shift_month, store_row, sync_state)

bp = Blueprint("main", __name__)


# ----------------------------------------------------------------- home

@bp.get("/")
@auth.login_required
def home():
    conn = db.get_db()
    now = clock.now_local()
    if g.user["role"] == "cro":
        return redirect(url_for("main.person", store=g.user["emp_store"],
                                user_id=g.user["emp_user_id"], month=now.strftime("%Y-%m")))
    cards = []
    mine = auth.stores_for(g.user, conn)
    for row in conn.execute("SELECT * FROM stores ORDER BY store"):
        if row["store"] not in mine:
            continue
        data = StoreData(conn, row["store"], now.date(), now.date(), now)
        cells = [data.cell(p["user_id"], now.date()) for p in data.people if p["on_device"]
                 or data.punches.get((p["user_id"], now.date()))]
        stale, _ = sync_state(row, now)
        cards.append({"row": row, "totals": summarize(cells), "people": len(cells),
                      "stale": stale})
    own_month = None
    if g.user["role"] == "manager":
        own_month = now.strftime("%Y-%m")
    return render_template("home.html", cards=cards, today=now.date(),
                           pending=len(pending_for(g.user, conn)), own_month=own_month)


# ----------------------------------------------------------------- month

@bp.get("/calendar/<store>")
@auth.require_role("owner", "manager")
def month_jump(store):
    require_store(store)
    wanted = request.args.get("m", "")
    try:
        parse_month_strict(wanted)
    except ValueError:
        wanted = clock.now_local().strftime("%Y-%m")
    return redirect(url_for("main.month", store=store, month=wanted))


@bp.get("/day/<store>")
@auth.require_role("owner", "manager")
def day_jump(store):
    require_store(store)
    wanted = request.args.get("d", "")
    try:
        date.fromisoformat(wanted)
    except ValueError:
        wanted = clock.today().isoformat()
    return redirect(url_for("main.day", store=store, day=wanted))


@bp.get("/person/<store>/<user_id>")
@auth.login_required
def person_jump(store, user_id):
    wanted = request.args.get("m", "")
    try:
        parse_month_strict(wanted)
    except ValueError:
        wanted = clock.now_local().strftime("%Y-%m")
    return redirect(url_for("main.person", store=store, user_id=user_id, month=wanted))


def parse_month_strict(text):
    y, m = (int(x) for x in text.split("-"))
    return date(y, m, 1)

@bp.get("/calendar/<store>/<month>")
@auth.require_role("owner", "manager")
def month(store, month):
    row = require_store(store)
    first = parse_month(month)
    first, last = month_bounds(first.year, first.month)
    now = clock.now_local()
    conn = db.get_db()
    data = StoreData(conn, store, first, last, now)
    show_all = request.args.get("all") == "1"
    who = request.args.get("who", "").strip().lower()

    days = list(daterange(first, last))
    rows = []
    for p in data.people:
        cells = data.row(p["user_id"])
        if not show_all and not p["on_device"] and not any(c.punches for c in cells):
            continue
        if who and who not in p["name"].lower() and who != p["user_id"]:
            continue
        rows.append({"person": p, "cells": cells, "totals": summarize(cells),
                     "can_act": auth.can_act_for(g.user, store, p["user_id"], conn)})
    headcount = []
    for i, d in enumerate(days):
        if d > now.date():
            headcount.append(None)
        else:
            headcount.append(sum(1 for r in rows if r["cells"][i].code in ("P", "LT")))
    return render_template(
        "month.html", store=row, first=first, days=days, rows=rows, headcount=headcount,
        prev=shift_month(first, -1).strftime("%Y-%m"), next=shift_month(first, 1).strftime("%Y-%m"),
        this_month=now.strftime("%Y-%m"), today=now.date(), show_all=show_all, who=who)


# ----------------------------------------------------------------- day

@bp.get("/day/<store>/<day>")
@auth.require_role("owner", "manager")
def day(store, day):
    row = require_store(store)
    d = parse_day(day)
    now = clock.now_local()
    data = StoreData(db.get_db(), store, d, d, now)
    present, absent = [], []
    for p in data.people:
        c = data.cell(p["user_id"], d)
        if c.punches:
            present.append((p, c))
        elif p["on_device"] or c.code in ("L", "CO", "A*"):
            absent.append((p, c))
    present.sort(key=lambda pc: pc[1].punches[0])
    order = {"NOT_IN_YET": 0, "A": 1, "A*": 2, "L": 3, "CO": 4, "WO": 5, "NODATA": 6, None: 7}
    absent.sort(key=lambda pc: (order.get(pc[1].code, 9), natural_key(pc[0]["user_id"])))
    counts = summarize([c for _, c in present] + [c for _, c in absent])
    counts["on_wo"] = sum(1 for _, c in present if c.weekly_off)
    timeline = build_timeline(present, data.rules, d)
    return render_template(
        "day.html", store=row, day=d, present=present, absent=absent, counts=counts,
        timeline=timeline, prev=(d - timedelta(days=1)).isoformat(),
        next=(d + timedelta(days=1)).isoformat(), today=now.date(), rules=data.rules,
        is_today=(d == now.date()), store_open=data.store_open_on(d))


def build_timeline(present, rules, d):
    """Geometry for the inline SVG: one bar per person from first to last
    punch, a dashed line at start + grace. The same first-punch-in,
    last-punch-out rule the old daily Excel chart used."""
    if not present:
        return None
    hours = []
    for _p, c in present:
        hours += [t.hour + t.minute / 60 for t in c.punches]
    lo = min([8.0] + [h - 0.5 for h in hours])
    hi = max([rules.close_time.hour + rules.close_time.minute / 60 + 0.5, 18.0] + [h + 0.5 for h in hours])
    lo, hi = float(int(lo)), float(int(hi) + 1 if hi % 1 else int(hi))
    span = hi - lo

    def x(t):
        return round((t.hour + t.minute / 60 + t.second / 3600 - lo) / span * 100, 3)

    late_at = datetime.combine(d, rules.late_after)
    bars = []
    for p, c in present:
        bars.append({
            "person": p, "cell": c,
            "x1": x(c.punches[0]), "x2": x(c.punches[-1]),
            "dots": [(x(t), t.strftime("%H:%M")) for t in c.punches],
            "late": c.code == "LT",
            "tip": ", ".join(t.strftime("%H:%M") for t in c.punches),
        })
    ticks = []
    h = int(lo)
    while h <= hi:
        label = f"{(h - 1) % 12 + 1}{'am' if h % 24 < 12 else 'pm'}"
        ticks.append((round((h - lo) / span * 100, 3), label))
        h += 1
    return {"bars": bars, "ticks": ticks, "start_x": x(late_at.time()),
            "start_label": f"start {rules.start_time:%H:%M} (+{rules.grace_minutes} min)"}


# ----------------------------------------------------------------- person

@bp.get("/person/<store>/<user_id>/<month>")
@auth.login_required
def person(store, user_id, month):
    conn = db.get_db()
    if not auth.can_view_person(g.user, store, user_id, conn):
        abort(404)
    row = store_row(store)
    emp = employee(store, user_id)
    if emp is None:
        abort(404)
    first = parse_month(month)
    first, last = month_bounds(first.year, first.month)
    now = clock.now_local()
    data = StoreData(conn, store, first, last, now, user_ids=[user_id])
    cells = data.row(user_id)
    weeks = []
    lead = [None] * first.weekday()
    padded = lead + cells
    padded += [None] * (-len(padded) % 7)
    for i in range(0, len(padded), 7):
        weeks.append(padded[i:i + 7])
    leaves = conn.execute(
        "SELECT l.*, u.display_name AS decided_by_name FROM leaves l "
        "LEFT JOIN users u ON u.id=l.decided_by WHERE l.store=? AND l.user_id=? "
        "ORDER BY l.start_date DESC, l.id DESC LIMIT 30", (store, str(user_id))).fetchall()
    wo_now, wo_from = data.wo_history(user_id).current(now.date())
    return render_template(
        "person.html", store=row, emp=emp, first=first, weeks=weeks, totals=summarize(cells),
        credits=data.credits(user_id), leaves=leaves, today=now.date(),
        prev=shift_month(first, -1).strftime("%Y-%m"), next=shift_month(first, 1).strftime("%Y-%m"),
        this_month=now.strftime("%Y-%m"), is_self=auth.is_self(g.user, store, user_id),
        wo_now=wo_now, wo_from=wo_from, comp_on=data.cfg.comp_from is not None, cfg=data.cfg,
        can_act=auth.can_act_for(g.user, store, user_id, conn))


@bp.app_template_filter("dayfmt")
def dayfmt(d):
    return fmt_day(d) if d else ""
