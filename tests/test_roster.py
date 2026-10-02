"""rosters/<store>.csv: names for listed numbers, everyone else hidden."""

from pathlib import Path

import pytest

import roster

KEY = "office-key-" + "x" * 30


@pytest.fixture
def rdir(tmp_path, app):
    d = tmp_path / "rosters"
    d.mkdir()
    app.config["ROSTER_DIR"] = d
    return d


def sync(app, ids):
    return app.test_client().post(
        "/api/sync", json={"staff": [{"user_id": str(i), "name": f"NN-{i}"} for i in ids], "punches": []},
        headers={"Authorization": f"Bearer {KEY}"})


def people(conn):
    return {r["user_id"]: (r["name"], r["hidden"]) for r in conn.execute(
        "SELECT * FROM employees WHERE store='office'")}


def test_roster_applied_on_first_sync(app, conn, make, rdir):
    make.store("office", key=KEY)
    (rdir / "office.csv").write_text("number,name\n2,Aliya Shirin\n12,Mohammed Wasim Uddin\n77,Not Enrolled\n")
    assert sync(app, [1, 2, 12, 150]).status_code == 200
    assert people(conn) == {"1": ("", 1), "2": ("Aliya Shirin", 0),
                            "12": ("Mohammed Wasim Uddin", 0), "150": ("", 1)}
    log = conn.execute("SELECT details FROM audit_log WHERE action='roster.apply'").fetchone()[0]
    assert '"named": 2' in log and '"hidden": 2' in log and '"77"' in log


def test_applied_once_then_new_people_and_edits_left_alone(app, conn, make, rdir, as_user):
    make.store("office", key=KEY)
    owner = make.user("boss", "owner")
    (rdir / "office.csv").write_text("number,name\n2,Aliya Shirin\n")
    sync(app, [1, 2])
    as_user(owner).post("/people/show", data={"person": "office:1"})        # you un-hide someone
    sync(app, [1, 2, 151])                                                  # a new joiner
    got = people(conn)
    assert got["1"][1] == 0 and got["151"] == ("", 0)
    # changing the file applies it again
    (rdir / "office.csv").write_text("number,name\n2,Aliya Shirin\n151,New Joiner\n")
    sync(app, [1, 2, 151])
    got = people(conn)
    assert got["151"] == ("New Joiner", 0) and got["1"][1] == 1


def test_applied_at_startup_when_staff_already_known(tmp_path, now, make, conn):
    from app import create_app
    import db
    rd = tmp_path / "r2"
    rd.mkdir()
    (rd / "office.csv").write_text("number,name\n5,Saba\n")
    path = tmp_path / "s.db"
    db.init_db(str(path))
    c = db.connect(str(path))
    c.execute("INSERT INTO stores (store, display_name) VALUES ('office','Office')")
    c.executemany("INSERT INTO employees (store, user_id, name, on_device, first_seen, last_seen) "
                  "VALUES ('office',?,'',1,'x','x')", [("5",), ("6",)])
    c.commit()
    create_app({"TESTING": True, "DB_PATH": str(path), "SECRET_KEY": "s", "STORAGE_DIR": str(tmp_path),
                "ROSTER_DIR": rd})
    got = {r["user_id"]: (r["name"], r["hidden"]) for r in c.execute("SELECT * FROM employees")}
    assert got == {"5": ("Saba", 0), "6": ("", 1)}


def test_the_real_office_roster_parses():
    names, _ = roster.load(Path(__file__).resolve().parent.parent / "rosters" / "office.csv")
    assert len(names) == 31 and names["2"] == "Aliya Shirin" and names["139"] == "Faizan Khan (Account)"


def test_roster_files_are_not_ignored_by_git():
    """A roster that git ignores never reaches the website."""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    for f in (root / "rosters").glob("*.csv"):
        r = subprocess.run(["git", "check-ignore", "-q", str(f)], cwd=root)
        assert r.returncode == 1, f"{f.name} is ignored by .gitignore"
