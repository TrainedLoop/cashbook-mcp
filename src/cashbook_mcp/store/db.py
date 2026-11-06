"""Encrypted DuckDB store: open, migrate, lock down, and hand out cursors.

The database file is attached to an in-memory connection with DuckDB's native
encryption. After migrations run, external access is disabled and the
configuration is locked, so cursors handed to callers (including the agent's
read-only SQL tool) cannot read files, install extensions or undo the lockdown.

The encryption key is never logged, stored on the instance or included in
exception messages.
"""

import os
import re
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_CATALOG = "cb"
_KEY_RE = re.compile(r"[0-9a-fA-F]{64}")
_MIGRATION_RE = re.compile(r"(\d{4})_[A-Za-z0-9_-]+\.sql")
_REDACTED = "<redacted>"


class DatabaseOpenError(Exception):
    """The database file could not be opened (missing or wrong key, I/O error)."""


class MigrationError(Exception):
    """A migration failed; the database file is left as it was before it."""


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _attach_sql(path: Path, key: str) -> str:
    # ATTACH does not accept bind parameters. The key is validated hex and the
    # path is quoted with doubled single quotes.
    return f"ATTACH {_quote(str(path))} AS {_CATALOG} (ENCRYPTION_KEY {_quote(key)})"


def _sanitize(message: str, key: str) -> str:
    return message.replace(key, _REDACTED)


def _attach(conn: duckdb.DuckDBPyConnection, path: Path, key: str) -> None:
    error: str | None = None
    try:
        conn.execute(_attach_sql(path, key))
        conn.execute(f"USE {_CATALOG}")
    except duckdb.Error as e:
        error = _sanitize(str(e), key)
    if error is not None:
        # Raised outside the except block so the original exception, whose
        # text may echo the ATTACH statement, is not chained as __context__.
        raise DatabaseOpenError(f"cannot open database: {error}")


def _load_crypto(conn: duckdb.DuckDBPyConnection) -> None:
    """Load httpfs (OpenSSL crypto, needed to write encrypted files) explicitly.

    Extensions are never installed or loaded implicitly: the image ships httpfs
    in a fixed directory (DUCKDB_EXTENSION_DIR) and the container has no
    writable $HOME to install into.
    """
    ext_dir = os.environ.get("DUCKDB_EXTENSION_DIR")
    error: str | None = None
    try:
        if ext_dir:
            conn.execute(f"SET extension_directory = {_quote(ext_dir)}")
        conn.execute("SET autoinstall_known_extensions = false")
        conn.execute("SET autoload_known_extensions = false")
        conn.execute("LOAD httpfs")
    except duckdb.Error as e:
        error = str(e)
    if error is not None:
        raise DatabaseOpenError(
            "cannot load the httpfs extension (install it with INSTALL httpfs, "
            f"or set DUCKDB_EXTENSION_DIR): {error}"
        )


def _detach(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("CHECKPOINT")
    conn.execute("USE memory")
    conn.execute(f"DETACH {_CATALOG}")


def _wal(path: Path) -> Path:
    return path.with_name(path.name + ".wal")


def _pending(migrations_dir: Path, current: int) -> list[tuple[int, Path]]:
    found: dict[int, Path] = {}
    for file in sorted(migrations_dir.iterdir()):
        m = _MIGRATION_RE.fullmatch(file.name)
        if not m:
            continue
        number = int(m.group(1))
        if number in found:
            raise MigrationError(
                f"duplicate migration number {number}: {found[number].name}, {file.name}"
            )
        found[number] = file
    return sorted((n, f) for n, f in found.items() if n > current)


def _current_version(conn: duckdb.DuckDBPyConnection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    if not rows:
        conn.execute("INSERT INTO schema_version VALUES (0)")
        return 0
    if len(rows) > 1:
        raise MigrationError("schema_version must hold exactly one row")
    return rows[0][0]


def _backup(conn: duckdb.DuckDBPyConnection, path: Path, key: str, version: int) -> None:
    """Copy the file to <path>.bak-<version> with the database detached."""
    _detach(conn)
    try:
        target = path.with_name(f"{path.name}.bak-{version}")
        tmp = target.with_name(target.name + ".tmp")
        shutil.copy2(path, tmp)
        os.replace(tmp, target)
        wal = _wal(path)
        if wal.exists():
            shutil.copy2(wal, _wal(target))
    except OSError as e:
        raise MigrationError(f"cannot back up database before migrating: {e}") from None
    finally:
        _attach(conn, path, key)


def _migrate(
    conn: duckdb.DuckDBPyConnection, path: Path, key: str, migrations_dir: Path
) -> None:
    current = _current_version(conn)
    pending = _pending(migrations_dir, current)
    if not pending:
        return
    if current >= 1:
        _backup(conn, path, key, current)
    for number, file in pending:
        sql = file.read_text()
        error: str | None = None
        conn.execute("BEGIN")
        try:
            conn.execute(sql)
            conn.execute("UPDATE schema_version SET version = ?", [number])
            conn.execute("COMMIT")
        except duckdb.Error as e:
            error = _sanitize(str(e), key)
            try:
                conn.execute("ROLLBACK")
            except duckdb.Error:
                pass
        if error is not None:
            raise MigrationError(f"migration {file.name} failed: {error}")


class Database:
    def __init__(self, conn: duckdb.DuckDBPyConnection, path: Path) -> None:
        self._conn = conn
        self._path = path
        self._lock = threading.Lock()
        self._closed = False

    @classmethod
    def open(
        cls, path: Path, key: str, *, migrations_dir: Path = MIGRATIONS_DIR
    ) -> "Database":
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise DatabaseOpenError("database key must be 64 hex characters")
        path = Path(path)
        conn = duckdb.connect(":memory:")
        try:
            _load_crypto(conn)
            _attach(conn, path, key)
            _migrate(conn, path, key, migrations_dir)
            conn.execute("SET memory_limit='256MB'")
            conn.execute("SET threads=2")
            conn.execute("SET enable_external_access=false")
            conn.execute("SET lock_configuration=true")
        except BaseException:
            conn.close()
            raise
        return cls(conn, path)

    def read(self) -> duckdb.DuckDBPyConnection:
        """A fresh cursor on the store. The caller closes it."""
        cur = self._conn.cursor()
        # Cursors do not inherit the parent's default database.
        cur.execute(f"USE {_CATALOG}")
        return cur

    @contextmanager
    def write(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """A cursor inside BEGIN/COMMIT, serialized; ROLLBACK on any exception."""
        with self._lock:
            cur = self.read()
            try:
                cur.execute("BEGIN")
                yield cur
                cur.execute("COMMIT")
            except BaseException:
                try:
                    cur.execute("ROLLBACK")
                except duckdb.Error:
                    pass
                raise
            finally:
                cur.close()

    def interrupt(self, cursor: duckdb.DuckDBPyConnection) -> None:
        """Abort the query running on cursor; it raises duckdb.InterruptException."""
        cursor.interrupt()

    def schema_version(self) -> int:
        cur = self.read()
        try:
            return cur.execute("SELECT version FROM schema_version").fetchone()[0]
        finally:
            cur.close()

    def size_bytes(self) -> int:
        """Size of the database file plus its WAL, if any."""
        total = self._path.stat().st_size
        wal = _wal(self._path)
        if wal.exists():
            total += wal.stat().st_size
        return total

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._conn.close()
