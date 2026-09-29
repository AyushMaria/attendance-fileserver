"""
Attendance website: live calendars, logins, leave and weekly offs.

Each store PC sends its device's staff list and punches to /api/sync every
30 minutes; everything else is worked out from those when a page is opened.

Railway variables:
    DB_PATH                    /data/.internal/attendance.db
    SECRET_KEY                 signs the sign-in cookie (keep it secret)
    STORAGE_DIR                /data - the old Excel reports, shown read-only on /archive
    REPORT_TZ                  stores' timezone (default Asia/Kolkata)
    BOOTSTRAP_OWNER_USERNAME   } creates the first Owner when there are no
    BOOTSTRAP_OWNER_PASSWORD   } accounts; delete both once you've signed in
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, g
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

import clock
import db

csrf = CSRFProtect()


def create_app(overrides=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "").strip(),
        DB_PATH=db.default_db_path(),
        STORAGE_DIR=os.environ.get("STORAGE_DIR", str(Path(__file__).parent / "uploads")),
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        WTF_CSRF_TIME_LIMIT=None,          # the token lives as long as the session
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        BOOTSTRAP_OWNER_USERNAME=os.environ.get("BOOTSTRAP_OWNER_USERNAME", "").strip(),
        BOOTSTRAP_OWNER_PASSWORD=os.environ.get("BOOTSTRAP_OWNER_PASSWORD", ""),
        DEMO_DATA=os.environ.get("DEMO_DATA", "") == "1",
    )
    if overrides:
        app.config.update(overrides)
    if not app.config["SECRET_KEY"]:
        # Both server processes must sign cookies with the same key, and it
        # must survive restarts, so without SECRET_KEY one is kept in a file
        # beside the database.
        app.config["SECRET_KEY"] = stored_secret(Path(app.config["DB_PATH"]).parent / "secret_key")

    # Railway serves https in front of the app; trust its forwarded headers
    # (one hop) so the app sees https, the real host and the visitor's IP.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    csrf.init_app(app)
    app.teardown_appcontext(db.close_db)

    from auth import bp as auth_bp, load_user, require_own_password
    from views.api import bp as api_bp
    from views.main import bp as main_bp
    from views.leave import bp as leave_bp
    from views.admin import bp as admin_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(leave_bp)
    app.register_blueprint(admin_bp)
    csrf.exempt(api_bp)                    # the sync key, no cookies
    app.before_request(load_user)
    app.before_request(require_own_password)

    register_template_helpers(app)

    db.init_db(app.config["DB_PATH"])
    conn = db.connect(app.config["DB_PATH"])
    try:
        import auth
        auth.bootstrap_owner(conn, app.config["BOOTSTRAP_OWNER_USERNAME"],
                             app.config["BOOTSTRAP_OWNER_PASSWORD"])
        if app.config["DEMO_DATA"]:
            import demo
            demo.seed(conn)
    finally:
        conn.close()
    return app


def stored_secret(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32))
    # the other process may have created it an instant ago; wait for content
    for _ in range(50):
        value = path.read_text().strip()
        if value:
            return value
        import time
        time.sleep(0.05)
    raise RuntimeError(f"Could not read the secret key from {path}")


def register_template_helpers(app):
    from attendance import CODE_LABELS, WEEKDAY_NAMES

    def localtime(value, fmt="%d %b %Y, %H:%M"):
        """A stored UTC time, shown in the stores' timezone."""
        if not value:
            return ""
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        local = dt.astimezone(clock.REPORT_TZ)
        return local.strftime(fmt)

    def ago(value):
        if not value:
            return "never"
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        mins = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
        if mins < 1:
            return "just now"
        if mins < 60:
            return f"{mins} min ago"
        if mins < 48 * 60:
            return f"{mins // 60} h ago"
        return f"{mins // (60 * 24)} days ago"

    def weekdays_label(days):
        if days is None:
            return "not set"
        if not days:
            return "none"
        return ", ".join(WEEKDAY_NAMES[d] for d in sorted(days))

    def isodate(value):
        """'2026-10-02' (or a date) -> 'Fri 2 Oct', with the year when it
        isn't this year."""
        if not value:
            return ""
        from datetime import date as _date
        d = _date.fromisoformat(value) if isinstance(value, str) else value
        text = f"{d:%a} {d.day} {d:%b}"
        return text if d.year == clock.today().year else f"{text} {d.year}"

    app.jinja_env.filters["isodate"] = isodate
    app.jinja_env.filters["strftime_hm"] = lambda dt: dt.strftime("%H:%M")
    app.jinja_env.filters["localtime"] = localtime
    app.jinja_env.filters["ago"] = ago
    app.jinja_env.filters["weekdays"] = weekdays_label
    app.jinja_env.globals["CODE_LABELS"] = CODE_LABELS

    @app.context_processor
    def header_info():
        from views.common import header_context
        return header_context()


if os.environ.get("ATTENDANCE_NO_APP") != "1":
    app = create_app()
