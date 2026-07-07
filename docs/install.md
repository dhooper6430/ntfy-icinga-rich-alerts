# Installation

A single-host, **native (no containers)** install: self-hosted **ntfy** behind your existing
**Apache2**, with the **dispatcher** and **relay** running next to Icinga on one Debian/Ubuntu
Icinga 2 master. Use the `example.com`/`push.example.com` placeholders below — substitute your own
domain — and pick secrets with `openssl rand -hex 32`.

> **Prefer zero self-hosting?** You can point `ntfy.base_url` at `https://ntfy.sh` and skip steps
> 1–2 entirely — the dispatcher, relay, and phone all talk to the public server. But free ntfy.sh
> topics are public (anyone who guesses the name can read your alerts), so use long, unguessable
> topic names.

## 0. Prerequisites

- A **Debian/Ubuntu** host running **Icinga 2** with the API feature enabled:
  ```bash
  icinga2 feature enable api && systemctl restart icinga2
  ```
  (already running **Apache2** for Icinga Web — we reuse it as the TLS front for ntfy).
- **Python 3.9+** (the dispatcher installer builds an isolated venv).
- A graph data source with your Icinga perfdata — **Graphite** (`icinga2 feature enable graphite`,
  the most common for a plain install), a **VictoriaMetrics** / Prometheus-compatible API, or a
  **Grafana** panel.

Then clone this repo on the host — **every `server/…` and `dispatcher/…` path below is relative to
it**, so run the rest of these commands from inside the clone:

```bash
sudo apt install -y git
git clone https://github.com/dhooper6430/ntfy-icinga-rich-alerts.git
cd ntfy-icinga-rich-alerts
```

---

## 1. Install ntfy (native)

Install ntfy from the official apt repository (full instructions:
<https://docs.ntfy.sh/install/#debianubuntu>):

```bash
sudo apt install ntfy        # after adding the ntfy apt repo per the docs link above
```

Drop in the server config and start it:

```bash
sudo cp server/server.example.yml /etc/ntfy/server.yml
#  edit /etc/ntfy/server.yml:
#    base-url:     https://push.example.com   (your domain; MUST be https for iOS push)
#    listen-http:  127.0.0.1:2586             (local only — Apache fronts it)
#    behind-proxy: true
#    upstream-base-url: https://ntfy.sh       (keep for iOS push; drop if Android/web only)
sudo systemctl enable --now ntfy
systemctl status ntfy
```

ntfy now listens on `127.0.0.1:2586`. It is not reachable from the network yet — Apache provides
that in the next step.

---

## 2. Put ntfy behind Apache

Reuse the Apache that already serves Icinga Web. Enable the proxy/WebSocket modules, install the
vhost, point it at your certificate, and reload:

```bash
sudo a2enmod proxy proxy_http proxy_wstunnel rewrite headers ssl
sudo cp server/apache.example.conf /etc/apache2/sites-available/push.example.com.conf
#  edit the file: set ServerName + SSLCertificateFile / SSLCertificateKeyFile for push.example.com
sudo a2ensite push.example.com
sudo apache2ctl configtest && sudo systemctl reload apache2
```

Phones now reach ntfy at **https://push.example.com** (Apache terminates TLS and proxies to the
local ntfy daemon, including the WebSocket live stream).

---

## 3. Create ntfy users and tokens

With `auth-default-access: "deny-all"`, nobody can read or write a topic until you grant it. Pick
two topic names — an **alert topic** (e.g. `alerts`) and an **ack topic** (e.g. `icinga-acks`).

> **On a local self-hosted ntfy you can skip the `dispatcher` and `relay` tokens below** —
> `./install.sh` (step 4) offers to create those two users + tokens for you. You still create a
> **read login per on-call person** here.

Otherwise create them by hand (the dispatcher + relay tokens go into `config.yml`):

```bash
# 1. A publisher user + write token for the dispatcher (writes BOTH topics) -> config.yml ntfy.token
sudo ntfy user add dispatcher
sudo ntfy access dispatcher alerts write
sudo ntfy access dispatcher icinga-acks write
sudo ntfy token add dispatcher            # prints tk_... -> config.yml ntfy.token + actions.ack_write_token

# 2. A read login per on-call person (they log into the app and subscribe to the alert topic)
sudo ntfy user add alice                  # prompts for a password
sudo ntfy access alice alerts read

# 3. A read user + token for the relay (subscribes to the ack topic outbound) -> config.yml relay.ack_read_token
sudo ntfy user add relay
sudo ntfy access relay icinga-acks read
sudo ntfy token add relay                 # prints tk_... -> config.yml relay.ack_read_token
```

A **single shared** alert topic is required: suppression is keyed per `host!service` (not per
user), so per-person topics would dedup each other away.

---

## 4. Install and configure the dispatcher

