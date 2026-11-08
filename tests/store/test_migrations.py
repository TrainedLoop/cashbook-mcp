import dataclasses
import shutil
from datetime import datetime

import duckdb
import pytest

from cashbook_mcp import models
from cashbook_mcp.store.db import MIGRATIONS_DIR, Database, MigrationError

KEY = "ab" * 32

TABLES = {
    "connections": models.Connection,
    "accounts": models.Account,
    "transactions": models.Transaction,
    "bills": models.Bill,
    "investments": models.Investment,
    "investment_transactions": models.InvestmentTransaction,
    "loans": models.Loan,
}


LATEST = max(int(f.name[:4]) for f in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def copy_migrations(dst, upto=LATEST):
    dst.mkdir()
    for f in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"):
        if int(f.name[:4]) <= upto:
            shutil.copy(f, dst)
    return dst


def columns(db, table):
    return {
        name: dtype
        for name, dtype in db.read().execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_catalog='cb' AND table_name=?",
            [table],
        ).fetchall()
    }


def test_fresh_db_is_at_latest_version(tmp_path):
    db = Database.open(tmp_path / "c.duckdb", KEY)
    try:
        assert db.schema_version() == LATEST
        assert db.read().execute("SELECT count(*) FROM schema_version").fetchone() == (1,)
    finally:
        db.close()
    assert not list(tmp_path.glob("*.bak-*"))


def test_columns_follow_models(tmp_path):
    db = Database.open(tmp_path / "c.duckdb", KEY)
    try:
        for table, model in TABLES.items():
            cols = columns(db, table)
            expected = {f.name for f in dataclasses.fields(model)}
            if table != "connections":
                expected |= {"first_seen_at", "last_seen_at", "removed_at"}
            if table == "transactions":
                expected |= {"changed_at"}
            assert set(cols) == expected, table
        tx = columns(db, "transactions")
        assert tx["amount"] == "DECIMAL(18,2)"
        assert tx["suspicion_reasons"] == "VARCHAR[]"
        assert tx["removed_at"] == "TIMESTAMP"
        assert columns(db, "accounts")["balance"] == "DECIMAL(18,2)"
        assert set(columns(db, "sync_runs")) == {
            "id", "alias", "started_at", "finished_at", "outcome",
            "rows_upserted", "rows_removed", "error",
        }
    finally:
        db.close()


def test_sync_runs_outcome_is_checked(tmp_path):
    db = Database.open(tmp_path / "c.duckdb", KEY)
    try:
        with db.write() as cur:
            for outcome in ("ok", "error", "discarded"):
                cur.execute(
                    "INSERT INTO sync_runs (alias, started_at, outcome) VALUES ('main', now(), ?)",
                    [outcome],
                )
        with pytest.raises(duckdb.ConstraintException), db.write() as cur:
            cur.execute(
                "INSERT INTO sync_runs (alias, started_at, outcome) "
                "VALUES ('main', now(), 'weird')"
            )
        ids = db.read().execute("SELECT id FROM sync_runs ORDER BY id").fetchall()
        assert len({i for (i,) in ids}) == 3
    finally:
        db.close()


def test_reopen_is_idempotent(tmp_path):
    path = tmp_path / "c.duckdb"
    Database.open(path, KEY).close()
    db = Database.open(path, KEY)
    try:
        assert db.schema_version() == LATEST
    finally:
        db.close()
    assert not list(tmp_path.glob("*.bak-*"))


