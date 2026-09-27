"""
Attendance file drop - a small server for Railway.

Two jobs, nothing more:
  * accepts report files pushed up from the school PC (token protected)
  * lists them in a browser so you can download them (password protected)

It never opens or parses the spreadsheets; they are stored and served back
exactly as uploaded.

Environment variables:
    UPLOAD_TOKEN   required by the upload script (X-API-Token header)
    WEB_USER       username for the browser login
    WEB_PASSWORD   password for the browser login
    STORAGE_DIR    where files live - point at a Railway volume, e.g. /data
    REPORT_TZ      timezone for displayed times (default Asia/Kolkata)
"""

import hashlib
import hmac
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (Flask, Response, abort, jsonify, redirect, request,
                   render_template_string, send_from_directory)
from werkzeug.utils import secure_filename

UPLOAD_TOKEN = os.environ.get("UPLOAD_TOKEN", "").strip()
WEB_USER = os.environ.get("WEB_USER", "").strip()
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "").strip()

# Railway wipes the container filesystem on every redeploy, so this must
# point at a mounted volume or uploads will vanish on the next deploy.
STORAGE_DIR = Path(os.environ.get("STORAGE_DIR", Path(__file__).parent / "uploads"))
REPORT_TZ = ZoneInfo(os.environ.get("REPORT_TZ", "Asia/Kolkata"))

ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv", ".pdf"}
MAX_UPLOAD_MB = 50

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def _startup():
    """File any pre-folder uploads into per-store folders. Import-time, so
    it runs under gunicorn too."""
    try:
        moved = migrate_flat_files()
        if moved:
            print(f"Filed {moved} legacy file(s) into store folders.")
    except Exception as e:  # noqa: BLE001 - must never block startup
        print(f"Could not migrate legacy files: {e}")


def human_size(num_bytes):
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes / 1024 / 1024:.1f} MB"


def safe_name(raw):
    """Strip any path components so an upload can never escape STORAGE_DIR."""
    cleaned = secure_filename(raw or "")
    return cleaned or None


UNSORTED = "unsorted"


def safe_folder(raw):
    """Sanitise a store folder name. Returns None if unusable."""
    cleaned = secure_filename(raw or "")
    return cleaned or None


def split_legacy_name(filename):
    """Old uploads were flat and prefixed, e.g. store3_attendance_X.xlsx.
    Split that into ("store3", "attendance_X.xlsx") so history can be filed
    into the right folder. Returns (None, filename) when there is no prefix."""
    stem, sep, rest = filename.partition("_")
    # Only treat the first segment as a store name when what follows still
    # looks like a report filename. Otherwise "attendance_2026-08-27.xlsx"
    # would be filed into a folder called "attendance".
    if sep and stem and rest.lower().startswith("attendance"):
        return stem, rest
    return None, filename


def migrate_flat_files():
    """Move any loose files at the top level into per-store subfolders.

    Runs once at startup so files uploaded before folders existed are not
    stranded. Never overwrites: if the destination is taken, the file is
    left where it is for you to look at.
    """
    moved = 0
    for path in list(STORAGE_DIR.iterdir()):
        try:
            if not path.is_file():
                continue
            folder, name = split_legacy_name(path.name)
            folder = safe_folder(folder) if folder else UNSORTED
            destination_dir = STORAGE_DIR / folder
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / name
            # link-then-unlink instead of rename: a hard link fails if the
            # destination already exists, so a file uploaded in the same
            # instant is never overwritten by an older one being filed.
            try:
                os.link(path, destination)
                path.unlink()
            except (FileExistsError, FileNotFoundError):
                raise
            except OSError:
                # filesystem without hard links: plain move, still refusing
                # to overwrite
                if destination.exists():
                    continue
                path.rename(destination)
            moved += 1
        except (FileExistsError, FileNotFoundError):
            # FileExists: the destination is taken - leave this one alone.
            # FileNotFound: the other server worker (gunicorn runs two,
            # both doing this at startup) already moved it. Either way,
            # carry on with the rest instead of abandoning the pass.
            continue
    return moved


def stored_files():
    """Every file, grouped by the folder it sits in."""
    rows = []
    for folder_path in STORAGE_DIR.iterdir():
        if not folder_path.is_dir():
            continue
        for path in folder_path.iterdir():
            if not path.is_file():
                continue
            stat = path.stat()
            rows.append({
                "folder": folder_path.name,
                "name": path.name,
                "key": f"{folder_path.name}/{path.name}",
                "size": stat.st_size,
                "size_label": human_size(stat.st_size),
                "modified": datetime.fromtimestamp(stat.st_mtime, REPORT_TZ),
            })
    return sorted(rows, key=lambda r: (r["folder"], -r["modified"].timestamp()))


def grouped_files():
    """[(folder, [rows...])] with the busiest-recent folder first."""
    groups = {}
    for row in stored_files():
        groups.setdefault(row["folder"], []).append(row)
    return sorted(groups.items(), key=lambda kv: kv[0])


# ----------------------------------------------------------------- auth

