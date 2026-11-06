"""Sign-in, re-authentication and sign-out (ported from mailhub's route tests)."""

import logging

import pytest
from admin_support import CLIENT_BASE, OWNER, PUBLIC, attrs, cookie_header, cookie_value
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cashbook_mcp.admin_web import api as api_module
from cashbook_mcp.admin_web.api import _UsedStates, admin_routes, safe_path
from cashbook_mcp.admin_web.session import (
    FLOW_COOKIE,
    FLOW_SECONDS,
    REAUTH_SECONDS,
    SESSION_COOKIE,
    SESSION_SECONDS,
    Signer,
    decode_session,
    is_fresh,
)

EXPIRED = "This sign-in link expired, start again"


def _session(env, token):
    return decode_session(Signer(env.ctx.key), token, env.clock(), env.ctx.allowed_emails)


def test_login_sets_flow_cookie_and_uses_public_redirect_uri(env):
    resp, flow, query = env.start_login()
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("https://accounts.google.com/")
    assert query["redirect_uri"] == [f"{PUBLIC}/admin/auth/callback"]
    assert "testserver" not in resp.headers["location"]
    assert query["state"][0] and query["nonce"][0] and query["code_challenge"][0]
    a = attrs(cookie_header(resp, FLOW_COOKIE))
    assert "httponly" in a and "secure" in a
    assert a["samesite"] == "Lax"
    assert a["path"] == "/admin"
    assert a["max-age"] == str(FLOW_SECONDS)
    assert flow and query["state"][0] not in flow
    assert cookie_header(resp, SESSION_COOKIE) is None


@pytest.mark.parametrize(
    ("next_", "expected"),
    [
        ("/admin", "/admin"),
        ("/admin?screen=disconnect&alias=main", "/admin?screen=disconnect&amp;alias=main"),
        ("/admin/connections", "/admin/connections"),
        ("//evil.example.com", "/admin"),
        ("https://evil.example.com", "/admin"),
        ("/admin//evil.example.com", "/admin"),
        ("/admin\\evil", "/admin"),
        ("/adminx", "/admin"),
        ("/other", "/admin"),
        ("/admin/\r\nSet-Cookie: x=1", "/admin"),
        ("/admin/../mcp", "/admin"),
        ("/admin/auth/login", "/admin"),
        ("/admin/auth/login?next=/admin/x", "/admin"),
        ("/admin/auth/callback", "/admin"),
        ("/admin/auth/logout", "/admin"),
    ],
)
def test_login_next_is_limited_to_admin_paths(env, next_, expected):
    _, flow, query = env.start_login(next_)
    resp = env.callback(flow, query["state"][0])
    assert resp.status_code == 200
    assert f'content="0;url={expected}"' in resp.text


def test_safe_path():
    assert safe_path("/admin?screen=x&alias=a") == "/admin?screen=x&alias=a"
    assert safe_path(None) == "/admin"
    assert safe_path("/admin/a/../b") == "/admin"


def test_callback_sets_session_and_meta_refreshes(env, caplog):
    caplog.set_level(logging.INFO, logger="cashbook-mcp")
    _, flow, query = env.start_login()
    resp = env.callback(flow, query["state"][0])
    assert resp.status_code == 200
    assert "location" not in resp.headers
    assert '<meta http-equiv="refresh" content="0;url=/admin">' in resp.text
    assert 'href="/admin"' in resp.text
    a = attrs(cookie_header(resp, SESSION_COOKIE))
    assert "httponly" in a and "secure" in a
    assert a["samesite"] == "Strict"
    assert a["path"] == "/admin"
    assert a["max-age"] == str(SESSION_SECONDS)
    s = _session(env, cookie_value(resp, SESSION_COOKIE))
    assert s.email == OWNER
    assert s.login_at == env.clock() and s.auth_at == env.clock()
    assert env.identity.calls[0]["redirect_uri"] == f"{PUBLIC}/admin/auth/callback"
    assert any(r.getMessage() == f"admin login email={OWNER}" for r in caplog.records)
    assert not any("code-1" in r.getMessage() for r in caplog.records)


def test_callback_deletes_flow_cookie(env):
    _, flow, query = env.start_login()
    a = attrs(cookie_header(env.callback(flow, query["state"][0]), FLOW_COOKIE))
    assert a["max-age"] == "0" and a["path"] == "/admin"


