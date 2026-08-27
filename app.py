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

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (Flask, Response, abort, jsonify, request,
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


def stored_files():
    rows = []
    for path in STORAGE_DIR.iterdir():
        if not path.is_file():
            continue
        stat = path.stat()
        rows.append({
            "name": path.name,
            "size": stat.st_size,
            "size_label": human_size(stat.st_size),
            "modified": datetime.fromtimestamp(stat.st_mtime, REPORT_TZ),
        })
    return sorted(rows, key=lambda r: r["modified"], reverse=True)


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

    destination = STORAGE_DIR / name
    existed = destination.exists()
    uploaded.save(destination)

    return jsonify({"stored": name,
                    "size": destination.stat().st_size,
                    "replaced": existed})


@app.get("/api/files")
def api_files():
    """Used by the upload script to skip files the server already has."""
    if UPLOAD_TOKEN and request.headers.get("X-API-Token", "") != UPLOAD_TOKEN:
        return jsonify({"error": "Invalid or missing upload token."}), 401
    return jsonify([{"name": r["name"], "size": r["size"]} for r in stored_files()])


@app.get("/files/<path:filename>")
def download(filename):
    if not browser_authorized():
        return needs_login()
    name = safe_name(filename)
    if not name or not (STORAGE_DIR / name).is_file():
        abort(404)
    return send_from_directory(STORAGE_DIR, name, as_attachment=True)


@app.get("/health")
def health():
    return jsonify({"ok": True, "files": len(stored_files())})


@app.get("/")
def index():
    if not browser_authorized():
        return needs_login()
    files = stored_files()
    return render_template_string(
        PAGE, files=files, unprotected=not (WEB_USER and WEB_PASSWORD),
        now=datetime.now(REPORT_TZ))


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
  .empty{text-align:center;padding:52px 20px;color:var(--muted)}
  .foot{margin-top:16px;font-size:12.5px;color:var(--muted)}
  @media(max-width:560px){td.hide,th.hide{display:none}}
</style>
</head>
<body>
<header><div class="wrap">
  <h1>Attendance files</h1>
  <div class="sub">{{ files|length }} file{{ '' if files|length == 1 else 's' }}
    &middot; {{ now.strftime('%d %b %Y, %H:%M') }}</div>
</div></header>
<main><div class="wrap">
  {% if unprotected %}
  <div class="warn"><strong>This page is not password protected.</strong>
    Anyone with the link can download these files. Set the WEB_USER and
    WEB_PASSWORD variables on your Railway service to require a login.</div>
  {% endif %}

  {% if files %}
  <table>
    <thead><tr>
      <th>File</th><th class="right hide">Size</th><th class="right">Uploaded</th>
    </tr></thead>
    <tbody>
    {% for f in files %}
      <tr>
        <td><a class="file" href="/files/{{ f.name }}">{{ f.name }}</a></td>
        <td class="right hide num">{{ f.size_label }}</td>
        <td class="right num">{{ f.modified.strftime('%d %b, %H:%M') }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  <p class="foot">Click a file to download it.</p>
  {% else %}
  <div class="empty">
    <p>No files yet.</p>
    <p>Run <span class="num">upload_reports.py</span> on the attendance PC.</p>
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
