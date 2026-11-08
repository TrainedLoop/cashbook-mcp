import threading
import time

import duckdb
import pytest

from cashbook_mcp.store.db import Database, DatabaseOpenError

KEY = "ab" * 32
OTHER_KEY = "cd" * 32
SECRET_TEXT = "ZZ-DISTINCTIVE-PAYEE-7731-ZZ"


@pytest.fixture
def db(tmp_path):
    database = Database.open(tmp_path / "cashbook.duckdb", KEY)
    yield database
    database.close()


def insert_connection(cur, alias="main"):
    cur.execute(
        "INSERT INTO connections (alias, item_id, connector_name, created_at) "
        "VALUES (?, ?, ?, now())",
        [alias, f"item-{alias}", SECRET_TEXT],
    )


def test_file_is_encrypted(tmp_path):
    path = tmp_path / "cashbook.duckdb"
    database = Database.open(path, KEY)
    with database.write() as cur:
        insert_connection(cur)
    database.close()

    for f in tmp_path.iterdir():
        assert SECRET_TEXT.encode() not in f.read_bytes(), f.name

    with pytest.raises(DatabaseOpenError) as exc:
        Database.open(path, OTHER_KEY)
    assert OTHER_KEY not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None

    reopened = Database.open(path, KEY)
    try:
        rows = reopened.read().execute("SELECT connector_name FROM connections").fetchall()
        assert rows == [(SECRET_TEXT,)]
    finally:
        reopened.close()


def test_open_rejects_malformed_key(tmp_path):
    bad = "x' ; DROP --" + "a" * 50
    with pytest.raises(DatabaseOpenError) as exc:
        Database.open(tmp_path / "cashbook.duckdb", bad)
    assert bad not in str(exc.value)
    assert not (tmp_path / "cashbook.duckdb").exists()


def test_open_quotes_path(tmp_path):
    path = tmp_path / "it's here.duckdb"
    database = Database.open(path, KEY)
    try:
        assert database.schema_version() >= 1
    finally:
        database.close()
    assert path.exists()


def test_external_access_locked(db, tmp_path):
    for sql in [
        "SELECT * FROM read_csv('/etc/passwd')",
        f"ATTACH '{tmp_path / 'other.db'}' AS x",
        "SET enable_external_access=true",
        "SET GLOBAL enable_external_access=true",
        "SET lock_configuration=false",
        "RESET lock_configuration",
        "SET memory_limit='4GB'",
        "INSTALL httpfs",
        "COPY connections TO '/tmp/cashbook-leak.csv'",
        "SELECT * FROM read_csv('https://example.com/x.csv')",
    ]:
        with pytest.raises(duckdb.Error):
            db.read().execute(sql)
    assert not (tmp_path / "other.db").exists()


def test_limits_applied(db):
    cur = db.read()
    assert cur.execute("SELECT current_setting('threads')").fetchone() == (2,)
    limit = cur.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    assert limit.startswith(("244.1", "256"))


def test_spill_files_next_to_store_and_encrypted(db):
    cur = db.read()
    temp_dir = cur.execute("SELECT current_setting('temp_directory')").fetchone()[0]
    assert temp_dir == str(db._path) + ".tmp"
    assert cur.execute("SELECT current_setting('temp_file_encryption')").fetchone() == (True,)


def test_read_cursor_sees_store_tables(db):
    cur = db.read()
    assert cur.execute("SELECT count(*) FROM v_transactions").fetchone() == (0,)
    assert cur.execute("SELECT current_database()").fetchone() == ("cb",)


def test_write_commits(db):
    with db.write() as cur:
        insert_connection(cur)
    assert db.read().execute("SELECT count(*) FROM connections").fetchone() == (1,)


def test_write_rolls_back(db):
    with pytest.raises(RuntimeError, match="boom"), db.write() as cur:
        insert_connection(cur)
        raise RuntimeError("boom")
    assert db.read().execute("SELECT count(*) FROM connections").fetchone() == (0,)
    with db.write() as cur:
        insert_connection(cur, "second")
    assert db.read().execute("SELECT alias FROM connections").fetchall() == [("second",)]


def test_write_is_serialized(db):
    inside = 0
    peak = 0
    guard = threading.Lock()

    def worker(i):
        nonlocal inside, peak
        with db.write() as cur:
            with guard:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.02)
            insert_connection(cur, f"a{i}")
            with guard:
                inside -= 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 1
    assert db.read().execute("SELECT count(*) FROM connections").fetchone() == (4,)


