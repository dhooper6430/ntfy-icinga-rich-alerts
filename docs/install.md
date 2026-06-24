# Installation

This walks through standing up the **server stack** (ntfy + broker) and installing the
**dispatcher** on an Icinga 2 master. Use `example.com` placeholders below — substitute your own
domain, and pick secrets with `openssl rand -hex 32`.

## 0. Prerequisites

- An **Icinga 2** master with the API feature enabled:
  ```bash
  icinga2 feature enable api && systemctl restart icinga2
  ```
- A **Docker** host for the server stack, with TLS for a public hostname such as
  `push.example.com` terminated by the bundled **Caddy** service (`server/Caddyfile.example`) or by
  a **tunnel** (Cloudflare Tunnel / Tailscale Funnel) — see [`reachability.md`](reachability.md).
- A graph data source — either a **Grafana** instance with a parametric panel, or a
  **VictoriaMetrics** / Prometheus-compatible API holding your Icinga performance data.

---

## 1. Server stack (ntfy + broker)

On the Docker host:

```bash
git clone https://github.com/<you>/ntfy-icinga-rich-alerts.git
cd ntfy-icinga-rich-alerts/server

cp .env.example .env
#  edit .env:
#    BROKER_SHARED_SECRET  = openssl rand -hex 32   (remember this — the dispatcher needs the same)
#    ICINGA_API_PASSWORD   = a password for the scoped ntfy-broker ApiUser
#    ICINGA_API_URL        = https://icinga.example.com:5665
#    ICINGA_API_INSECURE   = 1 if your Icinga API uses a self-signed cert

cp server.example.yml server.yml
#  edit server.yml: set base-url to https://push.example.com

docker compose up -d --build
```

By default the stack publishes ntfy on `127.0.0.1:8080` and the broker on `127.0.0.1:8081` (set
`NTFY_BIND` / `BROKER_BIND` in `.env` to change). Put **Caddy** in front for TLS — copy
`server/Caddyfile.example` to `Caddyfile`, set your domain, and bring up the opt-in service with
`docker compose --profile caddy up -d`. Caddy provisions a Let's Encrypt certificate automatically
(no manual cert step) and maps `/` → ntfy and `/broker/` → broker. If this host has no static IP or
sits behind CGNAT, front it with a tunnel instead — see [`reachability.md`](reachability.md).

### Create ntfy users and a topic

A single shared topic (e.g. `alerts`) is required — suppression is keyed per `host!service`, not
per user, so per-person topics would dedup each other away. Give the dispatcher a write token, and
each person a read-only login:

```bash
# publisher token for the dispatcher (read-write on the topic) -> goes in config.yml as ntfy.token
docker exec ntfy ntfy token add publisher

# a teammate, read-only on the topic
docker exec -e NTFY_PASSWORD='theirPassword' ntfy ntfy user add alice
docker exec ntfy ntfy access alice alerts read
```

On each phone: add server `https://push.example.com`, log in, subscribe to topic `alerts`.

---

## 2. Icinga side: the scoped ApiUser + apply rules

The Ack/Downtime buttons reach Icinga through a **scoped** ApiUser limited to acknowledge +
downtime. Which user depends on the action **transport** you pick (see step 3 and
[`reachability.md`](reachability.md)):

- **`broker` transport (default):** the phone POSTs the action to the broker, which calls Icinga as
  ApiUser **`ntfy-broker`**. Add it (in a file readable only by icinga, `0640 nagios`):

  ```icinga
  object ApiUser "ntfy-broker" {
    password = "CHANGE_ME"   // == ICINGA_API_PASSWORD in server/.env
    permissions = [ "actions/acknowledge-problem", "actions/schedule-downtime" ]
  }
  ```

- **`relay` transport (no inbound):** the buttons publish to an ntfy ack topic that `relay.py`
  subscribes to outbound and applies locally — so there is no broker and no public endpoint. It
  calls Icinga as ApiUser **`ntfy-relay`** with the *same* permissions. Define it the same way
  (just rename the object) and see [`reachability.md`](reachability.md) for the full walk-through.

Add the notification **User** and **apply rules** that route alerts to the dispatcher — copy
`dispatcher/icinga2/ntfy-notifications.conf.example` and adapt, or create the equivalents in
**Icinga Director**. The example wires every host/service notification to a single `ntfy-oncall`
User whose `vars.ntfy_topic` is `alerts`.

---

## 3. Install the dispatcher on the Icinga master

Copy this repo to the master (or just the `dispatcher/` directory) and run the installer:

```bash
sudo dispatcher/install.sh
```

The installer (idempotent):

