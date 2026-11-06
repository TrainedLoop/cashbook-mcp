import dataclasses
import shutil

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
        assert db.schema_version() == 1
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
        assert db.schema_version() == 1
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

    bad_dir = tmp_path / "migrations"
    bad_dir.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_initial.sql", bad_dir)
    (bad_dir / "0002_bad.sql").write_text(
        "CREATE TABLE half_done (x INTEGER);\nALTER TABLE connections DROP COLUMN nope;\n"
    )

    with pytest.raises(MigrationError) as exc:
        Database.open(path, KEY, migrations_dir=bad_dir)
    assert "0002_bad.sql" in str(exc.value)
    assert KEY not in str(exc.value)

    assert (tmp_path / "c.duckdb.bak-1").exists()

    db = Database.open(path, KEY)
    try:
        assert db.schema_version() == 1
        cur = db.read()
        assert cur.execute("SELECT alias FROM connections").fetchall() == [("main",)]
        tables = {r[0] for r in cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog='cb'"
        ).fetchall()}
        assert "half_done" not in tables
    finally:
        db.close()

    backup = Database.open(tmp_path / "c.duckdb.bak-1", KEY)
    try:
        assert backup.read().execute("SELECT alias FROM connections").fetchall() == [("main",)]
    finally:
        backup.close()


def test_successful_later_migration(tmp_path):
    path = tmp_path / "c.duckdb"
    Database.open(path, KEY).close()
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_initial.sql", mdir)
    (mdir / "0002_add.sql").write_text("CREATE TABLE extra (x INTEGER);")
    (mdir / "notes.txt").write_text("ignored")
    db = Database.open(path, KEY, migrations_dir=mdir)
    try:
        assert db.schema_version() == 2
        assert db.read().execute("SELECT count(*) FROM extra").fetchone() == (0,)
    finally:
        db.close()
    assert (tmp_path / "c.duckdb.bak-1").exists()


def test_migrations_ship_in_package():
    assert (MIGRATIONS_DIR / "0001_initial.sql").is_file()
    assert MIGRATIONS_DIR.name == "migrations"
    assert MIGRATIONS_DIR.parent.name == "store"
