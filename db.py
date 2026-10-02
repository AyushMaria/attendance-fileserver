"""
The database: one SQLite file on the Railway volume.

gunicorn runs two server processes that share this file, so every
connection uses WAL mode (readers never block the writer) and a busy
timeout (wait for the other process instead of failing).

The file lives in a hidden folder (/data/.internal/) so it never mixes with
the old Excel reports in /data/mall and /data/nirala.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    store          TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    start_time     TEXT NOT NULL DEFAULT '09:00',
    grace_minutes  INTEGER NOT NULL DEFAULT 10,
    close_time     TEXT NOT NULL DEFAULT '21:00',
    sync_key_hash  TEXT,
    last_sync_at   TEXT,
    last_sync_note TEXT NOT NULL DEFAULT '',
    clock_warning  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS employees (
    store        TEXT NOT NULL REFERENCES stores(store),
    user_id      TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    on_device    INTEGER NOT NULL DEFAULT 1,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY (store, user_id)
);

CREATE TABLE IF NOT EXISTS punches (
    store       TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    day         TEXT NOT NULL,
    ts          TEXT NOT NULL,
    punch_code  INTEGER,
    received_at TEXT NOT NULL,
    UNIQUE (store, user_id, ts)
);

CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY,
    username            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name        TEXT NOT NULL,
    password_hash       TEXT NOT NULL,
    role                TEXT NOT NULL CHECK (role IN ('owner','manager','cro')),
    emp_store           TEXT,
    emp_user_id         TEXT,
    active              INTEGER NOT NULL DEFAULT 1,
    session_version     INTEGER NOT NULL DEFAULT 1,
    failed_logins       INTEGER NOT NULL DEFAULT 0,
    locked_until        TEXT,
    created_at          TEXT NOT NULL,
    last_login_at       TEXT,
    last_login_ip       TEXT,
    password_changed_at TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    CHECK ((role = 'owner') = (emp_user_id IS NULL)),
    UNIQUE (emp_store, emp_user_id)
);

CREATE TABLE IF NOT EXISTS user_stores (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    store   TEXT NOT NULL REFERENCES stores(store),
    PRIMARY KEY (user_id, store)
);

CREATE TABLE IF NOT EXISTS weekly_offs (
    id             INTEGER PRIMARY KEY,
    store          TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    weekdays       TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    set_by         INTEGER NOT NULL REFERENCES users(id),
    set_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leaves (
    id              INTEGER PRIMARY KEY,
    store           TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    staff_comment   TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','cancelled')),
    manager_comment TEXT NOT NULL DEFAULT '',
    created_by      INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT NOT NULL,
    decided_by      INTEGER REFERENCES users(id),
    decided_at      TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY,
    at       TEXT NOT NULL,
    actor    INTEGER REFERENCES users(id),
    action   TEXT NOT NULL,
    target   TEXT NOT NULL DEFAULT '',
    details  TEXT NOT NULL DEFAULT '',
    ip       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comp_adjustments (
    id       INTEGER PRIMARY KEY,
    store    TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    day      TEXT NOT NULL,
    kind     TEXT NOT NULL CHECK (kind IN ('cancel','grant')),
    note     TEXT NOT NULL,
    set_by   INTEGER NOT NULL REFERENCES users(id),
    set_at   TEXT NOT NULL,
    UNIQUE (store, user_id, day, kind)
);

-- a store's shifts; each day a person counts as on the shift whose start
-- is closest to their first punch
CREATE TABLE IF NOT EXISTS shifts (
    id            INTEGER PRIMARY KEY,
    store         TEXT NOT NULL REFERENCES stores(store),
    name          TEXT NOT NULL,
    start_time    TEXT NOT NULL,
    end_time      TEXT NOT NULL,
    grace_minutes INTEGER NOT NULL DEFAULT 10
);

-- a day's status set by hand (forgot to punch, punched for someone else,
-- ...); wins over what the punches say
CREATE TABLE IF NOT EXISTS day_overrides (
    id       INTEGER PRIMARY KEY,
    store    TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    day      TEXT NOT NULL,
    code     TEXT NOT NULL CHECK (code IN ('P','LT','A','WO')),
    note     TEXT NOT NULL,
    set_by   INTEGER REFERENCES users(id),
    set_at   TEXT NOT NULL,
    UNIQUE (store, user_id, day)
);

-- leaves already taken in a leave year before the website tracked them
-- (the hand tally), per person
CREATE TABLE IF NOT EXISTS leave_tally (
    store      TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    year_start TEXT NOT NULL,          -- first day of the leave year, e.g. 2026-06-01
    used       INTEGER NOT NULL,
    set_by     INTEGER REFERENCES users(id),
    set_at     TEXT NOT NULL,
    PRIMARY KEY (store, user_id, year_start)
);

CREATE INDEX IF NOT EXISTS idx_punches_day ON punches(store, day, user_id);
CREATE INDEX IF NOT EXISTS idx_leaves_emp  ON leaves(store, user_id, start_date);
CREATE INDEX IF NOT EXISTS idx_leaves_stat ON leaves(status);
CREATE INDEX IF NOT EXISTS idx_wo_emp      ON weekly_offs(store, user_id, effective_from);
CREATE INDEX IF NOT EXISTS idx_audit_at    ON audit_log(at);
"""

