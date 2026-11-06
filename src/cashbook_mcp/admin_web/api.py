"""The /admin routes: owner sign-in and sign-out, the JSON API and the SPA. `admin_routes(ctx)` is the
single entry point.

Sign-in is a Google OIDC round trip (ported from mailhub). Google's return is a cross-site
navigation, so it does not carry the SameSite=Strict session cookie: the callback works only from
the SameSite=Lax flow cookie (signed, single use) and ends on a 200 page that moves on by
<meta http-equiv="refresh">, a same-site navigation that does carry the session cookie.

Every API call needs the session cookie (else 401). Mutations also need the X-CSRF-Token header
and a same-origin Origin (or Referer) (else 403 "csrf"); disconnecting also needs a recent Google
sign-in (else 403 "reauth_required"). Responses never carry secrets or Pluggy's error text.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import json
import logging
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from html import escape
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import anyio
import anyio.to_thread
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Match, Route, request_response

from cashbook_mcp.admin_web import spa
from cashbook_mcp.admin_web.headers import SECURITY_HEADERS
from cashbook_mcp.admin_web.identity import LoginError, OwnerIdentity
from cashbook_mcp.admin_web.session import (
    FLOW_COOKIE,
    FLOW_SECONDS,
    REAUTH_SECONDS,
    SESSION_COOKIE,
    SESSION_SECONDS,
    Flow,
    Session,
    Signer,
    check_csrf,
    csrf_token,
    decode_flow,
    decode_session,
    derive_key,
    encode_flow,
    encode_session,
    is_fresh,
    new_pkce,
    random_token,
    same_origin,
)
from cashbook_mcp.analysis import listing
from cashbook_mcp.analysis.filters import jsonable
from cashbook_mcp.config import Secrets, Settings
from cashbook_mcp.pluggy.client import PluggyClient, PluggyError
from cashbook_mcp.presenter import REVOKED
from cashbook_mcp.store.db import Database
from cashbook_mcp.store.scheduler import Scheduler
from cashbook_mcp.store.sync import (
    ALIAS_RE,
    MEUPLUGGY_CONNECTOR_ID,
    Syncer,
    ValidationError,
)

log = logging.getLogger("cashbook-mcp")

LOGIN_PATH = "/admin/auth/login"
LOGIN_CALLBACK_PATH = "/admin/auth/callback"
LOGOUT_PATH = "/admin/auth/logout"
USED_STATES_CAP = 1000  # consumed flow states remembered at once; beyond it, the oldest are evicted
# The sign-in code exchange runs on its own small thread limiter, not the default pool, so anonymous
# callback spam cannot starve the MCP tools.
ADMIN_THREADS = 4
ADMIN_LIMITER = anyio.CapacityLimiter(ADMIN_THREADS)
MAX_BODY_BYTES = 16 * 1024

EXPIRED_TEXT = "This sign-in link expired, start again"
NOT_ALLOWED_TEXT = "This Google account is not allowed"

_SAFE_PATH = re.compile(r"^/admin(?:[/?][A-Za-z0-9/_\-.~%?=&]*)?$")
_NO_RETURN = frozenset({LOGIN_PATH, LOGIN_CALLBACK_PATH, LOGOUT_PATH})
# Pluggy item ids are UUIDs; anything else never reaches Pluggy (it is part of a request path there).
_ITEM_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")

Endpoint = Callable[[Request], Awaitable[Response]]


@dataclass
class AdminContext:
    """Everything the admin routes need, built once at startup."""

    settings: Settings
    secrets: Secrets = field(repr=False)
    syncer: Syncer
    scheduler: Scheduler
    db: Database
    pluggy: PluggyClient
    identity: OwnerIdentity
    now: Callable[[], float] = time.time
    key: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.key = derive_key(self.secrets.session_key)

    @property
    def public_url(self) -> str:
        return self.settings.public_url

    @property
    def allowed_emails(self) -> frozenset[str]:
        return frozenset(e.strip().lower() for e in self.settings.allowed_emails)


def safe_path(value: object, default: str = "/admin") -> str:
    """`value` if it is a plain /admin path (no scheme, host, backslash, '//' or '..'), else default."""
    if not isinstance(value, str) or not _SAFE_PATH.match(value):
        return default
    path = value.split("?", 1)[0]
    if "//" in value or ".." in path.split("/"):
        return default
    return value


# --- responses ------------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """Strict-JSON copy: Decimal -> str, dates ISO."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return jsonable(value)