def test_migration_backup_and_failure(tmp_path):
    path = tmp_path / "c.duckdb"
    db = Database.open(path, KEY)
    with db.write() as cur:
        cur.execute(
            "INSERT INTO connections (alias, item_id, connector_name, created_at) "
            "VALUES ('main', 'item-1', 'Bank', now())"
        )
    db.close()

    bad_name = f"{LATEST + 1:04d}_bad.sql"
    bad_dir = copy_migrations(tmp_path / "migrations")
    (bad_dir / bad_name).write_text(
        "CREATE TABLE half_done (x INTEGER);\nALTER TABLE connections DROP COLUMN nope;\n"
    )

    with pytest.raises(MigrationError) as exc:
        Database.open(path, KEY, migrations_dir=bad_dir)
    assert bad_name in str(exc.value)
    assert KEY not in str(exc.value)

    assert (tmp_path / f"c.duckdb.bak-{LATEST}").exists()

    db = Database.open(path, KEY)
    try:
        assert db.schema_version() == LATEST
        cur = db.read()
        assert cur.execute("SELECT alias FROM connections").fetchall() == [("main",)]
        tables = {r[0] for r in cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog='cb'"
        ).fetchall()}
        assert "half_done" not in tables
    finally:
        db.close()

    backup = Database.open(tmp_path / f"c.duckdb.bak-{LATEST}", KEY)
    try:
        assert backup.read().execute("SELECT alias FROM connections").fetchall() == [("main",)]
    finally:
        backup.close()


def test_successful_later_migration(tmp_path):
    path = tmp_path / "c.duckdb"
    Database.open(path, KEY).close()
    mdir = copy_migrations(tmp_path / "migrations")
    (mdir / f"{LATEST + 1:04d}_add.sql").write_text("CREATE TABLE extra (x INTEGER);")
    (mdir / "notes.txt").write_text("ignored")
    db = Database.open(path, KEY, migrations_dir=mdir)
    try:
        assert db.schema_version() == LATEST + 1
        assert db.read().execute("SELECT count(*) FROM extra").fetchone() == (0,)
    finally:
        db.close()
    assert (tmp_path / f"c.duckdb.bak-{LATEST}").exists()


def test_v04_migration_keeps_v3_data(tmp_path):
    path = tmp_path / "c.duckdb"
    v3_dir = copy_migrations(tmp_path / "v3", 3)
    db = Database.open(path, KEY, migrations_dir=v3_dir)
    assert db.schema_version() == 3
    with db.write() as cur:
        cur.execute(
            "INSERT INTO connections (alias, item_id, connector_name, created_at) "
            "VALUES ('main', 'item-1', 'Bank', now())"
        )
        cur.execute(
            "INSERT INTO classification_changes (change_id, created_at, kind, note, count) "
            "VALUES ('c1', now(), 'refund', 'old', 2)"
        )
    db.close()

    db = Database.open(path, KEY)
    try:
        assert db.schema_version() == LATEST
        cur = db.read()
        assert cur.execute("SELECT alias FROM connections").fetchall() == [("main",)]
        assert cur.execute(
            "SELECT change_id, kind, category, note, count FROM classification_changes"
        ).fetchall() == [("c1", "refund", None, "old", 2)]
        cur.execute(
            "INSERT INTO classification_changes (change_id, created_at, category, count) "
            "VALUES ('c2', now(), 'Rent', 1)"
        )  # kind is nullable now
        assert cur.execute("SELECT count(*) FROM category_overrides").fetchone() == (0,)
        cols = {r[0] for r in cur.execute("DESCRIBE v_flows").fetchall()}
        assert {"pluggy_category", "pluggy_category_original", "category_overridden"} <= cols
    finally:
        db.close()
    assert (tmp_path / "c.duckdb.bak-3").exists()


