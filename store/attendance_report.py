"""
Daily Attendance Report Generator - eSSL K90 Pro
--------------------------------------------------
Connects to the biometric device over your school's LAN, downloads the
attendance logs, keeps only one day's punches, and saves them to an
Excel file (User Number, Name, Date, Time, Punch Type).

Setup (once per store):
    1. pip install -r requirements.txt
    2. Copy store_config.example.ini to store_config.ini and fill it in.

Usage:
    python attendance_report.py              -> today's attendance
    python attendance_report.py 2026-08-18   -> a specific date (YYYY-MM-DD)
"""

import socket
import sys
import io
from datetime import datetime, date, time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from zk import ZK
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment
from openpyxl.utils import get_column_letter
from openpyxl.drawing.image import Image as XLImage

# ----------------------------------------------------------------------
# CONFIGURATION - read from store_config.ini, NOT edited here.
# This file is identical at every store and is updated with `git pull`,
# so each store's own values live in store_config.ini next to it.
# ----------------------------------------------------------------------
from config import settings
settings.require()
DEVICE_IP = settings.get("device", "ip")
DEVICE_PORT = settings.getint("device", "port", 4370)
COMM_PASSWORD = settings.getint("device", "comm_password", 0)
TIMEOUT = settings.getint("device", "timeout", 10)

OUTPUT_FOLDER = settings.getpath("paths", "reports_folder", "attendance_reports")
EXPECTED_START_TIME = settings.gettime("device", "expected_start", "09:00")
# ----------------------------------------------------------------------

PUNCH_LABELS = {0: "Check In", 1: "Check Out", 2: "Break Out",
                3: "Break In", 4: "OT In", 5: "OT Out"}


def _natural_sort_key(user_id):
    """Numeric user numbers sort in numeric order (2 before 10); anything
    non-numeric falls back to plain string sort, after all numeric ones."""
    try:
        return (0, int(user_id))
    except (TypeError, ValueError):
        return (1, str(user_id))


