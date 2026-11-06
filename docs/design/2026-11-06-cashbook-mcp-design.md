# cashbook-mcp

Status: proposed
Date: 2026-11-06

## Context

The owner's bank accounts, credit cards, investments and loans are connected to Pluggy through the MeuPluggy connector, which is Pluggy's free path for an individual reading their own accounts through Brazilian Open Finance. cashbook-mcp is an MCP server that gives Claude read access to that data as a claude.ai custom connector. It reaches Claude on the web, on mobile and in scheduled cloud routines, without the owner's PC switched on.

It follows the shape of mailhub-mcp: one owner per instance, Google login with an email allowlist, a Docker image on ghcr, self-hosted behind Tailscale Funnel. It is a separate public portfolio project. It is unofficial and not affiliated with Pluggy.

### Goals

- The agent reads everything Pluggy exposes for the owner's connections: accounts and balances, bank and card transactions, credit card bills, investments and their transactions, and loans.
- The server prepares data so the agent reasons well. It provides a single-call overview, filtered search, aggregations, cash flow, recurring-charge candidates and read-only SQL. The agent makes every decision. The server provides facts and evidence, never conclusions.
- The server does not count money twice. Credit card bill payments and transfers between the owner's own accounts are classified and excluded from spending by default.
- History accumulates beyond Pluggy's window. Data is persisted, encrypted at rest.
- The owner connects, inspects and disconnects banks from an admin page, with no command line.
- Third-party text, such as PIX messages and merchant names, is treated as untrusted. Suspicious values are flagged individually.

### Non-goals

- Any write to Pluggy or to a bank: no payments, no PIX, no item updates. Pluggy's Payments API is out of scope permanently.
- Identity data (CPF, address, phones, e-mails from `GET /identity`). The agent does not need it, and exposing it only adds risk.
- Other data providers. v1 is Pluggy only. The domain models are the boundary. A provider interface is designed only when a second, real provider exists. OFX/CSV import is the likely candidate.
- Webhooks. They would need an unauthenticated public route, and polling `lastUpdatedAt` covers the need.
- Automatic backups of the database file. Backup is documented, not built.
- Server-side budgets, user categories, tags or notes.
- Migrating mailhub-mcp to the Tailscale sidecar. That is a separate follow-up. This project only ships the pattern.
- A pt-BR interface. The admin UI is English, like mailhub's.

### Threat model

The MCP endpoint and `/admin` are public on the internet through Funnel. The data is the owner's full financial history. The design defends against:

- unauthenticated access: only a verified Google login in `ALLOWED_EMAILS` gets an MCP token or an admin session;
- prompt injection through transaction text written by third parties. Anyone can send the owner a PIX with an arbitrary message;
- agent-written SQL reading files, reaching the network, changing settings, attaching databases, writing data or exhausting memory;
- CSRF, stolen sessions and replayed OAuth callbacks on `/admin`. This reuses mailhub's admin defenses;
- a browser-supplied Pluggy item id that belongs to another application or is not a MeuPluggy item;
- theft of the disk or of a copy of the database file. The file is encrypted, and the key lives outside the data volume.

It does not defend against a shell on the host. That attacker already holds the Pluggy client secret and can read the current window live. Persistence adds two exposures: history older than Pluggy's window, and data that survives revoking the Pluggy credentials. The owner accepted this.

## Pluggy facts this design relies on

These come from docs.pluggy.ai, read 2026-11-06.

- `POST /auth` with `clientId`/`clientSecret` returns an API key that is valid for 2 hours. Requests send it as `X-API-KEY`.
- No item listing is available on Free or Meu Pluggy personal accounts. `GET /v2/items` returns `403 LIST_ITEMS_FEATURE_NOT_ENABLED`. The application must store item ids itself. Pluggy hands them out in the Connect widget's `onSuccess`, or in webhooks.
- The MeuPluggy original item refreshes every 24 hours, on its own schedule per bank. The application's item mirrors it, does not auto-sync, and cannot be `PATCH`ed. There is no on-demand sync.
- Item fields used: `status` (`UPDATING`, `UPDATED`, `OUTDATED`, `LOGIN_ERROR`, `WAITING_USER_INPUT`), `executionStatus`, `error`, `lastUpdatedAt`, `consentExpiresAt`, `connector`.
- Read endpoints:
  - `GET /accounts?itemId`;
  - `GET /v2/transactions?accountId&dateFrom&dateTo&after`, cursor-paginated with 500 per page. The deprecated `GET /transactions` is removed 2026-12-31 and must not be used;
  - `GET /bills?accountId`;
  - `GET /investments?itemId`;
  - `GET /investments/{id}/transactions`;
  - `GET /loans?itemId`.
