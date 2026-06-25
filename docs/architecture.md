# Architecture

How the pieces fit together, and why.

## Components

| Component | Where it runs | What it does |
|---|---|---|
| **dispatcher** (`dispatcher/notify.py`) | on the Icinga master | Invoked by Icinga as a `NotificationCommand`. Reads the alert from environment macros, applies suppression, renders a graph, and publishes a rich ntfy message (with the graph uploaded into ntfy). |
| **relay** (`dispatcher/relay.py`) | next to the dispatcher (Icinga master) | Subscribes *outbound* to an ntfy ack topic, verifies the HMAC-signed Ack/Downtime action, and calls the **local** Icinga API as a scoped ApiUser. Run via `dispatcher/relay.service.example` (systemd). |
| **ntfy server** | the Icinga master (native, no containers) | Self-hosted push server (`apt install ntfy`). Holds users/ACLs, relays push to subscribed phones, optionally piggybacks ntfy.sh for iOS APNs. Listens on `127.0.0.1`. |
| **Apache** (`server/apache.example.conf`) | the Icinga master | The existing Apache that serves Icinga Web also reverse-proxies ntfy: terminates TLS and forwards `/` (and the WebSocket stream) to the local ntfy daemon, so phones can reach it. |
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
            (4) publish ntfy message ─────────┼──►  ntfy server  (graph PNG uploaded with it)
                  title/body/priority/tags    │       │
                  click (IcingaDB deep link)  │       │ (5) push to subscribed phones
                  actions (Ack / Downtime)    │       ▼
                  attached graph image        │    phone shows the alert + graph + buttons
                                              │       │
                                              │       │ (6) user taps Acknowledge / Downtime
                                              │       ▼
                                              │    publish signed action ──► ntfy ack topic
                                              │                                    │
                                              │       relay.py subscribes outbound ◄┘
                                              │       └──► local Icinga API (scoped ApiUser)
                                              ▼
                                       text-only fallback if any of (3)-(4) fail
```

1. A check enters a hard PROBLEM/RECOVERY state; Icinga runs the `ntfy-*-notification`
   `NotificationCommand`, passing all the alert details as environment variables.
2. **Suppression** decides whether this event should actually fire (see below). It **fails open**:
   if the state store is unreachable, the alert is sent rather than silently dropped.
3. The dispatcher **renders a graph** of the primary metric. Two backends: Grafana's render API
   (a parametric panel) or a matplotlib sparkline drawn from VictoriaMetrics. Rendering never
   raises — any failure just means a text-only alert.
4. The dispatcher **publishes to ntfy** — title, body, priority (from the state→priority map),
   a click-through deep link into IcingaDB Web, the Acknowledge / Downtime action buttons, and the
   graph PNG **uploaded into ntfy** (no shared filesystem, no separate graph host).
5. ntfy pushes to every subscribed device. For iOS, the public ntfy.sh upstream relays only a
   wake-up hash via Apple APNs; the phone then fetches the real message + graph from your server.
6. Tapping **Acknowledge** or **Downtime** publishes an HMAC-signed action message (a token over
   `action:host:service`) to an ntfy **ack topic**. `relay.py` subscribes to that topic
   **outbound** (exactly like the dispatcher publishes), verifies the token, and calls the local
   Icinga API as a **scoped** ApiUser (only `acknowledge-problem` + `schedule-downtime`).
   "Open in Icinga" just opens the deep link.

Because every connection the dispatcher and relay make is **outbound**, nothing of yours has to
accept an inbound connection except ntfy itself (fronted by Apache) — the buttons work behind CGNAT
and even against the public ntfy.sh.

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

## Why the relay (and not the phone calling Icinga directly)

- The Icinga API credentials never touch the device. The phone only ever holds a short HMAC token
  scoped to one `host!service` action, embedded in the button.
- Every connection is outbound: the dispatcher publishes, the relay subscribes. Nothing needs an
  inbound port-forward or tunnel, so it works behind CGNAT and even against public ntfy.sh.
- The relay collapses Icinga's "already acknowledged / already in downtime" (HTTP 409) into an
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