def probe_tcp_port(ip, port, timeout=5):
    """Raw TCP connection attempt, independent of the ZK protocol.
    Tells us whether ANYTHING is reachable at ip:port before we bother
    trying to speak the device protocol - separates a plain network/
    firewall/wrong-IP problem from a device-side setting problem."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def fetch_attendance():
    """Connect to the device and return (users, attendance_records).
    Tries TCP first, then UDP, since different devices/firmware prefer
    one or the other; reports progress and re-raises the last error if
    both fail."""
    last_error = RuntimeError("no connection attempt was made")
    for use_udp, label in ((False, "TCP"), (True, "UDP")):
        print(f"  attempting connection over {label} ...")
        zk = ZK(DEVICE_IP, port=DEVICE_PORT, timeout=TIMEOUT,
                password=COMM_PASSWORD, force_udp=use_udp, ommit_ping=True)
        conn = None
        try:
            conn = zk.connect()
            conn.disable_device()      # briefly pause new punches while we read
            users = conn.get_users()
            attendance = conn.get_attendance()
            print(f"  connected successfully over {label}")
            return users, attendance
        except Exception as e:
            last_error = e
            print(f"  {label} attempt failed: {e}")
        finally:
            if conn:
                try:
                    conn.enable_device()   # always resume normal operation
                except Exception:
                    pass
                try:
                    conn.disconnect()
                except Exception:
                    pass
    raise last_error


def build_user_lookup(users):
    """Map user_id -> name, so the report can show names, not just numbers."""
    return {u.user_id: u.name for u in users}


def filter_by_date(attendance, target_date):
    """Keep only the punches that happened on target_date."""
    return [a for a in attendance if a.timestamp.date() == target_date]


def find_absent_users(users, todays_records):
    """Enrolled users with zero punches among todays_records, sorted by
    user number."""
    punched_ids = {r.user_id for r in todays_records}
    absent = [u for u in users if u.user_id not in punched_ids]
    return sorted(absent, key=lambda u: _natural_sort_key(u.user_id))


def build_chart_rows(records, user_lookup):
    """Group a day's records by user, take the first punch as check-in and
    the last as check-out (robust to devices that mislabel punch type),
    skip users with zero punches, and sort earliest check-in first.
    Returns a list of dicts: user_id, name, in_dt, out_dt (None if only
    one punch that day)."""
    by_user = {}
    for r in records:
        by_user.setdefault(r.user_id, []).append(r)

    rows = []
    for user_id, recs in by_user.items():
        recs_sorted = sorted(recs, key=lambda r: r.timestamp)
        in_dt = recs_sorted[0].timestamp
        out_dt = recs_sorted[-1].timestamp if len(recs_sorted) > 1 else None
        rows.append({
            "user_id": user_id,
            "name": user_lookup.get(user_id, user_id),
            "in_dt": in_dt,
            "out_dt": out_dt,
        })

    rows.sort(key=lambda r: r["in_dt"])
    return rows


def _to_decimal_hour(dt_or_time):
    return dt_or_time.hour + dt_or_time.minute / 60


def generate_timeline_chart(chart_rows, expected_start, output_path):
    """Save a horizontal check-in/check-out dot plot as a PNG - the 'bird's
    eye view' of who checked in/out when, relative to expected_start."""
    n = len(chart_rows)
    fig_height = max(2.2, 0.35 * n + 1.2)
    fig, ax = plt.subplots(figsize=(9, fig_height), dpi=150)

    all_hours = [_to_decimal_hour(r["in_dt"]) for r in chart_rows]
    all_hours += [_to_decimal_hour(r["out_dt"]) for r in chart_rows if r["out_dt"]]
    x_min = min([8.0] + [h - 0.5 for h in all_hours])
    x_max = max([18.0] + [h + 0.5 for h in all_hours])

    in_color = "#378ADD"
    out_color = "#0C447C"
    line_color = "#378ADD"

    for i, row in enumerate(chart_rows):
        y = n - i  # first row (earliest check-in) plotted at the top
        in_h = _to_decimal_hour(row["in_dt"])
        if row["out_dt"] is not None:
            out_h = _to_decimal_hour(row["out_dt"])
            ax.plot([in_h, out_h], [y, y], "-", color=line_color, alpha=0.45, linewidth=2, zorder=1)
            ax.scatter([out_h], [y], color=out_color, s=45, zorder=3)
        else:
            ax.text(in_h + 0.15, y, "no check-out", va="center", fontsize=9, color="#767570")
        ax.scatter([in_h], [y], color=in_color, s=45, zorder=3)

    expected_h = _to_decimal_hour(expected_start)
    ax.axvline(expected_h, linestyle="--", color="#9c9b95", linewidth=1, zorder=0)
    start_hour_12 = expected_start.hour % 12 or 12
    ax.text(expected_h, n + 0.8, f"{start_hour_12}:{expected_start.minute:02d} start",
            ha="center", fontsize=9, color="#767570")

    ax.set_yticks([n - i for i in range(n)])
    ax.set_yticklabels([row["name"] for row in chart_rows], fontsize=10)
    ax.set_ylim(0.3, n + 1.3)

    tick_start, tick_end = int(x_min), int(x_max) + 1
    xticks = list(range(tick_start, tick_end + 1))
    xlabels = [f"{((h - 1) % 12) + 1}{'am' if h % 24 < 12 else 'pm'}" for h in xticks]
    ax.set_xticks(xticks)
    ax.set_xticklabels(xlabels, fontsize=9)
    ax.set_xlim(x_min, x_max)

    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color("#c3c2b7")
    ax.tick_params(axis="y", length=0)
    ax.tick_params(axis="x", length=3, colors="#767570")

    handles = [
        plt.Line2D([0], [0], marker="o", color="none", markerfacecolor=in_color, markersize=7, label="Check in"),
        plt.Line2D([0], [0], marker="o", color="none", markerfacecolor=out_color, markersize=7, label="Check out"),
    ]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.08 if n > 2 else -0.15),
              ncol=2, frameon=False, fontsize=9)

    fig.tight_layout()
    fig.savefig(output_path, format="png", bbox_inches="tight")
    plt.close(fig)
    return output_path


