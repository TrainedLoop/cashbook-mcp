from datetime import time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastmcp.server.auth.providers.google import GoogleProvider

from cashbook_mcp.auth import AllowlistGoogleProvider, build_auth
from cashbook_mcp.config import DEFAULT_REDIRECT_URIS, Secrets, Settings


def settings() -> Settings:
    return Settings(
        public_url="https://owner.example.ts.net",
        allowed_emails=frozenset({"me@example.com"}),
        allowed_redirect_uris=DEFAULT_REDIRECT_URIS,
        secrets_dir=Path("/secrets"),
        db_path=Path("/data/cashbook.duckdb"),
        port=8743,
        refresh_at=time(6, 0),
        tz=ZoneInfo("America/Sao_Paulo"),
        history_months=12,
        log_level="INFO",
    )


SECRETS = Secrets("pid", "psecret", "gid.apps.googleusercontent.com", "gsecret", "ab" * 32, "k" * 64)


def test_build_auth_uses_settings_and_secrets():
    auth = build_auth(settings(), SECRETS)
    assert isinstance(auth, AllowlistGoogleProvider)
    assert auth.allowed_emails == frozenset({"me@example.com"})
    assert str(auth.base_url).rstrip("/") == "https://owner.example.ts.net"


@pytest.mark.parametrize(
    ("claims", "ok"),
    [
        ({"email": "me@example.com", "email_verified": True}, True),
        ({"email": "ME@example.com", "email_verified": "true"}, True),
        ({"email": "me@example.com", "email_verified": False}, False),
        ({"email": "other@example.com", "email_verified": True}, False),
        ({}, False),
        (None, False),
    ],
)
async def test_allowlist(monkeypatch, claims, ok):
    async def fake_super(self, token):
        return SimpleNamespace(claims=claims) if claims is not None else None

    monkeypatch.setattr(GoogleProvider, "load_access_token", fake_super)
    prov = AllowlistGoogleProvider.__new__(AllowlistGoogleProvider)
    prov.allowed_emails = frozenset({"me@example.com"})
    assert (await prov.load_access_token("t") is not None) is ok


def test_build_auth_creates_oauth_state_dir_under_fastmcp_home(tmp_path, monkeypatch):
    import fastmcp

    home = tmp_path / "data" / "fastmcp"
    monkeypatch.setattr(fastmcp.settings, "home", home)
    build_auth(settings(), SECRETS)
    assert (home / "oauth-proxy").is_dir()


def test_google_login_asks_for_a_refresh_token():
    # Without prompt=consent Google issues a refresh token only on the first
    # authorization of the client, so logins would die when the 1 h Google
    # access token expires.
    params = build_auth(settings(), SECRETS)._extra_authorize_params
    assert params["access_type"] == "offline"
    assert set(params["prompt"].split()) == {"consent", "select_account"}
