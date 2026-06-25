# Quick start — ntfy.sh, no Docker (the easy path)

The fastest way to get rich Icinga alerts on your phone. You use the public **ntfy.sh** server,
run the **dispatcher** + a tiny **relay** natively on your Icinga host, and expose **nothing** —
no Docker, no reverse proxy, no open ports. Your Icinga box only ever makes *outbound* connections,
so this works on a single Debian/Ubuntu Icinga host behind any NAT/firewall.

> **Privacy note:** on free ntfy.sh, topics are **public** — anyone who knows the (random) topic
> name can read messages on it. Use the unguessable names generated below and treat them like
> secrets. For private alerts, reserve topics on [ntfy Pro](https://ntfy.sh/), or self-host ntfy
> ([install.md](install.md), option B). The **Acknowledge / Downtime** actions are safe either way:
> every action is HMAC-signed and the relay rejects anything it can't verify.

**What ends up running:** two small native Python pieces on the Icinga host — the **dispatcher**
(a `NotificationCommand` Icinga calls) and **`relay.py`** (a subscriber that turns button taps into
Icinga actions). No services are exposed to the internet.

## 1. Pick topic names + a secret

```bash
ALERT_TOPIC="icinga-alerts-$(openssl rand -hex 8)"
ACK_TOPIC="icinga-acks-$(openssl rand -hex 8)"
SHARED_SECRET="$(openssl rand -hex 32)"
printf 'ALERT_TOPIC=%s\nACK_TOPIC=%s\nSHARED_SECRET=%s\n' "$ALERT_TOPIC" "$ACK_TOPIC" "$SHARED_SECRET"
```

Keep these. The random topic names are your access control on free ntfy.sh; the shared secret is
the HMAC that authorises every Ack/Downtime.

## 2. Install the dispatcher (native, no Docker)

```bash
sudo apt install -y git python3-venv
git clone https://github.com/dhooper6430/ntfy-icinga-rich-alerts.git
cd ntfy-icinga-rich-alerts
sudo dispatcher/install.sh         # builds a venv, installs the NotificationCommands, reloads icinga2
sudo cp dispatcher/config.example.yml /opt/ntfy-icinga/dispatcher/config.yml
```

## 3. Configure

Edit `/opt/ntfy-icinga/dispatcher/config.yml` and set these keys (leave the other sections at their
example defaults):

```yaml
ntfy:
  base_url: "https://ntfy.sh"
  token: ""                      # public ntfy.sh topics need no token
  attachment_via: "upload"       # the graph rides ntfy (no broker). Use "none" for text-only.
icinga:
  web_url: "https://YOUR-ICINGA-WEB/icingaweb2"   # for the "Open in Icinga" tap-through
broker:
  shared_secret: "PASTE_SHARED_SECRET"            # from step 1 — secures Ack/Downtime
actions:
  transport: "relay"
  ack_topic: "PASTE_ACK_TOPIC"                    # from step 1
relay:
  icinga_api_url: "https://localhost:5665"
  icinga_api_user: "ntfy-relay"
  icinga_api_password: "PASTE_API_PASSWORD"       # you choose this; set it on the ApiUser in step 4
  icinga_api_insecure: true                       # the local Icinga API usually has a self-signed cert
render:
  backend: "vm"                  # point render.vm.base_url + query_template at your perfdata source
                                 # (VictoriaMetrics/Prometheus), or set backend: "grafana"
```

## 4. Enable the Icinga API + a scoped ApiUser

The relay needs the API to apply acks/downtimes. Enable it and add a **narrowly-scoped** user:

```bash
sudo icinga2 feature enable api && sudo systemctl reload icinga2
```

```icinga2
/* /etc/icinga2/conf.d/ntfy-relay-apiuser.conf */
object ApiUser "ntfy-relay" {
  password = "PASTE_API_PASSWORD"          // must match relay.icinga_api_password
  permissions = [ "actions/acknowledge-problem", "actions/schedule-downtime" ]
}
```

## 5. Wire the notification (topic + apply rule)

`dispatcher/install.sh` already installed the `NotificationCommand`s. Now tell Icinga *who* to
notify and *which topic* to publish to. Copy the example, set your alert topic, and install it:

```bash
sudo cp dispatcher/icinga2/ntfy-notifications.conf.example \
        /etc/icinga2/conf.d/ntfy-notifications.conf
sudoedit /etc/icinga2/conf.d/ntfy-notifications.conf
#  - set the User's  vars.ntfy_topic = "PASTE_ALERT_TOPIC"
#  - adjust the apply-rule assign to taste (which hosts/services notify)
sudo icinga2 daemon -C && sudo systemctl reload icinga2
```

## 6. Run the relay

```bash
sudo cp dispatcher/relay.service.example /etc/systemd/system/ntfy-icinga-relay.service
#  - check the paths/User in the unit match your install (default /opt/ntfy-icinga, user nagios)
sudo systemctl daemon-reload && sudo systemctl enable --now ntfy-icinga-relay
journalctl -u ntfy-icinga-relay -f          # should log "relay subscribing to https://ntfy.sh/..."
```

## 7. Subscribe your phone

Install the **ntfy** app (iOS / Android), keep the default server `ntfy.sh`, and subscribe to your
**ALERT_TOPIC**. (Push just works on ntfy.sh — no extra setup.)

## 8. Test it

Force a problem (e.g. stop a monitored service, or `icinga2 ... acknowledge`/a dummy check). Within
seconds you should get a rich notification with the perfdata graph and **Acknowledge / Downtime**
buttons. Tap one — the relay log shows it picked the action up and applied it to Icinga.

---

### Where to go from here

- **Want privacy?** Reserve your topics on ntfy Pro, or self-host ntfy (still no Docker — a native
  `apt install ntfy` behind your existing Apache/nginx). See [install.md](install.md) (option B).
- **Multiple people?** Everyone just subscribes to the same ALERT_TOPIC in their ntfy app.
- **How the no-inbound action path works** is described in [reachability.md](reachability.md).
