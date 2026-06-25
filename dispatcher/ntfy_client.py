"""Compose and publish an ntfy message.

Default path is JSON publishing with an `attach` URL (the PNG attached by URL). This cleanly
supports multi-line markdown bodies, action buttons, click-through and image attachments.
The "upload" path (PUT the PNG bytes uploaded into ntfy's attachment cache) is a fallback; note
the ntfy header-based message used there is effectively single-line.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

import requests

log = logging.getLogger("dispatcher.ntfy")

PRIORITY_NUM = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


@dataclass
class NtfyMessage:
    topic: str
    title: str
    body: str
    priority: str = "default"
    tags: list = field(default_factory=list)
    click: str = ""
    actions: list = field(default_factory=list)  # list of ntfy action dicts
    attach_url: str = ""
    attach_file: str = ""          # if set -> upload path
    filename: str = "graph.png"
    markdown: bool = True


class NtfyClient:
    def __init__(self, base_url: str, token: str = "") -> None:
        self.base = base_url.rstrip("/")
        self.token = token

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def publish(self, msg: NtfyMessage, timeout: float = 10, dry_run: bool = False):
        if msg.attach_file:
            return self._publish_upload(msg, timeout, dry_run)
        return self._publish_json(msg, timeout, dry_run)

    def _json_body(self, msg: NtfyMessage) -> dict:
        body = {
            "topic": msg.topic,
            "title": msg.title,
            "message": msg.body,
            "priority": PRIORITY_NUM.get(msg.priority, 3),
            "tags": msg.tags,
            "markdown": msg.markdown,
        }
        if msg.click:
            body["click"] = msg.click
        if msg.actions:
            body["actions"] = msg.actions
        if msg.attach_url:
            body["attach"] = msg.attach_url
            body["filename"] = msg.filename
        return body

    def _publish_json(self, msg: NtfyMessage, timeout: float, dry_run: bool):
        body = self._json_body(msg)
        if dry_run:
            return {"mode": "json", "url": f"{self.base}/", "body": body}
        resp = requests.post(f"{self.base}/", json=body, headers=self._auth(), timeout=timeout)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def _publish_upload(self, msg: NtfyMessage, timeout: float, dry_run: bool):
        url = f"{self.base}/{msg.topic}"
        headers = self._auth()
        headers.update(
            {
                "X-Title": msg.title,
                "X-Message": msg.body.replace("\n", " "),  # headers are single-line
                "X-Priority": str(PRIORITY_NUM.get(msg.priority, 3)),
                "X-Filename": msg.filename,
            }
        )
        if msg.tags:
            headers["X-Tags"] = ",".join(msg.tags)
        if msg.click:
            headers["X-Click"] = msg.click
        if msg.actions:
            headers["X-Actions"] = json.dumps(msg.actions)
        if msg.markdown:
            headers["X-Markdown"] = "yes"
        if dry_run:
            shown = {k: v for k, v in headers.items() if k != "Authorization"}
            return {"mode": "upload", "url": url, "headers": shown, "file": msg.attach_file}
        with open(msg.attach_file, "rb") as fh:
            resp = requests.put(url, data=fh, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json() if resp.content else {}
