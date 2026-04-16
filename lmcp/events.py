"""Internal event model and in-memory event bus for LMCP.

Events are the abstraction. SSE, logging, and future consumers subscribe to
the bus. The bus is decoupled from transport -- it has no HTTP/SSE awareness.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable
import threading

EVENT_VERSION = 1

# Canonical event types emitted by the bus.
EVENT_TYPES = frozenset({
    "client_auth",
    "server_auth",
    "rate_limited",
    "tool_call",
    "tool_result",
    "server_error",
    "config_change",
})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class BusEvent:
    """A single event on the internal bus. Every event has a type, timestamp,
    version, and a payload dict with event-specific data."""

    event_type: str
    event_version: int = EVENT_VERSION
    timestamp: str = field(default_factory=_utc_now_iso)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Subscriber callback type: receives a BusEvent, should not block.
Subscriber = Callable[[BusEvent], None]


class EventBus:
    """Thread-safe in-memory publish/subscribe event bus.

    Delivery is best-effort and non-blocking. If a subscriber raises, the
    exception is silently swallowed so other subscribers are not affected.
    Slow subscribers do not block the publisher or other subscribers.
    """

    def __init__(self) -> None:
        self._subscribers: dict[int, Subscriber] = {}
        self._next_id = 0
        self._lock = threading.Lock()

    def subscribe(self, callback: Subscriber) -> int:
        """Register a subscriber. Returns a subscription ID for unsubscribe."""
        with self._lock:
            sub_id = self._next_id
            self._next_id += 1
            self._subscribers[sub_id] = callback
        return sub_id

    def try_subscribe(self, callback: Subscriber, max_subscribers: int) -> int | None:
        """Atomically check count and subscribe. Returns ID or None if at cap."""
        with self._lock:
            if len(self._subscribers) >= max_subscribers:
                return None
            sub_id = self._next_id
            self._next_id += 1
            self._subscribers[sub_id] = callback
        return sub_id

    def unsubscribe(self, sub_id: int) -> None:
        """Remove a subscriber by ID. No-op if already removed."""
        with self._lock:
            self._subscribers.pop(sub_id, None)

    def publish(self, event: BusEvent) -> None:
        """Publish an event to all subscribers. Best-effort, non-blocking."""
        with self._lock:
            subscribers = list(self._subscribers.values())
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)
