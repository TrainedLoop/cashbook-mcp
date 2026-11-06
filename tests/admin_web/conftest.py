"""Fixtures for the admin web tests; the fakes and helpers live in admin_support.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from admin_support import (
    ASSET_JS,
    CLIENT_BASE,
    INDEX_HTML,
    KEY,
    SESSION_KEY,
    Clock,
    Env,
    FakeIdentity,
    FakePluggy,
    FakeScheduler,
    FakeSyncer,
    populate,
    settings,
)
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cashbook_mcp.admin_web import spa
from cashbook_mcp.admin_web.api import AdminContext, admin_routes
from cashbook_mcp.config import Secrets
from cashbook_mcp.store.db import Database


@pytest.fixture
def static_dir(tmp_path, monkeypatch) -> Path:
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(INDEX_HTML)
    (static / "assets" / "app-abc123.js").write_text(ASSET_JS)
    monkeypatch.setattr(spa, "STATIC_DIR", static)
    return static


@pytest.fixture
def env(tmp_path, static_dir):
    db = Database.open(tmp_path / "data.duckdb", KEY)
    populate(db)
    identity, syncer, pluggy, clock = FakeIdentity(), FakeSyncer(), FakePluggy(), Clock()
    ctx = AdminContext(
        settings=settings(tmp_path),
        secrets=Secrets("pid", "psecret", "gid", "gsecret", KEY, SESSION_KEY),
        syncer=syncer,  # type: ignore[arg-type]
        scheduler=FakeScheduler(),  # type: ignore[arg-type]
        db=db,
        pluggy=pluggy,  # type: ignore[arg-type]
        identity=identity,
        now=clock,
    )
    # As a context manager, so background reload tasks live on one event loop across requests.
    with TestClient(Starlette(routes=admin_routes(ctx)), base_url=CLIENT_BASE) as client:
        yield Env(ctx, client, identity, syncer, pluggy, clock, static_dir)
        syncer.release.set()
    db.close()