def save_to_excel(records, user_lookup, target_date, absent_users, chart_rows):
    OUTPUT_FOLDER.mkdir(exist_ok=True)
    filename = OUTPUT_FOLDER / f"attendance_{target_date.isoformat()}.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance"

    headers = ["User Number", "Name", "Date", "Time", "Punch Type"]
    ws.append(headers)
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = Font(name="Arial", bold=True)
        cell.alignment = Alignment(horizontal="center")

    # sort chronologically within each user, so each person's day reads top-to-bottom
    records_sorted = sorted(records, key=lambda a: (_natural_sort_key(a.user_id), a.timestamp))

    for rec in records_sorted:
        punch_value = getattr(rec, "punch", None)
        punch_label = PUNCH_LABELS.get(punch_value,
                                        punch_value if punch_value is not None else "")
        ws.append([
            rec.user_id,
            user_lookup.get(rec.user_id, ""),
            rec.timestamp.date().isoformat(),
            rec.timestamp.strftime("%H:%M:%S"),
            punch_label,
        ])
        for col in range(1, len(headers) + 1):
            ws.cell(row=ws.max_row, column=col).font = Font(name="Arial")

    # --- "no punch" section, appended at the bottom of the same sheet ---
    skip_rows = set()
    if absent_users:
        section_row = ws.max_row + 2   # leave one blank row as a separator
        ws.cell(row=section_row, column=1,
                value=f"No attendance recorded on {target_date.isoformat()} ({len(absent_users)})")
        ws.cell(row=section_row, column=1).font = Font(name="Arial", bold=True, size=12)
        skip_rows.add(section_row)

        sub_header_row = section_row + 1
        ws.cell(row=sub_header_row, column=1, value="User Number")
        ws.cell(row=sub_header_row, column=2, value="Name")
        for col in (1, 2):
            cell = ws.cell(row=sub_header_row, column=col)
            cell.font = Font(name="Arial", bold=True)
            cell.alignment = Alignment(horizontal="center")

        for i, u in enumerate(absent_users):
            row = sub_header_row + 1 + i
            ws.cell(row=row, column=1, value=u.user_id)
            ws.cell(row=row, column=2, value=u.name)
            ws.cell(row=row, column=1).font = Font(name="Arial")
            ws.cell(row=row, column=2).font = Font(name="Arial")

    # auto-width columns so nothing is cut off (the long section-header
    # sentence is excluded, or it would blow out column A's width)
    for col_idx, header in enumerate(headers, start=1):
        col_letter = get_column_letter(col_idx)
        lengths = [len(str(header))]
        for r in range(2, ws.max_row + 1):
            if r in skip_rows:
                continue
            val = ws.cell(row=r, column=col_idx).value
            lengths.append(len(str(val)) if val is not None else 0)
        ws.column_dimensions[col_letter].width = max(lengths) + 4

    ws.freeze_panes = "A2"

    # --- Timeline sheet: the "bird's eye view" chart ---
    ws_chart = wb.create_sheet("Timeline")
    if chart_rows:
        img_buffer = io.BytesIO()
        generate_timeline_chart(chart_rows, EXPECTED_START_TIME, img_buffer)
        img_buffer.seek(0)
        ws_chart.add_image(XLImage(img_buffer), "A1")
    else:
        ws_chart.cell(row=1, column=1,
                       value=f"No punches recorded on {target_date.isoformat()} to chart.")
        ws_chart.cell(row=1, column=1).font = Font(name="Arial")

    wb.save(filename)
    return filename


def main():
    target_date = date.today()
    if len(sys.argv) > 1:
        try:
            target_date = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
        except ValueError:
            print("Date must be in YYYY-MM-DD format, e.g. 2026-08-19")
            sys.exit(1)

    if not DEVICE_IP:
        print("No device IP set. Fill in [device] ip in store_config.ini.")
        sys.exit(1)

    print(f"Connecting to device at {DEVICE_IP}:{DEVICE_PORT} ...")
    port_open = probe_tcp_port(DEVICE_IP, DEVICE_PORT)
    if port_open:
        print(f"  network check OK - {DEVICE_IP}:{DEVICE_PORT} accepted a TCP connection")
    else:
        print(f"  network check: no plain TCP response from {DEVICE_IP}:{DEVICE_PORT}")

    try:
        users, attendance = fetch_attendance()
    except Exception as e:
        print("\nCould not connect to the device over TCP or UDP.")
        print(f"Last error: {e}\n")
        if not port_open:
            print("The network-level check above also failed, so start here:")
            print(f"  1. Re-check the IP on the device itself: Menu > Comm > Ethernet")
            print(f"     (the script is currently configured for {DEVICE_IP})")
            print("  2. Confirm the device is powered on and the Ethernet cable is seated")
            print(f"  3. From Command Prompt on this PC, run: ping {DEVICE_IP}")
            print("  4. If ping also fails, ask whoever manages the network whether this")
            print(f"     PC and the device are on the same subnet, and whether port "
                  f"{DEVICE_PORT} is allowed between them")
        else:
            print("The device DID respond at the network level, so this is more likely")
            print("a device-side setting than a network problem:")
            print("  - A Comm Key/password set on the device - put it in COMM_PASSWORD")
            print("  - The device set to cloud-only (ADMS) mode - check Comm > Cloud")
            print("    Server Setting on the device and switch to local/legacy mode")
        sys.exit(1)

    print(f"Pulled {len(attendance)} total log entries and "
          f"{len(users)} enrolled users from the device.")

    todays_records = filter_by_date(attendance, target_date)
    print(f"{len(todays_records)} punch(es) found for {target_date.isoformat()}.")

    user_lookup = build_user_lookup(users)
    absent_users = find_absent_users(users, todays_records)
    print(f"{len(absent_users)} enrolled user(s) have no attendance recorded "
          f"for {target_date.isoformat()}.")

    if not todays_records and not absent_users:
        print("Nothing to save for that date.")
        return

    chart_rows = build_chart_rows(todays_records, user_lookup)
    output_path = save_to_excel(todays_records, user_lookup, target_date, absent_users, chart_rows)
    print(f"Saved report to: {output_path.resolve()}")


if __name__ == "__main__":
    main()
