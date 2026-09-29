"""store/sync.py with a fake device and a fake website."""

import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

STORE = Path(__file__).resolve().parent.parent / "store"
KEY = "secret-sync-key-abcdefghijklmnopqrstuvwxyz"


@pytest.fixture
def sync(tmp_path, monkeypatch):
    ini = tmp_path / "store_config.ini"
    ini.write_text(f"""[store]
name = mall
[device]
ip = 192.168.1.201
[sync]
server_url = https://example.test/
sync_key = {KEY}
lookback_days = 3
""")
    monkeypatch.setenv("ATTENDANCE_CONFIG", str(ini))
    monkeypatch.syspath_prepend(str(STORE))
    for name in ("config", "sync"):
        sys.modules.pop(name, None)
    mod = importlib.import_module("sync")
    monkeypatch.setattr(mod, "MARKER", tmp_path / ".full_sync_done")
    monkeypatch.setattr(mod, "SYNC_LOG", tmp_path / "sync_log.txt")
    mod.tmp = tmp_path
    return mod


NOW = datetime(2026, 10, 7, 12, 0)


def device(n_old=4500, n_new=10):
    users = [SimpleNamespace(user_id="101", name="Aditi"), SimpleNamespace(user_id="102", name="Rahul")]
    att = [SimpleNamespace(user_id="101", timestamp=NOW - timedelta(days=30, minutes=i), punch=0)
           for i in range(n_old)]
    att += [SimpleNamespace(user_id="102", timestamp=NOW - timedelta(hours=i), punch=1) for i in range(n_new)]
    return lambda *a: (users, att)


class Site:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, url, key, body):
        if self.fail:
            raise RuntimeError("couldn't reach the website: offline")
        self.calls.append((url, key, body))
        return {"new_punches": len(body["punches"])}


def test_first_run_sends_everything_in_batches(sync):
    site = Site()
    assert sync.run([], fetch=device(), send=site, now=lambda: NOW) == 0
    sizes = [len(b["punches"]) for _u, _k, b in site.calls]
    assert sizes == [2000, 2000, 510]
    assert "staff" in site.calls[0][2] and all("staff" not in b for _u, _k, b in site.calls[1:])
    assert site.calls[0][0] == "https://example.test/api/sync"
    assert sync.MARKER.exists()


def test_later_runs_send_last_three_days(sync):
    sync.MARKER.write_text("x")
    site = Site()
    sync.run([], fetch=device(), send=site, now=lambda: NOW)
    assert [len(b["punches"]) for _u, _k, b in site.calls] == [10]


def test_all_flag_forces_full(sync):
    sync.MARKER.write_text("x")
    site = Site()
    sync.run(["--all"], fetch=device(), send=site, now=lambda: NOW)
    assert sum(len(b["punches"]) for _u, _k, b in site.calls) == 4510


def test_failed_send_logged_and_no_marker(sync):
    assert sync.run([], fetch=device(), send=Site(fail=True), now=lambda: NOW) == 1
    assert not sync.MARKER.exists()
    log = sync.SYNC_LOG.read_text()
    assert "FAILED" in log and "next run will try again" in log
    # the next run catches up
    site = Site()
    assert sync.run([], fetch=device(), send=site, now=lambda: NOW) == 0
    assert sync.MARKER.exists()


def test_device_unreachable(sync):
    def broken(*a):
        raise OSError("timed out")
    assert sync.run([], fetch=broken, send=Site(), now=lambda: NOW) == 1
    assert "couldn't read the device" in sync.SYNC_LOG.read_text()


def test_key_never_in_log(sync, capsys):
    sync.run([], fetch=device(), send=Site(), now=lambda: NOW)
    sync.run([], fetch=device(), send=Site(fail=True), now=lambda: NOW)
    out = capsys.readouterr().out
    assert KEY not in sync.SYNC_LOG.read_text() and KEY not in out


def test_empty_device_staff_not_sent(sync):
    site = Site()
    assert sync.run([], fetch=lambda *a: ([], []), send=site, now=lambda: NOW) == 1
    assert site.calls == []


def test_against_the_real_endpoint(sync, app, conn, make):
    """sync.py's batches, fed to the real /api/sync."""
    make.store("mall", key=KEY)
    client = app.test_client()

    def send(url, key, body):
        r = client.post("/api/sync", json=body, headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200, r.get_json()
        return r.get_json()

    assert sync.run([], fetch=device(), send=send, now=lambda: NOW) == 0
    assert conn.execute("SELECT COUNT(*) FROM punches").fetchone()[0] == 4510
    assert sync.run([], fetch=device(), send=send, now=lambda: NOW) == 0
    assert conn.execute("SELECT COUNT(*) FROM punches").fetchone()[0] == 4510
