import logging

import pytest
from admin_support import OWNER, PUBLIC, wait_until

from cashbook_mcp.admin_web.session import REAUTH_SECONDS
from cashbook_mcp.pluggy.client import PluggyError
from cashbook_mcp.store.sync import ValidationError

GETS = [
    "/admin/api/session",
    "/admin/api/connections",
    "/admin/api/connections/main/disconnect-impact",
    "/admin/api/server",
]
MUTATIONS = [
    ("POST", "/admin/api/connect-token", {"alias": "new"}),
    ("POST", "/admin/api/connections/preview", {"item_id": "item-9"}),
    ("POST", "/admin/api/connections", {"alias": "new", "item_id": "item-9"}),
    ("POST", "/admin/api/connections/main/reload", None),
    ("POST", "/admin/api/reload", None),
    ("DELETE", "/admin/api/connections/main", None),
]


# --- session --------------------------------------------------------------------------------


@pytest.mark.parametrize("path", GETS)
def test_api_requires_session_on_reads(env, path):
    resp = env.client.get(path)
    assert resp.status_code == 401
    assert resp.json() == {"error": "signed_out"}


@pytest.mark.parametrize(("method", "path", "body"), MUTATIONS)
def test_api_requires_session(env, method, path, body):
    resp = env.send(method, path, body, csrf="anything")
    assert resp.status_code == 401
    assert resp.json() == {"error": "signed_out"}
    assert env.syncer.calls == [] and env.pluggy.calls == []


def test_api_session_after_expiry_or_tamper_is_signed_out(env):
    token = env.login()
    env.client.cookies.set("cashbook_admin", token[:-2] + "xx")
    assert env.client.get("/admin/api/session").status_code == 401
    env.client.cookies.set("cashbook_admin", token)
    env.clock.t += 8 * 3600
    assert env.client.get("/admin/api/session").json() == {"error": "signed_out"}


def test_session_endpoint_returns_email_csrf_and_freshness(env):
    env.login()
    got = env.client.get("/admin/api/session").json()
    assert got["email"] == OWNER
    assert isinstance(got["csrf"], str) and len(got["csrf"]) >= 32
    assert got["reauth_fresh"] is True
    env.clock.t += REAUTH_SECONDS
    later = env.client.get("/admin/api/session").json()
    assert later["reauth_fresh"] is False
    assert later["csrf"] == got["csrf"]  # bound to the session id, not to the time


# --- CSRF and origin ------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), MUTATIONS)
def test_mutations_require_csrf_and_origin(env, method, path, body):
    env.login()
    token = env.csrf()
    refused = [
        {"csrf": None},
        {"csrf": ""},
        {"csrf": token + "x"},
        {"csrf": token, "origin": None},
        {"csrf": token, "origin": "null"},
        {"csrf": token, "origin": "https://evil.example.com"},
        {"csrf": token, "origin": "http://owner.example.ts.net"},
        {"csrf": token, "origin": "https://owner.example.ts.net.evil.example.com"},
        {"csrf": token, "origin": None, "referer": "https://evil.example.com/admin"},
    ]
    for case in refused:
        resp = env.send(method, path, body, **case)
        assert resp.status_code == 403, case
        assert resp.json() == {"error": "csrf"}, case
    assert env.syncer.calls == [] and env.pluggy.calls == []


def test_same_origin_referer_is_accepted_without_origin(env):
    env.login()
    resp = env.send("POST", "/admin/api/connect-token", {"alias": "new"}, origin=None, referer=f"{PUBLIC}/admin")
    assert resp.status_code == 200


def test_csrf_of_another_session_is_refused(env):
    env.login()
    other = env.csrf()
    env.client.cookies.clear()
    env.login()  # a sign-in without the session cookie present starts a new session id
    resp = env.send("POST", "/admin/api/connect-token", {"alias": "new"}, csrf=other)
    assert resp.status_code == 403