# Settings that apply to every store, with their defaults.
SETTING_DEFAULTS = {
    "comp_min_hours": "4",
    "comp_window_days": "30",
    "comp_from": "",          # empty = comp-offs are not earned yet
}


def connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        migrate(conn)
        conn.commit()
    finally:
        conn.close()


# Columns added after the first release: (table, column, definition).
ADDED_COLUMNS = [
    ("users", "must_change_password", "INTEGER NOT NULL DEFAULT 0"),
    # per-store policy (the school works differently from the stores)
    ("stores", "uses_weekly_offs", "INTEGER NOT NULL DEFAULT 1"),
    ("stores", "uses_comp_offs", "INTEGER NOT NULL DEFAULT 1"),
    ("stores", "leave_allowance", "INTEGER"),               # leaves per year; NULL = no allowance
    ("stores", "leave_year_start_month", "INTEGER NOT NULL DEFAULT 6"),
    ("stores", "leave_count_from", "TEXT"),                 # absences count as leave from this day
    ("stores", "tally_as_of", "TEXT"),                      # the hand tally covers up to this day
    # someone on a device who isn't staff (the Owner enrolled as an admin):
    # left out of every calendar, count and account creation
    ("employees", "hidden", "INTEGER NOT NULL DEFAULT 0"),
    # a name typed in on the website (the office device has no names):
    # the device sync never overwrites it
    ("employees", "name_locked", "INTEGER NOT NULL DEFAULT 0"),
]


def migrate(conn):
    """Add any newer columns to an existing database. Both server processes
    run this at startup, so losing the race to the other one is fine."""
    for table, column, definition in ADDED_COLUMNS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column in have:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise
            continue
        if (table, column) == ("employees", "name_locked"):
            # "NN-12" is the reader's placeholder for "no name on the device"
            conn.execute("UPDATE employees SET name='' WHERE name GLOB 'NN-[0-9]*'")
        if (table, column) == ("employees", "hidden"):
            # One time, when hiding arrives: the Owner is enrolled on every
            # device as its admin under the name Ayush, and asked for those
            # entries to be left out of the calendars.
            n = conn.execute("UPDATE employees SET hidden=1 "
                             "WHERE lower(trim(name))='ayush'").rowcount
            if n:
                audit(conn, None, "person.hide", "Ayush (every store)",
                      {"count": n, "why": "the Owner's admin enrolment on the devices"})


def get_db():
    """One connection per request, closed when the request ends."""
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit(conn, actor, action, target="", details=None, ip=""):
    conn.execute(
        "INSERT INTO audit_log (at, actor, action, target, details, ip) VALUES (?,?,?,?,?,?)",
        (utc_now_iso(), actor, action, target,
         json.dumps(details, ensure_ascii=False) if details else "", ip or ""))


def get_setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else SETTING_DEFAULTS[key]


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def default_db_path():
    return os.environ.get("DB_PATH") or str(Path(__file__).parent / "instance" / "attendance.db")
