# Example: Tailscale sidecar

This example publishes cashbook-mcp in one way. A `tailscale/tailscale` container joins your tailnet as its own machine (`cashbook`), and serves the app over public HTTPS with Funnel, proxying to it over their Docker network. No port is published on the host. Each service gets its own hostname on port 443.

```
claude.ai --HTTPS--> Funnel (:443) on the sidecar --cashbook:8743 on the named network--> cashbook
```

The container is still reachable from the internet. Two things keep it contained:

- **The firewall unit in [firewall/](firewall/).** The sidecar sits on a named network with a fixed subnet (`172.31.2.0/24`), and the firewall keeps that subnet away from the host, the LAN and the tailnet. Inside the subnet it only opens the app's port (`APP_PORT`, default 8743) for the sidecar.
- **Userspace mode.** Tailscale runs in userspace mode, so the app has no route into the tailnet.

This is an example, not a requirement. [docs/tutorial.md](../../docs/tutorial.md) lists the generic requirements.

## 1. Tailnet policy

In the Tailscale admin console:

1. Enable HTTPS certificates for the tailnet.
2. Open **Access controls** and then **JSON editor**.
3. Add a tag owner and allow Funnel for the tag:

```json
"tagOwners": { "tag:cashbook": ["autogroup:admin"] },
"nodeAttrs": [ { "target": ["tag:cashbook"], "attr": ["funnel"] } ]
```

Keep any existing `nodeAttrs` entries; add this one to the list. Tagged machines are not `autogroup:member`, so the tag needs its own `funnel` entry.

## 2. Auth key

1. Go to **Settings**, then **Keys**, and generate an auth key with these settings:
   - **Tags** on, with `tag:cashbook`;
   - **Reusable** off;
   - **Ephemeral** off.
2. Save the key on the host, readable only by root:

```bash
sudo install -m 600 -o root -g root /dev/stdin secrets/ts-authkey <<< 'tskey-auth-...'
```

The sidecar reads it with `TS_AUTHKEY=file:...`. The key is only needed on the first start, because the node identity is kept in the `cashbook-mcp-tailscale` volume. The app container cannot read the file: it is owned by root, and the app runs as uid 10001.

## 3. URL

The machine name comes from `TS_HOSTNAME` (default `cashbook`), so the address is `https://cashbook.<tailnet>.ts.net`, for example `https://cashbook.owner.example.ts.net`.

1. Set `PUBLIC_URL` to it in `.env`.
2. Register `<PUBLIC_URL>/auth/callback` and `<PUBLIC_URL>/admin/auth/callback` on the Google Web client (tutorial step 3).

The sidecar fills in the certificate domain in `serve.json` itself.

## 4. Start and isolate

From the repository root:

```bash
docker compose -f docker-compose.yml -f examples/tailscale-sidecar/compose.yml up -d
sudo cp examples/tailscale-sidecar/firewall/cashbook-mcp-firewall.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cashbook-mcp-firewall.service
curl -sf https://cashbook.<tailnet>.ts.net/health
```

The first request can take a few seconds while the certificate is issued (`docker logs cashbook-mcp-tailscale` shows `got cert`). Adjust the path in the `.service` file if you installed somewhere other than `/opt/cashbook-mcp`.

Verify the isolation. The first three connections must time out, and the last one must print `ok`. Replace the placeholders with the host's tailnet IP and your LAN router's address:

```bash
docker exec cashbook-mcp-cashbook-1 python -c "import socket; socket.create_connection(('172.31.2.1', 22), 3)"
docker exec cashbook-mcp-cashbook-1 python -c "import socket; socket.create_connection(('<host-tailnet-ip>', 22), 3)"
docker exec cashbook-mcp-cashbook-1 python -c "import socket; socket.create_connection(('<lan-router-ip>', 80), 3)"
docker exec cashbook-mcp-cashbook-1 python -c "import socket; socket.create_connection(('api.pluggy.ai', 443), 3); print('ok')"
```

(`docker compose ps` shows the app container's name.)

## Notes

- **Restarts:** both containers restart with `unless-stopped`, and they don't share a network namespace, so a restart of one doesn't cut the other off. The OAuth state and the database live in `./data`.
- **Moving from the shared-namespace version:** earlier versions of this example ran the app inside the sidecar's network namespace, so a sidecar restart left the app behind and Funnel answered 502. After `git pull`, rerun the firewall (`sudo systemctl restart cashbook-mcp-firewall`) and then `docker compose ... up -d --force-recreate`. The sidecar must be recreated too: it mounts `serve.json` as a single file, and `git pull` replaces that file, so a running sidecar keeps reading the old one.
- **Other services on the same host:** each one gets its own sidecar, hostname and subnet. If they share one firewall unit, list every subnet in `SUBNETS`.
- **Shutting down:** run `docker compose down`, disable the firewall unit, then remove the machine in the Tailscale admin console.
