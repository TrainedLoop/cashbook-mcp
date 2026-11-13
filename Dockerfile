# Base images are pinned by digest (multi-arch index); Dependabot bumps them.

# Stage 1: build the admin SPA. vite writes to ../src/cashbook_mcp/admin_web/static,
# which resolves to /build/src/cashbook_mcp/admin_web/static from /build/web.
FROM node:24-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9 AS web
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# Stage 2: the Python runtime. Node is not part of it.
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

COPY --from=ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
COPY --from=web /build/src/cashbook_mcp/admin_web/static ./src/cashbook_mcp/admin_web/static
RUN uv sync --frozen --no-dev

# DuckDB needs httpfs (OpenSSL crypto) to write encrypted files. Install it at
# build time into a fixed, world-readable directory: at runtime the server
# never installs or autoloads extensions, and the user has no home directory.
ENV DUCKDB_EXTENSION_DIR=/opt/duckdb-ext
RUN /app/.venv/bin/python -c "import duckdb; c = duckdb.connect(); c.execute(\"SET extension_directory = '/opt/duckdb-ext'\"); c.execute('INSTALL httpfs'); c.execute('LOAD httpfs')" \
    && chmod -R a+rX /opt/duckdb-ext

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin cashbook
USER cashbook

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/cashbook.duckdb \
    SECRETS_DIR=/secrets \
    PORT=8743 \
    FASTMCP_HOME=/data/fastmcp

EXPOSE 8743
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8743'), timeout=4)"]
CMD ["cashbook-mcp"]
