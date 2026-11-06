"""Async client for the Pluggy API (read-only apart from /auth and /connect_token)."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import httpx

KEY_TTL_SECONDS = 110 * 60
PAGE_SIZE = 500
MAX_429_RETRIES = 3
MAX_RETRY_AFTER = 60
BACKOFFS = (1, 2)


class PluggyError(Exception):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def _error_from(resp: httpx.Response) -> PluggyError:
    message, code = f"Pluggy returned HTTP {resp.status_code}", None
    try:
        body = resp.json()
        if isinstance(body, dict):
            message = str(body.get("message") or message)
            if body.get("code") is not None:
                code = str(body["code"])
    except ValueError:
        pass
    return PluggyError(message, status=resp.status_code, code=code)


class PluggyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = "https://api.pluggy.ai",
        transport: httpx.AsyncBaseTransport | None = None,
        max_concurrency: int = 4,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=20.0)
        self._sem = asyncio.Semaphore(max_concurrency)
        self._clock = clock
        self._sleep = sleep
        self._api_key: str | None = None
        self._key_at = 0.0
        self._auth_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- transport ---------------------------------------------------------

    async def _send(self, method: str, url: str, **kw: Any) -> httpx.Response:
        """One logical request: bounded concurrency, 429/5xx/transport retries."""
        rate_limited = 0
        failures = 0
        while True:
            try:
                async with self._sem:
                    resp = await self._http.request(method, url, **kw)
            except httpx.TransportError as exc:
                if failures >= len(BACKOFFS):
                    raise PluggyError(f"Pluggy request failed: {type(exc).__name__}") from exc
                await self._sleep(BACKOFFS[failures])
                failures += 1
                continue
            if resp.status_code == 429 and rate_limited < MAX_429_RETRIES:
                try:
                    wait = int(resp.headers.get("Retry-After") or 60)
                except ValueError:
                    wait = 60
                await self._sleep(min(wait, MAX_RETRY_AFTER))
                rate_limited += 1
                continue
            if resp.status_code >= 500 and failures < len(BACKOFFS):
                await self._sleep(BACKOFFS[failures])
                failures += 1
                continue
            return resp

    async def _auth(self, *, force_if: str | None = None) -> str:
        """Return a cached key, renewing when stale or when `force_if` was rejected."""
        async with self._auth_lock:
            fresh = self._api_key is not None and self._clock() - self._key_at < KEY_TTL_SECONDS
            if fresh and self._api_key != force_if:
                return self._api_key  # type: ignore[return-value]
            resp = await self._send(
                "POST",
                "/auth",
                json={"clientId": self._client_id, "clientSecret": self._client_secret},
            )
            if resp.status_code != 200:
                raise _error_from(resp)
            self._api_key = resp.json()["apiKey"]
            self._key_at = self._clock()
            return self._api_key  # type: ignore[return-value]

    async def _request(
        self, method: str, url: str, *, params: dict | None = None, json: Any = None
    ) -> Any:
        key = await self._auth()
        for attempt in range(2):
            resp = await self._send(
                method, url, params=params, json=json, headers={"X-API-KEY": key}
            )
            if resp.status_code == 401 and attempt == 0:
                key = await self._auth(force_if=key)
                continue
            break
        if resp.status_code >= 400:
            raise _error_from(resp)
        return resp.json()

    async def _paged(self, path: str, params: dict) -> list[dict]:
        rows: list[dict] = []
        page = 1
        while True:
            body = await self._request("GET", path, params={**params, "page": page, "pageSize": PAGE_SIZE})
            rows.extend(body.get("results", []))
            if page >= int(body.get("totalPages") or 1):
                return rows
            page += 1

    # -- public API --------------------------------------------------------

    async def check_credentials(self) -> None:
        await self._auth()

    async def get_item(self, item_id: str) -> dict:
        return await self._request("GET", f"/items/{item_id}")

    async def list_accounts(self, item_id: str) -> list[dict]:
        return await self._paged("/accounts", {"itemId": item_id})

    async def list_transactions(self, account_id: str, date_from: date, date_to: date) -> list[dict]:
        rows: list[dict] = []
        url: str | None = "/v2/transactions"
        params: dict | None = {
            "accountId": account_id,
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
        }
        while url:
            body = await self._request("GET", url, params=params)
            rows.extend(body.get("results", []))
            url, params = body.get("next"), None  # `next` already carries its query
        return rows

    async def list_bills(self, account_id: str) -> list[dict]:
        return await self._paged("/bills", {"accountId": account_id})

    async def list_investments(self, item_id: str) -> list[dict]:
        return await self._paged("/investments", {"itemId": item_id})

    async def list_investment_transactions(self, investment_id: str) -> list[dict]:
        return await self._paged(f"/investments/{investment_id}/transactions", {})

    async def list_loans(self, item_id: str) -> list[dict]:
        return await self._paged("/loans", {"itemId": item_id})

    async def create_connect_token(self, options: dict) -> str:
        body = await self._request("POST", "/connect_token", json={"options": options})
        return body["accessToken"]