@pytest.mark.parametrize("body", ["not json", "[1, 2]", '"text"', "", "{"])
def test_mutation_with_bad_json_body_is_400(env, body):
    env.login()
    resp = env.client.post(
        "/admin/api/connections",
        content=body,
        headers={"X-CSRF-Token": env.csrf(), "Origin": PUBLIC, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "bad_request"}
    assert env.syncer.calls == []


def test_oversized_body_is_refused(env):
    env.login()
    resp = env.send("POST", "/admin/api/connections", {"alias": "a", "item_id": "x" * 20_000})
    assert resp.status_code == 413
    assert env.syncer.calls == []


# --- disconnect -----------------------------------------------------------------------------


def test_disconnect_requires_fresh_reauth(env):
    env.login()
    env.clock.t += REAUTH_SECONDS
    resp = env.send("DELETE", "/admin/api/connections/main")
    assert resp.status_code == 403
    assert resp.json() == {"error": "reauth_required"}
    assert ("disconnect", "main") not in env.syncer.calls

    env.login()  # re-authentication with the session cookie present keeps the session, now fresh
    resp = env.send("DELETE", "/admin/api/connections/main")
    assert resp.status_code == 200
    assert resp.json() == {"disconnected": "main"}
    assert ("disconnect", "main") in env.syncer.calls


def test_disconnect_checks_csrf_before_freshness(env):
    env.login()
    env.clock.t += REAUTH_SECONDS
    resp = env.send("DELETE", "/admin/api/connections/main", csrf=None)
    assert resp.status_code == 403 and resp.json() == {"error": "csrf"}


@pytest.mark.parametrize("alias", ["nope", "Bad", "a" * 40])
def test_disconnect_unknown_alias_is_404(env, alias):
    env.login()
    resp = env.send("DELETE", f"/admin/api/connections/{alias}")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not_found"}
    assert not any(c[0] == "disconnect" for c in env.syncer.calls)


def test_disconnect_impact(env):
    env.login()
    resp = env.client.get("/admin/api/connections/main/disconnect-impact")
    assert resp.status_code == 200
    assert resp.json() == {
        "alias": "main",
        "total_transactions": 1240,
        "before_window": 300,
        "oldest_date": "2025-03-02",
        "window_start": "2025-11-06",
    }
    assert env.client.get("/admin/api/connections/nope/disconnect-impact").status_code == 404


# --- add connection -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code", ["alias_invalid", "alias_taken", "item_taken", "item_unknown", "item_not_meupluggy"]
)
def test_add_connection_validation_errors_map_to_422(env, code):
    env.login()
    env.syncer.errors["add_connection"] = ValidationError(code, "detail that is not shown")
    resp = env.send("POST", "/admin/api/connections", {"alias": "new", "item_id": "item-9"})
    assert resp.status_code == 422
    assert resp.json() == {"error": code}


@pytest.mark.parametrize("body", [{"alias": "new"}, {"item_id": "item-9"}, {}])
def test_add_connection_with_missing_fields_is_400(env, body):
    env.login()
    resp = env.send("POST", "/admin/api/connections", body)
    assert resp.status_code == 400
    assert resp.json() == {"error": "bad_request"}
    assert env.syncer.calls == []


@pytest.mark.parametrize(
    ("body", "code"),
    [({"alias": 1, "item_id": "item-9"}, "alias_invalid"), ({"alias": "Bad", "item_id": "item-9"}, "alias_invalid"),
     ({"alias": "new", "item_id": ["x"]}, "item_unknown")],
)  # fmt: skip
def test_add_connection_with_wrong_field_types_is_422_before_pluggy(env, body, code):
    env.login()
    resp = env.send("POST", "/admin/api/connections", body)
    assert resp.status_code == 422
    assert resp.json() == {"error": code}
    assert env.syncer.calls == []


@pytest.mark.parametrize("item_id", ["", "../connect_token", "item 9", "a/b", "x" * 65, "item-9?x=1"])
def test_add_connection_refuses_malformed_item_ids_without_pluggy(env, item_id):
    env.login()
    resp = env.send("POST", "/admin/api/connections", {"alias": "new", "item_id": item_id})
    assert resp.status_code == 422
    assert resp.json() == {"error": "item_unknown"}
    assert env.syncer.calls == []


def test_add_connection_stores_and_starts_first_load_in_background(env):
    env.login()
    env.syncer.release.clear()
    resp = env.send("POST", "/admin/api/connections", {"alias": "new", "item_id": "item-9"})
    assert resp.status_code == 201
    assert resp.json() == {"alias": "new", "started": True}
    assert env.syncer.calls[0] == ("add_connection", "new", "item-9", False)
    wait_until(lambda: ("load", "new") in env.syncer.calls)
    # the first load counts as a reload in progress
    assert env.send("POST", "/admin/api/connections/new/reload").status_code == 409
    env.syncer.release.set()


