#!/usr/bin/env python3
"""Ack relay — make the Acknowledge / Downtime buttons work with NO inbound exposure.

In "relay" action mode (config: actions.transport = relay) the notification buttons publish a
small HMAC-signed message to an ntfy *ack topic* instead of POSTing to the broker. This service
SUBSCRIBES to that topic — an outbound connection, exactly like the dispatcher's publish — and for
each valid message calls the local Icinga2 API to acknowledge the problem or schedule downtime.

Because every connection is outbound, the buttons work even when your Icinga has no public IP, no
port-forward and no tunnel: point the dispatcher and the phone at a reachable ntfy (your own or
the public ntfy.sh) and run this next to the dispatcher.

Config is read from the dispatcher's config.yml (see config.example.yml: the `relay:` section,
plus ntfy.base_url, actions.ack_topic and broker.shared_secret). Run it as a long-lived service —
see relay.service.example for a systemd unit.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import time

import requests

from config import load_config

log = logging.getLogger("relay")

DEFAULT_CONFIG = os.environ.get(
    "DISPATCHER_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yml"),
)


def sign(secret: str, value: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()[:32]


def verify(secret: str, value: str, token: str) -> bool:
    return bool(secret) and bool(token) and hmac.compare_digest(sign(secret, value), token)


class IcingaClient:
    """Minimal Icinga2 /v1/actions client (mirrors the broker's, kept self-contained)."""

    def __init__(self, rcfg: dict) -> None:
        self.url = str(rcfg.get("icinga_api_url", "https://localhost:5665")).rstrip("/")
        self.user = str(rcfg.get("icinga_api_user", ""))
        self.password = str(rcfg.get("icinga_api_password", ""))
        self.ca = rcfg.get("icinga_api_ca", "")
        self.insecure = bool(rcfg.get("icinga_api_insecure", False))

    @property
    def _verify_tls(self):
        if self.insecure:
            return False
        return self.ca or True

    def _filter(self, host: str, service: str) -> dict:
        # filter_vars keeps host/service out of the filter string (no injection).
        if service:
            return {"type": "Service",
                    "filter": "host.name==hostname && service.name==servicename",
                    "filter_vars": {"hostname": host, "servicename": service}}
        return {"type": "Host", "filter": "host.name==hostname", "filter_vars": {"hostname": host}}

    def _action(self, action: str, payload: dict):
        return requests.post(f"{self.url}/v1/actions/{action}", json=payload,
                             auth=(self.user, self.password),
                             headers={"Accept": "application/json"},
                             verify=self._verify_tls, timeout=10)

    def acknowledge(self, host, service, author, comment):
        return self._action("acknowledge-problem", {
            **self._filter(host, service), "author": author, "comment": comment,
            "notify": True, "sticky": False})

    def downtime(self, host, service, author, comment, hours):
        try:
            seconds = int(float(hours) * 3600)
        except (TypeError, ValueError):
            seconds = 3600
        now = int(time.time())
        return self._action("schedule-downtime", {
            **self._filter(host, service), "author": author, "comment": comment,
            "start_time": now, "end_time": now + seconds, "duration": seconds, "fixed": True})


def handle(message: str, secret: str, icinga: IcingaClient, default_comment: str) -> None:
    try:
        data = json.loads(message)
    except (ValueError, TypeError):
        return  # not one of our action messages
    action = (data.get("action") or "").strip()
    host = (data.get("host") or "").strip()
    service = (data.get("service") or "").strip()
    author = (data.get("author") or "ntfy").strip()
    if not host or action not in ("ack", "downtime"):
        log.warning("ignoring malformed action message (action=%r host=%r)", action, host)
        return
    if not verify(secret, f"{action}:{host}:{service}", data.get("token", "")):
        log.warning("REJECTED %s %s/%s — bad HMAC token", action, host, service)
        return
    target = f"{host}{('/' + service) if service else ''}"
    comment = data.get("comment") or default_comment
    try:
        if action == "ack":
            resp = icinga.acknowledge(host, service, author, comment)
        else:
            resp = icinga.downtime(host, service, author, comment, data.get("hours", 1))
        log.info("%s %s by %s -> http=%s", action, target, author, resp.status_code)
    except requests.RequestException as exc:
        log.error("%s %s failed to reach Icinga: %s", action, target, exc)


def subscribe_loop(cfg: dict) -> None:
    base = cfg["ntfy"]["base_url"].rstrip("/")
    topic = cfg["actions"]["ack_topic"]
    rcfg = cfg.get("relay", {})
    secret = cfg["broker"]["shared_secret"]
    default_comment = rcfg.get("default_comment", "Actioned from ntfy")
    icinga = IcingaClient(rcfg)
    url = f"{base}/{topic}/json"
    headers = {}
    if rcfg.get("ack_read_token"):
        headers["Authorization"] = f"Bearer {rcfg['ack_read_token']}"
    log.info("relay subscribing to %s (icinga=%s)", url, icinga.url)
    backoff = 1
    while True:
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(10, 75)) as r:
                r.raise_for_status()
                backoff = 1
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except ValueError:
                        continue
                    if evt.get("event") == "message":
                        handle(evt.get("message", ""), secret, icinga, default_comment)
        except requests.RequestException as exc:
            log.warning("subscription dropped (%s); reconnecting in %ss", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s relay: %(message)s")
    cfg = load_config(os.environ.get("DISPATCHER_CONFIG", DEFAULT_CONFIG))
    if cfg.get("actions", {}).get("transport") != "relay":
        log.error("actions.transport is not 'relay' in config.yml — nothing for the relay to do")
        return 1
    subscribe_loop(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
