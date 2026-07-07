"""HMAC signing for the Acknowledge / Downtime action messages (shared by notify.py + relay.py).

The token covers EVERY field the relay passes to the Icinga API — action, host, service, author,
comment and hours — plus an issue timestamp, so a captured message can't be replayed with a
different author, comment or downtime duration, and expires after `actions.token_ttl` seconds
(default 24h: long enough that a notification sitting on the phone overnight still works).

The signed value is a compact JSON array rather than a ":"-joined string so field values that
themselves contain ":" can't be shifted between fields.
"""
from __future__ import annotations

import hashlib
import hmac
import json

DEFAULT_TTL = 86400   # seconds a signed action stays valid
CLOCK_SKEW = 300      # tolerated seconds of dispatcher/relay clock disagreement


def _canonical(action: str, host: str, service: str, author: str,
               comment: str, hours, ts: int) -> str:
    return json.dumps(
        [action, host, service, author, comment, str(hours), int(ts)],
        separators=(",", ":"),
    )


def sign(secret: str, action: str, host: str, service: str, author: str,
         comment: str, hours, ts: int) -> str:
    value = _canonical(action, host, service, author, comment, hours, ts)
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()[:32]


def verify(secret: str, token, now: float, ttl: int, *, action: str, host: str,
           service: str, author: str, comment: str, hours, ts) -> bool:
    """Constant-time token check. Rejects missing/non-string tokens (an attacker can post
    arbitrary JSON to the ack topic — never let a weird type reach compare_digest), stale or
    future-dated timestamps, and any mismatch in the signed fields."""
    if not secret or not isinstance(token, str) or not token:
        return False
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return False
    if ts > now + CLOCK_SKEW or now - ts > ttl:
        return False
    expected = sign(secret, action, host, service, author, comment, hours, ts)
    return hmac.compare_digest(expected, token)
