"""
"Now" and "today" for the website, always in the stores' timezone.

Railway's clock runs on UTC, 5 1/2 hours behind India, so from midnight to
5:30 AM it still says it's yesterday. Everything that asks what today is
goes through now_local(); tests replace it to pin the time.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

REPORT_TZ = ZoneInfo(os.environ.get("REPORT_TZ", "Asia/Kolkata"))


def now_local():
    """The current time in the stores' timezone, without tzinfo attached,
    so it compares directly with the device's own (local) punch times."""
    return datetime.now(REPORT_TZ).replace(tzinfo=None)


def today():
    return now_local().date()
