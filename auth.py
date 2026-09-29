"""
Signing in, staying signed in, and who may see or do what.

Every permission rule in the guide's section 3 table lives in the helpers
at the bottom of this file, and every page calls them on the server, so a
typed-in address or form value can never reach another store's data.
Anything a person may not see returns 404, not 403, so staff can't
discover what exists at other stores.
"""

import functools
import secrets
from datetime import datetime, timedelta, timezone

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import db

bp = Blueprint("auth", __name__)

MAX_FAILURES = 5
LOCK_MINUTES = 15
MIN_PASSWORD = 8

# Checked against when the username doesn't exist, so a wrong username
# takes as long as a wrong password and the timing reveals nothing.
_DUMMY_HASH = generate_password_hash("not-a-real-password-" + secrets.token_hex(8))


# ----------------------------------------------------------------- helpers

def client_ip():
    return request.remote_addr or ""


def utc_now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def password_problem(password, username):
    """Why this password can't be used, or None if it's fine."""
    if len(password) < MIN_PASSWORD:
        return f"The password must be at least {MIN_PASSWORD} characters."
    if password.strip().lower() == (username or "").strip().lower():
        return "The password can't be the same as the username."
    return None


def generate_password():
    """12 characters, avoiding ones that are easy to misread (0/O, 1/l/I)."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def load_user():
    """Runs before every request: who is signed in, if anyone. A session is
    only honoured while the account is active and its session_version still
    matches, so a reset or deactivation signs someone out everywhere."""
    g.user = None
    uid = session.get("uid")
    if uid is None:
        return
    row = db.get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if row is None or not row["active"] or row["session_version"] != session.get("sv"):
        session.clear()
        return
    g.user = row


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.full_path
                                    if request.method == "GET" else None))
        return view(*args, **kwargs)
    return wrapped


def require_role(*roles):
    def decorator(view):
        @functools.wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user["role"] not in roles:
                abort(404)
            return view(*args, **kwargs)
        return wrapped
    return decorator


# ----------------------------------------------------------------- permissions

def all_stores(conn=None):
    conn = conn or db.get_db()
    return [r["store"] for r in conn.execute("SELECT store FROM stores ORDER BY store")]


def stores_for(user, conn=None):
    """The stores whose calendars and staff this person may see."""
    conn = conn or db.get_db()
    if user is None:
        return set()
    if user["role"] == "owner":
        return set(all_stores(conn))
    if user["role"] == "manager":
        return {r["store"] for r in conn.execute(
            "SELECT store FROM user_stores WHERE user_id=?", (user["id"],))}
    return set()          # CROs see only themselves (see can_view_person)


def is_self(user, store, user_id):
    return (user is not None and user["emp_store"] == store
            and str(user["emp_user_id"]) == str(user_id))


def can_view_person(user, store, user_id, conn=None):
    if user is None:
        return False
    if user["role"] == "cro":
        return is_self(user, store, user_id)
    return store in stores_for(user, conn) or is_self(user, store, user_id)


def login_role_of(store, user_id, conn=None):
    """The website role of the person with this device number, or None if
    they have no login."""
    conn = conn or db.get_db()
    row = conn.execute("SELECT role FROM users WHERE emp_store=? AND emp_user_id=?",
                       (store, str(user_id))).fetchone()
    return row["role"] if row else None


def can_act_for(user, store, user_id, conn=None):
    """May this person approve / reject / withdraw / record leave, change
    the weekly off, or cancel / grant comp-offs for that device person?

    Owner: anyone. Manager: CROs (and staff without a login) in stores they
    cover, never themselves and never another manager. CRO: nobody.
    Nobody acts on their own behalf.
    """
    if user is None:
        return False
    if is_self(user, store, user_id):
        return False
    if user["role"] == "owner":
        return store in set(all_stores(conn))
    if user["role"] == "manager":
        if store not in stores_for(user, conn):
            return False
        return login_role_of(store, user_id, conn) not in ("manager", "owner")
    return False


# ----------------------------------------------------------------- routes

@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None and request.method == "GET":
        return redirect(url_for("main.home"))
    error = None
    username = ""
    if request.method == "POST":
        conn = db.get_db()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        remember = request.form.get("remember") == "on"
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        now = utc_now()

        if row is not None and row["locked_until"] and parse_iso(row["locked_until"]) > now:
            check_password_hash(_DUMMY_HASH, password)
            until = current_app.jinja_env.filters["localtime"](row["locked_until"])
            error = (f"This account is locked after too many wrong passwords. "
                     f"Try again after {until}.")
            db.audit(conn, None, "login.locked", username, ip=client_ip())
            conn.commit()
        else:
            ok = check_password_hash(row["password_hash"] if row else _DUMMY_HASH, password)
            if row is not None and ok and row["active"]:
                conn.execute("UPDATE users SET failed_logins=0, locked_until=NULL, "
                             "last_login_at=?, last_login_ip=? WHERE id=?",
                             (iso(now), client_ip(), row["id"]))
                db.audit(conn, row["id"], "login.ok", username, ip=client_ip())
                conn.commit()
                # a fresh session: nothing from before signing in carries over
                session.clear()
                session["uid"] = row["id"]
                session["sv"] = row["session_version"]
                session.permanent = remember
                target = request.args.get("next") or ""
                if not target.startswith("/") or target.startswith("//"):
                    target = url_for("main.home")
                return redirect(target)

            error = "Wrong username or password."
            if row is not None and not row["active"] and ok:
                # right password, deactivated account: same message, logged
                db.audit(conn, row["id"], "login.inactive", username, ip=client_ip())
            elif row is not None:
                # count in the database itself: the two server processes
                # don't share memory, so read-then-write could lose a count
                conn.execute("UPDATE users SET failed_logins = failed_logins + 1 WHERE id=?",
                             (row["id"],))
                failures = conn.execute("SELECT failed_logins FROM users WHERE id=?",
                                        (row["id"],)).fetchone()[0]
                if failures >= MAX_FAILURES:
                    locked = now + timedelta(minutes=LOCK_MINUTES)
                    conn.execute("UPDATE users SET failed_logins=0, locked_until=? WHERE id=?",
                                 (iso(locked), row["id"]))
                    until = current_app.jinja_env.filters["localtime"](iso(locked))
                    error = (f"Wrong username or password. The account is now locked "
                             f"for {LOCK_MINUTES} minutes, until {until}.")
                db.audit(conn, None, "login.fail", username, ip=client_ip())
            else:
                db.audit(conn, None, "login.fail", username, ip=client_ip())
            conn.commit()
    return render_template("login.html", error=error, username=username)


@bp.post("/logout")
def logout():
    if g.user is not None:
        conn = db.get_db()
        db.audit(conn, g.user["id"], "logout", g.user["username"], ip=client_ip())
        conn.commit()
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    conn = db.get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "password":
            current = request.form.get("current", "")
            new = request.form.get("new", "")
            again = request.form.get("again", "")
            if not check_password_hash(g.user["password_hash"], current):
                flash("Your current password isn't right.", "error")
            elif new != again:
                flash("The two new passwords don't match.", "error")
            elif problem := password_problem(new, g.user["username"]):
                flash(problem, "error")
            else:
                version = g.user["session_version"] + 1
                conn.execute("UPDATE users SET password_hash=?, password_changed_at=?, "
                             "session_version=? WHERE id=?",
                             (generate_password_hash(new), iso(utc_now()), version, g.user["id"]))
                db.audit(conn, g.user["id"], "password.change", g.user["username"], ip=client_ip())
                conn.commit()
                session["sv"] = version     # stay signed in here, out everywhere else
                flash("Password changed. You've been signed out on every other device.", "ok")
                return redirect(url_for("auth.account"))
        elif action == "signout_all":
            conn.execute("UPDATE users SET session_version=session_version+1 WHERE id=?",
                         (g.user["id"],))
            db.audit(conn, g.user["id"], "signout.all", g.user["username"], ip=client_ip())
            conn.commit()
            session.clear()
            flash("Signed out on every device.", "ok")
            return redirect(url_for("auth.login"))
    return render_template("account.html")


# ----------------------------------------------------------------- first owner

def bootstrap_owner(conn, username, password, log=print):
    """Create the first Owner from environment variables - only when there
    are no accounts at all, so it can never add a second way in."""
    if not username or not password:
        return False
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return False
    now = iso(utc_now())
    try:
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, role, created_at, "
            "password_changed_at) VALUES (?,?,?,?,?,?)",
            (username, username, generate_password_hash(password), "owner", now, now))
    except Exception:          # the other server process got there first
        conn.rollback()
        return False
    db.audit(conn, None, "owner.bootstrap", username)
    conn.commit()
    log(f"Created the first Owner account '{username}'. Remove the "
        f"BOOTSTRAP_OWNER_* variables now that it exists.")
    return True
