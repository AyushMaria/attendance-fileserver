"""
Store rosters kept on GitHub: rosters/<store>.csv

For a device that has no names stored on it (the office's Identix), the
roster says who the real staff are:

    number,name
    2,Aliya Shirin
    12,Mohammed Wasim Uddin

When a roster file is new or has changed, the website applies it once:
  * everyone listed gets that name (kept from then on - the sync never
    overwrites it) and is shown on the calendars;
  * every other number on that device is hidden from the calendars,
    counts and login creation (their punches stay stored).

People enrolled on the device after that are not touched: they show up as
usual, so a new joiner isn't silently hidden. Add them to the file (or name
them on Stores > names) when they arrive.

The roster is applied at startup and after a store's sync - the first time
the store's staff are known, if the roster arrived before them.
"""

import csv
import hashlib
import io
from pathlib import Path

import db

ROSTER_DIR = Path(__file__).parent / "rosters"


def load(path):
    """{number: name} from a roster file. Bad lines are skipped."""
    text = Path(path).read_text(encoding="utf-8-sig")
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        number, name = row[0].strip(), row[1].strip()
        if not number or not number.isalnum() or number.lower() == "number":
            continue
        out[number] = name[:100]
    return out, hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def rosters(directory=ROSTER_DIR):
    if not Path(directory).is_dir():
        return {}
    return {p.stem: p for p in sorted(Path(directory).glob("*.csv"))}


def apply_all(conn, directory=ROSTER_DIR, log=print):
    for store, path in rosters(directory).items():
        apply_store(conn, store, path, log)


def apply_store(conn, store, path=None, log=print):
    """Apply one store's roster if it hasn't been applied in this version yet
    and the store's staff are known. Safe for both server processes at once."""
    if path is None:
        return False
    names, digest = load(path)
    key = f"roster:{store}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        done = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        people = [r["user_id"] for r in conn.execute(
            "SELECT user_id FROM employees WHERE store=?", (store,))]
        if (done and done["value"] == digest) or not people:
            conn.rollback()
            return False
        named = hidden = 0
        for uid in people:
            if uid in names:
                conn.execute("UPDATE employees SET name=?, name_locked=1, hidden=0 "
                             "WHERE store=? AND user_id=?", (names[uid], store, uid))
                named += 1
            else:
                conn.execute("UPDATE employees SET hidden=1 WHERE store=? AND user_id=?",
                             (store, uid))
                hidden += 1
        missing = sorted(set(names) - set(people), key=lambda u: (len(u), u))
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, digest))
        db.audit(conn, None, "roster.apply", store,
                 {"named": named, "hidden": hidden, "not_on_device": missing, "version": digest})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    log(f"Roster for {store}: {named} named, {hidden} hidden"
        + (f", not on the device yet: {', '.join(missing)}" if missing else ""))
    return True
