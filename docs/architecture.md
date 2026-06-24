# Architecture

How the pieces fit together, and why.

## Components

| Component | Where it runs | What it does |
|---|---|---|
| **dispatcher** (`dispatcher/notify.py`) | on the Icinga master | Invoked by Icinga as a `NotificationCommand`. Reads the alert from environment macros, applies suppression, renders a graph, and publishes a rich ntfy message. |
| **ntfy server** (`server/` stack) | a Docker host | Self-hosted push server. Holds users/ACLs, relays push to subscribed phones, optionally piggybacks ntfy.sh for iOS APNs. |
| **broker** (`server/broker/app.py`) | a Docker host (same stack) | Small Flask app. Serves the graph PNGs the dispatcher pushes, and handles the Acknowledge / Downtime button callbacks by calling the Icinga API. |
| **Caddy** (`server/Caddyfile.example`) *or a tunnel* | your edge | Terminates TLS (auto Let's Encrypt) and routes `/` → ntfy and `/broker/` → broker. Opt-in `caddy` service, or front the stack with a tunnel — see [`reachability.md`](reachability.md). |
| **graph data source** | existing | Either Grafana (render API) or VictoriaMetrics / Prometheus (query API) holding Icinga perfdata. |

## End-to-end flow

```
 (1) check goes CRITICAL
        │
        ▼
 Icinga master ── NotificationCommand ──►  dispatcher (notify.py)
                                              │
            (2) suppression: send or drop?    │  per host!service cooldown,
                                              │  severity break-through, SQLite/Redis store
                                              │
            (3) render graph PNG  ◄───────────┤  Grafana render API  OR  VictoriaMetrics query
                                              │
            (4) PUT PNG (HMAC-signed) ────────┼──►  broker  (stores it, returns nothing)
                                              │
            (5) publish ntfy message ─────────┼──►  ntfy server
                  title/body/priority/tags    │       │
                  click (IcingaDB deep link)  │       │ (6) push to subscribed phones
                  actions (Ack / Downtime)    │       ▼
                  attach (signed graph URL)    │    phone shows the alert + graph + buttons
                                              │       │
                                              │       │ (7) user taps Acknowledge / Downtime
                                              │       ▼
                                              │    HTTP POST (HMAC token) ──► broker ──► Icinga API
                                              │                                  (scoped ApiUser)
                                              ▼
                                       text-only fallback if any of (3)-(5) fail
```

1. A check enters a hard PROBLEM/RECOVERY state; Icinga runs the `ntfy-*-notification`
   `NotificationCommand`, passing all the alert details as environment variables.
2. **Suppression** decides whether this event should actually fire (see below). It **fails open**:
   if the state store is unreachable, the alert is sent rather than silently dropped.
3. The dispatcher **renders a graph** of the primary metric. Two backends: Grafana's render API
   (a parametric panel) or a matplotlib sparkline drawn from VictoriaMetrics. Rendering never
   raises — any failure just means a text-only alert.
4. The PNG is **PUT to the broker** over an HMAC-signed URL (no shared filesystem needed between
   the master and the Docker host). The phone later fetches it from the broker.
5. The dispatcher **publishes to ntfy** — title, body, priority (from the state→priority map),
   a click-through deep link into IcingaDB Web, the Acknowledge / Downtime action buttons, and the
   `attach` URL for the graph.
6. ntfy pushes to every subscribed device. For iOS, the public ntfy.sh upstream relays only a
   wake-up hash via Apple APNs; the phone then fetches the real message + graph from your server.
7. Tapping **Acknowledge** or **Downtime** POSTs to the broker with an HMAC token over
   `action:host:service`. The broker verifies the token and calls the Icinga API as a **scoped**
   ApiUser (only `acknowledge-problem` + `schedule-downtime`). "Open in Icinga" just opens the
   deep link.

## Suppression model

Keyed per `host!service`:

- Types in `always_notify_types` (recovery, ack, downtime, …) always pass.
- First sighting of a key → send.
- Escalation to a **higher severity** (incl. recovery → problem, `WARNING -> CRITICAL`) breaks
  through the cooldown and sends one alert, then the cooldown applies to the new state.
- A repeat **PROBLEM of the same state** is suppressed until the per-state cooldown elapses, then
  one reminder is sent and the timer resets.

Severity ranks: `OK/UP = 0 < WARNING = 1 < UNKNOWN = 2 < CRITICAL/DOWN = 3`. Pluggable store:
**SQLite** (single master, stdlib only) or **Redis** (shared across an HA pair so cross-master
flap dedups). The whole engine fails open on store errors.

## Why a broker (and not the phone calling Icinga directly)

- The Icinga API credentials never touch the device. The phone only ever holds a short HMAC token
  scoped to one `host!service` action.
- The broker doubles as the graph host, so the master needs no shared filesystem with the Docker
  host — it just PUTs the PNG over a signed URL.
- The broker collapses Icinga's "already acknowledged / already in downtime" (HTTP 409) into an
  idempotent success, so a double-tap doesn't surface an error on the phone.

## Notable rendering choices

- **One metric, not many.** A single clean line reads on a lock screen; small-multiples don't.
- **Transparent background + theme-neutral colours** so the one static image sits acceptably on a
  light *or* dark notification.
- **Negative-axis flip.** For an all-negative metric (e.g. a −48 V plant battery), a magnitude
  drop makes the value rise toward zero — which on a normal axis trends *up* and misreads as
  "healthy". The y-axis is inverted so a magnitude drop reads as a *down* trend.
- **`numpy` pinned `<2`** for portability — numpy 2.x wheels assume an x86-64-v2 CPU baseline and
  fail to import on older/limited vCPUs, which would silently drop the graph. numpy 1.x uses the
  base x86-64 ABI and imports everywhere.
