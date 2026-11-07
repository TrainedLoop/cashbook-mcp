"""Admin web test support: fake identity, syncer, scheduler and Pluggy client, a populated store
and a TestClient wrapper with sign-in helpers. Fixtures live in conftest.py."""

from __future__ import annotations

import threading
import time as time_module
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from starlette.testclient import TestClient

from cashbook_mcp.admin_web.api import AdminContext
from cashbook_mcp.admin_web.identity import LoginError, OwnerLogin
from cashbook_mcp.admin_web.session import FLOW_COOKIE, SESSION_COOKIE
from cashbook_mcp.config import DEFAULT_REDIRECT_URIS, Settings
from cashbook_mcp.models import Connection
from cashbook_mcp.store.db import Database
from cashbook_mcp.store.sync import DisconnectImpact, ItemPreview, SyncResult

PUBLIC = "https://owner.example.ts.net"
CLIENT_BASE = "http://testserver:8743"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
OWNER = "me@example.com"
KEY = "ab" * 32
SESSION_KEY = "s" * 64
TZ = ZoneInfo("America/Sao_Paulo")
SEEN = datetime(2026, 1, 1)
INDEX_HTML = '<!doctype html><title>cashbook admin</title><script type="module" src="/admin/assets/app-abc123.js"></script>'
ASSET_JS = "console.log('app');"


class FakeIdentity:
    """Answers like Google: each code works once, then the exchange fails."""

    def __init__(self) -> None:
        self.email = OWNER
        self.auth_time: float | None = None
        self.fail: str | None = None
        self.fail_reason = "token-exchange"
        self.calls: list[dict] = []
        self._used: set[str] = set()

    def authorization_url(self, redirect_uri, state, nonce, code_challenge, *, max_age=None):
        params = {"redirect_uri": redirect_uri, "state": state, "nonce": nonce, "code_challenge": code_challenge}
        if max_age is not None:
            params["max_age"] = str(max_age)
        return f"{GOOGLE_AUTH}?{urlencode(params)}"

    def complete(self, code, redirect_uri, code_verifier, nonce):
        self.calls.append({"code": code, "redirect_uri": redirect_uri, "code_verifier": code_verifier, "nonce": nonce})
        if self.fail:
            raise LoginError(self.fail, self.fail_reason)
        if code in self._used:
            raise LoginError("Google sign-in failed", "token-exchange")
        self._used.add(code)
        return OwnerLogin(self.email, self.auth_time)


def _connection(alias: str, item_id: str) -> Connection:
    return Connection(alias, item_id, "Bank", SEEN, "UPDATED", None, None, None, None, None, None, None)


class FakeSyncer:
    """Records calls. `errors[method]` raises; `release` (cleared) holds loads in progress."""

    def __init__(self) -> None:
        self.aliases: dict[str, str] = {"main": "item-1", "second": "item-2"}
        self.errors: dict[str, Exception] = {}
        self.calls: list[tuple] = []
        self.release = threading.Event()
        self.release.set()
        self.loads_started = threading.Event()
        self.preview_result = ItemPreview(
            item_id="item-9", bank="Banco Teste", status="UPDATED",
            accounts=[{"name": "Checking", "type": "BANK", "number": "****2345"}],
        )  # fmt: skip

    def _maybe_fail(self, name: str) -> None:
        if name in self.errors:
            raise self.errors[name]

    def connections(self) -> list[Connection]:
        self._maybe_fail("connections")
        return [_connection(a, i) for a, i in sorted(self.aliases.items())]

    async def preview(self, item_id: str) -> ItemPreview:
        self.calls.append(("preview", item_id))
        self._maybe_fail("preview")
        return self.preview_result

    async def add_connection(self, alias: str, item_id: str, *, load: bool = True) -> Connection:
        self.calls.append(("add_connection", alias, item_id, load))
        self._maybe_fail("add_connection")
        self.aliases[alias] = item_id
        if load:
            await self.load(alias)
        return _connection(alias, item_id)

    async def load(self, alias: str) -> SyncResult:
        import asyncio

        self.calls.append(("load", alias))
        self.loads_started.set()
        while not self.release.is_set():
            await asyncio.sleep(0.005)
        self._maybe_fail("load")
        return SyncResult(alias, "ok")

    async def disconnect(self, alias: str) -> None:
        self.calls.append(("disconnect", alias))
        self._maybe_fail("disconnect")
        self.aliases.pop(alias, None)

    def disconnect_impact(self, alias: str) -> DisconnectImpact:
        self.calls.append(("disconnect_impact", alias))
        self._maybe_fail("disconnect_impact")
        return DisconnectImpact(alias, 1240, 300, date(2025, 3, 2), date(2025, 11, 6))


class FakePluggy:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None

    async def create_connect_token(self, options: dict) -> str:
        self.calls.append(options)
        if self.error is not None:
            raise self.error
        return "connect-token-1"


class FakeScheduler:
    def __init__(self) -> None:
        self.last_full_refresh: datetime | None = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
        self.next_full_refresh = datetime(2026, 11, 7, 6, 0, tzinfo=TZ)


@dataclass
class Clock:
    t: float = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


