#!/usr/bin/env bash
# Install the Icinga rich-notifications dispatcher onto an Icinga master.
#
# Run this ON an Icinga master (or over ssh). Idempotent: re-running upgrades the code in place.
# When run INTERACTIVELY it also asks a handful of questions (hostnames, topics, tokens) and writes
# config.yml + the scoped ApiUser + the notification apply rules for you — so you don't hand-edit
# them. It auto-generates the HMAC secret and the ApiUser password.
#
# Non-interactive / CI: pipe from a non-tty or set NONINTERACTIVE=1 to install the code only and
# skip the questions (provide config.yml yourself).
#
# Environment overrides:
#   INSTALL_DIR    where the dispatcher lives        (default: /opt/ntfy-icinga)
#   CONFD          icinga2 conf.d directory          (default: /etc/icinga2/conf.d)
#   ICINGA_USER    owner of the install + confs      (default: nagios)
#   PYTHON         python for the venv               (default: python3)
#   NO_RELOAD=1    skip the icinga2 reload
#   NONINTERACTIVE=1  install code only, no questions
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/ntfy-icinga}"
CONFD="${CONFD:-/etc/icinga2/conf.d}"
ICINGA_USER="${ICINGA_USER:-nagios}"
PYTHON="${PYTHON:-python3}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # repo root (this script lives here)
D="${SRC}/dispatcher"                                  # dispatcher source dir
CONFIG="${INSTALL_DIR}/dispatcher/config.yml"

echo ">>> Installing ntfy dispatcher to ${INSTALL_DIR} on $(hostname -s)"

