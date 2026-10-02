"""
POST /api/sync - where each store PC sends its staff list and punches.

The store is worked out from the key alone. The body never names the store,
so the mall key can never write nirala data, whatever is sent.
"""

import hashlib
import re
import hmac
from datetime import datetime, timedelta

from flask import Blueprint, current_app, jsonify, request

import clock
import db
from attendance import MIN_DATE

bp = Blueprint("api", __name__)

MAX_PUNCHES = 5000

# What the device-reading library reports for someone with no name stored
# on the device, e.g. "NN-12". Never a real name.
PLACEHOLDER_NAME = re.compile(r"NN-\d+")


def hash_key(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def store_for_key(conn, key):
    if not key:
        return None
    wanted = hash_key(key)
    found = None
    for row in conn.execute("SELECT store, sync_key_hash FROM stores WHERE sync_key_hash IS NOT NULL"):
        # compare every row, in constant time, rather than stopping early
        if hmac.compare_digest(row["sync_key_hash"], wanted):
            found = row["store"]
    return found


@bp.post("/api/sync")
def sync():
    conn = db.get_db()
    header = request.headers.get("Authorization", "")
    key = header[7:].strip() if header.lower().startswith("bearer ") else ""
    store = store_for_key(conn, key)
    if store is None:
        db.audit(conn, None, "sync.refused", "", ip=request.remote_addr or "")
        conn.commit()
        return jsonify({"error": "Unknown or missing sync key. Enter this store's key on the "
                                 "website's Stores page."}), 401

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Expected a JSON object."}), 400
    punches = body.get("punches", [])
    if not isinstance(punches, list):
        return jsonify({"error": "punches must be a list."}), 400
    if len(punches) > MAX_PUNCHES:
        return jsonify({"error": f"Send at most {MAX_PUNCHES} punches per request."}), 413

    has_staff = "staff" in body
    staff = body.get("staff") or []
    if has_staff and (not isinstance(staff, list) or not staff):
        # an empty list almost always means the device read went wrong;
        # accepting it would mark everyone as gone
        return jsonify({"error": "The staff list is empty - the device read probably failed."}), 400

    # validate everything before writing anything
    now_local = clock.now_local()
    received = db.utc_now_iso()
    rows, future, impossible = [], [], []
    for p in punches:
        try:
            uid = str(p["user_id"]).strip()
            ts = datetime.strptime(str(p["ts"]), "%Y-%m-%d %H:%M:%S")
            code = p.get("punch")
            code = int(code) if code is not None else None
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            return jsonify({"error": f"Punch not understood: {p!r:.200}"}), 400
        if not uid or len(uid) > 32:
            return jsonify({"error": "A punch has a missing or overlong user_id."}), 400
        if code is not None and not -1000 <= code <= 1000:
            return jsonify({"error": f"Punch type out of range: {code}"}), 400
        # A date the calendars could never show means the device clock (or
        # that record) is badly wrong. Skip it and warn, rather than refuse
        # the batch: one corrupt record must not stop the store syncing.
        if ts.year < MIN_DATE.year or ts > now_local + timedelta(days=366):
            impossible.append(ts)
            continue
        if ts > now_local + timedelta(days=1):
            future.append(ts)
        stamp = ts.strftime("%Y-%m-%d %H:%M:%S")
        rows.append((store, uid, stamp[:10], stamp, code, received))

    people = []
    for s in staff:
        try:
            name = str(s.get("name") or "").strip()[:100]
            if PLACEHOLDER_NAME.fullmatch(name):
                name = ""          # no name stored on the device (the reader says "NN-12")
            people.append((str(s["user_id"]).strip(), name))
        except (KeyError, TypeError, AttributeError):
            return jsonify({"error": f"Staff entry not understood: {s!r:.200}"}), 400
    if any(not uid or len(uid) > 32 for uid, _ in people):
        return jsonify({"error": "A staff entry has a missing or overlong user_id."}), 400

    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO punches (store, user_id, day, ts, punch_code, received_at) "
        "VALUES (?,?,?,?,?,?)", rows)
    new = conn.total_changes - before

    if has_staff:
        listed = set()
        for uid, name in people:
            listed.add(uid)
            conn.execute(
                "INSERT INTO employees (store, user_id, name, on_device, first_seen, last_seen) "
                "VALUES (?,?,?,1,?,?) ON CONFLICT(store, user_id) DO UPDATE SET "
                "name=CASE WHEN employees.name_locked=1 THEN employees.name "
                "WHEN excluded.name != '' THEN excluded.name ELSE employees.name END, "
                "on_device=1, last_seen=excluded.last_seen",
                (store, uid, name, received, received))
        # never delete anyone: people removed from the device keep their history
        for r in conn.execute("SELECT user_id FROM employees WHERE store=? AND on_device=1",
                              (store,)).fetchall():
            if r["user_id"] not in listed:
                conn.execute("UPDATE employees SET on_device=0 WHERE store=? AND user_id=?",
                             (store, r["user_id"]))

    # punches from people no longer enrolled still need a row to show up
    for uid in {r[1] for r in rows}:
        conn.execute(
            "INSERT OR IGNORE INTO employees (store, user_id, name, on_device, first_seen, last_seen) "
            "VALUES (?,?,'',0,?,?)", (store, uid, received, received))

    note = f"{len(rows)} received, {new} new"
    if impossible:
        note += f", {len(impossible)} skipped"
    if future or impossible:
        parts = []
        if future:
            parts.append(f"{len(future)} punch(es) dated in the future "
                         f"(latest {max(future):%d %b %Y %H:%M})")
        if impossible:
            parts.append(f"{len(impossible)} punch(es) with impossible dates were skipped "
                         f"(e.g. {impossible[0]:%Y-%m-%d %H:%M})")
        warning = "; ".join(parts) + ". The device clock is probably wrong - check it."
        conn.execute("UPDATE stores SET clock_warning=? WHERE store=?", (warning, store))
    elif has_staff:
        conn.execute("UPDATE stores SET clock_warning='' WHERE store=?", (store,))
    conn.execute("UPDATE stores SET last_sync_at=?, last_sync_note=? WHERE store=?",
                 (received, note, store))
    db.audit(conn, None, "sync", store,
             {"received": len(rows), "new": new, "staff": len(people)},
             ip=request.remote_addr or "")
    conn.commit()
    if has_staff:
        # a roster that arrived before this store's staff were known
        try:
            import roster
            roster.apply_store(conn, store, roster.rosters(
                current_app.config.get("ROSTER_DIR", roster.ROSTER_DIR)).get(store))
        except Exception as e:  # noqa: BLE001 - never fail a sync over this
            current_app.logger.warning("roster for %s not applied: %s", store, e)
    return jsonify({"punches_received": len(rows), "new_punches": new, "staff": len(people)})


@bp.get("/health")
def health():
    conn = db.get_db()
    conn.execute("SELECT 1").fetchone()
    return jsonify({"ok": True})
