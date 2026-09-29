"""POST /api/sync."""

import pytest

MALL_KEY = "mall-key-" + "x" * 30
NIRALA_KEY = "nirala-key-" + "y" * 30


@pytest.fixture
def stores(make):
    make.store("mall", key=MALL_KEY)
    make.store("nirala", key=NIRALA_KEY)


def post(client, body, key=MALL_KEY):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return client.post("/api/sync", json=body, headers=headers)


BODY = {"staff": [{"user_id": "101", "name": "Aditi"}, {"user_id": "102", "name": "Rahul"}],
        "punches": [{"user_id": "101", "ts": "2026-10-06 09:02:00", "punch": 0},
                    {"user_id": "101", "ts": "2026-10-06 20:01:00", "punch": 1}]}


def count(conn, store=None):
    q, a = "SELECT COUNT(*) FROM punches", ()
    if store:
        q, a = q + " WHERE store=?", (store,)
    return conn.execute(q, a).fetchone()[0]


def test_same_batch_twice_adds_nothing(app, conn, stores):
    c = app.test_client()
    r1 = post(c, BODY).get_json()
    r2 = post(c, BODY).get_json()
    assert r1 == {"punches_received": 2, "new_punches": 2, "staff": 2}
    assert r2["new_punches"] == 0
    assert count(conn) == 2


def test_wrong_or_missing_key(app, stores):
    c = app.test_client()
    assert post(c, BODY, key="nope").status_code == 401
    assert post(c, BODY, key=None).status_code == 401


def test_mall_key_never_writes_nirala(app, conn, stores):
    body = dict(BODY, store="nirala")
    post(app.test_client(), body)
    assert count(conn, "nirala") == 0 and count(conn, "mall") == 2


def test_new_key_disables_old(app, conn, stores, make, as_user):
    owner = make.user("owner", "owner")
    c = as_user(owner)
    r = c.post("/stores/mall/key", data={"action": "enter", "key": "brand-new-key-" + "z" * 20})
    assert r.status_code == 302
    assert post(app.test_client(), BODY).status_code == 401
    assert post(app.test_client(), BODY, key="brand-new-key-" + "z" * 20).status_code == 200
    r = c.post("/stores/mall/key", data={"action": "create"})
    assert b"shown only this once" in r.data
    assert post(app.test_client(), BODY, key="brand-new-key-" + "z" * 20).status_code == 401


def test_empty_staff_list_refused(app, stores):
    assert post(app.test_client(), {"staff": [], "punches": []}).status_code == 400


def test_missing_person_marked_not_deleted(app, conn, stores):
    c = app.test_client()
    post(c, BODY)
    post(c, {"staff": [{"user_id": "101", "name": "Aditi"}], "punches": []})
    rows = {r["user_id"]: r["on_device"] for r in conn.execute("SELECT * FROM employees")}
    assert rows == {"101": 1, "102": 0}


def test_future_punch_sets_clock_warning(app, conn, stores, now):
    body = {"staff": BODY["staff"], "punches": [{"user_id": "101", "ts": "2027-01-01 09:00:00"}]}
    post(app.test_client(), body)
    warning = conn.execute("SELECT clock_warning FROM stores WHERE store='mall'").fetchone()[0]
    assert "future" in warning
    post(app.test_client(), BODY)
    assert conn.execute("SELECT clock_warning FROM stores WHERE store='mall'").fetchone()[0] == ""


def test_batch_limit(app, stores):
    body = {"punches": [{"user_id": "1", "ts": f"2026-01-01 09:{i // 60 % 60:02d}:{i % 60:02d}"}
                        for i in range(5001)]}
    assert post(app.test_client(), body).status_code == 413


def test_bad_timestamp_refused_and_nothing_written(app, conn, stores):
    body = {"staff": BODY["staff"], "punches": BODY["punches"] + [{"user_id": "1", "ts": "yesterday"}]}
    assert post(app.test_client(), body).status_code == 400
    assert count(conn) == 0


def test_punches_from_unknown_people_get_a_row(app, conn, stores):
    post(app.test_client(), {"staff": BODY["staff"], "punches": [{"user_id": "555", "ts": "2026-10-01 09:00:00"}]})
    row = conn.execute("SELECT * FROM employees WHERE user_id='555'").fetchone()
    assert row is not None and row["on_device"] == 0


def test_sync_records_status(app, conn, stores):
    post(app.test_client(), BODY)
    row = conn.execute("SELECT * FROM stores WHERE store='mall'").fetchone()
    assert row["last_sync_at"] and row["last_sync_note"] == "2 received, 2 new"
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='sync'").fetchone()[0] == 1


def test_sync_works_without_csrf_token(tmp_path, now):
    """With CSRF protection switched on, the sync still works on its key alone,
    and ordinary forms without a token are refused."""
    from app import create_app
    import db
    from views.api import hash_key
    app = create_app({"TESTING": True, "DB_PATH": str(tmp_path / "a.db"), "SECRET_KEY": "s",
                      "SESSION_COOKIE_SECURE": False, "STORAGE_DIR": str(tmp_path)})
    conn = db.connect(app.config["DB_PATH"])
    conn.execute("INSERT INTO stores (store, display_name, sync_key_hash) VALUES ('mall','Mall',?)",
                 (hash_key(MALL_KEY),))
    conn.commit()
    c = app.test_client()
    assert post(c, BODY).status_code == 200
    assert c.post("/login", data={"username": "x", "password": "y"}).status_code == 400