- Rate limits are per IP, per endpoint, per minute: 360 for auth, accounts, transactions and investments. A 429 response carries `Retry-After`.
- Connect tokens (`POST /connect_token`) are valid for 30 minutes.

To verify against real data during implementation:

- the sign convention for credit card transaction amounts;
- whether renewing consent or fixing a login error in MeuPluggy keeps the application's item id;
- the exact domains the Connect widget needs in the CSP;
- the minimum DuckDB version that provides database encryption, expected to be 1.4.

## Architecture

One Python 3.13 process, FastMCP. Package layout:

```
src/cashbook_mcp/
  server.py        FastMCP app, tool registration, audit log, entry point (cashbook-mcp)
  config.py        Settings.from_env() and validation
  auth.py          Google OAuth for the MCP connector, email allowlist
  cli.py           cashbook-admin: init, status
  prompts.py       MCP prompt texts
  models.py        Domain models: Connection, Account, Transaction, Bill, Investment,
                   InvestmentTransaction, Loan (provider-neutral)
  pluggy/
    client.py      HTTP client: API key cache/renewal, pagination, 429/Retry-After, bounded concurrency
    normalize.py   Pluggy JSON -> domain models (sign convention, kind classification)
  store/
    db.py          Encrypted DuckDB file: open, lock down, connections for writer and reader
    migrations/    Numbered SQL migrations + runner (schema_version, pre-migration backup)
    sync.py        Per-connection load and merge, scheduling (startup, daily, hourly check)
  content.py       clean_text, suspicion heuristics, in-value markers (ported from mailhub)
  analysis/
    overview.py    finance_overview
    summarize.py   summarize_transactions, cash_flow
    recurring.py   find_recurring
  sql.py           query_sql sandbox: statement check, limits, result shaping
  admin_web/       /admin: session, identity, CSRF (ported from mailhub), JSON API, static SPA serving
web/               React + TypeScript + Vite admin SPA (built into the image)
```

Ported from mailhub, then adapted rather than shared as a library:

- the session, CSRF, identity and re-authentication code;
- `clean_text` and the suspicion heuristics;
- security headers and the audit log pattern.

## Data model and normalization

### Tables

The tables live in the encrypted DuckDB file.

- **`connections`:**
  - `alias` (primary key, lowercase slug);
  - `item_id` (unique), `connector_name`;
  - `created_at`;
  - last item status fields;
  - `last_synced_at`, `last_sync_error`.
- **Data tables:** `accounts`, `transactions`, `bills`, `investments`, `investment_transactions` and `loans`.
  - Each is keyed by the Pluggy id and carries `alias`, `first_seen_at`, `last_seen_at` and `removed_at` (nullable).
  - `transactions` adds `amount` (signed), `currency`, `date`, `description`, `counterpart_name`, `merchant_name`, `pluggy_category`, `kind`, `kind_reason`, `text_suspicious` and `suspicion_reasons`.
- **`sync_runs`:** one row per load, with alias, start, end, outcome, row counts and error.
- **`schema_version`.**

### Views

The views are what the tools and SQL users are pointed at.

- **`v_transactions`:** `transactions` minus removed rows.
- **`v_spending`:** `v_transactions` minus `kind` in (`internal_transfer`, `card_bill_payment`, `investment`), with outflows only.
- Similar views exclude removed rows from the other tables.

### Sign convention

Negative means money left the owner. This holds for bank and card accounts alike, and normalization flips Pluggy's card sign if needed.

### Kind classification

`kind` is one of `purchase`, `income`, `internal_transfer`, `card_bill_payment`, `refund`, `investment`, `fee`, `loan`, `other`. Each row carries `kind_reason`, a short machine-readable explanation, for example `matched_card_bill:<bill_id>` or `counterpart_is_own_account:<alias>`.

The rules are deterministic and run after all connections load. They match:

- transfers between the owner's own accounts, by account number or tax id on both sides;
- card bill payments, by amount and date against `bills`;
- Pluggy's own category hints.

A wrong classification is visible and explainable, and the agent can override it in SQL.

### Untrusted text

Text columns are `description`, `counterpart_name` and `merchant_name`. They are cleaned at load (`clean_text`, length cap), and any marker-like sequences are removed from the source text.

If the heuristics flag a value, the stored value is wrapped as `⟦untrusted:suspicious⟧…⟦/untrusted⟧`, and `text_suspicious` and `suspicion_reasons` are set. Because the marker lives in the value, it survives aliasing, functions and concatenation in agent SQL.

