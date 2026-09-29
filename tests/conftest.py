import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

os.environ["ATTENDANCE_NO_APP"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import clock  # noqa: E402
import db  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

# Hashing is deliberately slow; tests make many accounts.
FAST_HASH = generate_password_hash("password123", method="pbkdf2:sha256:1000")


@pytest.fixture
def now(monkeypatch):
    """Pin 'now' (IST). Call now.set(datetime) to move it."""
    class Now:
        value = datetime(2026, 10, 7, 12, 0)

        def set(self, dt):
            self.value = dt

    n = Now()
    monkeypatch.setattr(clock, "now_local", lambda: n.value)
    return n


@pytest.fixture
def app(tmp_path, now):
    from app import create_app
    application = create_app({
        "TESTING": True,
        "DB_PATH": str(tmp_path / ".internal" / "attendance.db"),
        "SECRET_KEY": "test-secret",
        "SESSION_COOKIE_SECURE": False,
        "WTF_CSRF_ENABLED": False,
        "STORAGE_DIR": str(tmp_path),
    })
    return application


@pytest.fixture
def conn(app):
    c = db.connect(app.config["DB_PATH"])
    yield c
    c.close()


class Factory:
    def __init__(self, conn):
        self.conn = conn

    def store(self, store="mall", start="09:00", grace=10, close="21:00", key=None):
        from views.api import hash_key
        self.conn.execute(
            "INSERT INTO stores (store, display_name, start_time, grace_minutes, close_time, sync_key_hash) "
            "VALUES (?,?,?,?,?,?)", (store, store.title(), start, grace, close,
                                     hash_key(key) if key else None))
        self.conn.commit()

    def emp(self, store, user_id, name=None, on_device=1):
        self.conn.execute(
            "INSERT OR IGNORE INTO employees (store, user_id, name, on_device, first_seen, last_seen) "
            "VALUES (?,?,?,?,'x','x')", (store, str(user_id), name or f"P{user_id}", on_device))
        self.conn.commit()

    def punch(self, store, user_id, ts):
        self.emp(store, user_id)
        self.conn.execute(
            "INSERT OR IGNORE INTO punches (store, user_id, day, ts, punch_code, received_at) "
            "VALUES (?,?,?,?,0,'x')", (store, str(user_id), ts[:10], ts))
        self.conn.commit()

    def user(self, username, role, store=None, user_id=None, covers=(), active=1):
        if role != "owner":
            self.emp(store, user_id)
        cur = self.conn.execute(
            "INSERT INTO users (username, display_name, password_hash, role, emp_store, emp_user_id, "
            "active, created_at, password_changed_at) VALUES (?,?,?,?,?,?,?,'x','x')",
            (username, username.title(), FAST_HASH, role, store if role != "owner" else None,
             str(user_id) if role != "owner" else None, active))
        for s in covers:
            self.conn.execute("INSERT INTO user_stores (user_id, store) VALUES (?,?)", (cur.lastrowid, s))
        self.conn.commit()
        return cur.lastrowid

    def weekly_off(self, store, user_id, weekdays, eff="2026-01-01", by=None):
        by = by or self._any_user()
        self.conn.execute(
            "INSERT INTO weekly_offs (store, user_id, weekdays, effective_from, set_by, set_at) "
            "VALUES (?,?,?,?,?,'x')", (store, str(user_id), weekdays, eff, by))
        self.conn.commit()

    def leave(self, store, user_id, start, end, status="approved", comment="", by=None):
        by = by or self._any_user()
        cur = self.conn.execute(
            "INSERT INTO leaves (store, user_id, start_date, end_date, staff_comment, status, "
            "created_by, created_at, decided_by, decided_at) VALUES (?,?,?,?,?,?,?,'x',?,?)",
            (store, str(user_id), start, end, comment, status, by,
             by if status in ("approved", "rejected") else None,
             "2026-10-01T00:00:00Z" if status in ("approved", "rejected") else None))
        self.conn.commit()
        return cur.lastrowid

    def setting(self, key, value):
        db.set_setting(self.conn, key, value)
        self.conn.commit()

    def adjust(self, store, user_id, day, kind, by=None):
        self.conn.execute(
            "INSERT INTO comp_adjustments (store, user_id, day, kind, note, set_by, set_at) "
            "VALUES (?,?,?,?,'why',?,'x')", (store, str(user_id), day, kind, by or self._any_user()))
        self.conn.commit()

    def _any_user(self):
        row = self.conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if row:
            return row["id"]
        return self.user("sysowner", "owner")


@pytest.fixture
def make(conn):
    return Factory(conn)


def login(client, user_id, conn):
    row = conn.execute("SELECT session_version FROM users WHERE id=?", (user_id,)).fetchone()
    with client.session_transaction() as s:
        s["uid"] = user_id
        s["sv"] = row["session_version"]
    return client


@pytest.fixture
def as_user(app, conn):
    """as_user(user_id) -> a test client signed in as that account."""
    def make_client(user_id):
        return login(app.test_client(), user_id, conn)
    return make_client
