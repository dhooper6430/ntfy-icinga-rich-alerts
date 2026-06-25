# ntfy-icinga-rich-alerts

Rich Icinga 2 alerts on your phone via self-hosted [ntfy](https://ntfy.sh/): a lock-screen
performance graph, **Acknowledge / Downtime** buttons that act on Icinga right from the
notification, and HA-safe deduplication — no SaaS, all on infrastructure you run.

<p align="center">
  <img src="docs/ntfy_rich_notification_screenshot.jpeg" width="460"
       alt="An ntfy rich notification on a phone: state, check output, host, and an inline perfdata graph">
  <br><em>A notification on the lock screen — state, check output, host, and an inline perfdata graph.</em>
</p>

## How it works

On a problem, Icinga calls the **dispatcher** (a `NotificationCommand`); it renders a graph and
publishes a rich message to your ntfy server (the graph is uploaded into ntfy). Tapping
**Acknowledge / Downtime** publishes a small HMAC-signed message to an ntfy *ack topic*; a tiny
**relay** running next to the dispatcher subscribes to that topic (outbound) and applies the action
through a scoped Icinga API user. Everything on your side is outbound — the only thing exposed is
ntfy, behind the Apache you already run for Icinga Web.

## Install

Native, no containers, on a single Debian/Ubuntu Icinga 2 host.

1. **Self-host ntfy behind your Apache** (or point at the public `https://ntfy.sh`), and create your
   alert + ack topics and tokens — see **[docs/install.md](docs/install.md)**, steps 1–3.

2. **Clone and run the installer.** Run interactively it *asks for your hostnames, topics and
   tokens* and writes `config.yml`, the scoped `ntfy-relay` ApiUser, and the notification rules for
   you — auto-generating the HMAC secret and the ApiUser password:
   ```bash
   sudo apt install -y git python3-venv
   git clone https://github.com/dhooper6430/ntfy-icinga-rich-alerts.git
   cd ntfy-icinga-rich-alerts
   sudo ./install.sh
   ```

3. **Start the relay and subscribe a phone:**
   ```bash
   sudo cp dispatcher/relay.service.example /etc/systemd/system/ntfy-icinga-relay.service
   sudo systemctl daemon-reload && sudo systemctl enable --now ntfy-icinga-relay
   ```
   Install the ntfy app, point it at your server, and subscribe to your alert topic — then break
   something and watch a rich alert arrive.

The full walkthrough — ntfy + Apache setup, creating tokens, testing, and troubleshooting — is in
**[docs/install.md](docs/install.md)**; the data flow is in
**[docs/architecture.md](docs/architecture.md)**.

## Configuration

`install.sh` generates `config.yml` from your answers (re-run it to change them, or edit the file).
Every option is documented in
[`dispatcher/config.example.yml`](dispatcher/config.example.yml): the ntfy server + token, the graph
backend (a parametric **Grafana** panel or a Grafana-free **VictoriaMetrics**/Prometheus sparkline),
per-state cooldowns and ntfy priorities, host-name display trimming, and the suppression store —
**SQLite** for one master or **Redis** shared across an HA pair so it doesn't double-alert.

## Requirements

- A **Debian/Ubuntu** host running **Icinga 2** with the API feature enabled, already serving Icinga
  Web through **Apache** (reused as the TLS front for ntfy).
- **Python 3.9+** (the installer builds an isolated venv) and **ntfy** (the native package).
- A graph data source: a **Grafana** instance with a parametric panel, or a **VictoriaMetrics** /
  Prometheus-compatible API holding your Icinga performance data.

## Security

Acknowledge / Downtime actions are **HMAC-signed** and verified by the relay before it touches
Icinga, and the Icinga API is reached through a **scoped** ApiUser limited to acknowledge + schedule
downtime — phones never hold Icinga credentials. Everything runs on infrastructure you control; the
only optional third party is the public ntfy.sh APNs relay used for iOS push, which only ever
receives a wake-up hash of the topic name, not your message.

## License

MIT — see [LICENSE](LICENSE).