def test_replayed_callback_writes_nothing_a_second_time(env):
    _, flow, query = env.start_login()
    assert cookie_value(env.callback(flow, query["state"][0]), SESSION_COOKIE)
    again = env.callback(flow, query["state"][0])
    assert EXPIRED in again.text
    assert cookie_header(again, SESSION_COOKIE) is None
    assert cookie_header(again, FLOW_COOKIE) is not None
    assert len(env.identity.calls) == 1


def test_callback_without_flow_cookie_is_expired(env):
    _, _, query = env.start_login()
    resp = env.callback(None, query["state"][0])
    assert resp.status_code == 400 and EXPIRED in resp.text
    assert cookie_header(resp, SESSION_COOKIE) is None
    assert env.identity.calls == []


def test_callback_after_flow_lifetime_is_expired(env):
    _, flow, query = env.start_login()
    env.clock.t += FLOW_SECONDS
    resp = env.callback(flow, query["state"][0])
    assert EXPIRED in resp.text and cookie_header(resp, SESSION_COOKIE) is None


def test_callback_with_wrong_state_sets_no_session(env):
    _, flow, _ = env.start_login()
    resp = env.callback(flow, "not-the-state")
    assert EXPIRED in resp.text and cookie_header(resp, SESSION_COOKIE) is None
    assert env.identity.calls == []


def test_callback_with_non_ascii_state_is_expired(env):
    _, flow, _ = env.start_login()
    resp = env.callback(flow, "é")
    assert resp.status_code == 400 and EXPIRED in resp.text


def test_callback_with_consent_refused_sets_no_session(env):
    _, flow, query = env.start_login()
    env.client.cookies.set(FLOW_COOKIE, flow)
    resp = env.client.get(
        f"/admin/auth/callback?state={query['state'][0]}&error=access_denied<script>", follow_redirects=False
    )
    assert cookie_header(resp, SESSION_COOKIE) is None
    assert "<script>" not in resp.text
    assert env.identity.calls == []


def test_callback_login_error_logs_reason_only(env, caplog):
    caplog.set_level(logging.INFO, logger="cashbook-mcp")
    env.identity.fail = "Google sign-in failed"
    env.identity.fail_reason = "unverified-email"
    _, flow, query = env.start_login()
    resp = env.callback(flow, query["state"][0], code="secret-code")
    assert "Google sign-in failed" in resp.text
    assert cookie_header(resp, SESSION_COOKIE) is None
    assert any(r.getMessage() == "admin login failed reason=unverified-email" for r in caplog.records)
    assert not any("secret-code" in r.getMessage() for r in caplog.records)


def test_callback_refuses_owner_not_allowlisted(env, caplog):
    caplog.set_level(logging.INFO, logger="cashbook-mcp")
    env.identity.email = "stranger@example.com"
    _, flow, query = env.start_login()
    resp = env.callback(flow, query["state"][0])
    assert resp.status_code == 403
    assert "This Google account is not allowed" in resp.text
    assert cookie_header(resp, SESSION_COOKIE) is None


def test_allowlist_compares_lowercased(env):
    env.identity.email = "Me@Example.com"
    _, flow, query = env.start_login()
    resp = env.callback(flow, query["state"][0])
    assert _session(env, cookie_value(resp, SESSION_COOKIE)).email == OWNER


def test_auth_time_is_used_and_capped_at_now(env):
    s = _session(env, env.login(auth_time=env.clock() - 100))
    assert s.auth_at == env.clock() - 100 and s.has_auth_time is True
    assert _session(env, env.login(auth_time=env.clock() + 1000)).auth_at == env.clock()


def test_reauth_keeps_sid_and_login_at(env):
    first = _session(env, env.login())
    env.clock.t += 3600
    second = _session(env, env.login())
    assert second.sid == first.sid
    assert second.login_at == first.login_at
    assert second.auth_at == env.clock()


def test_reauth_cookie_lifetime_counts_from_first_login(env):
    env.login()
    env.clock.t += 3600
    _, flow, query = env.start_login()
    resp = env.callback(flow, query["state"][0], code="other")
    assert attrs(cookie_header(resp, SESSION_COOKIE))["max-age"] == str(SESSION_SECONDS - 3600)


