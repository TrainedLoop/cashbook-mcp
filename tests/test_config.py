import json
from datetime import time
from pathlib import Path

import pytest

from cashbook_mcp.config import ConfigError, Settings, load_secrets

BASE = {"PUBLIC_URL": "https://owner.example.ts.net/", "ALLOWED_EMAILS": " A@example.com , b@example.com"}


def test_defaults():
    s = Settings.from_env(BASE)
    assert s.public_url == "https://owner.example.ts.net"
    assert s.allowed_emails == {"a@example.com", "b@example.com"}
    assert s.port == 8743
    assert s.refresh_at == time(6, 0)
    assert s.tz.key == "America/Sao_Paulo"
    assert s.history_months == 12
    assert s.db_path == Path("/data/cashbook.duckdb")
    assert s.secrets_dir == Path("/secrets")
    assert s.allowed_redirect_uris == (
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    )


@pytest.mark.parametrize(
    ("override", "name"),
    [
        ({"REFRESH_AT": "25:00"}, "REFRESH_AT"),
        ({"HISTORY_MONTHS": "0"}, "HISTORY_MONTHS"),
        ({"TZ": "Mars/Base"}, "TZ"),
        ({"ALLOWED_EMAILS": ""}, "ALLOWED_EMAILS"),
        ({"PUBLIC_URL": None}, "PUBLIC_URL"),
        ({"PORT": "abc"}, "PORT"),
        ({"ALLOWED_REDIRECT_URIS": "https://*.example.com/cb"}, "ALLOWED_REDIRECT_URIS"),
        ({"ALLOWED_REDIRECT_URIS": " "}, "ALLOWED_REDIRECT_URIS"),
    ],
)
def test_invalid_values_raise(override, name):
    env = {**BASE, **override}
    env = {k: v for k, v in env.items() if v is not None}
    with pytest.raises(ConfigError, match=name):
        Settings.from_env(env)


def _write_secrets(d: Path) -> None:
    (d / "pluggy.json").write_text(json.dumps({"clientId": "cid", "clientSecret": "cs"}))
    (d / "google-oauth.json").write_text(json.dumps({"web": {"client_id": "gid", "client_secret": "gs"}}))
    (d / "db.key").write_text("ab" * 32 + "\n")
    (d / "session.key").write_text("k" * 64)


def test_load_secrets(tmp_path):
    _write_secrets(tmp_path)
    sec = load_secrets(tmp_path)
    assert (sec.pluggy_client_id, sec.pluggy_client_secret) == ("cid", "cs")
    assert (sec.google_client_id, sec.google_client_secret) == ("gid", "gs")
    assert sec.db_key == "ab" * 32
    assert sec.session_key == "k" * 64


@pytest.mark.parametrize("name", ["pluggy.json", "google-oauth.json", "db.key", "session.key"])
def test_missing_secret_file_raises(tmp_path, name):
    _write_secrets(tmp_path)
    (tmp_path / name).unlink()
    with pytest.raises(ConfigError, match=name):
        load_secrets(tmp_path)


@pytest.mark.parametrize("bad", ["ab" * 31, "zz" * 32, "ab" * 33])
def test_bad_db_key_raises(tmp_path, bad):
    _write_secrets(tmp_path)
    (tmp_path / "db.key").write_text(bad)
    with pytest.raises(ConfigError, match="db.key"):
        load_secrets(tmp_path)