## Storage and sync

- **The database file:**
  - one DuckDB file at `data/cashbook.duckdb`, encrypted with DuckDB's native database encryption;
  - the key is `secrets/db.key`, generated by `cashbook-admin init` and never stored in `data/`;
  - the server refuses to start if the key is missing or wrong.
- **Startup:**
  - run migrations;
  - serve existing data immediately;
  - start a background load of all connections.
  - Tools report "loading" only while the database has never completed a load.
- **Daily refresh:** a full load at `REFRESH_AT` (default `06:00`, timezone `TZ`, default `America/Sao_Paulo`).
- **Hourly check:**
  - `GET /items/{id}` per connection;
  - if `lastUpdatedAt` advanced, load that connection;
  - the status fields are updated either way.
- **Manual reload:** from `/admin` only, re-reading Pluggy. It cannot force a bank sync.
- **Load window:** `HISTORY_MONTHS` (default 12) back from today, or whatever Pluggy returns within it.
- **Merge, per connection, in one transaction:**
  - upsert every row Pluggy returned, setting `last_seen_at`;
  - rows inside the fetched date range that Pluggy no longer returns get `removed_at`;
  - rows older than the fetched range are untouched. This is the accumulated history.
  - On any error the transaction rolls back, the previous data stays, and the error is recorded on the connection and in `sync_runs`.
- **Isolation:** one connection failing never blocks the others. Every tool response carries `data_as_of` per connection and any connection error.
- **Disconnect:** deletes the connection and every row with its alias, in one transaction. Reconnecting the same bank starts empty with whatever Pluggy provides. There is no history carry-over between items.
- **Migrations:**
  - numbered SQL files, applied at startup in order;
  - before applying, the file is copied to `cashbook.duckdb.bak-<from_version>`;
  - a failed migration stops startup and leaves the original file intact.
- **Pluggy client:**
  - the API key is cached and renewed before expiry;
  - at most 4 concurrent requests;
  - 429 responses honor `Retry-After`;
  - short timeouts, bounded retries on 5xx and network errors.
- **DuckDB limits:** `memory_limit` 256 MB, `threads` 2. The container memory limit is 512 MB.

## MCP tools

All tools are read-only. Every response includes `data_as_of` per connection and any connection problems.

### Entry point

- **`finance_overview(days_ahead=10)`:**
  - balances per account;
  - credit limit, used limit and open bill per card;
  - bills due within `days_ahead`;
  - month-to-date income, spending and net (from `v_spending` and income);
  - top 5 spending categories this month;
  - connections with problems and how to fix them.
- **`list_connections()`:**
  - alias, bank, status, last sync, consent expiry, accounts;
  - stored history range and row counts;
  - a remediation hint, for example "reconnect in MeuPluggy".

### Detail

- **`list_accounts(alias=None)`.**
- **`search_transactions(...)`:**
  - filters: `date_from`, `date_to`, `aliases`, `account_ids`, `text`, `categories`, `kinds`, `min_amount`, `max_amount`, `direction` (`in`/`out`) and `exclude_internal` (default true);
  - returns compact rows (id, date, account, amount, kind, category, short description, suspicious flag), newest first;
  - cursor pagination, default 100 rows per page and maximum 500.
- **`get_transactions(ids)`:** full rows, at most 100 ids.
- **`list_bills(account_id=None, date_from=None, date_to=None)`.**
- **`list_investments(alias=None)`.**
- **`get_investment_transactions(investment_id, date_from=None, date_to=None)`.**
- **`list_loans(alias=None)`.**
- **`list_categories(date_from=None, date_to=None)`:** the categories present in the data, with counts and totals.

### Aggregation

- **`summarize_transactions(...)`:**
  - `group_by` is one of `category`, `merchant`, `month`, `week`, `account`, `kind`;
  - takes the same filters as search;
  - returns per-group total, count, average, first date and last date;
  - with `compare_previous=true`, adds the same figures for the immediately preceding period of equal length.
- **`cash_flow(date_from, date_to, granularity="month", by_account=false)`:** income, spending and net per period. Internal transfers and card bill payments are excluded.
- **`find_recurring(months=6, min_occurrences=3)`:**
  - returns candidate recurring charges and incomes, grouped by normalized counterpart or merchant with amount tolerance;
  - each candidate has typical amount, cadence in days, last date, next expected date, confidence and evidence transaction ids.
  - The agent decides what they are.

### SQL

