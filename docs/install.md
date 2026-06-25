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
- A graph data source — either a **Grafana** instance with a parametric panel, or a
  **VictoriaMetrics** / Prometheus-compatible API holding your Icinga performance data.

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
two topic names — an **alert topic** (e.g. `alerts`) and an **ack topic** (e.g. `icinga-acks`) —
then create:

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

## 4. Install the dispatcher on the Icinga master

```bash
sudo apt install python3-venv
sudo dispatcher/install.sh
```

The installer (idempotent — re-run it to upgrade in place):

- creates `/opt/ntfy-icinga` (override with `INSTALL_DIR=...`),
- copies the dispatcher code there and builds an isolated venv,
- `pip install`s the requirements (including matplotlib/numpy for the `vm` backend),
- installs `ntfy-commands.conf` into `/etc/icinga2/conf.d` (override with `CONFD=...`),
- runs `icinga2 daemon -C`, and only on success **reloads** icinga2.

It does **not** write secrets. Provide `config.yml` yourself:

```bash
cp /opt/ntfy-icinga/dispatcher/config.example.yml /opt/ntfy-icinga/dispatcher/config.yml
#  edit config.yml:
#    ntfy.base_url            = https://push.example.com   (or https://ntfy.sh for the no-self-host path)
#    ntfy.token               = tk_...                     (the dispatcher token from step 3)
#    actions.shared_secret    = openssl rand -hex 32       (HMAC on every Ack/Downtime action)
#    actions.ack_topic        = icinga-acks                (the ack topic from step 3)
#    actions.ack_write_token  = tk_...                     (dispatcher token; WRITE on the ack topic)
#    relay.ack_read_token     = tk_...                     (the relay token from step 3; READ on the ack topic)
#    relay.icinga_api_url/user/password   = the scoped ntfy-relay ApiUser (step 5)
#    render.backend           = vm   (or grafana)  + the matching render.vm / render.grafana block
#    icinga.web_url           = https://icinga.example.com (for the "Open in Icinga" deep link)
chown nagios:nagios /opt/ntfy-icinga/dispatcher/config.yml
chmod 0640 /opt/ntfy-icinga/dispatcher/config.yml
```

> **HA / multiple masters:** run `install.sh` on each master and provide `config.yml` on each. To
> dedup alerts across the pair, set `suppression.store: redis` and point every master's
> `suppression.redis_url` at one shared Redis.

---

## 5. Enable the Icinga API + the scoped `ntfy-relay` ApiUser

The Ack/Downtime buttons reach Icinga through `relay.py`, which calls the **local** Icinga API as a
**scoped** ApiUser limited to acknowledge + downtime. Define it in a file readable only by icinga
(`0640 nagios`):

```icinga
object ApiUser "ntfy-relay" {
  password = "CHANGE_ME"   // == relay.icinga_api_password in config.yml
  permissions = [ "actions/acknowledge-problem", "actions/schedule-downtime" ]
}
```

The API feature must be enabled (step 0) and reachable from the master itself (default port 5665).

---

## 6. Wire the notification

Route alerts to the dispatcher. Copy the example, set your alert topic and apply rules, install it,
validate, and reload:

```bash
#  edit dispatcher/icinga2/ntfy-notifications.conf.example:
#    - set the User "ntfy-oncall" vars.ntfy_topic to your alert topic (e.g. "alerts")
#    - adjust the two apply rules to match the hosts/services you want notified
sudo cp dispatcher/icinga2/ntfy-notifications.conf.example \
        /etc/icinga2/conf.d/ntfy-notifications.conf
sudo icinga2 daemon -C && sudo systemctl reload icinga2
```

(Or create the equivalent User + Notifications in **Icinga Director**.)

---

## 7. Run the relay

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

## 8. Subscribe a phone and test

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
runner — there is nothing vendor-specific in it. A typical pipeline copies the `dispatcher/`
directory to each master and runs `install.sh` there (e.g. `tar | ssh ... 'bash install.sh'`), once
per master. Keep secrets (`config.yml`, the ApiUser password) out of CI and provision them
out-of-band; the installer is intentionally secret-free so CI never has to carry them.
