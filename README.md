# ntfy-icinga-rich-alerts

Rich, self-hosted push notifications for [Icinga 2](https://icinga.com/) via
[ntfy](https://ntfy.sh/) — with a lock-screen performance graph, **Acknowledge / Downtime**
action buttons, and HA-safe deduplication. No SaaS, no per-seat pricing, you own the data.

## Why

Most "real" alerting that reaches a phone routes through a third-party SaaS. This project gives
you the same glanceable, actionable experience on infrastructure you run yourself:

- **Self-hosted, no SaaS** — your own ntfy server; alerts and acknowledgements never leave your
  control. (iOS instant-push optionally piggybacks the public ntfy.sh APNs relay, which only ever
  sees a wake-up hash of the topic, not your message.)
- **Rich push** — the notification carries a **performance graph** rendered for the failing
  metric, **Acknowledge** and **Downtime 1h** buttons that act on Icinga right from the lock
  screen, and a deep link straight into Icinga Web.
- **HA-safe dedup** — a fixed-cooldown suppression model keyed per `host!service`, optionally
  shared across multiple Icinga masters via Redis, so an HA pair doesn't double-alert.

## Features

- One notification per problem, then quiet for a configurable cooldown — with **severity
  break-through** (e.g. `WARNING -> CRITICAL`, or a fresh problem after recovery) sending one
  extra alert.
- **State → priority mapping** (`CRITICAL/DOWN -> urgent`, `WARNING -> high`, …) so severe
  problems break through Do-Not-Disturb.
- **Transparent single-metric graph** that blends into the phone's light/dark theme, large fonts
  for a small screen, and a **negative-axis flip** so an all-negative metric (e.g. a −48 V
  battery) reads as a *down* trend when its magnitude drops.
- **Acknowledge / Downtime buttons** that POST to a small broker (HMAC-signed action tokens) which
  calls the Icinga API behind a **scoped** API user — the Icinga credentials never touch the phone.
- **IcingaDB Web deep link** ("Open in Icinga") on every alert.
- **Two graph backends:** render a parametric **Grafana** panel, or draw a compact **matplotlib**
  sparkline straight from **VictoriaMetrics** (Grafana-free).
- **Fails open and degrades gracefully:** if the graph, image upload, or suppression store fail,
  the alert still goes out (text-only if needed) — a missed alert is worse than a duplicate.

## Architecture

```
            Icinga master                                  phone (ntfy app)
   ┌───────────────────────────┐                        ┌──────────────────┐
   │  NotificationCommand       │   publish (token)      │  subscribed to   │
   │    └─ dispatcher (notify.py)├──────────────────────►│  topic "alerts"  │
   │         renders graph PNG  │      ntfy server ◄──────┤  (push + buttons)│
   │         + PUTs it to broker│   fetch graph / SSE     └────────┬─────────┘
   └─────────┬─────────────────┘                                  │ taps
             │ HMAC-signed PUT (graph)                            │ Ack / Downtime
             │                                                    ▼
        ┌────▼──────────────────────────┐  Icinga API (scoped ApiUser)  ┌──────────┐
        │  broker  (Flask)              │ ─────────────────────────────►│  Icinga  │
        │   /ack  /downtime  /graph/... │   acknowledge / schedule dt    │   API    │
        └───────────────────────────────┘                                └──────────┘

  graph data source (pick one): Grafana render API  OR  VictoriaMetrics query API
  TLS for ntfy + broker is terminated by Caddy (server/Caddyfile.example) or a tunnel.
```

- **dispatcher** (`dispatcher/`) — Python, runs *on the Icinga master*, invoked by Icinga as a
  `NotificationCommand`. Applies suppression, renders the graph, publishes the ntfy message.
- **server stack** (`server/`) — Docker Compose: the **ntfy** server plus the **broker** (a small
  Flask app serving graph PNGs and handling Ack/Downtime callbacks).
- **Caddy** (opt-in `caddy` service) terminates TLS and fronts both, fetching a Let's Encrypt
  certificate automatically — see `server/Caddyfile.example`. Behind CGNAT or without a static IP,
  front the stack with a **tunnel** (Cloudflare Tunnel or Tailscale Funnel) instead; see
  [`docs/reachability.md`](docs/reachability.md).

## Quick start

1. **Stand up the server stack** (ntfy + broker) on a Docker host:
   ```bash
   cd server
   cp .env.example .env            # set BROKER_SHARED_SECRET, ICINGA_API_PASSWORD, ICINGA_API_URL
   cp server.example.yml server.yml   # set base-url to your domain
   docker compose up -d --build
   ```
   Put Caddy in front for TLS — copy `server/Caddyfile.example` to `Caddyfile`, set your domain,
   and run `docker compose --profile caddy up -d`; Caddy provisions a Let's Encrypt certificate
   automatically. No static IP / behind CGNAT? Use a tunnel instead — see
   [`docs/reachability.md`](docs/reachability.md).

2. **Create the ntfy users/topic.** Add a publisher token (read-write on the topic) for the
   dispatcher, and a read-only login per person:
   ```bash
   docker exec ntfy ntfy access everyone alerts deny           # default deny
   docker exec ntfy ntfy token add <publisher-user>            # -> tk_... for config.yml
   docker exec -e NTFY_PASSWORD='...' ntfy ntfy user add alice
   docker exec ntfy ntfy access alice alerts read
   ```

3. **Install the dispatcher on your Icinga master:**
   ```bash
   sudo dispatcher/install.sh        # builds a venv, installs the NotificationCommands, reloads icinga2
   cp dispatcher/config.example.yml /opt/ntfy-icinga/dispatcher/config.yml
   # edit config.yml: ntfy base_url + token, broker base_url + shared_secret, render backend
   ```

4. **Wire the NotificationCommand + apply rule** (and the scoped `ntfy-broker` ApiUser) — see
   `dispatcher/icinga2/ntfy-notifications.conf.example`, or create the equivalents in Icinga
   Director.

5. **Subscribe a phone:** point the ntfy app at `https://push.example.com`, log in, subscribe to
   the `alerts` topic. Trigger a test problem and watch a rich alert with a graph and buttons
   arrive.

Full step-by-step is in [`docs/install.md`](docs/install.md); the data flow is in
[`docs/architecture.md`](docs/architecture.md).

## Configuration

All dispatcher behaviour lives in `config.yml` (copy from `dispatcher/config.example.yml`). Any
`${ENV_VAR}` reference is expanded at load time, so secrets can stay in the environment.

| Section | Key | What it does |
|---|---|---|
| `ntfy` | `base_url` / `token` | your ntfy server + a write token for the topic |
| `ntfy` | `attachment_via` | `url` (PNG served by broker, recommended), `upload`, or `none` |
| `icinga` | `web_url` | base URL for the "Open in Icinga" deep link |
| `broker` | `base_url` / `shared_secret` | the broker URL + HMAC secret (must match `server/.env`) |
| `render` | `backend` | `grafana` (render a panel) or `vm` (matplotlib sparkline from VictoriaMetrics) |
| `render` | `window` / `cache_ttl` / `timeout` | graph look-back, on-disk reuse window, render deadline |
| `render` | `tz_offset_hours` | x-axis timezone offset from UTC (default `0`; e.g. `8` for UTC+8) |
| `render.grafana` / `render.vm` | … | backend-specific URL, token, and the parametric query/panel |
| `suppression` | `store` | `sqlite` (single master) or `redis` (HA / shared across masters) |
| `suppression` | `cooldowns` | per-state quiet window in seconds (OK/UP = 0 → recoveries always send) |
| `suppression` | `always_notify_types` | notification types that always bypass the cooldown |
| `routing` | `priority_map` | state → ntfy priority (`min|low|default|high|urgent`) |
| `routing` | `crit_broadcast_topic` | optional extra topic that also gets sev-1 alerts |
| `display` | `strip_domains` | domain suffixes stripped from host names *in the title/body only* |

### How suppression works

Keyed per `host!service`:

- Notification types in `always_notify_types` (recovery, ack, downtime, …) **always** pass.
- The **first** time a key is ever seen → send.
- A jump to a **higher severity** (an escalation — including recovery → problem, or
  `WARNING -> CRITICAL`) **breaks through** the cooldown and sends one alert.
- A repeat **PROBLEM of the same state** is **suppressed** until the per-state cooldown elapses,
  then one reminder is sent and the timer resets.

The state store is pluggable: **SQLite** for a single master (no extra service), or **Redis**
shared across an HA pair so flapping between masters dedups. Suppression **fails open** — if the
store is unreachable the alert is sent anyway.

## Security

- **You own the data.** Alerts, acknowledgements, and the graph all stay on infrastructure you
  control. The only optional third party is the public ntfy.sh APNs relay used for iOS instant
  push, and it only ever receives a SHA-256 wake-up of the topic name — never your message.
- **TLS at the edge.** Neither ntfy nor the broker terminate TLS; put them behind the opt-in
  **Caddy** service (`server/Caddyfile.example`), which provisions a Let's Encrypt certificate
  automatically, or behind a **tunnel** (Cloudflare Tunnel / Tailscale Funnel) that supplies TLS —
  see [`docs/reachability.md`](docs/reachability.md).
- **Action buttons are HMAC-signed.** Each Ack/Downtime button carries a short HMAC token over
  `action:host:service`, verified by the broker before it calls Icinga. The Icinga API itself is
  reached through a **scoped** ApiUser limited to `acknowledge-problem` + `schedule-downtime`, so
  the phone never holds Icinga credentials.
- Keep `config.yml`, `server.yml`, and `.env` out of version control (the included `.gitignore`
  already does this) — only the `*.example.*` templates are tracked.

## Requirements

- **Icinga 2** with the API feature enabled (`icinga2 feature enable api`).
- A **Docker** host for the `server/` stack (ntfy + broker) and a **reverse proxy** for TLS.
- **Python 3.9+** on the Icinga master (the installer builds an isolated venv). The `vm` render
  backend additionally needs `matplotlib` + `numpy` (installed automatically).
- A graph data source: a **Grafana** instance with a parametric panel, *or* **VictoriaMetrics**
  (or any Prometheus-compatible API) holding your Icinga perfdata.
- The **ntfy** app on the devices you want to alert.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Daniel Hooper.
