# Example: Tailscale sidecar

One way to publish cashbook-mcp: a `tailscale/tailscale` container joins your tailnet as its own machine (`cashbook`) and serves the app over public HTTPS with Funnel. The app shares the sidecar's network namespace, so **nothing listens on the host** and no firewall unit is needed. Each service gets its own hostname on port 443.

```
claude.ai --HTTPS--> Funnel (:443) --> tailscale container --127.0.0.1:8743--> cashbook (same netns)
```

This is an example, not a requirement; [docs/tutorial.md](../../docs/tutorial.md) lists the generic requirements.

## 1. Enable HTTPS and Funnel

In the Tailscale admin console, enable HTTPS certificates for the tailnet and allow Funnel for the `cashbook` machine (the `funnel` node attribute in the policy).

## 2. Create an auth key

Create a reusable or single-use auth key (Settings, Keys), tagged if you use tags, and save it as `secrets/ts-authkey`:

```bash
umask 077 && printf '%s' 'tskey-auth-...' > secrets/ts-authkey
```

The sidecar reads it through `TS_AUTHKEY_FILE`; the key is only needed for the first start, and node state is kept in the `tailscale-state` volume.

## 3. Configure the URL

The machine's name is `cashbook`, so its address is `https://cashbook.<tailnet>.ts.net` (for example `https://cashbook.owner.example.ts.net`). In `.env` set:

```
PUBLIC_URL=https://cashbook.owner.example.ts.net
TS_CERT_DOMAIN=cashbook.owner.example.ts.net
```

`serve.json` uses `TS_CERT_DOMAIN` to publish Funnel on `:443` and proxy to `http://127.0.0.1:8743`. Register `<PUBLIC_URL>/auth/callback` and `<PUBLIC_URL>/admin/auth/callback` on the Google Web client (tutorial step 3).

## 4. Start

From the repository root:

```bash
docker compose -f docker-compose.yml -f examples/tailscale-sidecar/compose.yml up -d
```

Paths in the override resolve against the first file, so run it from the root. The root compose file publishes no port and this override adds none: `ss -ltn` on the host shows nothing for 8743 or 443.

## Notes

- Both containers restart with `unless-stopped`, and the OAuth state lives in `./data`.
- To shut down: `docker compose down`, then remove the machine in the Tailscale admin console and revoke the auth key.
