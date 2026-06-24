# Reaching the action buttons without a static IP

The **Acknowledge / Downtime** buttons work by having the phone send the action to *something on
the public internet* that can in turn talk to your Icinga. By default that "something" is the
**broker**, so the broker URL (`https://push.example.com/broker/...`) must be reachable from the
phone wherever it is. You do **not** need a static IP for this — pick whichever tier fits.

> **Shortcut:** if you don't want to expose *anything*, jump to **option 3 (relay)**. Combined with
> the public ntfy.sh it needs zero inbound and no `server/` stack or Caddy at all — your Icinga box
> only makes outbound connections. Options 1–2 below are for when you self-host ntfy / the broker
> and need to expose them.

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

If you can't (or won't) expose anything inbound, the action can ride ntfy itself: the **Acknowledge
/ Downtime** buttons publish a small HMAC-signed message to an ntfy *ack topic*, and `relay.py` on
your side **subscribes** to that topic (an outbound connection, exactly like the dispatcher) and
calls the local Icinga API. Combined with `ntfy.attachment_via: upload` — so the graph goes into
ntfy rather than the broker — this needs **zero** inbound and even works against the public
ntfy.sh; the broker stack becomes optional. The HMAC signature still authorises every action.

### Enable it

1. **Pick a reachable ntfy.** Your own server behind a tunnel, or just `https://ntfy.sh`. The only
   thing that must be reachable is ntfy (the phone talks to it for everything anyway) — nothing of
   yours is exposed.
2. **Create an ack topic + tokens.** Choose a hard-to-guess topic name (e.g. `icinga-acks-7f3a`).
   Issue an ntfy token with **write** on it (embedded in the button) and one with **read** on it
   (the relay subscribes).
3. **Configure the dispatcher** (`config.yml`):
   ```yaml
   ntfy:
     attachment_via: "upload"     # graph goes into ntfy, not the broker
   actions:
     transport: "relay"
     ack_topic: "icinga-acks-7f3a"
     ack_write_token: "${NTFY_ACK_WRITE_TOKEN}"
   relay:
     ack_read_token: "${NTFY_ACK_READ_TOKEN}"
     icinga_api_url: "https://localhost:5665"
     icinga_api_user: "ntfy-relay"
     icinga_api_password: "${ICINGA_API_PASSWORD}"
   ```
   `broker.shared_secret` is still the HMAC secret (both transports use it). Use a **scoped** Icinga
   ApiUser limited to `actions/acknowledge-problem` + `actions/schedule-downtime`.
4. **Run the relay** next to the dispatcher — `dispatcher/relay.py`, either directly
   (`/opt/ntfy-icinga/venv/bin/python3 .../relay.py`) or via the bundled systemd unit
   (`dispatcher/relay.service.example` → `systemctl enable --now ntfy-icinga-relay`).

Now tapping **Acknowledge** publishes a signed message the relay applies to Icinga within a second
— no broker, no inbound, no static IP. (The "Open in Icinga" deep link still needs Icinga Web
reachable from the phone, so it only resolves if you've exposed that; otherwise ignore that button.)
