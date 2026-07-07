"""End-to-end check that the buttons notify.py builds verify against relay.handle() —
the two sides sign the same canonical value."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dispatcher"))

import notify
from icinga_macros import AlertEvent
from test_relay import TTL, FakeIcinga, SECRET

import relay


@pytest.fixture
def cfg():
    return {
        "ntfy": {"base_url": "https://push.example.com"},
        "icinga": {"web_url": "https://icinga.example.com/icingaweb2"},
        "actions": {"shared_secret": SECRET, "ack_topic": "icinga-acks"},
    }


def make_event(service="http"):
    return AlertEvent(
        object_type="service" if service else "host",
        notification_type="PROBLEM",
        host_name="web1.example.net", host_display="web1", host_address="10.0.0.1",
        state="CRITICAL", output="it broke", display=service or "web1",
        service_name=service, service_display=service,
        user_name="oncall", ntfy_topic="alerts",
    )


def test_buttons_round_trip_through_relay(cfg):
    actions = notify.build_actions(cfg, make_event())
    buttons = [a for a in actions if a["action"] == "http"]
    assert len(buttons) == 2  # Acknowledge + Downtime 1h
    icinga = FakeIcinga()
    for btn in buttons:
        relay.handle(btn["body"], SECRET, icinga, "default comment", TTL)
    assert [c[0] for c in icinga.calls] == ["ack", "downtime"]
    assert all(c[1] == "web1.example.net" and c[3] == "oncall" for c in icinga.calls)
    # the Downtime button's signed hours reach the Icinga call unchanged
    assert icinga.calls[1][5] == 1


def test_button_body_carries_ts_and_token(cfg):
    body = json.loads(notify.build_actions(cfg, make_event())[0]["body"])
    assert isinstance(body["ts"], int)
    assert isinstance(body["token"], str) and len(body["token"]) == 32
