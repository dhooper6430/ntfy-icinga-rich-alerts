#!/usr/bin/env python3
"""Icinga rich-notification dispatcher.

Called by Icinga2 as a NotificationCommand (one host variant, one service variant). Reads
the runtime macros from the environment, applies fixed-cooldown suppression, renders a
performance-graph PNG, and publishes a rich ntfy message (priority, tags, deep link,
Acknowledge / Downtime action buttons, and the graph image) to the recipient's topic.

Usage:
    notify.py --object-type service [--config PATH] [--dry-run] [--verbose]
    notify.py --object-type host    [--config PATH] [--dry-run] [--verbose]
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.parse

import requests

from config import load_config
from icinga_macros import AlertEvent
from ntfy_client import NtfyClient, NtfyMessage
from render import render_graph
from suppression import SuppressionEngine

log = logging.getLogger("dispatcher")

DEFAULT_CONFIG = os.environ.get(
    "DISPATCHER_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yml"),
)

STATE_TAGS = {
    "CRITICAL": "rotating_light",
    "DOWN": "rotating_light",
    "WARNING": "warning",
    "UNKNOWN": "grey_question",
    "OK": "white_check_mark",
    "UP": "white_check_mark",
}


def sign(secret: str, value: str) -> str:
    """Short HMAC token authorising a broker callback / graph fetch."""
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()[:32]


def build_click_url(web_url: str, event: AlertEvent) -> str:
    """Deep link into Icinga Web (IcingaDB Web).

    Some installs serve Icinga Web at the document ROOT (e.g.
    https://icinga.example.com/icingadb/service?...), others under /icingaweb2. If your
    Icinga Web is at the root, point web_url at the root and we strip a legacy /icingaweb2
    suffix defensively (a /icingaweb2 path there 404s and breaks the page's CSS/JS). A
    direct (not #!) link is used so it survives the login redirect; %20 encoding matches the
    form Icinga Web itself emits."""
    base = web_url.rstrip("/")
    if base.endswith("/icingaweb2"):
        base = base[: -len("/icingaweb2")]
    if event.is_service:
        q = urllib.parse.urlencode({"name": event.service_name, "host.name": event.host_name},
                                   quote_via=urllib.parse.quote)
        return f"{base}/icingadb/service?{q}"
    q = urllib.parse.urlencode({"name": event.host_name}, quote_via=urllib.parse.quote)
    return f"{base}/icingadb/host?{q}"


def build_actions(cfg: dict, event: AlertEvent) -> list:
    """Acknowledge + 1h Downtime buttons that call the broker (which talks to the Icinga API),
    plus an Open-in-Icinga view button. Only for active problems."""
    if not event.is_problem:
        return []
    broker = cfg["broker"]["base_url"].rstrip("/")
    secret = cfg["broker"]["shared_secret"]
    host, service = event.host_name, event.service_name
    actor = event.user_name or "ntfy"

    def http_action(label: str, path: str, action_key: str, extra: dict) -> dict:
        payload = {"host": host, "service": service, "author": actor,
                   "token": sign(secret, f"{action_key}:{host}:{service}"), **extra}
        return {
            "action": "http",
            "label": label,
            "url": f"{broker}{path}",
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload),
            "clear": True,
        }

    actions = [
        http_action("Acknowledge", "/ack", "ack", {}),
        http_action("Downtime 1h", "/downtime", "downtime", {"hours": 1}),
        {"action": "view", "label": "Open in Icinga",
         "url": build_click_url(cfg["icinga"]["web_url"], event), "clear": False},
    ]
    return actions[:3]  # ntfy caps at 3


def graph_url(cfg: dict, url_filename: str) -> str:
    """Signed broker URL the phone fetches the graph PNG from."""
    broker = cfg["broker"]["base_url"].rstrip("/")
    token = sign(cfg["broker"]["shared_secret"], url_filename)
    return f"{broker}/graph/{url_filename}?t={token}"


def upload_graph(cfg: dict, graph_path: str, timeout: float = 8) -> str:
    """PUT the rendered PNG to the broker (no shared filesystem needed). Returns the signed
    fetch URL, or "" on failure (caller falls back to text-only)."""
    url_filename = os.path.basename(graph_path)
    url = graph_url(cfg, url_filename)
    try:
        with open(graph_path, "rb") as fh:
            resp = requests.put(url, data=fh, timeout=timeout)
        resp.raise_for_status()
        return url
    except Exception as exc:
        log.warning("graph upload to broker failed: %s", exc)
        return ""


def short_host(cfg: dict, name: str) -> str:
    """Strip a configured domain suffix from a host name *for display only*. The full FQDN is
    still used for the metric query, the Icinga deep link, and broker ack/downtime. Useful when
    host names are already descriptive and the domain is just noise on a phone. Configure the
    suffix list with display.strip_domains (default: empty, i.e. show the full name)."""
    for suffix in cfg.get("display", {}).get("strip_domains", []):
        if suffix and name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def build_message(
    cfg: dict,
    event: AlertEvent,
    topic: str,
    attach_url: str = "",
    attach_file: str = "",
    filename: str = "",
) -> NtfyMessage:
    state = event.state
    tag = STATE_TAGS.get(state, "bell")
    priority = cfg["routing"]["priority_map"].get(state, "default")

    host_disp = short_host(cfg, event.host_display)
    if event.is_service:
        title = f"{state} · {host_disp} / {event.service_display}"
    else:
        title = f"HOST {state} · {host_disp}"

    lines = []
    if event.output:
        lines.append(event.output.strip())
    lines.append("")
    lines.append(f"Host: {short_host(cfg, event.host_name)} ({event.host_address})")
    if event.is_service:
        lines.append(f"Service: {event.service_display}")
    lines.append(f"State: {state} · Type: {event.notification_type}")
    if event.long_date_time:
        lines.append(f"When: {event.long_date_time}")
    if event.notification_type == "ACKNOWLEDGEMENT" and event.author:
        lines.append(f"Acked by: {event.author} — {event.comment}")
    body = "\n".join(lines)

    return NtfyMessage(
        topic=topic,
        title=title,
        body=body,
        markdown=False,
        priority=priority,
        tags=[tag],
        click=build_click_url(cfg["icinga"]["web_url"], event),
        actions=build_actions(cfg, event),
        attach_url=attach_url,
        attach_file=attach_file,
        filename=filename or "graph.png",
    )


def target_topics(cfg: dict, event: AlertEvent) -> list:
    topics = []
    if event.ntfy_topic:
        topics.append(event.ntfy_topic)
    routing = cfg.get("routing", {})
    bcast = routing.get("crit_broadcast_topic") or ""
    if bcast and state_in_broadcast(routing, event.state) and bcast not in topics:
        topics.append(bcast)
    return topics


def state_in_broadcast(routing: dict, state: str) -> bool:
    return state in (routing.get("crit_broadcast_states") or [])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Icinga rich-notification dispatcher")
    parser.add_argument("--object-type", choices=["host", "service"], required=True)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true", help="print payload, do not send")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)
    event = AlertEvent.from_env(args.object_type, os.environ)
    log.info("event key=%s type=%s state=%s topic=%s",
             event.key, event.notification_type, event.state, event.ntfy_topic or "-")

    topics = target_topics(cfg, event)
    if not topics:
        log.error("no target topic (NTFY_TOPIC unset and no broadcast match); nothing to send")
        return 0

    # Suppression must FAIL OPEN: if the store (e.g. the shared redis) is unreachable, send the
    # notification rather than going silent — a missed alert is worse than a duplicate.
    try:
        engine = SuppressionEngine.from_config(cfg["suppression"])
        decision = engine.evaluate(event, time.time())
        log.info("suppression: send=%s (%s)", decision.send, decision.reason)
        if not decision.send:
            return 0
    except Exception as exc:
        log.error("suppression store error (%s) — failing OPEN (sending)", exc)

    attach_via = cfg["ntfy"].get("attachment_via", "url")
    attach_url = ""
    attach_file = ""
    display_name = f"{event.host_name}-{event.service_name or 'host'}.png"
    if attach_via != "none":
        graph_path = render_graph(event, cfg["render"])
        log.info("graph: %s", graph_path or "none (text-only)")
        if graph_path:
            if attach_via == "upload":
                attach_file = graph_path
            elif args.dry_run:  # show the URL without pushing to the broker
                attach_url = graph_url(cfg, os.path.basename(graph_path))
            else:  # url: push the PNG to the broker; the phone fetches it from there
                attach_url = upload_graph(
                    cfg, graph_path, timeout=float(cfg["render"].get("timeout", 8))
                )

    client = NtfyClient(cfg["ntfy"]["base_url"], cfg["ntfy"].get("token", ""))
    timeout = float(cfg["ntfy"].get("timeout", 10))
    rc = 0
    for topic in topics:
        msg = build_message(
            cfg, event, topic,
            attach_url=attach_url, attach_file=attach_file, filename=display_name,
        )
        try:
            result = client.publish(msg, timeout=timeout, dry_run=args.dry_run)
            if args.dry_run:
                print(json.dumps(result, indent=2))
            else:
                log.info("published to %s", topic)
        except Exception as exc:
            log.error("publish to %s failed: %s", topic, exc)
            # The alert must get through even if the image does not: if this message carried
            # a graph, retry once text-only so the notification still fires.
            if not args.dry_run and (msg.attach_file or msg.attach_url):
                try:
                    client.publish(build_message(cfg, event, topic), timeout=timeout)
                    log.warning("published to %s text-only (image send failed)", topic)
                except Exception as exc2:
                    log.error("text-only fallback to %s also failed: %s", topic, exc2)
                    rc = 1
            else:
                rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