def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(_plain(data), status_code=status)


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def _page(title: str, text: str, link_text: str = "Back", to: str = "/admin", status: int = 200) -> HTMLResponse:
    body = (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{escape(title)}</title>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head><body>'
        f'<h1>{escape(title)}</h1><p>{escape(text)}</p><p><a href="{escape(to)}">{escape(link_text)}</a></p>'
        "</body></html>"
    )
    return HTMLResponse(body, status_code=status)


def done_page(message: str, to: str = "/admin") -> HTMLResponse:
    """200 page that moves on by <meta http-equiv="refresh"> (never a 302 back into /admin)."""
    to = escape(safe_path(to))
    body = (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{escape(message)}</title>'
        f'<meta http-equiv="refresh" content="0;url={to}"></head><body>'
        f'<h1>{escape(message)}</h1><p><a href="{to}">Continue</a></p></body></html>'
    )
    return HTMLResponse(body)


def _expired() -> HTMLResponse:
    return _page("Sign-in expired", EXPIRED_TEXT, "Sign in", LOGIN_PATH, 400)


def _is_api(path: str) -> bool:
    return path == "/admin/api" or path.startswith("/admin/api/")


def _internal_error(path: str) -> Response:
    if _is_api(path):
        return _error("internal", 500)
    return _page("Error", "Something went wrong. Try again.", status=500)


# --- cookies --------------------------------------------------------------------------------


def _set_flow_cookie(resp: Response, ctx: AdminContext, flow: Flow) -> None:
    resp.set_cookie(
        FLOW_COOKIE,
        encode_flow(Signer(ctx.key), flow),
        max_age=FLOW_SECONDS,
        path="/admin",
        secure=True,
        httponly=True,
        samesite="Lax",
    )


def _clear_flow_cookie(resp: Response) -> None:
    resp.delete_cookie(FLOW_COOKIE, path="/admin", secure=True, httponly=True, samesite="Lax")


def _set_session_cookie(resp: Response, ctx: AdminContext, s: Session, now: float) -> None:
    remaining = SESSION_SECONDS - (now - s.login_at)
    resp.set_cookie(
        SESSION_COOKIE,
        encode_session(Signer(ctx.key), s),
        max_age=max(1, math.floor(remaining)),
        path="/admin",
        secure=True,
        httponly=True,
        samesite="Strict",
    )


def _clear_session_cookie(resp: Response) -> None:
    resp.delete_cookie(SESSION_COOKIE, path="/admin", secure=True, httponly=True, samesite="Strict")


# --- session and request checks -------------------------------------------------------------


def _current_session(request: Request, ctx: AdminContext) -> Session | None:
    return decode_session(Signer(ctx.key), request.cookies.get(SESSION_COOKIE), ctx.now(), ctx.allowed_emails)


def require_session(request: Request, ctx: AdminContext) -> Session | Response:
    """The signed-in owner's session, or 401 {"error": "signed_out"}."""
    s = _current_session(request, ctx)
    if s is None:
        return _error("signed_out", 401)
    return s


def checked_mutation(request: Request, ctx: AdminContext, *, fresh: bool = False) -> Session | Response:
    """Gate for a state-changing call, before the body is read: the session (else 401), then the
    same origin and the CSRF header (else 403 "csrf"), then, with `fresh`, a recent Google sign-in
    (else 403 "reauth_required")."""
    s = require_session(request, ctx)
    if isinstance(s, Response):
        return s
    if not same_origin(ctx.public_url, request.headers.get("origin"), request.headers.get("referer")):
        return _error("csrf", 403)
    if not check_csrf(ctx.key, s, request.headers.get("x-csrf-token")):
        return _error("csrf", 403)
    if fresh and not is_fresh(s, ctx.now()):
        return _error("reauth_required", 403)
    return s


