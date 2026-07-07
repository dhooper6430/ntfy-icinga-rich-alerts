"""Fixed-cooldown suppression with a pluggable state store.

Decision model (per host!service key):
  * Notification types in `always_notify_types` (recovery, ack, downtime, ...) always pass.
  * First time we ever see a key -> send.
  * Hard-state change vs. last notified state -> send (breaks through the cooldown).
  * Repeat PROBLEM of the SAME state -> suppress until the per-state cooldown elapses,
    then send one reminder and reset the timer.

The store is abstracted so a single Icinga master can use SQLite (no extra service) and an
HA pair can share state via Redis. Tests use InMemoryStore.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Optional, Protocol

# Severity rank per state. A transition to a HIGHER rank (escalation — incl. clear->problem and
# problem->worse like WARNING->CRITICAL) breaks through the cooldown and sends ONE alert, then the
# cooldown applies to the new state. Same-or-lower rank (repeats, de-escalations) stays suppressed.
# OK/UP=0 so a recovery-then-problem also counts as an escalation (a new failure episode).
SEVERITY = {"OK": 0, "UP": 0, "WARNING": 1, "UNKNOWN": 2, "CRITICAL": 3, "DOWN": 3}


def _rank(state) -> int:
    return SEVERITY.get((state or "").upper(), 0)


@dataclass
class Decision:
    send: bool
    reason: str


class Store(Protocol):
    def get(self, key: str) -> Optional[dict]: ...
    def put(self, key: str, state: str, ts: float, ntype: str) -> None: ...


class InMemoryStore:
    """Volatile store for tests."""

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    def get(self, key: str) -> Optional[dict]:
        rec = self.data.get(key)
        return dict(rec) if rec else None

    def put(self, key: str, state: str, ts: float, ntype: str) -> None:
        self.data[key] = {
            "last_hard_state": state,
            "last_notified_ts": ts,
            "last_notification_type": ntype,
        }


class SqliteStore:
    """Single-master persistent store (stdlib only, survives restarts)."""

    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS suppression ("
                "key TEXT PRIMARY KEY, last_hard_state TEXT, "
                "last_notified_ts REAL, last_notification_type TEXT)"
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def get(self, key: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT last_hard_state, last_notified_ts, last_notification_type "
                "FROM suppression WHERE key = ?",
                (key,),
            ).fetchone()
        if not row:
            return None
        return {
            "last_hard_state": row[0],
            "last_notified_ts": row[1],
            "last_notification_type": row[2],
        }

    def put(self, key: str, state: str, ts: float, ntype: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO suppression(key, last_hard_state, last_notified_ts, last_notification_type) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "last_hard_state=excluded.last_hard_state, "
                "last_notified_ts=excluded.last_notified_ts, "
                "last_notification_type=excluded.last_notification_type",
                (key, state, ts, ntype),
            )


class RedisStore:
    """HA store shared across Icinga masters."""

    def __init__(self, url: str, ttl_seconds: int = 7 * 24 * 3600) -> None:
        import redis  # lazy import; only needed in HA mode

        self.r = redis.from_url(url, decode_responses=True)
        self.ttl = ttl_seconds

    def get(self, key: str) -> Optional[dict]:
        h = self.r.hgetall(f"supp:{key}")
        if not h:
            return None
        return {
            "last_hard_state": h.get("s"),
            "last_notified_ts": float(h.get("ts", 0.0)),
            "last_notification_type": h.get("t"),
        }

    def put(self, key: str, state: str, ts: float, ntype: str) -> None:
        name = f"supp:{key}"
        self.r.hset(name, mapping={"s": state, "ts": ts, "t": ntype})
        self.r.expire(name, self.ttl)


def build_store(supp_cfg: dict) -> Store:
    backend = (supp_cfg.get("store") or "sqlite").lower()
    if backend == "redis":
        return RedisStore(supp_cfg["redis_url"])
    return SqliteStore(supp_cfg.get("sqlite_path", "/var/lib/icinga-ntfy/suppression.db"))


class SuppressionEngine:
    def __init__(
        self,
        store: Store,
        cooldowns: dict,
        always_notify_types,
        default_cooldown: int = 900,
    ) -> None:
        self.store = store
        self.cooldowns = {k.upper(): int(v) for k, v in (cooldowns or {}).items()}
        self.always_types = {t.upper() for t in (always_notify_types or [])}
        self.default_cooldown = default_cooldown

    @classmethod
    def from_config(cls, supp_cfg: dict) -> "SuppressionEngine":
        return cls(
            store=build_store(supp_cfg),
            cooldowns=supp_cfg.get("cooldowns", {}),
            always_notify_types=supp_cfg.get("always_notify_types", []),
        )

    def evaluate(self, event, now: float) -> Decision:
        """Decide whether to send `event` at time `now`. Records state on every send."""
        key = event.key
        state = event.state

        if event.notification_type in self.always_types:
            self.store.put(key, state, now, event.notification_type)
            return Decision(True, f"always-notify type {event.notification_type}")

        rec = self.store.get(key)
        if rec is None:
            self.store.put(key, state, now, event.notification_type)
            return Decision(True, "first notification for key")

        # Escalation: a jump to a HIGHER severity (WARNING->CRITICAL, or recovery->problem) sends
        # ONE alert and resets the timer. Same-or-lower severity (repeats, de-escalations) is held
        # to the cooldown — so staff get one alert per escalation, not one per check.
        if _rank(state) > _rank(rec["last_hard_state"]):
            self.store.put(key, state, now, event.notification_type)
            return Decision(True, f"escalation {rec['last_hard_state']} -> {state}")

        cooldown = self.cooldowns.get(state, self.default_cooldown)
        if cooldown <= 0:
            self.store.put(key, state, now, event.notification_type)
            return Decision(True, f"no cooldown configured for {state}")

        elapsed = now - float(rec["last_notified_ts"])
        if elapsed >= cooldown:
            self.store.put(key, state, now, event.notification_type)
            return Decision(True, f"cooldown elapsed ({int(elapsed)}s >= {cooldown}s) — reminder")

        return Decision(
            False,
            f"suppressed: {int(elapsed)}s < {cooldown}s cooldown for {state}",
        )