# 1. Code (never clobber an existing config.yml — we copy code + the example, not secrets).
mkdir -p "${INSTALL_DIR}/dispatcher" "${INSTALL_DIR}/state" "${INSTALL_DIR}/cache"
for f in "${D}"/*.py "${D}/requirements.txt"; do
  [ -e "$f" ] && cp -a "$f" "${INSTALL_DIR}/dispatcher/"
done
cp -a "${D}/icinga2" "${INSTALL_DIR}/dispatcher/"
cp -a "${D}/config.example.yml" "${INSTALL_DIR}/dispatcher/config.example.yml"

# 2. venv + dependencies (network-tolerant so pip can't hang forever on a slow PyPI mirror).
PIP_NET="--timeout 20 --retries 2"
[ -d "${INSTALL_DIR}/venv" ] || "${PYTHON}" -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install -q ${PIP_NET} --upgrade pip >/dev/null 2>&1 || true
"${INSTALL_DIR}/venv/bin/pip" install -q ${PIP_NET} -r "${INSTALL_DIR}/dispatcher/requirements.txt"
# The "vm" backend needs the full matplotlib import chain (pyplot pulls in numpy). Verify + repair.
"${INSTALL_DIR}/venv/bin/python3" -c 'import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot' 2>/dev/null \
  || "${INSTALL_DIR}/venv/bin/pip" install -q ${PIP_NET} --force-reinstall --no-cache-dir -r "${INSTALL_DIR}/dispatcher/requirements.txt"

# 3. NotificationCommands (loaded via the conf.d/*.conf include glob).
install -m 0640 -o "${ICINGA_USER}" -g "${ICINGA_USER}" \
  "${D}/icinga2/ntfy-commands.conf" "${CONFD}/ntfy-commands.conf"

# 4. Interactive configuration -------------------------------------------------------------------
ask() {  # ask VAR "prompt" ["default"]
  local __v="$1" __p="$2" __d="${3:-}" __a
  if [ -n "$__d" ]; then read -r -p "    ${__p} [${__d}]: " __a; __a="${__a:-$__d}"
  else read -r -p "    ${__p}: " __a; fi
  printf -v "$__v" '%s' "$__a"
}
CFG_DONE=
if [ "${NONINTERACTIVE:-}" != "1" ] && [ -t 0 ]; then
  do_cfg=y
  [ -f "$CONFIG" ] && read -r -p ">>> config.yml already exists — reconfigure (overwrites it)? [y/N]: " do_cfg
  case "${do_cfg:-n}" in [yY]*)
    echo ">>> A few questions — your answers become config.yml + the ApiUser + the notification rules:"
    ask NTFY_BASE   "ntfy server URL (your own, or https://ntfy.sh)" "https://push.example.com"
    ask ALERT_TOPIC "alert topic (the phone subscribes to this)" "alerts"
    ask ACK_TOPIC   "ack topic (the buttons publish here; the relay subscribes)" "icinga-acks"

    # Tokens: when ntfy is installed locally we create the dispatcher + relay users/tokens for you;
    # otherwise (or for ntfy.sh) you paste tokens you made yourself (ntfy token add / account page).
    NTFY_TOKEN=""; RELAY_TOKEN=""
    if command -v ntfy >/dev/null 2>&1 && [[ "$NTFY_BASE" != *"ntfy.sh"* ]]; then
      read -r -p "    ntfy is installed here — auto-create the dispatcher + relay users/tokens? [Y/n]: " __mk
      if [[ -z "${__mk}" || "${__mk}" =~ ^[Yy] ]]; then
        # dispatcher: WRITE on the alert + ack topics (publishes alerts and the button payloads)
        NTFY_PASSWORD="$(openssl rand -hex 16)" ntfy user add dispatcher >/dev/null 2>&1 || true
        ntfy access dispatcher "$ALERT_TOPIC" write >/dev/null 2>&1 || true
        ntfy access dispatcher "$ACK_TOPIC"   write >/dev/null 2>&1 || true
        NTFY_TOKEN="$(ntfy token add dispatcher 2>/dev/null | grep -oE 'tk_[A-Za-z0-9]+' | head -1 || true)"
        # relay: READ on the ack topic (subscribes to it)
        NTFY_PASSWORD="$(openssl rand -hex 16)" ntfy user add relay >/dev/null 2>&1 || true
        ntfy access relay "$ACK_TOPIC" read >/dev/null 2>&1 || true
        RELAY_TOKEN="$(ntfy token add relay 2>/dev/null | grep -oE 'tk_[A-Za-z0-9]+' | head -1 || true)"
        if [ -n "$NTFY_TOKEN" ] && [ -n "$RELAY_TOKEN" ]; then
          echo "    created ntfy users 'dispatcher' (write on ${ALERT_TOPIC} + ${ACK_TOPIC}) and 'relay' (read on ${ACK_TOPIC}), with tokens."
        else
          echo "    could not auto-create tokens (is ntfy set up with an auth file + deny-all?) — enter them manually:"
        fi
      fi
    fi
    [ -n "$NTFY_TOKEN" ]  || ask NTFY_TOKEN  "ntfy WRITE token for the dispatcher (tk_...; blank for public ntfy.sh topics)" ""
    [ -n "$RELAY_TOKEN" ] || ask RELAY_TOKEN "ntfy READ token for the relay (tk_...; blank for public ntfy.sh topics)" ""

    ask WEB_URL     "Icinga Web URL (for the 'Open in Icinga' link)" "https://icinga.example.com/icingaweb2"
    ask BACKEND     "graph backend: vm, graphite, or grafana" "vm"
    case "$BACKEND" in
      grafana)
        ask RENDER_URL "Grafana base URL" "http://localhost:3000"
        ask GRAFANA_TOK "Grafana service-account token (glsa_...)" ""
        RENDER_BLOCK="  grafana:
    base_url: \"${RENDER_URL}\"
    token: \"${GRAFANA_TOK}\"
    dashboard_uid: \"icinga-perf\"
    dashboard_slug: \"icinga-perfdata\"
    panel_id: 2
    width: 1000
    height: 500
    scale: 2
    theme: \"light\""
        ;;
      graphite)
        ask RENDER_URL "graphite-web base URL" "http://localhost:8080"
        RENDER_BLOCK="  graphite:
    base_url: \"${RENDER_URL}\"
    target_template: \"icinga2.{host}.services.{service}.*.perfdata.*.value\"
    host_target_template: \"icinga2.{host}.host.*.perfdata.*.value\""
        ;;
      *)
        BACKEND="vm"
        ask RENDER_URL "VictoriaMetrics/Prometheus query base URL (incl. any path, e.g. http://vm:8481/select/0/prometheus)" "http://localhost:8428"
        RENDER_BLOCK="  vm:
    base_url: \"${RENDER_URL}\"
    query_template: 'state_check_perfdata{{icinga2_host_name=\"{host}\",icinga2_service_name=\"{service}\"}}'
    series_label: \"perfdata_label\""
        ;;
    esac
    ask API_URL "local Icinga API URL" "https://localhost:5665"

    SHARED_SECRET="$(openssl rand -hex 32)"
    API_PASSWORD="$(openssl rand -hex 24)"

    umask 077
    cat > "$CONFIG" <<YAML
# Generated by install.sh ($(date -u +%Y-%m-%dT%H:%M:%SZ)). Re-run install.sh to regenerate, or edit by hand.
ntfy:
  base_url: "${NTFY_BASE}"
  token: "${NTFY_TOKEN}"
  attachment_via: "upload"
icinga:
  web_url: "${WEB_URL}"
display:
  strip_domains: []
actions:
  shared_secret: "${SHARED_SECRET}"
  ack_topic: "${ACK_TOPIC}"
  ack_write_token: "${NTFY_TOKEN}"
relay:
  ack_read_token: "${RELAY_TOKEN}"
  default_comment: "Actioned from ntfy"
  icinga_api_url: "${API_URL}"
  icinga_api_user: "ntfy-relay"
  icinga_api_password: "${API_PASSWORD}"
  icinga_api_insecure: true
render:
  backend: "${BACKEND}"
  cache_dir: "${INSTALL_DIR}/cache"
  cache_ttl: 300
  window: "3h"
  timeout: 8
  tz_offset_hours: 0
${RENDER_BLOCK}
suppression:
  store: "sqlite"
  sqlite_path: "${INSTALL_DIR}/state/suppression.db"
  cooldowns: { CRITICAL: 900, DOWN: 900, WARNING: 3600, UNKNOWN: 1800, OK: 0, UP: 0 }
  always_notify_types: [RECOVERY, ACKNOWLEDGEMENT, CUSTOM, FLAPPINGSTART, FLAPPINGEND, DOWNTIMESTART, DOWNTIMEEND]
routing:
  priority_map: { CRITICAL: urgent, DOWN: urgent, WARNING: high, UNKNOWN: default, OK: low, UP: low }
  crit_broadcast_topic: ""
  crit_broadcast_states: [CRITICAL, DOWN]
YAML
    umask 022

    cat > "${CONFD}/ntfy-relay-apiuser.conf" <<CONF
object ApiUser "ntfy-relay" {
  password = "${API_PASSWORD}"
  permissions = [ "actions/acknowledge-problem", "actions/schedule-downtime" ]
}
CONF
    # Notification rules with your topic set. The assign matches every host/service that has
    # enable_notifications (Icinga's default) — narrow it in the generated file if you want less.
    sed "s|vars.ntfy_topic = \"alerts\"|vars.ntfy_topic = \"${ALERT_TOPIC}\"|" \
      "${D}/icinga2/ntfy-notifications.conf.example" > "${CONFD}/ntfy-notifications.conf"

    chown "${ICINGA_USER}:${ICINGA_USER}" "$CONFIG" "${CONFD}/ntfy-relay-apiuser.conf" "${CONFD}/ntfy-notifications.conf"
    chmod 0640 "$CONFIG" "${CONFD}/ntfy-relay-apiuser.conf" "${CONFD}/ntfy-notifications.conf"
    echo ">>> Wrote config.yml, ntfy-relay-apiuser.conf, and ntfy-notifications.conf (secret + ApiUser password auto-generated)."
    CFG_DONE=1
  ;; esac
fi

# 5. Ownership + permissions on the install tree.
chown -R "${ICINGA_USER}:${ICINGA_USER}" "${INSTALL_DIR}"
chmod +x "${INSTALL_DIR}/dispatcher/notify.py"

# 6. Validate, then reload (never restart — a reload won't drop active checks).
if icinga2 daemon -C >/dev/null 2>&1; then
  if [ "${NO_RELOAD:-}" = "1" ]; then
    echo ">>> OK: installed; skipping icinga2 reload (NO_RELOAD=1)"
  else
    systemctl reload icinga2
    echo ">>> OK: dispatcher installed + icinga2 reloaded on $(hostname -s)"
  fi
else
  echo "ERROR: icinga2 daemon -C failed — NOT reloading"; icinga2 daemon -C 2>&1 | tail -15; exit 1
fi

# 7. What's left.
if [ "${CFG_DONE}" = "1" ]; then
  cat <<NEXT

Almost done — a few things you do yourself:
  * Make ntfy reachable at ${NTFY_BASE} (self-hosted ntfy behind Apache — docs/install.md steps 1-2 —
    or use https://ntfy.sh).
  * Create a read login for each on-call person (they sign into the ntfy app with it, then subscribe
    to "${ALERT_TOPIC}"):
      sudo ntfy user add alice && sudo ntfy access alice "${ALERT_TOPIC}" read
  * Run the relay:
      sudo cp dispatcher/relay.service.example /etc/systemd/system/ntfy-icinga-relay.service
      sudo systemctl daemon-reload && sudo systemctl enable --now ntfy-icinga-relay
  (Optional: narrow the 'assign where' in ${CONFD}/ntfy-notifications.conf if not every host/service should notify.)
NEXT
elif [ ! -f "$CONFIG" ]; then
  echo "NOTE: ${CONFIG} not present. Re-run interactively to generate it, or copy config.example.yml and edit."
fi