async def _json_body(request: Request) -> dict | Response:
    """The request body as a JSON object; 413 when too large, 400 when not a JSON object."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return _error("too_large", 413)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return _error("too_large", 413)
    try:
        data = json.loads(body)
    except ValueError:
        return _error("bad_request", 400)
    if not isinstance(data, dict):
        return _error("bad_request", 400)
    return data


def admin_endpoint(ctx: AdminContext, fn: Endpoint) -> Endpoint:
    """Wrap an admin handler: no stack traces (the error type only is logged), and the security
    headers on every response. Cache-Control stays when the handler set its own (the SPA)."""

    async def endpoint(request: Request) -> Response:
        try:
            resp = await fn(request)
        except Exception as exc:  # noqa: BLE001 - never show a stack trace; log only the type
            log.error("admin request failed path=%s error=%s", request.url.path, type(exc).__name__)
            resp = _internal_error(request.url.path)
        for name, value in SECURITY_HEADERS.items():
            if name == "Cache-Control" and "cache-control" in resp.headers:
                continue
            resp.headers[name] = value
        return resp

    return endpoint


class _AnyMethod:
    """An endpoint as a plain ASGI app. Starlette limits a function endpoint without `methods` to
    GET; an ASGI app with `methods=None` receives every method, so the fallback answers them."""

    def __init__(self, endpoint: Endpoint) -> None:
        self._app = request_response(endpoint)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self._app(scope, receive, send)


# --- single use of login flows --------------------------------------------------------------


class _UsedStates:
    """States of OAuth flows already consumed, so a replayed callback is refused even if the flow
    cookie comes back. Every entry lives FLOW_SECONDS, so insertion order is expiry order: pruning
    pops from the front until the first live entry. At most `cap` entries are kept."""

    def __init__(self, cap: int | None = None) -> None:
        self._cap = USED_STATES_CAP if cap is None else cap
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)

    def claim(self, state: str, now: float) -> bool:
        with self._lock:
            while self._seen:
                _, until = next(iter(self._seen.items()))
                if until > now:
                    break
                self._seen.popitem(last=False)
            if state in self._seen:
                return False
            # Full: forget the oldest rather than refuse, so nobody can lock the owner out by
            # filling the store. Forgetting is safe: Google's authorization codes work once, so a
            # replay of an evicted state still fails at the code exchange.
            while len(self._seen) >= self._cap:
                self._seen.popitem(last=False)
            self._seen[state] = now + FLOW_SECONDS
            return True


# --- background reloads ---------------------------------------------------------------------


class _Reloads:
    """Reloads started from the admin page, one at a time per alias. The tasks are kept here so they
    are not garbage-collected while running; a finished task removes itself."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def running(self, alias: str) -> bool:
        task = self._tasks.get(alias)
        return task is not None and not task.done()

    def start(self, alias: str, work: Callable[[], Awaitable[Any]]) -> bool:
        """Start `work` for `alias` in the background; False when one is already running."""
        if self.running(alias):
            return False
        task = asyncio.create_task(self._run(alias, work), name=f"admin-reload-{alias}")
        self._tasks[alias] = task

        def forget(done: asyncio.Task) -> None:
            if self._tasks.get(alias) is done:
                del self._tasks[alias]

        task.add_done_callback(forget)
        return True

    @staticmethod
    async def _run(alias: str, work: Callable[[], Awaitable[Any]]) -> None:
        try:
            result = await work()
        except Exception as exc:  # noqa: BLE001 - the text may quote Pluggy: log only the type
            log.error("admin reload failed alias=%s error=%s", alias, type(exc).__name__)
            return
        log.info("admin reload alias=%s outcome=%s", alias, getattr(result, "outcome", "?"))


def _badge(row: dict, syncing: bool) -> str:
    if syncing:
        return "syncing"
    if row.get("error_code") == REVOKED:
        return "consent revoked"
    if row.get("status") == "LOGIN_ERROR":
        return "login error"
    if row.get("status") in ("OUTDATED", "WAITING_USER_INPUT") or row.get("sync_failed"):
        return "outdated"
    return "ok"


def _app_version() -> str:
    try:
        return version("cashbook-mcp")
    except PackageNotFoundError:
        return "unknown"


# --- routes ---------------------------------------------------------------------------------