def test_add_connection_pluggy_failure_is_generic(env):
    env.login()
    env.syncer.errors["add_connection"] = PluggyError("raw upstream body with secrets", status=500)
    resp = env.send("POST", "/admin/api/connections", {"alias": "new", "item_id": "item-9"})
    assert resp.status_code == 502
    assert resp.json() == {"error": "pluggy_unavailable"}
    assert "secrets" not in resp.text


# --- preview --------------------------------------------------------------------------------


def test_preview(env):
    env.login()
    resp = env.send("POST", "/admin/api/connections/preview", {"item_id": "item-9"})
    assert resp.status_code == 200
    assert resp.json() == {
        "item_id": "item-9",
        "connector_name": "MeuPluggy",
        "status": "UPDATED",
        "accounts": [{"name": "Checking", "type": "BANK", "number": "****2345"}],
    }
    assert env.syncer.calls == [("preview", "item-9")]


@pytest.mark.parametrize("code", ["item_unknown", "item_not_meupluggy"])
def test_preview_validation_errors_map_to_422(env, code):
    env.login()
    env.syncer.errors["preview"] = ValidationError(code)
    resp = env.send("POST", "/admin/api/connections/preview", {"item_id": "item-9"})
    assert resp.status_code == 422 and resp.json() == {"error": code}


def test_preview_refuses_malformed_item_id(env):
    env.login()
    resp = env.send("POST", "/admin/api/connections/preview", {"item_id": "../items"})
    assert resp.status_code == 422 and resp.json() == {"error": "item_unknown"}
    assert env.syncer.calls == []


# --- connect token --------------------------------------------------------------------------


@pytest.mark.parametrize("alias", ["Bad", "-a", "a" * 33, "", "a_b", "new\n", 5, None])
def test_connect_token_validates_alias_first(env, alias):
    env.login()
    resp = env.send("POST", "/admin/api/connect-token", {"alias": alias})
    assert resp.status_code == 422
    assert resp.json() == {"error": "alias_invalid"}
    assert env.pluggy.calls == []


def test_connect_token_refuses_a_taken_alias(env):
    env.login()
    resp = env.send("POST", "/admin/api/connect-token", {"alias": "main"})
    assert resp.status_code == 422
    assert resp.json() == {"error": "alias_taken"}
    assert env.pluggy.calls == []


def test_connect_token_issues_token_restricted_to_meupluggy(env):
    env.login()
    resp = env.send("POST", "/admin/api/connect-token", {"alias": "new-bank"})
    assert resp.status_code == 200
    assert resp.json() == {"accessToken": "connect-token-1", "connectorIds": [200]}
    assert env.pluggy.calls == [{"avoidDuplicates": True}]


def test_connect_token_pluggy_error_is_generic(env, caplog):
    env.login()
    env.pluggy.error = PluggyError("raw upstream body with secrets", status=500)
    resp = env.send("POST", "/admin/api/connect-token", {"alias": "new"})
    assert resp.status_code == 502
    assert resp.json() == {"error": "pluggy_unavailable"}
    assert "secrets" not in resp.text


# --- reload ---------------------------------------------------------------------------------


def test_reload_runs_in_background_and_rejects_a_concurrent_one(env):
    env.login()
    env.syncer.release.clear()
    resp = env.send("POST", "/admin/api/connections/main/reload")
    assert resp.status_code == 202
    assert resp.json() == {"started": True}
    wait_until(env.syncer.loads_started.is_set)
    again = env.send("POST", "/admin/api/connections/main/reload")
    assert again.status_code == 409
    assert again.json() == {"error": "reload_in_progress"}
    rows = {c["alias"]: c for c in env.client.get("/admin/api/connections").json()["connections"]}
    assert rows["main"]["syncing"] is True and rows["main"]["badge"] == "syncing"
    assert rows["second"]["syncing"] is False

    env.syncer.release.set()

    def finished():
        rows = env.client.get("/admin/api/connections").json()["connections"]
        return not next(c for c in rows if c["alias"] == "main")["syncing"]

    wait_until(finished)
    assert env.send("POST", "/admin/api/connections/main/reload").status_code == 202
    assert env.syncer.calls.count(("load", "main")) >= 1