`./install.sh`, run **interactively**, installs the dispatcher and then asks a handful of
questions — your ntfy URL, the alert/ack topics, the graph backend + its URL, your Icinga Web URL,
and the local Icinga API URL. If ntfy is on this host it offers to **create the `dispatcher` +
`relay` ntfy users/tokens for you** (otherwise you paste the ones from step 3). From your answers it
writes **all three** config files and auto-generates the HMAC secret and the ApiUser password:

- `config.yml` — the dispatcher config,
- `/etc/icinga2/conf.d/ntfy-relay-apiuser.conf` — the scoped `ntfy-relay` ApiUser
  (`actions/acknowledge-problem` + `actions/schedule-downtime`),
- `/etc/icinga2/conf.d/ntfy-notifications.conf` — the notification apply rules, with your alert topic.

```bash
sudo apt install -y python3-venv
sudo ./install.sh        # answer the prompts
```

It then validates the Icinga config (`icinga2 daemon -C`) and reloads. Two things to check afterward:

- The Icinga **API feature** must be enabled and reachable from the master (step 0:
  `icinga2 feature enable api`) — the relay calls it on `https://localhost:5665`.
- The generated `ntfy-notifications.conf` notifies **every** host/service with `enable_notifications`;
  narrow the `assign where` in that file if you want a smaller scope, then `sudo systemctl reload icinga2`.

> Re-running `install.sh` upgrades the code in place and asks before overwriting an existing
> `config.yml`. For CI / unattended installs, set `NONINTERACTIVE=1` to install the code only and
> provide `config.yml` yourself (copy `dispatcher/config.example.yml`).

> **HA / multiple masters:** run the installer on each master; set `suppression.store: redis` in each
> `config.yml`, pointing at one shared Redis, so the pair dedups.

---

## 5. Run the relay

`relay.py` subscribes outbound to the ack topic and applies the button actions. Run it as a systemd
service:

```bash
sudo cp dispatcher/relay.service.example /etc/systemd/system/ntfy-icinga-relay.service
#  adjust User + the /opt/ntfy-icinga paths in the unit if you changed INSTALL_DIR
sudo systemctl daemon-reload
sudo systemctl enable --now ntfy-icinga-relay
journalctl -u ntfy-icinga-relay -f        # watch it subscribe
```

---

## 6. Subscribe a phone and test

1. Install the **ntfy** app, add server `https://push.example.com`, log in as your read user
   (step 3), and subscribe to the **alert topic** (e.g. `alerts`).
2. From the master, dry-run the dispatcher — it prints the exact ntfy payload without sending:
   ```bash
   cd /opt/ntfy-icinga/dispatcher
   DISPATCHER_CONFIG=./config.yml \
   HOSTNAME=router-1 HOSTADDRESS=192.0.2.10 \
   SERVICENAME=ping SERVICESTATE=CRITICAL SERVICEOUTPUT="100% packet loss" \
   NOTIFICATIONTYPE=PROBLEM NTFY_TOPIC=alerts \
   ../venv/bin/python3 notify.py --object-type service --dry-run --verbose
   ```
3. Drop `--dry-run` to actually publish, then schedule a real check to fail and confirm a rich
   alert (graph + Acknowledge / Downtime buttons + "Open in Icinga") lands on the phone. Tap
   **Acknowledge** and verify the problem is acknowledged in Icinga (and that
   `journalctl -u ntfy-icinga-relay` logs the action).

---

## Troubleshooting

- **No notification at all** — check `journalctl -u icinga2` and run the dry-run command above with
  `--verbose`. Confirm the apply rule matches and the User's `vars.ntfy_topic` is set.
- **Notification but no graph** — the graph fails open (you still get text). Check the dispatcher's
  log line `graph: none (text-only)`; verify the render backend URL/token and that the query
  returns a series. The `vm` backend needs the full matplotlib import chain — re-run `install.sh`,
  which force-reinstalls the pinned set if it's broken.
- **Buttons return an error** — confirm `relay.py` is running
  (`systemctl status ntfy-icinga-relay`), that it can subscribe to the ack topic (correct
  `ntfy.base_url`, `actions.ack_topic`, and `relay.ack_read_token`), and that the `ntfy-relay`
  ApiUser can reach the local Icinga API (`relay.icinga_api_*`). "Already acknowledged / already in
  downtime" is reported as success.
- **No iOS push** — `base-url` must be `https` and `upstream-base-url: "https://ntfy.sh"` must be
  set in `/etc/ntfy/server.yml`; confirm Apache is serving valid TLS for `push.example.com`.

## Deploying via CI (optional)

`install.sh` is a single-host installer designed to be run directly **or over ssh** from any CI
runner — there is nothing vendor-specific in it. A typical pipeline copies the repo (`install.sh` +
`dispatcher/`) to each master and runs `NONINTERACTIVE=1 ./install.sh` there (which installs the
code only, skipping the questions), once per master. In that mode you provide `config.yml` yourself
(out-of-band) — the installer is intentionally secret-free, so CI never has to carry your secrets.
