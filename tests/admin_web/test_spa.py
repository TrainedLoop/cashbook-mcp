import shutil

import pytest
from admin_support import ASSET_JS, INDEX_HTML, PUBLIC

from cashbook_mcp.admin_web.headers import CSP, SECURITY_HEADERS

EXPECTED_CSP = (
    "default-src 'none'; script-src 'self' https://cdn.pluggy.ai; "
    "style-src 'self' 'unsafe-inline' https://cdn.pluggy.ai; img-src 'self' data: https:; "
    "connect-src 'self' https://api.pluggy.ai; frame-src https://connect.pluggy.ai; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def test_headers_values():
    assert CSP == EXPECTED_CSP
    assert SECURITY_HEADERS == {
        "Content-Security-Policy": EXPECTED_CSP,
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        "Cache-Control": "no-store",
    }


@pytest.mark.parametrize("path", ["/admin", "/admin/", "/admin/connections", "/admin/connect/new?x=1"])
def test_spa_fallback_and_cache_headers(env, path):
    resp = env.client.get(path)
    assert resp.status_code == 200
    assert resp.text == INDEX_HTML
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-cache"


def test_spa_serves_hashed_assets_immutable(env):
    resp = env.client.get("/admin/assets/app-abc123.js")
    assert resp.status_code == 200
    assert resp.text == ASSET_JS
    assert "javascript" in resp.headers["content-type"]
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize(
    "path",
    [
        "/admin/assets/missing.js",
        "/admin/assets/%2e%2e/index.html",
        "/admin/assets/..%2findex.html",
        "/admin/assets/sub/app.js",
        "/admin/assets/.hidden",
        "/admin/assets/",
    ],
)
def test_spa_asset_misses_are_404_not_index(env, path):
    resp = env.client.get(path)
    assert resp.status_code == 404
    assert resp.text != INDEX_HTML
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/admin/api", "/admin/api/", "/admin/api/nope", "/admin/auth", "/admin/auth/nope"])
def test_api_and_auth_paths_never_fall_back_to_index(env, path):
    resp = env.client.get(path)
    assert resp.status_code == 404
    assert resp.text != INDEX_HTML


def test_spa_head_is_allowed_and_other_methods_are_not(env):
    assert env.client.head("/admin").status_code == 200
    resp = env.client.post("/admin/connections", headers={"Origin": PUBLIC})
    assert resp.status_code == 405


def test_spa_not_built_is_503(env):
    shutil.rmtree(env.static)
    for path in ("/admin", "/admin/connections", "/admin/assets/app-abc123.js"):
        resp = env.client.get(path)
        assert resp.status_code == 503
        assert "Admin UI not built" in resp.text
    # the API keeps working without a front build
    assert env.client.get("/admin/api/session").status_code == 401


def test_security_headers_on_all_admin_responses(env, monkeypatch):
    def check(resp, cache="no-store"):
        assert resp.headers["content-security-policy"] == EXPECTED_CSP, resp.request.url
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["referrer-policy"] == "same-origin"
        assert resp.headers["cache-control"] == cache

    check(env.client.get("/admin"), "no-cache")
    check(env.client.get("/admin/connections"), "no-cache")
    check(env.client.get("/admin/assets/app-abc123.js"), "public, max-age=31536000, immutable")
    check(env.client.get("/admin/assets/missing.js"))
    check(env.client.get("/admin/api/session"))  # 401
    check(env.client.get("/admin/api/nope"))  # 404
    check(env.client.put("/admin/api/session"))  # 405
    check(env.client.post("/admin"))  # 405
    resp, flow, query = env.start_login()
    check(resp)  # redirect to Google
    check(env.callback(flow, query["state"][0]))  # done page
    check(env.callback(None, "x"))  # expired page
    env.login()
    check(env.client.get("/admin/api/session"))
    check(env.client.get("/admin/api/connections"))
    check(env.client.get("/admin/api/server"))
    check(env.send("POST", "/admin/api/connect-token", {"alias": "new"}))
    check(env.send("POST", "/admin/api/connect-token", {"alias": "new"}, csrf=None))  # 403
    env.syncer.errors["disconnect_impact"] = RuntimeError("boom")
    check(env.client.get("/admin/api/connections/main/disconnect-impact"))  # 500
    check(env.send("POST", "/admin/auth/logout"))
    shutil.rmtree(env.static)
    check(env.client.get("/admin"))  # 503


@pytest.mark.parametrize("path", ["/admin", "/admin/assets/app-abc123.js", "/admin/assets/missing.js"])
def test_nosniff_on_admin_responses(env, path):
    resp = env.client.get(path)
    assert resp.headers["x-content-type-options"] == "nosniff"
