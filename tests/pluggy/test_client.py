import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from cashbook_mcp.pluggy.client import PluggyClient, PluggyError

FIX = Path(__file__).parent.parent / "fixtures" / "pluggy"


def load(name):
    return json.loads((FIX / name).read_text())


class Env:
    """Fake clock/sleep plus a request-recording transport."""

    def __init__(self, handler):
        self.now = 0.0
        self.sleeps: list[float] = []
        self.requests: list[httpx.Request] = []
        self.handler = handler

    def clock(self):
        return self.now

    async def sleep(self, s):
        self.sleeps.append(s)

    def _handle(self, request):
        self.requests.append(request)
        if request.url.path == "/auth":
            return httpx.Response(200, json={"apiKey": f"key-{self.auths}"})
        return self.handler(request)

    @property
    def auths(self):
        return sum(1 for r in self.requests if r.url.path == "/auth")

    def client(self, **kw):
        return PluggyClient(
            "cid",
            "secret",
            transport=httpx.MockTransport(self._handle),
            clock=self.clock,
            sleep=self.sleep,
            **kw,
        )


def ok(request):
    return httpx.Response(200, json=load("item.json"))


async def test_auth_cached_and_renewed():
    env = Env(ok)
    c = env.client()
    await c.get_item("item-1")
    await c.get_item("item-1")
    assert env.auths == 1
    assert env.requests[0].method == "POST"
    assert json.loads(env.requests[0].content) == {"clientId": "cid", "clientSecret": "secret"}
    assert env.requests[1].headers["X-API-KEY"] == "key-1"
    env.now += 111 * 60
    await c.get_item("item-1")
    assert env.auths == 2
    assert env.requests[-1].headers["X-API-KEY"] == "key-2"
    await c.aclose()


async def test_401_renews_once():
    calls = []

    def handler(request):
        calls.append(request.headers["X-API-KEY"])
        if len(calls) == 1:
            return httpx.Response(401, json={"message": "expired"})
        return httpx.Response(200, json=load("item.json"))

    env = Env(handler)
    c = env.client()
    item = await c.get_item("item-1")
    assert item["connector"]["name"] == "MeuPluggy"
    assert env.auths == 2
    assert calls == ["key-1", "key-2"]


async def test_401_twice_raises():
    env = Env(lambda r: httpx.Response(401, json={"message": "no"}))
    with pytest.raises(PluggyError) as ei:
        await env.client().get_item("item-1")
    assert ei.value.status == 401
    assert env.auths == 2


async def test_v2_cursor_pagination():
    def handler(request):
        if "after" in request.url.params:
            assert request.url.path == "/v2/transactions"
            assert request.url.params["after"] == "cursor-1"
            return httpx.Response(200, json=load("transactions_page2.json"))
        return httpx.Response(200, json=load("transactions_page1.json"))

    env = Env(handler)
    rows = await env.client().list_transactions("acc-bank-1", date(2026, 10, 1), date(2026, 11, 5))
    assert [r["id"] for r in rows] == ["tx-bank-1", "tx-bank-2", "tx-bank-3", "tx-card-1", "tx-card-2", "tx-card-3"]
    first = env.requests[1]
    assert first.url.path == "/v2/transactions"
    assert first.url.params["accountId"] == "acc-bank-1"
    assert first.url.params["dateFrom"] == "2026-10-01"
    assert first.url.params["dateTo"] == "2026-11-05"
    assert len(env.requests) == 3


async def test_page_pagination():
    def handler(request):
        page = int(request.url.params["page"])
        assert request.url.params["pageSize"] == "500"
        assert request.url.params["accountId"] == "acc-credit-1"
        bills = load("bills.json")["results"]
        return httpx.Response(
            200, json={"total": 2, "totalPages": 2, "page": page, "results": [bills[page - 1]]}
        )

    env = Env(handler)
    rows = await env.client().list_bills("acc-credit-1")
    assert [r["id"] for r in rows] == ["bill-1", "bill-0"]


async def test_response_without_total_pages_is_single_page():
    env = Env(lambda r: httpx.Response(200, json={"results": load("accounts.json")["results"]}))
    rows = await env.client().list_accounts("item-1")
    assert len(rows) == 2
    assert len(env.requests) == 2  # auth + one page


async def test_429_honors_retry_after():
    n = []

    def handler(request):
        n.append(1)
        if len(n) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=load("item.json"))

    env = Env(handler)
    await env.client().get_item("item-1")
    assert env.sleeps == [7]


async def test_429_capped_and_limited():
    env = Env(lambda r: httpx.Response(429, headers={"Retry-After": "500"}))
    with pytest.raises(PluggyError) as ei:
        await env.client().get_item("item-1")
    assert ei.value.status == 429
    assert env.sleeps == [60, 60, 60]


async def test_5xx_retries_then_raises():
    env = Env(lambda r: httpx.Response(503, json={"message": "down"}))
    with pytest.raises(PluggyError) as ei:
        await env.client().get_item("item-1")
    assert ei.value.status == 503
    assert env.sleeps == [1, 2]


async def test_transport_error_retries():
    n = []

    def handler(request):
        n.append(1)
        if len(n) < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=load("item.json"))

    env = Env(handler)
    await env.client().get_item("item-1")
    assert env.sleeps == [1, 2]


async def test_create_connect_token():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/connect_token"
        assert json.loads(request.content) == {"options": {"itemId": "item-1"}}
        return httpx.Response(200, json={"accessToken": "tok"})

    env = Env(handler)
    assert await env.client().create_connect_token({"itemId": "item-1"}) == "tok"


async def test_check_credentials():
    env = Env(ok)
    await env.client().check_credentials()
    assert env.auths == 1


async def test_auth_failure_raises():
    def handler(request):
        return httpx.Response(403, json={"message": "bad creds", "code": 403})

    env = Env(handler)
    env.requests = []
    c = PluggyClient(
        "cid",
        "secret",
        transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"message": "bad creds"})),
        clock=env.clock,
        sleep=env.sleep,
    )
    with pytest.raises(PluggyError) as ei:
        await c.check_credentials()
    assert ei.value.status == 403


async def test_never_calls_deprecated_or_identity():
    def handler(request):
        p = request.url.path
        if p == "/v2/transactions":
            return httpx.Response(200, json={"results": [], "next": None})
        if p.startswith("/items/"):
            return httpx.Response(200, json=load("item.json"))
        return httpx.Response(200, json={"total": 0, "totalPages": 1, "page": 1, "results": []})

    env = Env(handler)
    c = env.client()
    await c.get_item("i")
    await c.list_accounts("i")
    await c.list_transactions("a", date(2026, 1, 1), date(2026, 2, 1))
    await c.list_bills("a")
    await c.list_investments("i")
    await c.list_investment_transactions("inv")
    await c.list_loans("i")
    paths = [r.url.path for r in env.requests]
    assert paths
    assert not any(p.startswith(("/transactions", "/identity")) for p in paths)
    assert "/investments/inv/transactions" in paths
    assert all(r.method == "GET" for r in env.requests if r.url.path != "/auth")
