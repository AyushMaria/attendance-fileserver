"""
Uploads the Excel reports to your Railway file server.

Standard library only - nothing to pip install on the attendance PC.
Run it from Task Scheduler after the report tasks.

    python upload_reports.py            upload anything from the last 7 days
                                        the server does not already have
    python upload_reports.py 24         only look at the last 24 hours
    python upload_reports.py all        upload everything in the folder

Works with both server layouts: the current one that keeps every store's
files in one list with a name prefix (mall_attendance_...), and the
per-store folder layout (mall/attendance_...). The filename is always
sent with the store prefix plus the store name as a separate field, so
whichever server is running files it correctly.
"""

import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# ----------------------------------------------------------------------
# CONFIGURATION - read from store_config.ini, NOT edited here.
# This file is identical at every store and is updated with `git pull`.
# The upload token lives only in store_config.ini, which git never sees.
# ----------------------------------------------------------------------
from config import settings
settings.require()


def normalise_url(url):
    """Tolerate the usual copy-paste slips: a missing https:// and a
    trailing slash (which made requests go to //upload)."""
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    return url


LOCAL_FOLDER = settings.getpath("paths", "reports_folder", "attendance_reports")
SERVER_URL = normalise_url(settings.get("upload", "server_url"))
UPLOAD_TOKEN = settings.get("upload", "upload_token")

# This store's name, e.g. mall or nirala. Must be different at every store:
# a daily report has the same filename everywhere, so two stores sharing a
# name would overwrite each other on the server.
STORE_NAME = settings.get("store", "name")

# Look back a week by default. Files the server already has are skipped, so
# this costs nothing - but a day missed to an internet outage gets filled in
# on the next run instead of being lost for good.
MAX_AGE_HOURS = settings.getfloat("upload", "max_age_hours", 168)

ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv", ".pdf"}
TIMEOUT_SECONDS = 60

LOG_FILE = Path(__file__).parent / "upload_log.txt"
# ----------------------------------------------------------------------


def log(message):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # never let logging failure stop an upload


def select_files(folder, max_age_hours, allowed_extensions, now=None):
    """Files changed within the window. max_age_hours=None means all of them."""
    if not folder.exists():
        return []
    now = now if now is not None else time.time()
    cutoff = None if max_age_hours is None else now - max_age_hours * 3600

    chosen = []
    for path in folder.iterdir():
        if not path.is_file():
            continue
        if allowed_extensions is not None and path.suffix.lower() not in allowed_extensions:
            continue
        if cutoff is None or path.stat().st_mtime >= cutoff:
            chosen.append(path)
    return sorted(chosen, key=lambda p: p.stat().st_mtime, reverse=True)


def remote_name(path):
    """Name sent with the upload: prefixed with the store, as on the
    current server (mall_attendance_2026-09-27.xlsx)."""
    return f"{STORE_NAME}_{path.name}"


def already_on_server(path, remote):
    """True if the server holds this exact file under either layout."""
    size = path.stat().st_size
    return (remote.get(remote_name(path)) == size                  # flat
            or remote.get(f"{STORE_NAME}/{path.name}") == size)    # per-folder


def encode_multipart(field_name, path):
    """Build a multipart/form-data body by hand.

    Written out rather than pulled from `requests` so this script needs
    nothing installed on the attendance PC.
    """
    boundary = f"----attendance{uuid.uuid4().hex}"
    mime, _ = mimetypes.guess_type(path.name)
    mime = mime or "application/octet-stream"

    # the store name as its own field - ignored by the current server,
    # used by the per-store folder server to pick the folder
    folder = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="folder"\r\n\r\n'
        f"{STORE_NAME}\r\n"
    ).encode("utf-8")
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; '
        f'filename="{remote_name(path)}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")

    body = folder + head + path.read_bytes() + tail
    return body, f"multipart/form-data; boundary={boundary}"


def remote_files():
    """What the server already holds, as {name: size}. Empty dict on failure,
    which just means nothing gets skipped."""
    request = urllib.request.Request(
        f"{SERVER_URL}/api/files", headers={"X-API-Token": UPLOAD_TOKEN})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return {f["name"]: f["size"] for f in json.loads(response.read().decode())}


def upload_one(path):
    body, content_type = encode_multipart("file", path)
    request = urllib.request.Request(
        f"{SERVER_URL}/upload", data=body, method="POST",
        headers={"Content-Type": content_type, "X-API-Token": UPLOAD_TOKEN})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode())


def describe_error(e):
    if isinstance(e, urllib.error.HTTPError):
        detail = e.read().decode("utf-8", errors="replace")[:300]
        if e.code == 401:
            return ("server rejected the token - check upload_token in "
                    "store_config.ini matches UPLOAD_TOKEN on Railway")
        if e.code == 413:
            return "file is larger than the server accepts"
        return f"server returned {e.code}: {detail}"
    if isinstance(e, urllib.error.URLError):
        return (f"could not reach {SERVER_URL} ({e.reason}) - check this PC's "
                f"internet connection and server_url in store_config.ini")
    return str(e)


def main():
    max_age_hours = MAX_AGE_HOURS
    if len(sys.argv) > 1:
        if sys.argv[1].lower() == "all":
            max_age_hours = None
        else:
            try:
                max_age_hours = float(sys.argv[1])
            except ValueError:
                print("Usage: python upload_reports.py [hours|all]")
                sys.exit(1)

    problems = []
    if not SERVER_URL or "your-app.up.railway.app" in SERVER_URL:
        problems.append("[upload] server_url")
    if not UPLOAD_TOKEN or "put-your" in UPLOAD_TOKEN:
        problems.append("[upload] upload_token")
    if not STORE_NAME or STORE_NAME == "store-name-here":
        problems.append("[store] name")
    if problems:
        log(f"Fill in {', '.join(problems)} in store_config.ini, then run again.")
        sys.exit(1)

    if not LOCAL_FOLDER.exists():
        log(f"Folder not found: {LOCAL_FOLDER}")
        sys.exit(1)

    window = "all files" if max_age_hours is None else f"last {max_age_hours:g}h"
    files = select_files(LOCAL_FOLDER, max_age_hours, ALLOWED_EXTENSIONS)
    if not files:
        log(f"Nothing to upload ({window}).")
        return

    try:
        already = remote_files()
    except Exception as e:  # noqa: BLE001 - not fatal, just means no skipping
        log(f"Could not list existing files ({describe_error(e)}). "
            f"Uploading everything.")
        already = {}

    failures = uploaded = skipped = 0
    for path in files:
        # same name and same byte count means the server already has this
        # exact file - re-uploading a regenerated report is still allowed
        # because its size changes when the contents do
        if already_on_server(path, already):
            skipped += 1
            continue
        try:
            result = upload_one(path)
            verb = "Replaced" if result.get("replaced") else "Uploaded"
            log(f"{verb} {path.name} ({result['size'] / 1024:.0f} KB)")
            uploaded += 1
        except Exception as e:  # noqa: BLE001 - carry on with the rest
            log(f"FAILED {path.name}: {describe_error(e)}")
            failures += 1

    log(f"Done ({window}): {uploaded} uploaded, {skipped} already current, "
        f"{failures} failed.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()