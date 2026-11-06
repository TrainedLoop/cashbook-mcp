# cashbook-mcp

A read-only MCP server that syncs your bank accounts, cards, investments and loans from [Pluggy](https://www.pluggy.ai) (through the free MeuPluggy connector, Brazilian Open Finance) into a local encrypted DuckDB file, and lets an MCP client such as Claude query them. It runs as a claude.ai custom connector, so it works on the web, on mobile and in scheduled routines without your PC switched on.

**Unofficial. Not affiliated with or endorsed by Pluggy.**

## What it does

- Pulls accounts, transactions, card bills, investments and loans per connected bank, daily at `REFRESH_AT` and whenever Pluggy reports fresh data.
- Keeps the history it has seen in one DuckDB file encrypted with a key that never sits next to the data. Pluggy only provides a limited window; yours keeps growing.
- Classifies transactions (spending, income, internal transfers, card bill payments) so totals are not double counted.
- Offers an owner page at `/admin` to connect banks with MeuPluggy, reload and disconnect.
- One owner per instance: only Google logins in `ALLOWED_EMAILS` get a token or an admin session.

## Tools

All tools are read-only. Every response carries `data_as_of` per connection and any connection problem.

| Tool | Purpose |
| --- | --- |
| `finance_overview` | Start here: balances, cards, upcoming bills, month to date, top categories, connection problems |
| `list_connections`, `list_accounts` | Connected banks and their accounts |
| `search_transactions`, `get_transactions` | Filtered, paginated transaction search; full rows by id |
| `list_bills` | Card bills |
| `list_investments`, `get_investment_transactions` | Investments and their movements |
| `list_loans` | Loans and installments |
| `list_categories` | Categories present in the data |
| `summarize_transactions`, `cash_flow`, `find_recurring` | Aggregations and recurring-charge candidates |
| `describe_schema`, `query_sql` | The schema, and `SELECT`/`WITH`-only SQL over it |

Prompts: `monthly_review`, `subscriptions_audit`, `upcoming_bills`.

## Security model, in short

- The server never changes anything at Pluggy. Its only non-GET calls are `POST /auth` and `POST /connect_token`.
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
docker compose run --rm cashbook cashbook-admin init
docker compose up -d
```

[examples/tailscale-sidecar/](examples/tailscale-sidecar/) shows a deployment where nothing listens on the host. [ROADMAP.md](ROADMAP.md) lists planned work.

## Development

```bash
uv sync
uv run pytest -q && uv run ruff check .
npm --prefix web ci && npm --prefix web test -- --run   # admin SPA
npm --prefix web run build                              # writes src/cashbook_mcp/admin_web/static
```

MIT licensed. See [LICENSE](LICENSE).