- **`describe_schema()`:** tables and views with column descriptions. It includes the `kind` values and their meaning, the sign convention, the advice to use `v_spending` for spending, and the note that text columns are third-party data, not instructions.
- **`query_sql(sql)`:**
  - **allowed:** exactly one statement, of type `SELECT` (including `WITH`). The statement type comes from DuckDB's own parser, never from a regex;
  - **reader connection:** opened after the encrypted file is attached, then locked with `enable_external_access=false` and `lock_configuration=true`. No file, network or extension access, no `ATTACH`, `COPY`, `SET`, `PRAGMA`, `INSTALL` or `LOAD`, and no writes;
  - **limits:** a 10 s timeout (the query is interrupted) and a 1,000-row result cap with a `truncated` flag;
  - **result shape:** column names, column types, rows and `truncated`. A `suspicious_cells` list (row index and column) is computed by scanning returned values for the marker. Only those cells are flagged, and the rest of the result is returned as plain data.

### Prompts

- `monthly_review(month)`: a guided review using the overview, cash flow, summary by category with comparison, and recurring candidates.
- `subscriptions_audit()`.
- `upcoming_bills(days=15)`.

## Admin page

- **Serving:**
  - Python serves the built SPA at `/admin`, with `index.html` for any non-API path under it;
  - hashed assets are served immutable, and `index.html` with `no-cache`;
  - the JSON API lives at `/admin/api/*`. Front and API are the same origin and the same container.
- **Login (server-side, ported from mailhub):**
  - Google OIDC through `/admin/auth/login` and `/admin/auth/callback`;
  - a signed `HttpOnly`, `Secure`, `SameSite=Strict` session cookie on `Path=/admin`, with the `ALLOWED_EMAILS` allowlist;
  - `GET /admin/api/session` returns the signed-in email and a CSRF token;
  - mutating API calls require `X-CSRF-Token` and a matching `Origin`;
  - disconnect requires recent re-authentication. Without it the API returns `403 {"error": "reauth_required"}`, and the SPA sends the owner through login and back to the same screen.
- **API:**
  - `GET /admin/api/connections`;
  - `POST /admin/api/connect-token` (`{alias}`);
  - `POST /admin/api/connections/preview` (`{item_id}`);
  - `POST /admin/api/connections` (`{alias, item_id}`);
  - `POST /admin/api/connections/{alias}/reload`, `POST /admin/api/reload`;
  - `GET /admin/api/connections/{alias}/disconnect-impact`;
  - `DELETE /admin/api/connections/{alias}`;
  - `GET /admin/api/server`.
- **Item validation (server-side, never trusting the browser):**
  - `GET /items/{id}` with the server's API key must succeed, which proves the item belongs to this application;
  - the connector must be MeuPluggy;
  - the item id must not already be stored;
  - the alias must match `^[a-z0-9][a-z0-9-]{0,31}$` and be unused.
  - Success stores the connection and starts its first load.
- **Screens:**
  1. **Sign in:** "Continue with Google".
  2. **Connections:**
     - per alias: bank, status badge (`ok`, `outdated`, `login error`, `consent revoked`, `syncing`) with remediation text, last sync, stored history range and row count, and accounts;
     - actions: "Reload now" per connection and for all, and "Disconnect".
  3. **Connect with MeuPluggy:**
     - the owner enters an alias, which is validated first, and the server issues a connect token with `options.avoidDuplicates`;
     - `react-pluggy-connect` opens restricted to the MeuPluggy connector;
     - `onSuccess` sends only the item id to `POST /admin/api/connections`.
  4. **Add existing item id:** alias + item id, then a preview (bank and accounts), then confirm.
  5. **Disconnect:**
     - shows what is irrecoverable, for example "1,240 transactions before 2026-03 are no longer provided by Pluggy and will be lost";
     - the owner confirms by typing the alias, with re-authentication.
  6. **Server footer:** last full refresh, next scheduled refresh, database size, schema version, app version.
- **Front stack:**
  - React, TypeScript, Vite and `react-pluggy-connect`;
  - a small `fetch` hook, no data library and no router;
  - hand-written CSS with color tokens, light and dark themes, usable on a phone.
- **Headers:** the same security headers as mailhub. The CSP allows only `'self'` plus the Pluggy Connect script, frame and API origins. No inline script.

## Deployment

- **Dockerfile:**
  - stage 1 builds `web/` with Node 22;
  - stage 2 is `python:3.13-slim` with uv and the built assets copied in. Node is not in the runtime image.
- **Image:** `ghcr.io/trainedloop/cashbook-mcp`, linux/amd64 and linux/arm64. `release.yml` publishes on a `vX.Y.Z` tag that matches `pyproject.toml`.
- **`docker-compose.yml`:**
  - the app with volumes `data/` (read-write) and `secrets/` (read-only), and a 512 MB memory limit;
  - a health check on `/health`;
  - no published port by default.