def test_reload_of_unknown_alias_is_404(env):
    env.login()
    resp = env.send("POST", "/admin/api/connections/nope/reload")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not_found"}


def test_reload_failure_is_logged_without_detail(env, caplog):
    caplog.set_level(logging.INFO, logger="cashbook-mcp")
    env.login()
    env.syncer.errors["load"] = RuntimeError("secret detail")
    assert env.send("POST", "/admin/api/connections/main/reload").status_code == 202
    wait_until(lambda: any("admin reload failed" in r.getMessage() for r in caplog.records))
    assert not any("secret detail" in r.getMessage() for r in caplog.records)


def test_reload_all_starts_every_connection_not_already_loading(env):
    env.login()
    env.syncer.release.clear()
    assert env.send("POST", "/admin/api/connections/main/reload").status_code == 202
    resp = env.send("POST", "/admin/api/reload")
    assert resp.status_code == 202
    assert resp.json() == {"started": True, "aliases": ["second"]}
    again = env.send("POST", "/admin/api/reload")
    assert again.status_code == 409 and again.json() == {"error": "reload_in_progress"}
    env.syncer.release.set()
    wait_until(lambda: ("load", "second") in env.syncer.calls)


# --- reads ----------------------------------------------------------------------------------


def test_connections_list_with_badges_and_no_raw_errors(env):
    env.login()
    resp = env.client.get("/admin/api/connections")
    assert resp.status_code == 200
    rows = {c["alias"]: c for c in resp.json()["connections"]}
    assert set(rows) == {"main", "second", "third", "fourth"}
    assert rows["main"]["badge"] == "ok"
    assert rows["main"]["bank"] == "Bank A"
    assert rows["main"]["last_synced_at"] == "2026-11-06T09:00:00+00:00"
    assert rows["main"]["history"] == {"transactions": 1, "date_from": "2026-10-01", "date_to": "2026-10-01"}
    assert rows["main"]["accounts"][0]["name"] == "Checking"
    assert rows["second"]["badge"] == "login error" and rows["second"]["hint"]
    assert rows["third"]["badge"] == "outdated" and rows["third"]["sync_failed"] is True
    assert rows["fourth"]["badge"] == "consent revoked"
    assert "raw secret body" not in resp.text
    assert all("last_sync_error" not in c for c in rows.values())


def test_server_info(env):
    env.login()
    resp = env.client.get("/admin/api/server")
    assert resp.status_code == 200
    got = resp.json()
    assert got["last_full_refresh"] == "2026-11-06T09:00:00+00:00"
    assert got["next_full_refresh"] == "2026-11-07T06:00:00-03:00"
    assert got["schema_version"] == env.ctx.db.schema_version()
    assert got["db_size_bytes"] == env.ctx.db.size_bytes() > 0
    assert got["app_version"] == "0.1.0"
    for secret in ("psecret", "gsecret", "ab" * 32, "s" * 64):
        assert secret not in resp.text


def test_server_info_before_any_full_refresh(env):
    env.login()
    env.ctx.scheduler.last_full_refresh = None
    assert env.client.get("/admin/api/server").json()["last_full_refresh"] is None


# --- errors ---------------------------------------------------------------------------------


def test_unexpected_error_is_generic_500(env, caplog):
    caplog.set_level(logging.INFO, logger="cashbook-mcp")
    env.login()
    env.syncer.errors["disconnect_impact"] = RuntimeError("secret detail")
    resp = env.client.get("/admin/api/connections/main/disconnect-impact")
    assert resp.status_code == 500
    assert resp.json() == {"error": "internal"}
    assert "secret" not in resp.text
    assert not any("secret detail" in r.getMessage() for r in caplog.records)
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)


def test_unknown_api_path_is_json_404(env):
    env.login()
    resp = env.client.get("/admin/api/nope")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not_found"}


def test_wrong_method_on_api_route_is_405(env):
    env.login()
    resp = env.send("PUT", "/admin/api/session")
    assert resp.status_code == 405
    assert resp.json() == {"error": "method_not_allowed"}
    assert env.send("GET", "/admin/api/reload").status_code == 405