def test_reauth_login_asks_for_max_age_and_first_login_does_not(env):
    assert "max_age" not in env.start_login()[2]
    env.login()
    assert env.start_login()[2]["max_age"] == [str(REAUTH_SECONDS)]


def test_reauth_with_stale_auth_time_ends_fresh_and_warns(env, caplog):
    caplog.set_level(logging.INFO, logger="cashbook-mcp")
    env.login(auth_time=env.clock())
    env.clock.t += 7200
    s = _session(env, env.login(auth_time=env.clock() - 3600))
    assert is_fresh(s, env.clock())
    warns = [r for r in caplog.records if r.getMessage() == "admin reauth: google returned a stale auth_time"]
    assert len(warns) == 1


def test_first_login_keeps_old_auth_time(env):
    s = _session(env, env.login(auth_time=env.clock() - 3600))
    assert s.auth_at == env.clock() - 3600 and not is_fresh(s, env.clock())


def test_login_callback_that_raises_still_deletes_flow_cookie(env, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret")

    monkeypatch.setattr(env.identity, "complete", boom)
    _, flow, query = env.start_login()
    resp = env.callback(flow, query["state"][0])
    assert resp.status_code == 500
    assert "secret" not in resp.text
    assert attrs(cookie_header(resp, FLOW_COOKIE))["max-age"] == "0"
    assert resp.headers["x-frame-options"] == "DENY"


# --- logout ---------------------------------------------------------------------------------


def test_logout_with_csrf_clears_cookies(env):
    env.login()
    resp = env.send("POST", "/admin/auth/logout")
    assert resp.status_code == 200
    assert resp.json() == {"signed_out": True}
    assert attrs(cookie_header(resp, SESSION_COOKIE))["max-age"] == "0"
    assert attrs(cookie_header(resp, FLOW_COOKIE))["max-age"] == "0"


@pytest.mark.parametrize("case", [{"csrf": None}, {"origin": "https://evil.example.com"}, {"origin": "null"}])
def test_logout_needs_csrf_and_origin(env, case):
    env.login()
    resp = env.send("POST", "/admin/auth/logout", **case)
    assert resp.status_code == 403
    assert cookie_header(resp, SESSION_COOKIE) is None


def test_logout_without_session_is_401(env):
    assert env.send("POST", "/admin/auth/logout", csrf="x").status_code == 401


def test_logout_get_is_not_allowed(env):
    env.login()
    assert env.client.get("/admin/auth/logout").status_code == 405


# --- used states ------------------------------------------------------------------------------


def test_used_states_prunes_expired_entries_from_the_front():
    store = _UsedStates()
    assert store.claim("a", 0.0)
    assert store.claim("b", 1.0)
    assert not store.claim("a", 2.0)
    assert store.claim("c", FLOW_SECONDS + 0.5)
    assert len(store) == 2
    assert store.claim("a", FLOW_SECONDS + 0.5)


def test_used_states_evicts_oldest_beyond_the_cap():
    store = _UsedStates(cap=3)
    assert all(store.claim(s, 0.0) for s in ("a", "b", "c", "d", "e"))
    assert len(store) == 3
    assert store.claim("f", 1.0)
    assert not store.claim("f", 1.0)
    assert store.claim("a", 1.0)


def test_replay_of_an_evicted_login_state_fails_at_the_code_exchange(env, monkeypatch):
    monkeypatch.setattr(api_module, "USED_STATES_CAP", 1)
    with TestClient(Starlette(routes=admin_routes(env.ctx)), base_url=CLIENT_BASE) as client:
        env.client = client
        _, flow, query = env.start_login()
        assert cookie_value(env.callback(flow, query["state"][0], code="once"), SESSION_COOKIE)
        _, flow2, query2 = env.start_login()
        assert cookie_value(env.callback(flow2, query2["state"][0], code="twice"), SESSION_COOKIE)
        again = env.callback(flow, query["state"][0], code="once")
        assert cookie_header(again, SESSION_COOKIE) is None
        assert "Google sign-in failed" in again.text
        assert [c["code"] for c in env.identity.calls] == ["once", "twice", "once"]
