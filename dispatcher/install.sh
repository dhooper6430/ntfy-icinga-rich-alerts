#!/usr/bin/env bash
# Install the Icinga rich-notifications dispatcher onto an Icinga master.
#
# Run this ON an Icinga master (or invoke it over ssh). It is idempotent: re-running upgrades
# the code and dependencies in place. It does NOT touch your secrets — you provide config.yml
# yourself (see the note at the end).
#
# What it does:
#   1. Create the install dir (default /opt/ntfy-icinga; override with INSTALL_DIR).
#   2. Copy dispatcher/* there.
#   3. Build a venv and pip install -r requirements.txt (network-tolerant: --timeout/--retries).
#   4. Install icinga2/ntfy-commands.conf into the icinga2 conf.d (override with CONFD).
#   5. icinga2 daemon -C  (config check) — and only then  systemctl reload icinga2.
#
# Configurable via environment:
#   INSTALL_DIR   where the dispatcher lives           (default: /opt/ntfy-icinga)
#   CONFD         icinga2 conf.d directory             (default: /etc/icinga2/conf.d)
#   ICINGA_USER   owner of the install + conf files    (default: nagios)
#   PYTHON        python interpreter to build the venv (default: python3)
#   NO_RELOAD     set to 1 to skip the icinga2 reload  (default: unset)
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/ntfy-icinga}"
CONFD="${CONFD:-/etc/icinga2/conf.d}"
ICINGA_USER="${ICINGA_USER:-nagios}"
PYTHON="${PYTHON:-python3}"

# Directory this script lives in (the repo's dispatcher/ dir).
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ">>> Installing ntfy dispatcher to ${INSTALL_DIR} on $(hostname -s)"

# 1. Lay down the code (preserve a pre-existing config.yml — we copy code, not secrets).
mkdir -p "${INSTALL_DIR}/dispatcher" "${INSTALL_DIR}/state" "${INSTALL_DIR}/cache"
# Copy everything under dispatcher/ EXCEPT this installer and the example config, so we never
# clobber the operator's real config.yml.
for f in "${SRC}"/*.py "${SRC}/requirements.txt"; do
  [ -e "$f" ] && cp -a "$f" "${INSTALL_DIR}/dispatcher/"
done
cp -a "${SRC}/icinga2" "${INSTALL_DIR}/dispatcher/"
# Seed config.example.yml so the operator can copy it on the master, but never overwrite config.yml.
cp -a "${SRC}/config.example.yml" "${INSTALL_DIR}/dispatcher/config.example.yml"

# 2. venv + dependencies. Network-tolerant flags so pip never hangs forever on a slow/unreachable
#    PyPI mirror (a missed alert is worse than a slightly slower deploy).
PIP_NET="--timeout 20 --retries 2"
[ -d "${INSTALL_DIR}/venv" ] || "${PYTHON}" -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install -q ${PIP_NET} --upgrade pip >/dev/null 2>&1 || true
"${INSTALL_DIR}/venv/bin/pip" install -q ${PIP_NET} -r "${INSTALL_DIR}/dispatcher/requirements.txt"

# The "vm" render backend needs the FULL matplotlib import chain (pyplot pulls in numpy — a plain
# `import matplotlib` does NOT exercise it). Verify it, and force-reinstall the pinned set if it's
# broken (e.g. a stale numpy that won't run on this CPU's ABI).
"${INSTALL_DIR}/venv/bin/python3" -c 'import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot' 2>/dev/null \
  || "${INSTALL_DIR}/venv/bin/pip" install -q ${PIP_NET} --force-reinstall --no-cache-dir -r "${INSTALL_DIR}/dispatcher/requirements.txt"

# 3. Install the NotificationCommands (loaded via the conf.d/*.conf include glob).
install -m 0640 -o "${ICINGA_USER}" -g "${ICINGA_USER}" \
  "${SRC}/icinga2/ntfy-commands.conf" "${CONFD}/ntfy-commands.conf"

# 4. Ownership + permissions on the install tree.
chown -R "${ICINGA_USER}:${ICINGA_USER}" "${INSTALL_DIR}"
chmod +x "${INSTALL_DIR}/dispatcher/notify.py"

# 5. Validate, then reload (never restart — a reload won't drop active checks).
if icinga2 daemon -C >/dev/null 2>&1; then
  if [ "${NO_RELOAD:-}" = "1" ]; then
    echo ">>> OK: code installed; skipping icinga2 reload (NO_RELOAD=1)"
  else
    systemctl reload icinga2
    echo ">>> OK: dispatcher installed + icinga2 reloaded on $(hostname -s)"
  fi
else
  echo "ERROR: icinga2 daemon -C failed — NOT reloading"
  icinga2 daemon -C 2>&1 | tail -15
  exit 1
fi

# --- Secrets are YOUR responsibility -----------------------------------------------------------
# This installer does NOT write config.yml (it carries the ntfy token + the actions HMAC secret).
# Provide it once, out of band:
#
#   cp ${INSTALL_DIR}/dispatcher/config.example.yml ${INSTALL_DIR}/dispatcher/config.yml
#   # edit config.yml: set ntfy.token, actions.shared_secret, render backend, etc.
#   chown ${ICINGA_USER}:${ICINGA_USER} ${INSTALL_DIR}/dispatcher/config.yml
#   chmod 0640 ${INSTALL_DIR}/dispatcher/config.yml
#
# You ALSO need the notification apply rules + a scoped "ntfy-relay" ApiUser for the Ack/Downtime
# buttons (see icinga2/ntfy-notifications.conf.example, or create them in Icinga Director), and the
# relay running as a service (see relay.service.example). Full walk-through: docs/install.md.
if [ ! -f "${INSTALL_DIR}/dispatcher/config.yml" ]; then
  echo "NOTE: ${INSTALL_DIR}/dispatcher/config.yml is not present yet."
  echo "      Copy config.example.yml -> config.yml and fill it in before alerts will send."
fi
