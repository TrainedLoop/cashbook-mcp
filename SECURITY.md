# Security policy

cashbook-mcp gives an AI assistant read access to a person's full financial history, and one narrow local write path (labels only), so security reports are taken seriously.
This is a personal open-source project: responses are best effort, with no guaranteed timeline.

## Supported version

Only the latest code on `main` is supported. Please reproduce the problem there before reporting.

## Reporting a vulnerability

Report privately. Do not open a public issue or pull request for a vulnerability.

Use the repository's **Security** tab, then **Report a vulnerability** (GitHub private vulnerability reporting / Security Advisories).

## In scope

- `query_sql` sandbox escapes: reading files, reaching the network, attaching databases, changing settings, writing data or exhausting memory
- The reclassification write path: changing anything other than a transaction's kind, skipping the journal, the batch limit or the daily cap, or an undo that does not restore the previous state
- Prompt-injection bypasses: transaction text that makes the assistant act against the owner's wishes; sanitizer or untrusted-marker bypasses
- Authentication and allowlist flaws (`ALLOWED_EMAILS`, token handling, Google OAuth, admin sessions, CSRF, re-authentication)
- Item validation bypasses on `/admin` (adding a Pluggy item that is not the owner's or not a MeuPluggy item)
- Any request to Pluggy other than `GET` calls, `POST /auth` and `POST /connect_token`
- Exposure of financial data, keys or credentials to anyone other than the owner

## Out of scope

- Problems that require access to the host, the `.env` file or the `secrets/` folder
- Issues in third-party services (Pluggy, Google, Tailscale) or dependencies without a way to exploit them here
- Running the server without HTTPS or with an allowlist that includes other people

## What to include

- What you found and its impact
- Steps or a minimal proof of concept (a failing test is ideal)
- The commit you tested against and your configuration

Please do not include real financial data or credentials in a report.

## What to expect

I will acknowledge the report when I can, assess it, fix it on `main` and credit you if you wish.
Please allow reasonable time for a fix before disclosing publicly.
