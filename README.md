# cashbook-mcp

An MCP server, read-only toward your banks, that syncs your bank accounts, cards, investments and loans from [Pluggy](https://www.pluggy.ai) (through the free MeuPluggy connector, Brazilian Open Finance) into a local encrypted DuckDB file, and lets an MCP client such as Claude query them. It runs as a claude.ai custom connector, so it works on the web, on mobile and in scheduled routines without your PC switched on.

**Unofficial. Not affiliated with or endorsed by Pluggy.**

## What it does

- Pulls accounts, transactions, card bills, investments and loans per connected bank, daily at `REFRESH_AT` and whenever Pluggy reports fresh data.
- Keeps the history it has seen in one DuckDB file encrypted with a key that never sits next to the data. Pluggy only provides a limited window; yours keeps growing.
- Classifies transactions (spending, income, internal transfers, card bill payments) so totals are not double counted, with personal, business and consolidated scopes for owners who run a company.
- Tracks net worth from daily balance snapshots, forecasts cash for the next 30 to 90 days and reports changes in recurring charges.
- Lets the assistant fix a classification after asking you: journaled, capped and reversible.
- Offers an owner page at `/admin` to connect banks with MeuPluggy, set each connection's role and each account's purpose, reload and disconnect.
- One owner per instance: only Google logins in `ALLOWED_EMAILS` get a token or an admin session.

## Tools

Every response carries `data_as_of` per connection and any connection problem.

| Tool | Purpose |
| --- | --- |
| `finance_overview` | Start here: balances, cards, upcoming bills, month to date, business block, net worth, forecast, connection problems |
| `list_connections`, `list_accounts` | Connected banks and their accounts |
| `search_transactions`, `get_transactions` | Filtered, paginated transaction search; full rows by id |
| `list_bills` | Card bills |
| `list_investments`, `get_investment_transactions` | Investments and their movements |
| `list_loans` | Loans and installments |
| `list_categories` | Categories present in the data |
| `summarize_transactions`, `cash_flow` | Aggregations by scope (`personal`, `business`, `consolidated`), by one or two dimensions |
| `net_worth`, `cash_forecast` | Net worth over time; projected cash balance for the next 30 to 90 days |
| `find_recurring`, `recurring_changes` | Recurring-charge candidates; price increases, new, stopped and duplicate charges |
| `review_classification` | Queue of transactions the server is unsure about, with hints |
| `reclassify_transactions`, `undo_reclassification`, `list_reclassifications` | The one local write path: change a transaction's kind, undo a batch, list batches |
| `describe_schema`, `query_sql` | The schema, and `SELECT`/`WITH`-only SQL over it |

All tools are read-only except `reclassify_transactions` and `undo_reclassification`, which change local labels only.

Prompts: `monthly_review`, `subscriptions_audit`, `upcoming_bills`, `review_classifications`, `weekly_review`.

## Security model, in short

- The server never changes anything at Pluggy. Its only non-GET calls are `POST /auth` and `POST /connect_token`.
- Locally there is one write path: the assistant can set the kind of specific transactions. Every change is journaled, reversible and capped per day.
- Only a verified Google login listed in `ALLOWED_EMAILS` can use the connector or `/admin`.
- `query_sql` allows one `SELECT`/`WITH` statement, parsed by DuckDB, on a locked-down reader connection: no files, network, extensions, `ATTACH` or writes. 10 s timeout, 1,000 rows.
- Transaction text is written by third parties (anyone can send you a PIX with a message). It is cleaned, capped and wrapped in untrusted markers when suspicious, and the tool descriptions say it is data, not instructions.
- The database file is encrypted; the key lives in `secrets/db.key`, outside the data volume.
- It does not defend against a shell on the host. See [SECURITY.md](SECURITY.md) and [docs/architecture.md](docs/architecture.md).

## Quick start

You need Docker, an HTTPS address for the server, a free Pluggy account and a Google OAuth Web client. The full walk-through is in the **[tutorial](docs/tutorial.md)**. In short:

```bash
mkdir -p data secrets && cp .env.example .env   # edit PUBLIC_URL and ALLOWED_EMAILS
# put pluggy.json and google-oauth.json in secrets/, then:
sudo chown -R 10001:10001 data secrets
docker run --rm -v "$PWD/data:/data" -v "$PWD/secrets:/secrets" \
  ghcr.io/trainedloop/cashbook-mcp:0.2 cashbook-admin init
docker compose up -d
```

> **Before the first release** the image is not published yet. Build it from this checkout with
> `docker build -t cashbook-mcp:dev .`, use `cashbook-mcp:dev` in place of `ghcr.io/trainedloop/cashbook-mcp:0.2`
> above, and point `image:` in `docker-compose.yml` at it.

Upgrading from 0.1 recreates the store; see "Upgrading from 0.1" in the [tutorial](docs/tutorial.md).

[examples/tailscale-sidecar/](examples/tailscale-sidecar/) shows a deployment with a Tailscale sidecar: its own hostname on port 443, no port published on the host, and a firewall unit that isolates the container. [ROADMAP.md](ROADMAP.md) lists planned work.

## Development

```bash
uv sync
uv run pytest -q && uv run ruff check .
npm --prefix web ci && npm --prefix web test -- --run   # admin SPA
npm --prefix web run build                              # writes src/cashbook_mcp/admin_web/static
```

MIT licensed. See [LICENSE](LICENSE).