- **`examples/tailscale-sidecar/`:**
  - a `tailscale/tailscale` container joins the tailnet as its own machine, with its own `*.ts.net` hostname;
  - the app uses `network_mode: service:tailscale`;
  - a serve config publishes Funnel on 443 to the app;
  - the auth key goes in `secrets/`;
  - nothing listens on the host. This replaces mailhub's loopback port + firewall unit pattern, and gives each service its own hostname on 443 instead of competing for Funnel's three ports.
- **Secrets:**
  - `secrets/pluggy.json` (`clientId`, `clientSecret`);
  - `secrets/db.key`;
  - `secrets/google-oauth.json`: a new Google Web OAuth client, used for the MCP connector and `/admin` login;
  - the Tailscale auth key, in the example only.
- **`cashbook-admin`:**
  - `init` generates `db.key`, checks the Pluggy credentials with `POST /auth`, and creates or migrates the database;
  - `status` shows connections, last syncs, errors and database info.
- **Configuration:**
  - `ALLOWED_EMAILS`;
  - `PUBLIC_URL`;
  - `REFRESH_AT`, `TZ`;
  - `HISTORY_MONTHS`;
  - `LOG_LEVEL`.

## Repository

- **Hosting:** public at `TrainedLoop/cashbook-mcp`, MIT license. The owner creates the GitHub repository.
- **Language:** code, comments, commits and docs in English.
- **README:** what it does, the line "unofficial, not affiliated with Pluggy", setup with MeuPluggy, and the tool list.
- **Other docs:** `docs/tutorial.md`, `docs/architecture.md`, `SECURITY.md`, `ROADMAP.md`, and a project site on GitHub Pages like mailhub's.
- **CI:** ruff, pytest, Vitest, a front build and a Docker build. Dependabot covers pip, npm, Actions and Docker. Private vulnerability reporting is on.
- **No personal data in the repo:** no tailnet names, host details, real transactions or real item ids. Fixtures are synthetic.

## Testing

- **Pluggy client:** recorded-shape synthetic JSON fixtures through a mock HTTP transport. Covers pagination, API key renewal, 429 with `Retry-After`, 5xx retries and timeouts.
- **Normalization:** the sign convention for bank and card, and every `kind` rule with positive and negative cases (own-account PIX, card bill payment matched to a bill, refund, fee).
- **Aggregations:** golden tests on a synthetic dataset. The dataset must include a card bill payment plus the card purchases it pays, and an internal transfer, and asserts spending counts each purchase once.
- **Sync and merge,** on a temporary encrypted database:
  - upsert by Pluggy id;
  - `removed_at` inside the window, rows older than the window untouched;
  - rollback on mid-load failure;
  - one connection's failure leaves the others loaded;
  - disconnect removes every row of the alias.
- **Migrations:** each migration is applied to a database at the previous version. The pre-migration backup exists, and a failing migration leaves the original intact.
- **SQL sandbox:**
  - rejection of `read_csv`/`read_parquet`/file paths, `ATTACH`, `COPY`, `SET`/`RESET`, `PRAGMA`, `INSTALL`/`LOAD`, `INSERT`/`UPDATE`/`DELETE`/`CREATE`/`DROP`, and multiple statements;
  - the timeout interrupts a long query;
  - the row cap sets `truncated`.
- **Injection:**
  - the marker survives `AS`, `upper()` and `||`;
  - forged marker sequences in source text are stripped;
  - `suspicious_cells` lists exactly the flagged cells.
- **Admin API:**
  - unauthenticated requests are rejected;
  - a missing or incorrect CSRF token is rejected, and so is a bad `Origin`;
  - `reauth_required` on disconnect;
  - item validation rejects unknown items, non-MeuPluggy items, duplicates and bad aliases.
- **Front:** Vitest + Testing Library for the connect flow (token → widget success → POST) and the disconnect modal (impact shown, alias confirmation, reauth redirect).
- **Manual, on the real host:**
  - the card sign convention;
  - item id stability after renewing consent in MeuPluggy;
  - the CSP domains for the widget;
  - a first full load and an hourly check picking up a bank's daily sync;
  - the claude.ai connector end-to-end, on web and mobile.

## Follow-ups (not in this project)

- Move mailhub-mcp to the Tailscale sidecar pattern.
- An OFX/CSV import provider, if a real need appears. This is the point to introduce a provider interface.
- Optional history carry-over when an alias is reconnected to a new item.
