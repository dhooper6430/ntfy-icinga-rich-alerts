"""Tests for the relay's message handling: hostile input must never crash the process,
tokens must cover every Icinga-bound field and expire, and Icinga rejections must be loud."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dispatcher"))

import action_tokens
import relay

SECRET = "test-secret"
TTL = action_tokens.DEFAULT_TTL


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    @property
    def ok(self):
        return self.status_code < 400


class FakeIcinga:
    """Stands in for relay.IcingaClient; records the calls that got through verification."""

    def __init__(self, status_code=200):
        self.status_code = status_code
        self.calls = []

    def acknowledge(self, host, service, author, comment):
        self.calls.append(("ack", host, service, author, comment))
        return FakeResponse(self.status_code)

    def downtime(self, host, service, author, comment, hours):
        self.calls.append(("downtime", host, service, author, comment, hours))
        return FakeResponse(self.status_code)


def signed_message(action="ack", host="web1", service="http", author="alice",
                   ts=None, **extra):
    ts = int(time.time()) if ts is None else ts
    token = action_tokens.sign(SECRET, action, host, service, author,
                               "", extra.get("hours", ""), ts)
    return json.dumps({"action": action, "host": host, "service": service,
                       "author": author, "ts": ts, "token": token, **extra})


def run(message, icinga=None):
    icinga = icinga or FakeIcinga()
    relay.handle(message, SECRET, icinga, "default comment", TTL)
    return icinga


# --- hostile input must not raise (the ack topic is world-writable on ntfy.sh) ---

@pytest.mark.parametrize("payload", [
    "null", "42", "true", "[]", '"a string"', "not json at all", "",
    '{"action": null}',
    '{"action": 1, "host": {"a": 2}}',
    '{"action": "ack", "host": "web1", "token": 1}',
    '{"action": "ack", "host": "web1", "token": true, "ts": []}',
    '{"action": "ack", "host": "web1", "token": "x", "ts": "bogus"}',
])
def test_hostile_input_is_dropped_without_crashing(payload):
    icinga = run(payload)
    assert icinga.calls == []


# --- token verification ---

def test_valid_ack_goes_through():
    icinga = run(signed_message())
    assert icinga.calls == [("ack", "web1", "http", "alice", "default comment")]


def test_valid_downtime_goes_through_with_signed_hours():
    icinga = run(signed_message(action="downtime", hours=1))
    assert icinga.calls == [("downtime", "web1", "http", "alice", "default comment", 1)]


def test_tampered_author_is_rejected():
    msg = json.loads(signed_message())
    msg["author"] = "mallory"
    assert run(json.dumps(msg)).calls == []


def test_injected_comment_is_rejected():
    msg = json.loads(signed_message())
    msg["comment"] = "totally legit"
    assert run(json.dumps(msg)).calls == []


def test_tampered_downtime_hours_is_rejected():
    msg = json.loads(signed_message(action="downtime", hours=1))
    msg["hours"] = 24 * 365
    assert run(json.dumps(msg)).calls == []


def test_expired_token_is_rejected():
    stale = int(time.time()) - TTL - 10
    assert run(signed_message(ts=stale)).calls == []


def test_future_dated_token_is_rejected():
    future = int(time.time()) + action_tokens.CLOCK_SKEW + 60
    assert run(signed_message(ts=future)).calls == []


def test_replay_with_shifted_ts_is_rejected():
    msg = json.loads(signed_message())
    msg["ts"] = msg["ts"] + 1  # try to extend the token's life
    assert run(json.dumps(msg)).calls == []


def test_wrong_secret_is_rejected():
    icinga = FakeIcinga()
    relay.handle(signed_message(), "other-secret", icinga, "default comment", TTL)
    assert icinga.calls == []


# --- Icinga response handling ---

def test_icinga_rejection_is_logged_as_error(caplog):
    run(signed_message(), FakeIcinga(status_code=401))
    assert any(r.levelname == "ERROR" and "REJECTED by Icinga" in r.getMessage()
               for r in caplog.records)


def test_409_is_idempotent_success_not_error(caplog):
    run(signed_message(), FakeIcinga(status_code=409))
    assert not any(r.levelname == "ERROR" for r in caplog.records)


# --- cursor persistence ---

def test_cursor_roundtrip(tmp_path):
    path = str(tmp_path / "relay-cursor")
    c = relay.Cursor(path)
    assert c.last_id == ""
    c.save("msg-abc123")
    assert relay.Cursor(path).last_id == "msg-abc123"


def test_cursor_unwritable_fails_open(tmp_path):
    c = relay.Cursor(str(tmp_path / "no-such-dir" / "cursor"))
    c.save("msg-1")  # must not raise
    assert c.last_id == "msg-1"
