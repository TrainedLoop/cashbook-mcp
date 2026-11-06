"""cashbook-admin: `init` creates the keys and the database, `status` reports the store."""

import argparse
import asyncio
import os
import secrets
import sys
from pathlib import Path

from cashbook_mcp.analysis.listing import list_connections
from cashbook_mcp.config import ConfigError, load_secrets
from cashbook_mcp.pluggy.client import PluggyClient, PluggyError
from cashbook_mcp.store.db import Database, DatabaseOpenError, MigrationError

KEY_FILES = {
    "db.key": lambda: secrets.token_hex(32),
    "session.key": lambda: secrets.token_urlsafe(48),
}


def _fail(message: str, code: int = 1) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _write_new_key(path: Path, value: str) -> bool:
    """Create path with mode 0600 holding value; False (untouched) if it already exists."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
    path.chmod(0o600)  # O_CREAT's mode is masked by the umask
    return True


async def _check_pluggy(client_id: str, client_secret: str) -> None:
    client = PluggyClient(client_id, client_secret)
    try:
        await client.check_credentials()
    finally:
        await client.aclose()


def _init(secrets_dir: Path, db_path: Path) -> int:
    if not secrets_dir.is_dir():
        return _fail(f"secrets directory {secrets_dir} does not exist")
    for name, make in KEY_FILES.items():
        created = _write_new_key(secrets_dir / name, make())
        print(f"{name}: {'created' if created else 'kept existing'}")
    loaded = load_secrets(secrets_dir)
    try:
        asyncio.run(_check_pluggy(loaded.pluggy_client_id, loaded.pluggy_client_secret))
    except PluggyError as e:
        return _fail(f"Pluggy rejected the credentials in pluggy.json: {e}")
    print("Pluggy credentials: ok")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database.open(db_path, loaded.db_key)
    try:
        print(f"database: {db_path} (schema version {db.schema_version()})")
    finally:
        db.close()
    return 0


def _status(secrets_dir: Path, db_path: Path) -> int:
    if not db_path.exists():
        return _fail(f"database {db_path} does not exist; run `cashbook-admin init` first")
    loaded = load_secrets(secrets_dir)
    db = Database.open(db_path, loaded.db_key)
    try:
        conns = list_connections(db)
        cur = db.read()
        try:
            runs = {
                alias: (finished, outcome)
                for alias, finished, outcome in cur.execute(
                    "SELECT alias, max_by(finished_at, started_at), max_by(outcome, started_at) "
                    "FROM sync_runs GROUP BY alias"
                ).fetchall()
            }
        finally:
            cur.close()
        print(f"connections: {len(conns)}")
        for c in conns:
            code = f", error code {c['error_code']}" if c["error_code"] else ""
            print(f"- {c['alias']} ({c['bank']}): status {c['status'] or 'unknown'}{code}")
            print(f"    last successful sync: {c['last_synced_at'] or 'never'}")
            finished, outcome = runs.get(c["alias"], (None, None))
            if finished is not None:
                print(f"    last sync run: {finished.isoformat()} ({outcome})")
            if c["consent_expires_at"]:
                print(f"    consent expires: {c['consent_expires_at']}")
            history = c["history"]
            print(f"    transactions: {history['transactions']}"
                  + (f" ({history['date_from']} to {history['date_to']})" if history["date_from"] else ""))  # fmt: skip
            if c["last_sync_error"]:
                print(f"    last sync error: {c['last_sync_error']}")
            if c["hint"]:
                print(f"    action: {c['hint']}")
        print(f"schema version: {db.schema_version()}")
        print(f"database size: {db.size_bytes():,} bytes")
    finally:
        db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cashbook-admin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in [
        ("init", "create db.key and session.key if missing, check Pluggy credentials, create or migrate the db"),
        ("status", "show connections, last syncs, errors and database info (stop the server first)"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--secrets", type=Path, default=Path(os.environ.get("SECRETS_DIR") or "/secrets"))
        p.add_argument("--db", type=Path, default=Path(os.environ.get("DB_PATH") or "/data/cashbook.duckdb"))
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return _init(args.secrets, args.db)
        return _status(args.secrets, args.db)
    except (ConfigError, DatabaseOpenError, MigrationError) as e:
        return _fail(str(e))
    except OSError as e:
        return _fail(f"{type(e).__name__}: {e.strerror or e}")


if __name__ == "__main__":
    sys.exit(main())
