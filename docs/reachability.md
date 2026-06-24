# Reaching the action buttons without a static IP

The **Acknowledge / Downtime** buttons work by having the phone send the action to *something on
the public internet* that can in turn talk to your Icinga. By default that "something" is the
**broker**, so the broker URL (`https://push.example.com/broker/...`) must be reachable from the
phone wherever it is. You do **not** need a static IP for this — pick whichever tier fits.

## 1. Tunnel — recommended for home / dynamic IP / CGNAT

A tunnel opens an **outbound** connection from your host and gives you a public hostname that
routes back in: no static IP, no port forwarding, works behind CGNAT.

- **Cloudflare Tunnel** (`cloudflared`): create a tunnel, then add public-hostname routes for
  `push.example.com` → `http://localhost:8080` (ntfy) and `push.example.com` path `/broker/*` →
  `http://localhost:8081` (broker). Cloudflare terminates TLS, so you don't run Caddy.
- **Tailscale Funnel**: `tailscale funnel` the ntfy/broker ports to publish them on your
  `*.ts.net` name with TLS.

Then set the dispatcher's `ntfy.base_url` + `broker.base_url` and the ntfy app to that hostname.

## 2. Dynamic DNS + port-forward

If your ISP gives you a routable (even if changing) public IP and you can forward ports: run a
DDNS updater (DuckDNS, Cloudflare DDNS, …) for `push.example.com`, forward 80/443 to the host, and
let Caddy fetch a certificate automatically (`docker compose --profile caddy up -d`). This does
**not** work behind CGNAT (you have no routable IP) — use a tunnel instead.

## 3. No inbound at all — route acks back through ntfy (advanced)

If you can't (or won't) expose anything inbound, the action can ride ntfy itself: the
**Acknowledge** button publishes a small HMAC-signed message to an ntfy *ack topic*, and a tiny
consumer on your side **subscribes** to that topic (an outbound connection, exactly like the
dispatcher) and calls the local Icinga API. Combined with `ntfy.attachment_via: upload` — so the
graph goes into ntfy rather than the broker — this needs **zero** inbound and even works against
the public ntfy.sh; the broker becomes optional. The HMAC signature still authorises every action.

This mode is not wired into the default config yet. Open an issue if you'd like it — it can be
added as an alternative transport to the HTTP broker.