def settings(tmp_path: Path) -> Settings:
    return Settings(
        public_url=PUBLIC,
        allowed_emails=frozenset({OWNER}),
        allowed_redirect_uris=DEFAULT_REDIRECT_URIS,
        secrets_dir=tmp_path,
        db_path=tmp_path / "data.duckdb",
        port=8743,
        refresh_at=time(6, 0),
        tz=TZ,
        history_months=12,
        log_level="INFO",
    )


def populate(db: Database) -> None:
    with db.write() as cur:
        cur.execute(
            "INSERT INTO connections (alias, item_id, connector_name, created_at, status, error_code, "
            "last_synced_at, last_sync_error) VALUES "
            "('main', 'item-1', 'Bank A', ?, 'UPDATED', NULL, ?, NULL), "
            "('second', 'item-2', 'Bank B', ?, 'LOGIN_ERROR', NULL, NULL, NULL), "
            "('third', 'item-3', 'Bank C', ?, 'UPDATED', NULL, NULL, 'Pluggy said: raw secret body'), "
            "('fourth', 'item-4', 'Bank D', ?, 'UPDATED', 'USER_AUTHORIZATION_REVOKED', NULL, NULL)",
            [SEEN, datetime(2026, 11, 6, 9, 0), SEEN, SEEN, SEEN],
        )
        cur.execute(
            "INSERT INTO accounts (id, alias, item_id, type, subtype, name, balance, currency, "
            "first_seen_at, last_seen_at) VALUES "
            "('acc-main', 'main', 'item-1', 'BANK', 'CHECKING_ACCOUNT', 'Checking', 1200.00, 'BRL', ?, ?)",
            [SEEN, SEEN],
        )
        cur.execute(
            "INSERT INTO transactions (id, alias, account_id, date, amount, currency, description, kind, "
            "kind_reason, text_suspicious, first_seen_at, last_seen_at) VALUES "
            "('t1', 'main', 'acc-main', DATE '2026-10-01', -12.50, 'BRL', 'Coffee', 'purchase', 'test', false, ?, ?)",
            [SEEN, SEEN],
        )


@dataclass
class Env:
    ctx: AdminContext
    client: TestClient
    identity: FakeIdentity
    syncer: FakeSyncer
    pluggy: FakePluggy
    clock: Clock
    static: Path
    logins: int = 0
    extra: dict = field(default_factory=dict)

    # -- sign-in -------------------------------------------------------------

    def start_login(self, next_: str | None = None):
        """GET /admin/auth/login; returns (response, flow cookie value, query of the Google URL)."""
        url = "/admin/auth/login" + (f"?{urlencode({'next': next_})}" if next_ is not None else "")
        resp = self.client.get(url, follow_redirects=False)
        flow = cookie_value(resp, FLOW_COOKIE)
        query = parse_qs(urlsplit(resp.headers.get("location", "")).query)
        return resp, flow, query

    def callback(self, flow: str | None, state: str, code: str = "code-1", **extra):
        self.client.cookies.clear()
        if flow is not None:
            self.client.cookies.set(FLOW_COOKIE, flow)
        params = {"state": state, "code": code, **extra}
        return self.client.get(f"/admin/auth/callback?{urlencode(params)}", follow_redirects=False)

    def login(self, email: str = OWNER, auth_time: float | None = None) -> str:
        """Run the whole sign-in flow and leave only the session cookie on the client. Returns it."""
        self.identity.email = email
        self.identity.auth_time = auth_time
        self.logins += 1
        _, flow, query = self.start_login()
        resp = self.callback(flow, query["state"][0], code=f"login-code-{self.logins}")
        token = cookie_value(resp, SESSION_COOKIE)
        assert token, resp.text
        self.client.cookies.clear()
        self.client.cookies.set(SESSION_COOKIE, token)
        return token

    def csrf(self) -> str:
        return self.client.get("/admin/api/session").json()["csrf"]

    # -- API calls -----------------------------------------------------------

    def send(self, method: str, path: str, body=None, *, csrf: str | None | bool = True,
             origin: str | None = PUBLIC, referer: str | None = None):  # fmt: skip
        headers = {}
        if csrf is True:
            csrf = self.csrf()
        if csrf:
            headers["X-CSRF-Token"] = csrf
        if origin is not None:
            headers["Origin"] = origin
        if referer is not None:
            headers["Referer"] = referer
        kw = {"json": body} if body is not None else {}
        return self.client.request(method, path, headers=headers, follow_redirects=False, **kw)


def cookie_header(resp, name: str) -> str | None:
    for h in resp.headers.get_list("set-cookie"):
        if h.split("=", 1)[0].strip() == name:
            return h
    return None


def cookie_value(resp, name: str) -> str | None:
    h = cookie_header(resp, name)
    if h is None:
        return None
    return h.split("=", 1)[1].split(";", 1)[0].strip().strip('"')


def attrs(header: str) -> dict[str, str]:
    out = {}
    for p in [p.strip() for p in header.split(";")[1:]]:
        k, _, v = p.partition("=")
        out[k.lower()] = v
    return out


def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time_module.monotonic() + timeout
    while not predicate():
        assert time_module.monotonic() < deadline, "condition not met in time"
        time_module.sleep(0.005)
