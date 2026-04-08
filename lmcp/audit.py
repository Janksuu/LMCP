from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
import json
import threading

if TYPE_CHECKING:
    from .events import EventBus


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AuditEvent:
    event: str
    client_id: str | None = None
    server_id: str | None = None
    tool_name: str | None = None
    allowed: bool | None = None
    reason: str | None = None
    detail: dict | None = None
    ts: str = field(default_factory=_utc_now_iso)


# Mapping from AuditEvent.event string to BusEvent.event_type.
# Audit events not in this map are emitted with their original event string.
AUDIT_TO_BUS_EVENT: dict[str, str] = {
    "client_auth": "client_auth",
    "server_auth": "server_auth",
    "rate_limited": "rate_limited",
}


class AuditLogger:
    def __init__(self, path: str | Path, event_bus: EventBus | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._event_bus = event_bus

    def write(self, event: AuditEvent) -> None:
        payload = asdict(event)
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        if self._event_bus is not None:
            from .events import BusEvent
            bus_event_type = AUDIT_TO_BUS_EVENT.get(event.event, event.event)
            self._event_bus.publish(BusEvent(
                event_type=bus_event_type,
                payload=payload,
            ))