def admin_routes(ctx: AdminContext) -> list[Route]:
    login_states = _UsedStates()
    reloads = _Reloads()
    signer = Signer(ctx.key)

    async def aliases() -> set[str]:
        return {c.alias for c in await asyncio.to_thread(ctx.syncer.connections)}

    async def known_alias(request: Request) -> str | None:
        alias = request.path_params.get("alias", "")
        if not ALIAS_RE.fullmatch(alias) or alias not in await aliases():
            return None
        return alias

    # --- sign-in ----------------------------------------------------------------------------

    async def login(request: Request) -> Response:
        now = ctx.now()
        current = _current_session(request, ctx)
        verifier, challenge = new_pkce()
        flow = Flow(
            kind="login",
            state=random_token(),
            nonce=random_token(),
            code_verifier=verifier,
            created_at=now,
            return_to=_return_to(request.query_params.get("next")),
            sid=current.sid if current else None,
            email=current.email if current else None,
            login_at=current.login_at if current else None,
        )
        url = ctx.identity.authorization_url(
            _redirect_uri(ctx), flow.state, flow.nonce, challenge, max_age=REAUTH_SECONDS if current else None
        )
        resp = RedirectResponse(url, status_code=303)
        _set_flow_cookie(resp, ctx, flow)
        return resp

    async def auth_callback(request: Request) -> Response:
        """Whatever the outcome, a 500 included, the flow cookie is deleted."""
        try:
            resp = await finish_login(request)
        except Exception as exc:  # noqa: BLE001 - never show a stack trace; log only the type
            log.error("admin request failed path=%s error=%s", request.url.path, type(exc).__name__)
            resp = _internal_error(request.url.path)
        _clear_flow_cookie(resp)
        return resp

    async def finish_login(request: Request) -> Response:
        now = ctx.now()
        flow = decode_flow(signer, request.cookies.get(FLOW_COOKIE), now)
        state = request.query_params.get("state") or ""
        if flow is None or flow.kind != "login":
            return _expired()
        if not hmac.compare_digest(flow.state.encode("ascii"), state.encode("utf-8")):
            return _expired()
        if not login_states.claim(flow.state, now):
            return _expired()
        if request.query_params.get("error"):
            log.info("admin login cancelled")  # Google's error value is not logged: it is caller input
            return _page("Sign-in cancelled", "Google sign-in was cancelled.", "Sign in", LOGIN_PATH, 400)
        code = request.query_params.get("code")
        if not code:
            return _expired()
        try:
            owner = await anyio.to_thread.run_sync(
                partial(ctx.identity.complete, code, _redirect_uri(ctx), flow.code_verifier, flow.nonce),
                limiter=ADMIN_LIMITER,
            )
        except LoginError as exc:
            log.warning("admin login failed reason=%s", exc.reason)  # a fixed code; never the code or token
            return _page("Sign-in failed", str(exc), "Sign in", LOGIN_PATH, 400)
        email = owner.email.lower()
        if email not in ctx.allowed_emails:
            log.warning("admin login refused email=%s", email)
            return _page("Not allowed", NOT_ALLOWED_TEXT, status=403)

        reauth = (
            flow.sid is not None
            and flow.email is not None
            and flow.email.lower() == email
            and flow.login_at is not None
            and 0 <= now - flow.login_at < SESSION_SECONDS
        )
        auth_time = owner.auth_time
        has_auth_time = auth_time is not None
        if isinstance(auth_time, (int, float)) and math.isfinite(auth_time):
            auth_at = min(float(auth_time), now)
        else:
            auth_at = now
        if reauth and now - auth_at >= REAUTH_SECONDS:
            # Google ignored max_age; the completed round trip is the fallback proof (as when the
            # claim is absent), so the action does not send the owner around again forever.
            log.warning("admin reauth: google returned a stale auth_time")
            auth_at = now
        session = Session(
            email=email,
            sid=flow.sid if reauth and flow.sid else random_token(),
            login_at=flow.login_at if reauth and flow.login_at is not None else now,
            auth_at=auth_at,
            has_auth_time=has_auth_time,
        )
        log.info("admin login email=%s%s", email, " reauth" if reauth else "")
        resp = done_page("Signed in", flow.return_to)
        _set_session_cookie(resp, ctx, session, now)
        return resp

    async def logout(request: Request) -> Response:
        s = checked_mutation(request, ctx)
        if isinstance(s, Response):
            return s
        log.info("admin logout email=%s", s.email)
        resp = _json({"signed_out": True})
        _clear_session_cookie(resp)
        _clear_flow_cookie(resp)  # a sign-in started before signing out must not finish after it
        return resp

    # --- API: reads -------------------------------------------------------------------------

    async def session_info(request: Request) -> Response:
        s = require_session(request, ctx)
        if isinstance(s, Response):
            return s
        return _json({"email": s.email, "csrf": csrf_token(ctx.key, s), "reauth_fresh": is_fresh(s, ctx.now())})

    async def connections(request: Request) -> Response:
        s = require_session(request, ctx)
        if isinstance(s, Response):
            return s
        rows = await asyncio.to_thread(listing.list_connections, ctx.db)
        for row in rows:
            # The stored error text may quote Pluggy's response: the page gets a flag and the hint.
            row["sync_failed"] = row.pop("last_sync_error", None) is not None
            row["syncing"] = reloads.running(row["alias"])
            row["badge"] = _badge(row, row["syncing"])
        return _json({"connections": rows})

    async def disconnect_impact(request: Request) -> Response:
        s = require_session(request, ctx)
        if isinstance(s, Response):
            return s
        alias = await known_alias(request)
        if alias is None:
            return _error("not_found", 404)
        impact = await asyncio.to_thread(ctx.syncer.disconnect_impact, alias)
        return _json(dataclasses.asdict(impact))

    async def server_info(request: Request) -> Response:
        s = require_session(request, ctx)
        if isinstance(s, Response):
            return s

        def read() -> tuple[int, int]:
            return ctx.db.size_bytes(), ctx.db.schema_version()

        size, schema = await asyncio.to_thread(read)
        return _json({
            "last_full_refresh": ctx.scheduler.last_full_refresh,
            "next_full_refresh": ctx.scheduler.next_full_refresh,
            "db_size_bytes": size,
            "schema_version": schema,
            "app_version": _app_version(),
        })  # fmt: skip

    # --- API: mutations ---------------------------------------------------------------------

    async def connect_token(request: Request) -> Response:
        s = checked_mutation(request, ctx)
        if isinstance(s, Response):
            return s
        body = await _json_body(request)
        if isinstance(body, Response):
            return body
        alias = body.get("alias")
        if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias):
            return _error("alias_invalid", 422)
        if alias in await aliases():
            return _error("alias_taken", 422)
        try:
            token = await ctx.pluggy.create_connect_token({"avoidDuplicates": True})
        except PluggyError as exc:
            log.warning("admin connect token failed status=%s", exc.status)
            return _error("pluggy_unavailable", 502)
        log.info("admin connect token issued alias=%s", alias)
        return _json({"accessToken": token, "connectorIds": [MEUPLUGGY_CONNECTOR_ID]})

    async def preview(request: Request) -> Response:
        s = checked_mutation(request, ctx)
        if isinstance(s, Response):
            return s
        body = await _json_body(request)
        if isinstance(body, Response):
            return body
        if "item_id" not in body:
            return _error("bad_request", 400)
        item_id = body["item_id"]
        if not isinstance(item_id, str) or not _ITEM_ID.fullmatch(item_id):
            return _error("item_unknown", 422)
        try:
            result = await ctx.syncer.preview(item_id)
        except ValidationError as exc:
            return _error(exc.code, 422)
        except PluggyError as exc:
            log.warning("admin preview failed status=%s", exc.status)
            return _error("pluggy_unavailable", 502)
        return _json(dataclasses.asdict(result))

    async def add_connection(request: Request) -> Response:
        s = checked_mutation(request, ctx)
        if isinstance(s, Response):
            return s
        body = await _json_body(request)
        if isinstance(body, Response):
            return body
        if "alias" not in body or "item_id" not in body:
            return _error("bad_request", 400)
        alias, item_id = body["alias"], body["item_id"]
        if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias):
            return _error("alias_invalid", 422)
        if not isinstance(item_id, str) or not _ITEM_ID.fullmatch(item_id):
            return _error("item_unknown", 422)
        try:
            await ctx.syncer.add_connection(alias, item_id, load=False)
        except ValidationError as exc:
            return _error(exc.code, 422)
        except PluggyError as exc:
            log.warning("admin add connection failed status=%s", exc.status)
            return _error("pluggy_unavailable", 502)
        log.info("admin connection added alias=%s", alias)
        reloads.start(alias, partial(ctx.syncer.load, alias))
        return _json({"alias": alias, "started": True}, 201)

    async def reload_one(request: Request) -> Response:
        s = checked_mutation(request, ctx)
        if isinstance(s, Response):
            return s
        alias = await known_alias(request)
        if alias is None:
            return _error("not_found", 404)
        if not reloads.start(alias, partial(ctx.syncer.load, alias)):
            return _error("reload_in_progress", 409)
        return _json({"started": True}, 202)

    async def reload_all(request: Request) -> Response:
        s = checked_mutation(request, ctx)
        if isinstance(s, Response):
            return s
        known = sorted(await aliases())
        started = [a for a in known if reloads.start(a, partial(ctx.syncer.load, a))]
        if known and not started:
            return _error("reload_in_progress", 409)
        return _json({"started": True, "aliases": started}, 202)

    async def disconnect(request: Request) -> Response:
        s = checked_mutation(request, ctx, fresh=True)
        if isinstance(s, Response):
            return s
        alias = await known_alias(request)
        if alias is None:
            return _error("not_found", 404)
        await ctx.syncer.disconnect(alias)
        log.info("admin disconnect alias=%s email=%s", alias, s.email)
        return _json({"disconnected": alias})

    # --- table ------------------------------------------------------------------------------

    def route(path: str, fn: Endpoint, method: str) -> Route:
        return Route(path, admin_endpoint(ctx, fn), methods=[method])

    explicit = [
        route(LOGIN_PATH, login, "GET"),
        route(LOGIN_CALLBACK_PATH, auth_callback, "GET"),
        route(LOGOUT_PATH, logout, "POST"),
        route("/admin/api/session", session_info, "GET"),
        route("/admin/api/connections", connections, "GET"),
        route("/admin/api/connections", add_connection, "POST"),
        route("/admin/api/connections/preview", preview, "POST"),
        route("/admin/api/connect-token", connect_token, "POST"),
        route("/admin/api/reload", reload_all, "POST"),
        route("/admin/api/connections/{alias}/reload", reload_one, "POST"),
        route("/admin/api/connections/{alias}/disconnect-impact", disconnect_impact, "GET"),
        route("/admin/api/connections/{alias}", disconnect, "DELETE"),
        route("/admin/api/server", server_info, "GET"),
        route("/admin", spa.index, "GET"),
        route("/admin/assets/{file:path}", spa.asset, "GET"),
    ]

    async def fallback(request: Request) -> Response:
        """Any other /admin path or method: 405 where a route has the path, 404 under api/ and
        auth/, else the SPA's index.html for GET."""
        path = request.scope["path"]
        if any(r.matches(request.scope)[0] == Match.PARTIAL for r in explicit):
            if _is_api(path):
                return _error("method_not_allowed", 405)
            return _page("Not allowed", "Method not allowed.", status=405)
        if _is_api(path):
            return _error("not_found", 404)
        if path == "/admin/auth" or path.startswith("/admin/auth/"):
            return _page("Not found", "There is no such admin page.", status=404)
        if request.method in ("GET", "HEAD"):
            return await spa.index(request)
        return _page("Not allowed", "Method not allowed.", status=405)

    any_method = _AnyMethod(admin_endpoint(ctx, fallback))
    return [*explicit, Route("/admin", any_method, methods=None), Route("/admin/{rest:path}", any_method, methods=None)]


def _return_to(value: object) -> str:
    """Where a finished login goes: a safe /admin page that does not start another round trip."""
    path = safe_path(value)
    if path.split("?", 1)[0] in _NO_RETURN:
        return "/admin"
    return path


def _redirect_uri(ctx: AdminContext) -> str:
    return ctx.public_url.rstrip("/") + LOGIN_CALLBACK_PATH