def test_interrupt(db):
    cur = db.read()
    errors = []

    def run():
        try:
            cur.execute("SELECT count(*) FROM range(100000000000) a")
        except duckdb.Error as e:
            errors.append(e)

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.3)
    db.interrupt(cur)
    t.join(10)
    assert not t.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], duckdb.InterruptException)


def test_views_exclude_removed(db):
    with db.write() as cur:
        insert_connection(cur)
        for tid, amount, kind, removed in [
            ("t1", "-10.00", "purchase", False),
            ("t2", "-20.00", "purchase", True),
            ("t3", "-30.00", "internal_transfer", False),
            ("t4", "-40.00", "card_bill_payment", False),
            ("t5", "-50.00", "investment", False),
            ("t6", "100.00", "income", False),
            ("t7", "-5.00", "fee", False),
        ]:
            cur.execute(
                "INSERT INTO transactions (id, alias, account_id, date, amount, currency, "
                "description, kind, kind_reason, text_suspicious, suspicion_reasons, "
                "first_seen_at, last_seen_at, removed_at) VALUES "
                "(?, 'main', 'a1', DATE '2026-03-10', ?, 'BRL', 'x', ?, '', false, "
                "['r1'], now(), now(), CASE WHEN ? THEN now() END)",
                [tid, amount, kind, removed],
            )
        cur.execute(
            "INSERT INTO accounts (id, alias, item_id, type, subtype, name, balance, "
            "currency, first_seen_at, last_seen_at, removed_at) VALUES "
            "('a1', 'main', 'item-main', 'BANK', 'CHECKING', 'Acc', 1.50, 'BRL', now(), now(), NULL),"
            "('a2', 'main', 'item-main', 'BANK', 'CHECKING', 'Old', 0, 'BRL', now(), now(), now())"
        )
    cur = db.read()
    ids = [r[0] for r in cur.execute("SELECT id FROM v_transactions ORDER BY id").fetchall()]
    assert ids == ["t1", "t3", "t4", "t5", "t6", "t7"]
    spend = [r[0] for r in cur.execute("SELECT id FROM v_flows WHERE flow = 'spending' ORDER BY id").fetchall()]
    assert spend == ["t1", "t7"]
    assert cur.execute("SELECT id FROM v_accounts").fetchall() == [("a1",)]
    reasons = cur.execute("SELECT suspicion_reasons FROM transactions WHERE id='t1'").fetchone()
    assert reasons == (["r1"],)


def test_all_views_exist(db):
    cur = db.read()
    for view in [
        "v_accounts", "v_transactions", "v_bills", "v_investments",
        "v_investment_transactions", "v_loans", "v_flows",
    ]:
        assert cur.execute(f"SELECT count(*) FROM {view}").fetchone() == (0,)


def test_open_never_autoinstalls_extensions(tmp_path, monkeypatch):
    # An empty extension directory: httpfs is missing, and open must fail
    # instead of downloading it (the container has no writable $HOME).
    empty = tmp_path / "ext"
    empty.mkdir()
    monkeypatch.setenv("DUCKDB_EXTENSION_DIR", str(empty))
    with pytest.raises(DatabaseOpenError, match="httpfs"):
        Database.open(tmp_path / "cashbook.duckdb", KEY)
    assert list(empty.iterdir()) == []
    assert not (tmp_path / "cashbook.duckdb").exists()


def test_open_disables_extension_autoload(db):
    cur = db.read()
    for name in ("autoinstall_known_extensions", "autoload_known_extensions"):
        assert cur.execute(f"SELECT current_setting('{name}')").fetchone() == (False,)


def test_schema_tables_and_checks(db):
    assert db.schema_version() >= 2
    cur = db.read()
    try:
        tables = {r[0] for r in cur.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        assert {
            "account_settings",
            "balance_snapshots",
            "investment_snapshots",
            "classification_changes",
            "classification_overrides",
        } <= tables
        tx_cols = {r[0] for r in cur.execute("DESCRIBE transactions").fetchall()}
        assert {"installment_number", "installment_total", "purchase_date"} <= tx_cols
    finally:
        cur.close()
    with db.write() as cur:
        insert_connection(cur)
        assert cur.execute("SELECT role FROM connections").fetchone() == ("personal",)
    with pytest.raises(duckdb.ConstraintException), db.write() as cur:
        cur.execute("UPDATE connections SET role = 'other'")
    with pytest.raises(duckdb.ConstraintException), db.write() as cur:
        cur.execute("INSERT INTO account_settings VALUES ('a', 'main', 'bogus')")
