# Installation

This walks through standing up the **server stack** (ntfy + broker) and installing the
**dispatcher** on an Icinga 2 master. Use `example.com` placeholders below — substitute your own
domain, and pick secrets with `openssl rand -hex 32`.

## 0. Prerequisites

- An **Icinga 2** master with the API feature enabled:
  ```bash
  icinga2 feature enable api && systemctl restart icinga2
  ```
- A **Docker** host for the server stack, and a **reverse proxy** (nginx in the examples) that can
  terminate TLS for a public hostname such as `push.example.com`.
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
`NTFY_BIND` / `BROKER_BIND` in `.env` to change). Put your reverse proxy in front — copy
`server/nginx.example.conf` to your nginx `sites-enabled/`, drop in a TLS certificate, and reload
nginx. The proxy maps `/` → ntfy and `/broker/` → broker.

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

## 2. Icinga side: the scoped broker ApiUser + apply rules

The broker calls Icinga as a **scoped** ApiUser limited to acknowledge + downtime. Add it (in a
file readable only by icinga, `0640 nagios`):

```icinga
object ApiUser "ntfy-broker" {
  password = "CHANGE_ME"   // == ICINGA_API_PASSWORD in server/.env
  permissions = [ "actions/acknowledge-problem", "actions/schedule-downtime" ]
}
```

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
- **Buttons return an error** — confirm the broker can reach the Icinga API, the `ntfy-broker`
  ApiUser password matches `ICINGA_API_PASSWORD`, and `broker.shared_secret` equals
  `BROKER_SHARED_SECRET`. "Already acknowledged / already in downtime" is reported as success.
