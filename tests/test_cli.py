import json
import stat
from datetime import datetime

import pytest

from cashbook_mcp import cli
from cashbook_mcp.pluggy.client import PluggyError
from cashbook_mcp.store.db import Database


class FakePluggy:
    calls = 0
    fail = False

    def __init__(self, client_id, client_secret, **kwargs):
        assert (client_id, client_secret) == ("pid", "psecret")

    async def check_credentials(self):
        FakePluggy.calls += 1
        if FakePluggy.fail:
            raise PluggyError("Invalid credentials", status=401)

    async def aclose(self):
        pass


@pytest.fixture
def secrets_dir(tmp_path, monkeypatch):
    d = tmp_path / "secrets"
    d.mkdir()
    (d / "pluggy.json").write_text(json.dumps({"clientId": "pid", "clientSecret": "psecret"}))
    (d / "google-oauth.json").write_text(json.dumps({"web": {"client_id": "gid", "client_secret": "gs"}}))
    FakePluggy.calls = 0
    FakePluggy.fail = False
    monkeypatch.setattr(cli, "PluggyClient", FakePluggy)
    return d


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_init_creates_keys_once(secrets_dir, tmp_path, capsys):
    db_path = tmp_path / "data" / "cashbook.duckdb"
    argv = ["init", "--secrets", str(secrets_dir), "--db", str(db_path)]
    assert cli.main(argv) == 0
    db_key = (secrets_dir / "db.key").read_text().strip()
    session_key = (secrets_dir / "session.key").read_text().strip()
    assert len(db_key) == 64 and int(db_key, 16) >= 0
    assert len(session_key) >= 48
    assert _mode(secrets_dir / "db.key") == 0o600
    assert _mode(secrets_dir / "session.key") == 0o600
    assert db_path.exists()
    assert FakePluggy.calls == 1

    assert cli.main(argv) == 0
    assert (secrets_dir / "db.key").read_text().strip() == db_key
    assert (secrets_dir / "session.key").read_text().strip() == session_key
    assert _mode(secrets_dir / "db.key") == 0o600
    out = capsys.readouterr().out
    assert db_key not in out and session_key not in out
    db = Database.open(db_path, db_key)
    db.close()


def test_init_fails_on_bad_pluggy_credentials(secrets_dir, tmp_path, capsys):
    FakePluggy.fail = True
    rc = cli.main(["init", "--secrets", str(secrets_dir), "--db", str(tmp_path / "c.duckdb")])
    assert rc != 0
    assert "Pluggy" in capsys.readouterr().err
    assert not (tmp_path / "c.duckdb").exists()


def test_init_fails_without_pluggy_json(secrets_dir, tmp_path, capsys):
    (secrets_dir / "pluggy.json").unlink()
    rc = cli.main(["init", "--secrets", str(secrets_dir), "--db", str(tmp_path / "c.duckdb")])
    assert rc != 0
    assert "pluggy.json" in capsys.readouterr().err


def test_status_prints_connections_and_db_info(secrets_dir, tmp_path, capsys):
    db_path = tmp_path / "c.duckdb"
    assert cli.main(["init", "--secrets", str(secrets_dir), "--db", str(db_path)]) == 0
    key = (secrets_dir / "db.key").read_text().strip()
    db = Database.open(db_path, key)
    with db.write() as cur:
        cur.execute(
            "INSERT INTO connections (alias, item_id, connector_name, created_at, status, error_code, "
            "last_synced_at, last_sync_error) VALUES "
            "('main', 'item-1', 'Bank A', ?, 'LOGIN_ERROR', 'INVALID_CREDENTIALS', ?, 'boom')",
            [datetime(2026, 1, 1), datetime(2026, 11, 6, 9, 0)],
        )
    db.close()
    capsys.readouterr()
    assert cli.main(["status", "--secrets", str(secrets_dir), "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "main" in out and "Bank A" in out and "LOGIN_ERROR" in out
    assert "2026-11-06T09:00:00" in out
    assert "boom" in out
    assert "schema version: 2" in out
    assert "database size:" in out


def test_status_fails_without_database(secrets_dir, tmp_path):
    (secrets_dir / "db.key").write_text("ab" * 32)
    (secrets_dir / "session.key").write_text("s" * 64)
    assert cli.main(["status", "--secrets", str(secrets_dir), "--db", str(tmp_path / "missing.duckdb")]) != 0


def test_unknown_command_is_an_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["nope"])
    assert exc.value.code != 0