def needs_login():
    return Response(
        "Sign in to view the attendance files.", 401,
        {"WWW-Authenticate": 'Basic realm="Attendance files"'})


def browser_authorized():
    """Browser access uses HTTP basic auth - the browser shows its own login
    box, so there is no session or cookie handling to get wrong."""
    if not WEB_USER or not WEB_PASSWORD:
        return True  # not configured yet; the banner in the page warns about it
    auth = request.authorization
    return bool(auth and auth.username == WEB_USER and auth.password == WEB_PASSWORD)


# -------------------------------------------------------------- routes

@app.post("/upload")
def upload():
    if UPLOAD_TOKEN and request.headers.get("X-API-Token", "") != UPLOAD_TOKEN:
        return jsonify({"error": "Invalid or missing upload token."}), 401

    uploaded = request.files.get("file")
    if uploaded is None:
        return jsonify({"error": "No file was included in the request."}), 400

    raw = uploaded.filename or ""
    # A legitimate report filename never contains a path separator, so treat
    # one as a bad request rather than quietly rewriting it into a junk file.
    if "/" in raw or "\\" in raw or ".." in raw:
        return jsonify({"error": "Filename must not contain a path."}), 400

    name = safe_name(raw)
    if not name:
        return jsonify({"error": "That filename is not usable."}), 400

    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        return jsonify({
            "error": f"{Path(name).suffix or 'That file type'} is not accepted. "
                     f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"}), 400

    # Preferred: the uploader tells us which store it is. Older uploaders
    # do not, so fall back to the prefix baked into the filename.
    folder = safe_folder(request.form.get("folder", ""))
    if folder:
        # The uploader still sends "mall_attendance_X.xlsx" so it keeps
        # working with the older flat server too. Inside the mall folder
        # that prefix is redundant, so drop it.
        if name.startswith(f"{folder}_"):
            name = name[len(folder) + 1:]
    else:
        derived, name = split_legacy_name(name)
        folder = safe_folder(derived) if derived else UNSORTED

    destination_dir = STORAGE_DIR / folder
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / name
    existed = destination.exists()
    uploaded.save(destination)

    return jsonify({"stored": f"{folder}/{name}",
                    "folder": folder,
                    "name": name,
                    "size": destination.stat().st_size,
                    "replaced": existed})


@app.get("/api/files")
def api_files():
    """Used by the upload script to skip files the server already has."""
    if UPLOAD_TOKEN and request.headers.get("X-API-Token", "") != UPLOAD_TOKEN:
        return jsonify({"error": "Invalid or missing upload token."}), 401
    return jsonify([{"name": r["key"], "folder": r["folder"],
                     "size": r["size"]} for r in stored_files()])


@app.get("/files/<folder>/<path:filename>")
def download(folder, filename):
    if not browser_authorized():
        return needs_login()
    safe_dir = safe_folder(folder)
    name = safe_name(filename)
    if not safe_dir or not name:
        abort(400)
    target = STORAGE_DIR / safe_dir / name
    if not target.is_file():
        abort(404)
    return send_from_directory(STORAGE_DIR / safe_dir, name, as_attachment=True)


def delete_token(filename):
    """A per-file token derived from the login password.

    Browsers attach basic-auth credentials automatically, including on
    requests triggered by another site. Without this, a page you visited
    elsewhere could POST to /delete and your browser would helpfully
    authenticate it. Only someone who knows WEB_PASSWORD can compute a
    valid token, so a forged request fails.
    """
    return hmac.new(WEB_PASSWORD.encode("utf-8"),
                    filename.encode("utf-8"), hashlib.sha256).hexdigest()


@app.post("/delete")
def delete():
    if not browser_authorized():
        return needs_login()

    # with no password configured the page is public, so deleting is off
    if not (WEB_USER and WEB_PASSWORD):
        abort(403)

    raw = request.form.get("filename", "")
    raw_folder = request.form.get("folder", "")
    for value in (raw, raw_folder):
        if "/" in value or "\\" in value or ".." in value:
            abort(400)

    name = safe_name(raw)
    safe_dir = safe_folder(raw_folder)
    if not name or not safe_dir:
        abort(400)

    key = f"{safe_dir}/{name}"
    supplied = request.form.get("token", "")
    if not hmac.compare_digest(supplied, delete_token(key)):
        abort(403)

    target = STORAGE_DIR / safe_dir / name
    if not target.is_file():
        abort(404)
    target.unlink()

    return redirect("/")


@app.get("/health")
def health():
    return jsonify({"ok": True, "files": len(stored_files())})


@app.get("/")
def index():
    if not browser_authorized():
        return needs_login()
    groups = grouped_files()
    total = sum(len(rows) for _, rows in groups)
    protected = bool(WEB_USER and WEB_PASSWORD)
    if protected:
        for _, rows in groups:
            for row in rows:
                row["token"] = delete_token(row["key"])
    return render_template_string(
        PAGE, groups=groups, total=total, unprotected=not protected,
        can_delete=protected, now=datetime.now(REPORT_TZ))


_startup()


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Attendance files</title>
<style>
  :root {
    --ink:#16191f; --muted:#5c6470; --line:#e3e6ec; --paper:#f6f7f9;
    --navy:#1f3a5f; --warn-bg:#fdf1d6; --warn-fg:#8a5a00;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5
    -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
  header{background:var(--navy);color:#fff;padding:18px 20px}
  .wrap{max-width:840px;margin:0 auto}
  h1{margin:0;font-size:19px;font-weight:600}
  .sub{color:rgba(255,255,255,.75);font-size:13px;margin-top:2px}
  main{padding:24px 20px 60px}
  .num{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    font-variant-numeric:tabular-nums}
  .warn{background:var(--warn-bg);color:var(--warn-fg);border:1px solid #eddcb4;
    border-radius:8px;padding:11px 13px;font-size:13.5px;margin-bottom:18px}
  table{width:100%;border-collapse:collapse;background:#fff;border:1px solid
    var(--line);border-radius:12px;overflow:hidden}
  th,td{text-align:left;padding:11px 14px;border-bottom:1px solid var(--line)}
  th{background:var(--paper);font-size:11.5px;text-transform:uppercase;
    letter-spacing:.05em;color:var(--muted)}
  tbody tr:last-child td{border-bottom:0}
  tbody tr:hover{background:#f9fafb}
  a.file{color:var(--navy);text-decoration:none;font-weight:500}
  a.file:hover{text-decoration:underline}
  td.right{text-align:right;color:var(--muted);white-space:nowrap}
  section{margin-bottom:26px}
  section h2{font-size:13px;font-weight:600;text-transform:uppercase;
    letter-spacing:.06em;color:var(--muted);margin:0 0 8px 2px;
    display:flex;align-items:center;gap:8px}
  .count{background:#e7ebf1;color:var(--navy);border-radius:999px;
    padding:1px 8px;font-size:11px;letter-spacing:0}
  form.del{margin:0}
  button.del{font:inherit;font-size:13px;padding:4px 11px;border-radius:6px;
    border:1px solid #eccbc7;background:#fff;color:#a3251c;cursor:pointer}
  button.del:hover{background:#fbe9e7}
  button.del:focus-visible{outline:2px solid #a3251c;outline-offset:1px}
  .empty{text-align:center;padding:52px 20px;color:var(--muted)}
  .foot{margin-top:16px;font-size:12.5px;color:var(--muted)}
  @media(max-width:560px){td.hide,th.hide{display:none}}
</style>
</head>
<body>
<header><div class="wrap">
  <h1>Attendance files</h1>
  <div class="sub">{{ total }} file{{ '' if total == 1 else 's' }} in
    {{ groups|length }} folder{{ '' if groups|length == 1 else 's' }}
    &middot; {{ now.strftime('%d %b %Y, %H:%M') }}</div>
</div></header>
<main><div class="wrap">
  {% if unprotected %}
  <div class="warn"><strong>This page is not password protected.</strong>
    Anyone with the link can download these files. Set the WEB_USER and
    WEB_PASSWORD variables on your Railway service to require a login.</div>
  {% endif %}

  {% if groups %}
  {% for folder, rows in groups %}
  <section>
    <h2>{{ folder }} <span class="count">{{ rows|length }}</span></h2>
    <table>
      <thead><tr>
        <th>File</th><th class="right hide">Size</th><th class="right">Uploaded</th>
        {% if can_delete %}<th></th>{% endif %}
      </tr></thead>
      <tbody>
      {% for f in rows %}
        <tr>
          <td><a class="file" href="/files/{{ f.folder }}/{{ f.name }}">{{ f.name }}</a></td>
          <td class="right hide num">{{ f.size_label }}</td>
          <td class="right num">{{ f.modified.strftime('%d %b, %H:%M') }}</td>
          {% if can_delete %}
          <td class="right">
            <form class="del" method="post" action="/delete"
                  onsubmit="return confirm('Delete {{ f.folder }}/{{ f.name }} from the server?\n\nThe copy on the attendance PC is not affected.');">
              <input type="hidden" name="folder" value="{{ f.folder }}">
              <input type="hidden" name="filename" value="{{ f.name }}">
              <input type="hidden" name="token" value="{{ f.token }}">
              <button class="del" type="submit">Delete</button>
            </form>
          </td>
          {% endif %}
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </section>
  {% endfor %}
  <p class="foot">Click a file to download it.{% if can_delete %}
    Deleting removes the server copy only &mdash; re-run
    <span class="num">upload_reports.py all</span> on that store's PC to
    restore it.{% endif %}</p>
  {% else %}
  <div class="empty">
    <p>No files yet.</p>
    <p>Run <span class="num">upload_reports.py</span> on an attendance PC.</p>
  </div>
  {% endif %}
</div></main>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    if not UPLOAD_TOKEN:
        print("UPLOAD_TOKEN is not set - anyone could upload files.")
    app.run(host="0.0.0.0", port=port)
