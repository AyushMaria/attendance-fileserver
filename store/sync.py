"""
Sends this store's staff list and punches from the eSSL K90 Pro to the
attendance website. Runs every 30 minutes from tasks.bat.

    python sync.py         the last few days (lookback_days in the settings)
    python sync.py --all   everything on the device

The first successful run sends everything by itself, bringing the device's
whole history over. After the website accepts it, a marker file
(.full_sync_done) is written and later runs send only the last few days;
the website ignores punches it already has, so re-sending costs nothing and
a PC that was off over a weekend catches up on its next run.

Settings come from store_config.ini: [device] and [sync].
"""

import json
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from config import settings

HERE = Path(__file__).resolve().parent
MARKER = HERE / ".full_sync_done"
SYNC_LOG = HERE / "sync_log.txt"
BATCH = 2000


def log_line(text):
    """One line per run in sync_log.txt, as well as the full output that
    tasks.bat puts in run_log.txt. Never contains the key."""
    try:
        with open(SYNC_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
    except OSError:
        pass
    print(text)


def fetch_attendance(ip, port, timeout, password):
    """Connect to the device and return (users, attendance_records).
    TCP first, then UDP; the device is paused while it's read (under 2
    seconds, usually) and always resumed afterwards."""
    from zk import ZK
    last_error = RuntimeError("no connection attempt was made")
    for use_udp, label in ((False, "TCP"), (True, "UDP")):
        print(f"  connecting over {label} ...")
        zk = ZK(ip, port=port, timeout=timeout, password=password,
                force_udp=use_udp, ommit_ping=True)
        conn = None
        try:
            conn = zk.connect()
            conn.disable_device()
            users = conn.get_users()
            attendance = conn.get_attendance()
            print(f"  read the device over {label}")
            return users, attendance
        except Exception as e:  # noqa: BLE001 - try the other protocol
            last_error = e
            print(f"  {label} failed: {e}")
        finally:
            if conn:
                try:
                    conn.enable_device()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    conn.disconnect()
                except Exception:  # noqa: BLE001
                    pass
    raise last_error


def build_batches(users, attendance, since=None):
    """The staff list plus punches (on or after `since`, or all), split into
    batches of BATCH punches. The staff list goes in the first batch."""
    staff = [{"user_id": str(u.user_id), "name": u.name or ""} for u in users]
    punches = [{"user_id": str(a.user_id),
                "ts": a.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "punch": getattr(a, "punch", None)}
               for a in attendance if since is None or a.timestamp >= since]
    punches.sort(key=lambda p: p["ts"])
    batches = [punches[i:i + BATCH] for i in range(0, len(punches), BATCH)] or [[]]
    out = [{"staff": staff, "punches": batches[0]}]
    out += [{"punches": b} for b in batches[1:]]
    return out, len(punches)


def post(url, key, body, timeout=60):
    """POST one batch. Returns the website's reply; raises with a readable
    message on failure."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}",
        "User-Agent": "attendance-sync/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:  # noqa: BLE001
            detail = ""
        raise RuntimeError(f"the website refused the data ({e.code}) {detail}".strip()) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        raise RuntimeError(f"couldn't reach the website: {reason}") from None


def run(argv, fetch=fetch_attendance, send=post, now=datetime.now):
    settings.require()
    ip = settings.get("device", "ip")
    port = settings.getint("device", "port", 4370)
    timeout = settings.getint("device", "timeout", 10)
    password = settings.getint("device", "comm_password", 0)
    server = settings.get("sync", "server_url").rstrip("/")
    key = settings.get("sync", "sync_key")
    lookback = settings.getint("sync", "lookback_days", 3)
    store = settings.get("store", "name", "this store")

    if not ip:
        log_line("FAILED: no device IP. Fill in [device] ip in store_config.ini.")
        return 1
    if not server.startswith("http") or not key or "put-this" in key:
        log_line("FAILED: fill in [sync] server_url and sync_key in store_config.ini.")
        return 1

    full = "--all" in argv or not MARKER.exists()
    since = None if full else (now() - timedelta(days=lookback))
    print(f"Syncing {store}: {'everything on the device' if full else f'the last {lookback} days'}")

    try:
        users, attendance = fetch(ip, port, timeout, password)
    except Exception as e:  # noqa: BLE001
        log_line(f"FAILED: couldn't read the device at {ip}:{port} - {e}")
        return 1
    if not users:
        log_line("FAILED: the device returned no staff list, so nothing was sent.")
        return 1

    batches, total = build_batches(users, attendance, since)
    url = f"{server}/api/sync"
    new = 0
    for i, body in enumerate(batches, 1):
        try:
            reply = send(url, key, body)
        except Exception as e:  # noqa: BLE001
            log_line(f"FAILED on batch {i} of {len(batches)}: {e}. "
                     f"The next run will try again.")
            return 1
        new += int(reply.get("new_punches", 0))
    if full:
        MARKER.write_text(f"full sync accepted {datetime.now():%Y-%m-%d %H:%M:%S}\n", encoding="utf-8")
    log_line(f"OK: {len(attendance)} punches on the device, {total} sent, {new} new on the website, "
             f"{len(users)} staff{' (full sync)' if full else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
