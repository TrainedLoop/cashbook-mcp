#!/bin/sh
# Keeps the internet-facing cashbook-mcp sidecar and app away from everything local: the host itself (SSH, a home automation
# server, other services), the LAN and the tailnet. Only outbound internet
# (Pluggy, Google OAuth, Tailscale control and relays) stays open. Idempotent.
#
# SUBNETS (space-separated) must list the subnet of the sidecar's network
# (SUBNET in .env, default 172.31.2.0/24 in ../compose.yml). The private ranges
# below include other services' subnets, so they cannot reach each other.
# Inside the subnet only APP_PORT (PORT in .env, default 8743) is open, for the
# sidecar to reach the app.
set -eu
set -f  # no glob expansion of $SUBNETS

SUBNETS=${SUBNETS:-172.31.2.0/24}
APP_PORT=${APP_PORT:-8743}

add() {
    chain=$1; shift
    iptables -C "$chain" "$@" 2>/dev/null || iptables -I "$chain" "$@"
}

for subnet in $SUBNETS; do
    # Container -> host. -I inserts at the top, so add the DROP before the
    # ACCEPT for replies on connections the host opened.
    add INPUT -s "$subnet" -j DROP
    add INPUT -s "$subnet" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

    # Container -> LAN, other private ranges and the tailnet (100.64.0.0/10).
    for dst in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16; do
        add DOCKER-USER -s "$subnet" -d "$dst" -m conntrack --ctstate NEW -j DROP
    done
    # Inserted last, so it is checked before the DROPs: sidecar -> app.
    add DOCKER-USER -s "$subnet" -d "$subnet" -p tcp --dport "$APP_PORT" -m conntrack --ctstate NEW -j ACCEPT
done
