#!/usr/bin/env python3
"""Action broker for ntfy notifications.

Two jobs:
  1. Receive the Acknowledge / Downtime action-button callbacks the phone fires, validate
     the HMAC token, and call the Icinga2 API (kept off the device behind a scoped ApiUser).
  2. Serve the rendered graph PNGs so the phone can fetch the notification image directly.

Run:  gunicorn -b 0.0.0.0:8080 app:app     (or  flask --app app run  for local dev)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

import requests
from flask import Flask, abort, jsonify, request, send_from_directory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s broker: %(message)s")
log = logging.getLogger("broker")

SECRET = os.environ.get("BROKER_SHARED_SECRET", "")
ICINGA_URL = os.environ.get("ICINGA_API_URL", "https://localhost:5665").rstrip("/")
ICINGA_USER = os.environ.get("ICINGA_API_USER", "")
ICINGA_PASSWORD = os.environ.get("ICINGA_API_PASSWORD", "")
ICINGA_CA = os.environ.get("ICINGA_API_CA", "")
ICINGA_INSECURE = os.environ.get("ICINGA_API_INSECURE", "0") == "1"
GRAPH_CACHE_DIR = os.path.abspath(os.environ.get("GRAPH_CACHE_DIR", "/var/cache/icinga-ntfy/graphs"))
DEFAULT_COMMENT = os.environ.get("DEFAULT_COMMENT", "Actioned from ntfy")
MAX_GRAPH_BYTES = int(os.environ.get("MAX_GRAPH_BYTES", 5 * 1024 * 1024))
GRAPH_TTL = int(os.environ.get("GRAPH_TTL", 3600))

app = Flask(__name__)


def sign(value: str) -> str:
    return hmac.new(SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()[:32]


def _verify(value: str, token: str) -> bool:
    return bool(SECRET) and bool(token) and hmac.compare_digest(sign(value), token)


def _icinga_verify():
    if ICINGA_INSECURE:
        return False
    return ICINGA_CA or True


def _icinga_action(action: str, payload: dict):
    resp = requests.post(
        f"{ICINGA_URL}/v1/actions/{action}",
        json=payload,
        auth=(ICINGA_USER, ICINGA_PASSWORD),
        headers={"Accept": "application/json"},
        verify=_icinga_verify(),
        timeout=10,
    )
    return resp


def _icinga_outcome(resp):
    """(ok, already, detail) from an Icinga2 /v1/actions response.

    Icinga signals "already acknowledged" / "already in downtime" as a 409 — at the HTTP
    status and/or the per-object result code. That's the desired end state, so treat it as
    idempotent success rather than surfacing it as an upstream (502) error to the phone.
    """
    results, detail = [], (resp.text or "")[:200]
    try:
        results = resp.json().get("results") or []
    except ValueError:
        results = []
    if results:
        detail = "; ".join(r.get("status", "") for r in results)[:200] or detail
    codes = [int(r["code"]) for r in results if isinstance(r.get("code"), (int, float))]
    already = resp.status_code == 409 or 409 in codes
    ok = (resp.ok and all(200 <= c < 300 for c in codes)) if codes else resp.ok
    return ok, already, detail


def _object_filter(host: str, service: str) -> dict:
    """Build a safe Icinga2 filter using filter_vars (no string interpolation)."""
    if service:
        return {
            "type": "Service",
            "filter": "host.name==hostname && service.name==servicename",
            "filter_vars": {"hostname": host, "servicename": service},
        }
    return {
        "type": "Host",
        "filter": "host.name==hostname",
        "filter_vars": {"hostname": host},
    }


def _payload_in():
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or "").strip()
    service = (data.get("service") or "").strip()
    author = (data.get("author") or "ntfy").strip()
    return data, host, service, author


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.post("/ack")
def ack():
    data, host, service, author = _payload_in()
    if not host:
        abort(400, "missing host")
    if not _verify(f"ack:{host}:{service}", data.get("token", "")):
        abort(403, "bad token")
    payload = {
        **_object_filter(host, service),
        "author": author,
        "comment": data.get("comment") or DEFAULT_COMMENT,
        "notify": True,
        "sticky": False,
    }
    resp = _icinga_action("acknowledge-problem", payload)
    ok, already, detail = _icinga_outcome(resp)
    target = f"{host}{('/' + service) if service else ''}"
    log.info("ack %s by %s -> http=%s ok=%s already=%s", target, author, resp.status_code, ok, already)
    if ok:
        return f"Acknowledged {target}", 200
    if already:
        return f"Already acknowledged {target}", 200
    return f"Icinga error {resp.status_code}: {detail}", 502


@app.post("/downtime")
def downtime():
    data, host, service, author = _payload_in()
    if not host:
        abort(400, "missing host")
    if not _verify(f"downtime:{host}:{service}", data.get("token", "")):
        abort(403, "bad token")
    try:
        hours = float(data.get("hours", 1))
    except (TypeError, ValueError):
        hours = 1.0
    seconds = int(hours * 3600)
    now = int(time.time())
    payload = {
        **_object_filter(host, service),
        "author": author,
        "comment": data.get("comment") or DEFAULT_COMMENT,
        "start_time": now,
        "end_time": now + seconds,
        "duration": seconds,
        "fixed": True,
    }
    resp = _icinga_action("schedule-downtime", payload)
    ok, already, detail = _icinga_outcome(resp)
    target = f"{host}{('/' + service) if service else ''}"
    log.info("downtime %sh %s by %s -> http=%s ok=%s already=%s", hours, target, author, resp.status_code, ok, already)
    if ok:
        return f"Downtime {hours:g}h scheduled for {target}", 200
    if already:
        return f"Downtime already in place for {target}", 200
    return f"Icinga error {resp.status_code}: {detail}", 502


def _prune_graphs():
    """Best-effort removal of graph PNGs older than GRAPH_TTL to keep the dir bounded."""
    try:
        cutoff = time.time() - GRAPH_TTL
        for name in os.listdir(GRAPH_CACHE_DIR):
            path = os.path.join(GRAPH_CACHE_DIR, name)
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except OSError:
        pass


@app.put("/graph/<path:filename>")
def put_graph(filename: str):
    """Dispatcher pushes the rendered PNG here (no shared filesystem required)."""
    filename = os.path.basename(filename)
    if not _verify(filename, request.args.get("t", "")):
        abort(403, "bad token")
    data = request.get_data(cache=False)
    if not data:
        abort(400, "empty body")
    if len(data) > MAX_GRAPH_BYTES:
        abort(413, "too large")
    os.makedirs(GRAPH_CACHE_DIR, exist_ok=True)
    with open(os.path.join(GRAPH_CACHE_DIR, filename), "wb") as fh:
        fh.write(data)
    _prune_graphs()
    return ("stored", 201)


@app.get("/graph/<path:filename>")
def graph(filename: str):
    # basename guards against path traversal; token is HMAC of the filename
    filename = os.path.basename(filename)
    if not _verify(filename, request.args.get("t", "")):
        abort(403, "bad token")
    if not os.path.isfile(os.path.join(GRAPH_CACHE_DIR, filename)):
        abort(404)
    return send_from_directory(GRAPH_CACHE_DIR, filename, mimetype="image/png", max_age=300)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
