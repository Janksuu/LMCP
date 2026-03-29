from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
import json
import threading


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


class AuditLogger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event: AuditEvent) -> None:
        payload = asdict(event)
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