- creates `/opt/ntfy-icinga` (override with `INSTALL_DIR=...`),
- copies the dispatcher code there and builds an isolated venv,
- `pip install`s the requirements (including matplotlib/numpy for the `vm` backend),
- installs `ntfy-commands.conf` into `/etc/icinga2/conf.d` (override with `CONFD=...`),
- runs `icinga2 daemon -C`, and only on success **reloads** icinga2.

It does **not** write secrets. Provide `config.yml` yourself:

```bash
cp /opt/ntfy-icinga/dispatcher/config.example.yml /opt/ntfy-icinga/dispatcher/config.yml
#  edit config.yml:
#    ntfy.base_url   = https://push.example.com
#    ntfy.token      = tk_...                     (the publisher token from step 1)
#    broker.base_url = https://push.example.com/broker
#    broker.shared_secret = <same as BROKER_SHARED_SECRET in server/.env>
#    icinga.web_url  = https://icinga.example.com
#    render.backend  = vm   (or grafana)  + the matching render.vm / render.grafana block
chown nagios:nagios /opt/ntfy-icinga/dispatcher/config.yml
chmod 0640 /opt/ntfy-icinga/dispatcher/config.yml
```

Run the installer again whenever you update the code; it upgrades in place.

> **Action buttons — two transports.** `actions.transport` in `config.yml` chooses how the
> Ack/Downtime buttons reach Icinga. The default **`broker`** sends the action from the phone to the
> broker (which must be reachable from the phone — public IP or tunnel; uses the `ntfy-broker`
> ApiUser from step 2). The alternative **`relay`** needs **no inbound** at all: the buttons publish
> to an ntfy ack topic that `relay.py` watches outbound and applies via the `ntfy-relay` ApiUser, so
> the broker/server stack becomes optional and it works even against public ntfy.sh. The full
> step-by-step (config keys + running `relay.py` via `dispatcher/relay.service.example`) is in
> [`reachability.md`](reachability.md).

> **HA / multiple masters:** run `install.sh` on each master and provide `config.yml` on each. To
> dedup alerts across the pair, set `suppression.store: redis` and point every master's
> `suppression.redis_url` at one shared Redis.

---

## 4. Test

From the master, fake the Icinga macros and dry-run the dispatcher — it prints the exact ntfy
payload without sending:

```bash
cd /opt/ntfy-icinga/dispatcher
DISPATCHER_CONFIG=./config.yml \
HOSTNAME=router-1 HOSTADDRESS=192.0.2.10 \
SERVICENAME=ping SERVICESTATE=CRITICAL SERVICEOUTPUT="100% packet loss" \
NOTIFICATIONTYPE=PROBLEM NTFY_TOPIC=alerts \
../venv/bin/python3 notify.py --object-type service --dry-run --verbose
```

Drop `--dry-run` to actually publish, then schedule a real check to fail and confirm a rich alert
(graph + Acknowledge / Downtime buttons + "Open in Icinga") lands on the phone. Tap **Acknowledge**
and verify the problem is acknowledged in Icinga.

---

## Deploying via CI (optional)

`install.sh` is a single-host installer designed to be run directly **or over ssh** from any CI
runner — there is nothing vendor-specific in it. A typical pipeline copies the `dispatcher/`
directory to each master and runs `install.sh` there (e.g. `tar | ssh ... 'bash install.sh'`), once
per master. Keep secrets (`config.yml`, the ApiUser password) out of CI and provision them
out-of-band; the installer is intentionally secret-free so CI never has to carry them.

## Troubleshooting

- **No notification at all** — check `journalctl -u icinga2` and run the dry-run command above with
  `--verbose`. Confirm the apply rule matches and the User's `vars.ntfy_topic` is set.
- **Notification but no graph** — the graph fails open (you still get text). Check the dispatcher's
  log line `graph: none (text-only)`; verify the render backend URL/token and that the query
  returns a series. The `vm` backend needs the full matplotlib import chain — re-run `install.sh`,
  which force-reinstalls the pinned set if it's broken.
- **Buttons return an error** — on the **broker** transport, confirm the broker can reach the
  Icinga API, the `ntfy-broker` ApiUser password matches `ICINGA_API_PASSWORD`, and
  `broker.shared_secret` equals `BROKER_SHARED_SECRET`. On the **relay** transport, confirm
  `relay.py` is running (e.g. `systemctl status ntfy-icinga-relay`), it can subscribe to the ack
  topic, and the `ntfy-relay` ApiUser can reach the local Icinga API. "Already acknowledged /
  already in downtime" is reported as success on either transport.
