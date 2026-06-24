"""Render a performance-graph PNG for an alert and return a local file path.

Default backend "grafana": GET Grafana's render API for a parametric panel (the PromQL
lives in the dashboard, templated by $host/$service, so this stays metric-agnostic).
Optional backend "vm": query VictoriaMetrics' Prometheus API and draw a compact sparkline
with matplotlib (lock-screen optimised). Results are cached on disk for cache_ttl seconds.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger("dispatcher.render")

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str, default: int = 10800) -> int:
    """Parse '3h'/'30m'/'1d' into seconds."""
    text = (text or "").strip().lower()
    if not text:
        return default
    try:
        if text[-1] in _UNIT_SECONDS:
            return int(float(text[:-1]) * _UNIT_SECONDS[text[-1]])
        return int(text)
    except ValueError:
        return default


def _cache_path(cache_dir: str, event) -> str:
    os.makedirs(cache_dir, exist_ok=True)
    digest = hashlib.sha1(event.key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(cache_dir, f"{digest}.png")


def render_graph(event, cfg: dict) -> Optional[str]:
    """Return a path to a cached/fresh PNG, or None if rendering failed.

    Never raises: a graph is a nice-to-have. ANY failure (unwritable cache dir, bad
    config, backend/query/render error) returns None so the alert still goes out text-only.
    """
    try:
        cache_dir = cfg.get("cache_dir", "/tmp/icinga-ntfy-graphs")
        ttl = int(cfg.get("cache_ttl", 300))
        path = _cache_path(cache_dir, event)

        if (
            os.path.exists(path)
            and os.path.getsize(path) > 0
            and (time.time() - os.path.getmtime(path)) < ttl
        ):
            log.debug("graph cache hit: %s", path)
            return path

        backend = (cfg.get("backend") or "grafana").lower()
        timeout = float(cfg.get("timeout", 8))
        if backend == "vm":
            ok = _render_vm(event, cfg, path, timeout)
        else:
            ok = _render_grafana(event, cfg, path, timeout)
        return path if ok else None
    except Exception as exc:  # never let a render failure block the alert
        log.warning("graph render failed: %s", exc)
        return None


def _render_grafana(event, cfg: dict, out_path: str, timeout: float) -> bool:
    g = cfg["grafana"]
    base = g["base_url"].rstrip("/")
    url = f"{base}/render/d-solo/{g['dashboard_uid']}/{g['dashboard_slug']}"
    params = {
        "orgId": 1,
        "panelId": g["panel_id"],
        "from": f"now-{cfg.get('window', '3h')}",
        "to": "now",
        "width": g.get("width", 1000),
        "height": g.get("height", 500),
        "scale": g.get("scale", 2),
        "theme": g.get("theme", "light"),
        "var-host": event.host_name,
    }
    if event.is_service:
        params["var-service"] = event.service_name
    headers = {"Authorization": f"Bearer {g['token']}"} if g.get("token") else {}

    resp = requests.get(url, params=params, headers=headers, timeout=timeout, stream=True)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    if "image" not in ctype:
        raise RuntimeError(f"grafana render returned non-image ({ctype}): {resp.text[:200]}")
    with open(out_path, "wb") as fh:
        for chunk in resp.iter_content(8192):
            fh.write(chunk)
    return os.path.getsize(out_path) > 0


def _render_vm(event, cfg: dict, out_path: str, timeout: float) -> bool:
    import matplotlib  # lazy: only needed for the VM backend

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from datetime import datetime, timezone, timedelta

    # Graph x-axis timezone: default UTC (offset 0). Override with render.tz_offset_hours in
    # config.yml to render the x-axis in your local time (e.g. 8 for UTC+8). DST is not handled —
    # this is a fixed offset, fine for a glanceable lock-screen sparkline.
    tz = timezone(timedelta(hours=int(cfg.get("tz_offset_hours", 0))))
    v = cfg["vm"]
    base = v["base_url"].rstrip("/")
    query = v["query_template"].format(host=event.host_name, service=event.service_name)
    window = parse_duration(cfg.get("window", "3h"))
    end = time.time()
    start = end - window
    step = max(window // 300, 15)

    resp = requests.get(
        f"{base}/api/v1/query_range",
        params={"query": query, "start": start, "end": end, "step": step},
        timeout=timeout,
    )
    resp.raise_for_status()
    series = resp.json().get("data", {}).get("result", [])
    if not series:
        raise RuntimeError("VictoriaMetrics returned no series for query")

    # One clean graph — the first/primary metric only (multi-metric small-multiples were too busy
    # for a lock screen). Transparent background + theme-neutral colours so it sits on a light OR
    # dark notification; bigger fonts for a small screen.
    label_key = v.get("series_label", "perfdata_label")
    s = series[0]
    xs = [datetime.fromtimestamp(float(t), tz=tz) for t, _ in s["values"]]
    ys = [float(val) for _, val in s["values"]]
    lbl = s.get("metric", {}).get(label_key) or s.get("metric", {}).get("__name__", "")

    LINE = "#3b9eff"   # saturated blue — reads on both light and dark
    TEXT = "#8a8f98"   # neutral grey — legible on both (one static image can't perfectly match a theme)
    # Negative metrics (e.g. a -48V battery): a magnitude drop makes the value rise toward 0, which
    # on a normal axis trends UP and misreads as "voltage rising". Flip the y-axis (below) so a
    # magnitude drop reads as a DOWN trend — more-negative (healthier) sits at the top.
    neg = bool(ys) and max(ys) <= 0
    fig, ax = plt.subplots(figsize=(7, 2.6), dpi=160)
    ax.plot(xs, ys, linewidth=2.6, color=LINE, solid_capstyle="round")
    ax.fill_between(xs, ys, (max(ys) if neg else min(ys)) if ys else 0, color=LINE, alpha=0.10)
    if lbl:
        ax.set_title(lbl[:40], fontsize=14, color=TEXT, pad=8)
    ax.tick_params(labelsize=12, colors=TEXT, length=0)
    ax.grid(True, axis="y", alpha=0.2, color=TEXT, linewidth=0.7)
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.margins(x=0.02)
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=5))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))
    if neg:
        ax.invert_yaxis()
    fig.tight_layout()
    fig.savefig(out_path, transparent=True)
    plt.close(fig)
    return os.path.getsize(out_path) > 0
