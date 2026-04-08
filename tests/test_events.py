"""Tests for the internal event model and event bus."""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from lmcp.events import EVENT_VERSION, EVENT_TYPES, BusEvent, EventBus
from lmcp.audit import AuditEvent, AuditLogger, AUDIT_TO_BUS_EVENT


def test_bus_event_has_required_fields() -> None:
    event = BusEvent(event_type="client_auth", payload={"client_id": "test"})
    d = event.to_dict()
    assert d["event_type"] == "client_auth"
    assert d["event_version"] == EVENT_VERSION
    assert "timestamp" in d
    assert isinstance(d["payload"], dict)


def test_bus_event_version_is_int() -> None:
    event = BusEvent(event_type="test")
    assert isinstance(event.event_version, int)
    assert event.event_version == EVENT_VERSION


def test_event_types_are_defined() -> None:
    assert len(EVENT_TYPES) > 0
    for t in ["client_auth", "server_auth", "rate_limited"]:
        assert t in EVENT_TYPES


def test_subscribe_and_publish() -> None:
    bus = EventBus()
    received: list[BusEvent] = []
    bus.subscribe(lambda e: received.append(e))

    bus.publish(BusEvent(event_type="client_auth", payload={"test": True}))
    assert len(received) == 1
    assert received[0].event_type == "client_auth"
    assert received[0].payload["test"] is True


def test_multiple_subscribers() -> None:
    bus = EventBus()
    received_a: list[BusEvent] = []
    received_b: list[BusEvent] = []
    bus.subscribe(lambda e: received_a.append(e))
    bus.subscribe(lambda e: received_b.append(e))

    bus.publish(BusEvent(event_type="server_auth"))
    assert len(received_a) == 1
    assert len(received_b) == 1


def test_unsubscribe() -> None:
    bus = EventBus()
    received: list[BusEvent] = []
    sub_id = bus.subscribe(lambda e: received.append(e))

    bus.publish(BusEvent(event_type="test"))
    assert len(received) == 1

    bus.unsubscribe(sub_id)
    bus.publish(BusEvent(event_type="test"))
    assert len(received) == 1  # no new event after unsubscribe


def test_unsubscribe_idempotent() -> None:
    bus = EventBus()
    sub_id = bus.subscribe(lambda e: None)
    bus.unsubscribe(sub_id)
    bus.unsubscribe(sub_id)  # should not raise


def test_failing_subscriber_does_not_block_others() -> None:
    bus = EventBus()
    received: list[BusEvent] = []

    def bad_subscriber(e: BusEvent) -> None:
        raise RuntimeError("I fail")

    bus.subscribe(bad_subscriber)
    bus.subscribe(lambda e: received.append(e))

    bus.publish(BusEvent(event_type="test"))
    assert len(received) == 1  # second subscriber still got the event


def test_subscriber_count() -> None:
    bus = EventBus()
    assert bus.subscriber_count == 0
    s1 = bus.subscribe(lambda e: None)
    s2 = bus.subscribe(lambda e: None)
    assert bus.subscriber_count == 2
    bus.unsubscribe(s1)
    assert bus.subscriber_count == 1
    bus.unsubscribe(s2)
    assert bus.subscriber_count == 0


def test_thread_safety() -> None:
    bus = EventBus()
    received: list[BusEvent] = []
    lock = threading.Lock()

    def safe_append(e: BusEvent) -> None:
        with lock:
            received.append(e)

    bus.subscribe(safe_append)

    def publish_batch() -> None:
        for _ in range(100):
            bus.publish(BusEvent(event_type="test"))

    threads = [threading.Thread(target=publish_batch) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(received) == 500


def test_audit_write_emits_bus_event() -> None:
    bus = EventBus()
    received: list[BusEvent] = []
    bus.subscribe(lambda e: received.append(e))

    with tempfile.TemporaryDirectory() as td:
        audit = AuditLogger(Path(td) / "audit.log", event_bus=bus)
        audit.write(AuditEvent(
            event="client_auth",
            client_id="test_client",
            allowed=True,
            reason="token_match",
        ))

    assert len(received) == 1
    assert received[0].event_type == "client_auth"
    assert received[0].payload["client_id"] == "test_client"
    assert received[0].payload["allowed"] is True
    assert received[0].event_version == EVENT_VERSION


def test_audit_write_without_bus() -> None:
    """AuditLogger still works without an event bus."""
    with tempfile.TemporaryDirectory() as td:
        audit = AuditLogger(Path(td) / "audit.log")
        audit.write(AuditEvent(event="client_auth", client_id="x"))
        log_content = (Path(td) / "audit.log").read_text()
        assert "client_auth" in log_content


def test_audit_to_bus_mapping() -> None:
    """All mapped audit events produce the correct bus event type."""
    for audit_event_name, bus_event_name in AUDIT_TO_BUS_EVENT.items():
        bus = EventBus()
        received: list[BusEvent] = []
        bus.subscribe(lambda e: received.append(e))

        with tempfile.TemporaryDirectory() as td:
            audit = AuditLogger(Path(td) / "audit.log", event_bus=bus)
            audit.write(AuditEvent(event=audit_event_name))

        assert received[0].event_type == bus_event_name


def test_unmapped_audit_event_uses_original_name() -> None:
    """Audit events not in the mapping still emit with their original event string."""
    bus = EventBus()
    received: list[BusEvent] = []
    bus.subscribe(lambda e: received.append(e))

    with tempfile.TemporaryDirectory() as td:
        audit = AuditLogger(Path(td) / "audit.log", event_bus=bus)
        audit.write(AuditEvent(event="some_custom_event"))

    assert received[0].event_type == "some_custom_event"
