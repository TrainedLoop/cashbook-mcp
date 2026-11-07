# Tutorial: from zero to a working connector

You end up with cashbook-mcp running in Docker behind a public HTTPS URL, with your banks connected through MeuPluggy and the server added as a custom connector in claude.ai.

An instance belongs to one person: only your Google login may use the connector and `/admin`, and every connected bank is yours. If someone else wants the same, they run their own instance.

cashbook-mcp is unofficial and not affiliated with or endorsed by Pluggy. It only reads your data.

## 0. Prerequisites

- A Linux host (or any machine) with Docker and Docker Compose.
- A public HTTPS address for it. Decide it first: it is `PUBLIC_URL` below (for example `https://cashbook.owner.example.ts.net`, see [examples/tailscale-sidecar](../examples/tailscale-sidecar/)).
- A Google account and a Brazilian bank account that supports Open Finance.

## 1. Create a Pluggy app

1. Sign up at https://dashboard.pluggy.ai and create an application.
2. Copy its **Client ID** and **Client Secret** into `secrets/pluggy.json`:

   ```json
   { "clientId": "...", "clientSecret": "..." }
   ```

## 2. Connect your banks in MeuPluggy

MeuPluggy is Pluggy's free connector for an individual reading their own accounts. Sign in at https://meu.pluggy.ai with your own login and connect each bank or card there; this is the consent step of Open Finance. Banks can also be connected later, from `/admin` (step 6).

## 3. Create the Google OAuth Web client

1. Create a project at https://console.cloud.google.com and open **Google Auth Platform**. Set up Branding and Audience (user type **External**) and publish the app (*In production*) so logins keep working. For personal use an unverified app is fine; Google shows an "unverified app" warning at login.
2. Under **Clients**, create an OAuth client of type **Web application** with two authorized redirect URIs:
   - `<PUBLIC_URL>/auth/callback` (the connector login);
   - `<PUBLIC_URL>/admin/auth/callback` (the `/admin` sign-in).
3. Save its JSON as `secrets/google-oauth.json` (the file Google offers to download, with a `web` object holding `client_id` and `client_secret`).

## 4. Initialize

```bash
mkdir -p data secrets
cp .env.example .env     # set PUBLIC_URL and ALLOWED_EMAILS (your Google login)
# pluggy.json and google-oauth.json must already be in secrets/ (steps 1 and 3)
sudo chown -R 10001:10001 data secrets && sudo chmod 600 secrets/*
docker run --rm -v "$PWD/data:/data" -v "$PWD/secrets:/secrets" \
  ghcr.io/trainedloop/cashbook-mcp:0.1 cashbook-admin init
```

`pluggy.json` and `google-oauth.json` must already be in `secrets/`. `init` generates `secrets/db.key` and `secrets/session.key` when missing, checks the Pluggy credentials with `POST /auth`, and creates or migrates the database. It needs `secrets/` writable (the compose file mounts it read-only for the server), so run it as above, or on a checkout with `uv run cashbook-admin init --secrets ./secrets --db ./data/cashbook.duckdb`. The container user is uid 10001, which is why `data` and `secrets` are chowned to it before `init` runs.

Before the first release is published, build the image locally instead of pulling it: `docker build -t ghcr.io/trainedloop/cashbook-mcp:0.1 .` (compose then uses that tag).

## 5. Start the server

```bash
docker compose up -d
```

For the Tailscale sidecar, see [examples/tailscale-sidecar](../examples/tailscale-sidecar/). Check `curl -s <PUBLIC_URL>/health`.

`cashbook-admin status` shows connections, last syncs and database info, but DuckDB allows one process on the encrypted file: it cannot open the database while the server runs. Use `/admin` while it is up, or stop the server first (`docker compose stop`).

## 6. Add connections in `/admin`

1. Open `<PUBLIC_URL>/admin` and sign in with Google (your login in `ALLOWED_EMAILS`; any other is refused).
2. Choose **Connect with MeuPluggy** and complete the Pluggy Connect widget. The page then lists every bank MeuPluggy shares, with its accounts, and suggests an alias for each (`^[a-z0-9][a-z0-9-]{0,31}$`, for example `inter` or `itau-joint`). Untick the ones you do not want and choose **Add**. Banks already connected show their alias. Or use **Add existing item id** with an alias and an item id from MeuPluggy, check the preview and confirm.
3. The first load starts at once. The status badge moves from `syncing` to `ok`; history reaches back `HISTORY_MONTHS` (12) at first and grows from there.

Disconnecting an alias deletes it and all its stored rows, needs a recent Google sign-in and asks you to type the alias. Reconnecting the same bank starts empty with what Pluggy provides.

## 7. Add the claude.ai custom connector

In claude.ai go to **Settings, Connectors, Add custom connector** and enter `<PUBLIC_URL>/mcp`. Log in with the Google account in `ALLOWED_EMAILS`. Then ask, for example, "How was my spending this month?" Claude starts with `finance_overview`.

## What the tools can and cannot do

- Everything is read-only, at the server and toward Pluggy.
- `query_sql` runs a single `SELECT` or `WITH` statement with a 10 s timeout and a 1,000-row cap. Anything else is refused.
- Text from transactions is written by third parties and is flagged and capped; the assistant is told to treat it as data.

## Backup

The database is one file. Copy `data/cashbook.duckdb` (with the server stopped, or from a snapshot) and store `secrets/db.key` **separately**, for example in a password manager: a copy of the file without the key is unreadable, and the key without the file is useless. Without `db.key` a backup cannot be opened. Keep `session.key`, `pluggy.json` and `google-oauth.json` with your other secrets; they can be recreated, but the key cannot.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `PUBLIC_URL` | required | Public HTTPS URL, no trailing slash |
| `ALLOWED_EMAILS` | required | Comma-separated Google logins allowed in |
| `REFRESH_AT` | `06:00` | Daily full refresh (24h, in `TZ`) |
| `TZ` | `America/Sao_Paulo` | IANA time zone |
| `HISTORY_MONTHS` | `12` | How far back each load reaches |
| `LOG_LEVEL` | `INFO` | Log level |
| `PORT` | `8743` | Port inside the container |
| `ALLOWED_REDIRECT_URIS` | Claude callbacks | MCP client callback allowlist |

## Troubleshooting

- **Startup fails with a config error:** the log names the variable or file. `db.key` must be 64 hex characters.
- **Login loops or `redirect_uri_mismatch`:** the redirect URIs in the Google client must match `PUBLIC_URL` exactly.
- **A connection shows `login error` or `consent revoked`:** reconnect it in MeuPluggy; `/admin` shows the remedy.