def test_v5_keeps_overrides_journal_and_adds_one_off_flow(tmp_path):
    path = tmp_path / "c.duckdb"
    v4 = copy_migrations(tmp_path / "v4", 4)
    db = Database.open(path, KEY, migrations_dir=v4)
    assert db.schema_version() == 4
    with db.write() as cur:
        cur.execute(
            "INSERT INTO connections (alias, item_id, connector_name, created_at) "
            "VALUES ('main', 'item-1', 'Bank', now())"
        )
        cur.execute(
            "INSERT INTO accounts (id, alias, item_id, type, subtype, name, balance, currency, "
            "first_seen_at, last_seen_at) "
            "VALUES ('a1', 'main', 'item-1', 'BANK', 'X', 'Acc', 0, 'BRL', now(), now())"
        )
        for tid, kind in (("t1", "purchase"), ("t2", "purchase")):
            cur.execute(
                "INSERT INTO transactions (id, alias, account_id, date, amount, currency, description, "
                "kind, kind_reason, pluggy_category, first_seen_at, last_seen_at) "
                "VALUES (?, 'main', 'a1', '2026-01-02', -5, 'BRL', 'x', ?, 'sign', 'Shopping', now(), now())",
                [tid, kind],
            )
        cur.execute("INSERT INTO category_overrides VALUES ('t1', 'main', 'Housing', 'c1')")
        cur.execute(
            "INSERT INTO classification_changes (change_id, created_at, kind, category, note, count) "
            "VALUES ('c1', now(), NULL, 'Housing', 'n', 1)"
        )
    db.close()

    db = Database.open(path, KEY)
    try:
        assert db.schema_version() == LATEST
        cur = db.read()
        assert cur.execute("SELECT category FROM category_overrides").fetchall() == [("Housing",)]
        assert cur.execute("SELECT change_id FROM classification_changes").fetchall() == [("c1",)]
        assert cur.execute(
            "SELECT pluggy_category, flow FROM v_flows WHERE id = 't1'"
        ).fetchone() == ("Housing", "spending")
    finally:
        db.close()


def test_v6_backfills_changed_at_and_keeps_overrides(tmp_path):
    path = tmp_path / "c.duckdb"
    v5 = copy_migrations(tmp_path / "v5", 5)
    db = Database.open(path, KEY, migrations_dir=v5)
    assert db.schema_version() == 5
    with db.write() as cur:
        cur.execute(
            "INSERT INTO connections (alias, item_id, connector_name, created_at) "
            "VALUES ('main', 'item-1', 'Bank', now())"
        )
        cur.execute(
            "INSERT INTO accounts (id, alias, item_id, type, subtype, name, balance, currency, "
            "first_seen_at, last_seen_at) "
            "VALUES ('a1', 'main', 'item-1', 'BANK', 'X', 'Acc', 0, 'BRL', now(), now())"
        )
        for tid, seen in (("t1", "2026-01-02 10:00:00"), ("t2", "2026-02-03 11:00:00")):
            cur.execute(
                "INSERT INTO transactions (id, alias, account_id, date, amount, currency, description, "
                "kind, kind_reason, pluggy_category, first_seen_at, last_seen_at) "
                "VALUES (?, 'main', 'a1', '2026-01-02', -5, 'BRL', 'x', 'purchase', 'sign', 'Shopping', ?, now())",
                [tid, seen],
            )
        cur.execute("INSERT INTO category_overrides VALUES ('t1', 'main', 'Housing', 'c1')")
        cur.execute("INSERT INTO classification_overrides VALUES ('t2', 'main', 'one_off', 'c2')")
    db.close()

    db = Database.open(path, KEY)
    try:
        assert db.schema_version() == LATEST
        cur = db.read()
        assert cur.execute(
            "SELECT id, changed_at = first_seen_at, payment_method FROM transactions ORDER BY id"
        ).fetchall() == [("t1", True, None), ("t2", True, None)]
        assert cur.execute("SELECT category FROM category_overrides").fetchall() == [("Housing",)]
        assert cur.execute("SELECT kind FROM classification_overrides").fetchall() == [("one_off",)]
        assert cur.execute(
            "SELECT pluggy_category, payment_method, changed_at FROM v_flows WHERE id = 't1'"
        ).fetchone() == ("Housing", None, datetime(2026, 1, 2, 10, 0))
        assert cur.execute("SELECT next_auto_sync_at FROM connections").fetchone() == (None,)
    finally:
        db.close()
