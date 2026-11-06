"""Packaging invariants: compose shape, release tag check, no personal data in the repo."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Built by concatenation so this file does not match its own patterns.
FORBIDDEN = [
    re.compile("tail" + r"[0-9a-f]{4,}\.ts\.net"),
    re.compile("@" + "purplemetrics"),
]
SKIP_DIRS = {".git", ".superpowers", "node_modules", ".venv", "dist", ".pytest_cache", ".ruff_cache", "__pycache__"}
SKIP_FILES = {"uv.lock", "package-lock.json", Path(__file__).name}
STATIC_BUILD = ROOT / "src" / "cashbook_mcp" / "admin_web" / "static"


def _compose(path: str) -> str:
    return (ROOT / path).read_text()


def test_compose_publishes_no_ports():
    for path in ("docker-compose.yml", "examples/tailscale-sidecar/compose.yml"):
        assert not re.search(r"^\s*ports\s*:", _compose(path), re.MULTILINE), path


def test_compose_secrets_volume_is_read_only():
    text = _compose("docker-compose.yml")
    lines = [ln for ln in text.splitlines() if ":/secrets" in ln]
    assert lines
    assert all(ln.strip().rstrip('"').endswith(":ro") for ln in lines)


def test_compose_limits_memory_and_restarts():
    for path in ("docker-compose.yml", "examples/tailscale-sidecar/compose.yml"):
        text = _compose(path)
        assert re.search(r"^\s*mem_limit:\s*512m\s*$", text, re.MULTILINE), path
        assert re.search(r"^\s*restart:\s*unless-stopped\s*$", text, re.MULTILINE), path


def test_release_checks_tag_against_pyproject_version():
    text = (ROOT / ".github/workflows/release.yml").read_text()
    assert "pyproject.toml" in text
    assert "GITHUB_REF_NAME" in text
    assert "ghcr.io/trainedloop/cashbook-mcp" in text
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert version


def test_dockerfile_shape():
    text = (ROOT / "Dockerfile").read_text()
    assert "node:22-slim" in text and "python:3.13-slim@sha256:" in text
    assert "CMD [\"cashbook-mcp\"]" in text
    assert "PORT=8743" in text


def test_dockerignore_excludes_secrets_and_data():
    entries = (ROOT / ".dockerignore").read_text().split()
    for needed in ("secrets/", "data/", ".superpowers/", ".git", "node_modules", ".venv"):
        assert needed in entries


def test_no_personal_data_in_repo():
    hits = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if not path.is_file() or path.name in SKIP_FILES:
            continue
        if SKIP_DIRS & set(rel.parts) or STATIC_BUILD in path.parents:
            continue
        text = path.read_text(errors="ignore")
        hits += [str(rel) for pat in FORBIDDEN if pat.search(text)]
    assert not hits, hits


def test_dockerfile_keeps_fastmcp_state_on_the_data_volume():
    # The OAuth proxy stores client registrations under FASTMCP_HOME; the image
    # user has no home, so it must point at the persistent, writable volume.
    text = (ROOT / "Dockerfile").read_text()
    assert re.search(r"FASTMCP_HOME=/data/fastmcp\b", text)


def test_dockerfile_ships_httpfs_and_disables_autoinstall_paths():
    text = (ROOT / "Dockerfile").read_text()
    assert "DUCKDB_EXTENSION_DIR=/opt/duckdb-ext" in text
    assert "INSTALL httpfs" in text


def test_ci_smoke_opens_database_in_the_image():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "HOME=/nonexistent" in text
    assert "Database.open" in text


def test_env_example_mentions_sidecar_cert_domain():
    assert re.search(r"^# TS_CERT_DOMAIN=", (ROOT / ".env.example").read_text(), re.MULTILINE)


def test_readme_quick_start_has_local_build_note():
    text = (ROOT / "README.md").read_text()
    quick = text[text.index("## Quick start") : text.index("## Development")]
    assert "docker build -t cashbook-mcp:dev ." in quick
