"""Serving the built admin SPA: hashed assets immutable, index.html (no-cache) for every other path.

The front is built into STATIC_DIR (git-ignored) by the Docker build. Without it (development
without a front build) every SPA path answers 503 and the JSON API keeps working.
"""

from __future__ import annotations

import re
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response

STATIC_DIR = Path(__file__).parent / "static"

ASSET_CACHE = "public, max-age=31536000, immutable"
INDEX_CACHE = "no-cache"
NOT_BUILT = "Admin UI not built"

# One flat file name as Vite emits it (name-hash.ext): no separators, no leading dot.
_ASSET_NAME = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}$")


def _not_built() -> Response:
    return PlainTextResponse(NOT_BUILT, status_code=503)


def _not_found() -> Response:
    return PlainTextResponse("Not found", status_code=404)


def _static_dir() -> Path | None:
    # Read at request time, so a front built after startup (or a test's directory) is picked up.
    return STATIC_DIR if STATIC_DIR.is_dir() else None


async def asset(request: Request) -> Response:
    static = _static_dir()
    if static is None:
        return _not_built()
    name = request.path_params.get("file", "")
    if not isinstance(name, str) or not _ASSET_NAME.fullmatch(name) or ".." in name:
        return _not_found()
    path = static / "assets" / name
    if not path.is_file():
        return _not_found()
    return FileResponse(path, headers={"Cache-Control": ASSET_CACHE})


async def index(request: Request) -> Response:
    static = _static_dir()
    if static is None:
        return _not_built()
    path = static / "index.html"
    if not path.is_file():
        return _not_built()
    return FileResponse(path, media_type="text/html; charset=utf-8", headers={"Cache-Control": INDEX_CACHE})
