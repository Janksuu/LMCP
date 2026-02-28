from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json
import re
import stat as _stat
import sys as _sys


@dataclass
class LmcpSettings:
    host: str = "127.0.0.1"
    port: int = 7345
    audit_log: str = "logs/audit.log"
    loopback_only: bool = True


@dataclass
class ClientConfig:
    client_id: str
    token: str
    allow_servers: list[str] = field(default_factory=list)


@dataclass
class ToolPolicy:
    mode: str = "allow_all"
    allow_tools: list[str] = field(default_factory=list)
    deny_tools: list[str] = field(default_factory=list)


@dataclass
class ServerTimeouts:
    initialize_s: float | None = None
    tools_list_s: float | None = None
    tools_call_s: float | None = None
    retry_on_timeout: int = 0
    retry_backoff_s: float = 1.0


@dataclass
class ServerConfig:
    server_id: str
    transport: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    tool_policy: ToolPolicy = field(default_factory=ToolPolicy)
    stdio_mode: str = "newline"
    timeouts: ServerTimeouts = field(default_factory=ServerTimeouts)


@dataclass
class Registry:
    path: Path
    lmcp: LmcpSettings
    clients: dict[str, ClientConfig]
    servers: dict[str, ServerConfig]


def _coerce_tool_policy(raw: dict[str, Any] | None) -> ToolPolicy:
    if not raw:
        return ToolPolicy()
    return ToolPolicy(
        mode=str(raw.get("mode", "allow_all")),
        allow_tools=[str(x) for x in raw.get("allow_tools", [])],
        deny_tools=[str(x) for x in raw.get("deny_tools", [])],
    )


def _coerce_server_timeouts(raw: dict[str, Any] | None) -> ServerTimeouts:
    if not raw:
        return ServerTimeouts()

    def _to_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    retry_on_timeout_raw = raw.get("retry_on_timeout", 0)
    try:
        retry_on_timeout = int(retry_on_timeout_raw)
    except (TypeError, ValueError):
        retry_on_timeout = 0

    retry_backoff_s_raw = raw.get("retry_backoff_s", 1.0)
    try:
        retry_backoff_s = float(retry_backoff_s_raw)
    except (TypeError, ValueError):
        retry_backoff_s = 1.0

    return ServerTimeouts(
        initialize_s=_to_float(raw.get("initialize_s")),
        tools_list_s=_to_float(raw.get("tools_list_s")),
        tools_call_s=_to_float(raw.get("tools_call_s")),
        retry_on_timeout=max(0, retry_on_timeout),
        retry_backoff_s=max(0.0, retry_backoff_s),
    )


def load_registry(path: str | Path) -> Registry:
    try:
        import yaml  # type: ignore
    except Exception as exc:  # pragma: no cover - import guard
        raise RuntimeError(
            "PyYAML is required to load LMCP registry.yaml. Install with: pip install pyyaml"
        ) from exc

    registry_path = Path(path).resolve()
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}

    lmcp_raw = data.get("lmcp", {}) or {}
    lmcp = LmcpSettings(
        host=str(lmcp_raw.get("host", "127.0.0.1")),
        port=int(lmcp_raw.get("port", 7345)),
        audit_log=str(lmcp_raw.get("audit_log", "logs/audit.log")),
        loopback_only=bool(lmcp_raw.get("loopback_only", True)),
    )

    clients_raw = data.get("clients", {}) or {}
    clients: dict[str, ClientConfig] = {}
    for client_id, raw in clients_raw.items():
        clients[client_id] = ClientConfig(
            client_id=client_id,
            token=str((raw or {}).get("token", "")),
            allow_servers=[str(x) for x in (raw or {}).get("allow_servers", [])],
        )

    servers_raw = data.get("servers", {}) or {}
    servers: dict[str, ServerConfig] = {}
    for server_id, raw in servers_raw.items():
        raw = raw or {}
        servers[server_id] = ServerConfig(
            server_id=server_id,
            transport=str(raw.get("transport", "stdio")),
            command=raw.get("command"),
            args=[str(x) for x in raw.get("args", [])],
            env={str(k): str(v) for k, v in (raw.get("env", {}) or {}).items()},
            cwd=raw.get("cwd"),
            url=raw.get("url"),
            headers={str(k): str(v) for k, v in (raw.get("headers", {}) or {}).items()},
            tool_policy=_coerce_tool_policy(raw.get("tool_policy")),
            stdio_mode=str(raw.get("stdio_mode", "newline")),
            timeouts=_coerce_server_timeouts(raw.get("timeouts")),
        )

    return Registry(path=registry_path, lmcp=lmcp, clients=clients, servers=servers)


def load_schema(path: str | Path) -> dict[str, Any]:
    schema_path = Path(path).resolve()
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost"}


def validate_registry_data(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    # Basic structure checks (schema-level)
    try:
        import jsonschema  # type: ignore

        schema = load_schema(Path(__file__).resolve().parent.parent / "config" / "registry.schema.json")
        jsonschema.validate(instance=data, schema=schema)
    except Exception as exc:
        errors.append(f"schema_validation_failed:{exc}")
        return errors

    lmcp = data.get("lmcp", {})
    host = str(lmcp.get("host", "127.0.0.1"))
    loopback_only = bool(lmcp.get("loopback_only", True))
    if loopback_only and not _is_loopback_host(host):
        errors.append("lmcp.host must be loopback when loopback_only is true")

    clients = data.get("clients", {}) or {}
    servers = data.get("servers", {}) or {}
    server_ids = set(servers.keys())

    for client_id, cfg in clients.items():
        token = str((cfg or {}).get("token", ""))
        if token.strip().lower().startswith("replace-me") or token.strip() == "":
            errors.append(f"client '{client_id}' has placeholder or empty token")
        allow_servers = cfg.get("allow_servers", []) or []
        for server_id in allow_servers:
            if server_id not in server_ids:
                errors.append(f"client '{client_id}' allows unknown server '{server_id}'")

    for server_id, cfg in servers.items():
        transport = str((cfg or {}).get("transport", ""))
        if transport == "stdio":
            if not (cfg or {}).get("command"):
                errors.append(f"server '{server_id}' stdio missing command")
        elif transport == "http":
            if not (cfg or {}).get("url"):
                errors.append(f"server '{server_id}' http missing url")
            url = str((cfg or {}).get("url", ""))
            if loopback_only and url:
                if not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", url):
                    errors.append(f"server '{server_id}' http url is not loopback while loopback_only is true")
        else:
            errors.append(f"server '{server_id}' has invalid transport '{transport}'")

        timeouts = (cfg or {}).get("timeouts", {}) or {}
        for key in ("initialize_s", "tools_list_s", "tools_call_s", "retry_backoff_s"):
            value = timeouts.get(key)
            if value is None:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                errors.append(f"server '{server_id}' timeouts.{key} must be a number")
                continue
            if numeric <= 0:
                errors.append(f"server '{server_id}' timeouts.{key} must be > 0")

        retry_raw = timeouts.get("retry_on_timeout")
        if retry_raw is not None:
            try:
                retry_value = int(retry_raw)
            except (TypeError, ValueError):
                errors.append(f"server '{server_id}' timeouts.retry_on_timeout must be an integer >= 0")
            else:
                if retry_value < 0:
                    errors.append(f"server '{server_id}' timeouts.retry_on_timeout must be >= 0")

    return errors


def check_registry_permissions(path: Path) -> list[str]:
    """Return warning strings if registry file permissions are too permissive.

    Only meaningful on POSIX systems (Linux, macOS). Returns an empty list on
    Windows, where file access control is managed by ACLs rather than mode bits.
    """
    if _sys.platform == "win32":
        return []
    try:
        import os
        mode = os.stat(path).st_mode
    except OSError:
        return []
    if mode & (_stat.S_IRGRP | _stat.S_IROTH):
        octal = oct(_stat.S_IMODE(mode))
        return [
            f"registry file is readable by group or other users (mode {octal}). "
            f"Restrict with: chmod 600 {path}"
        ]
    return []


def validate_registry_file(path: str | Path) -> list[str]:
    try:
        import yaml  # type: ignore
    except Exception as exc:
        return [f"pyyaml_required:{exc}"]
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return validate_registry_data(data)


def registry_to_json(registry: Registry) -> str:
    payload = {
        "lmcp": registry.lmcp.__dict__,
        "clients": {k: v.__dict__ for k, v in registry.clients.items()},
        "servers": {
            k: {
                **{kk: vv for kk, vv in v.__dict__.items() if kk not in {"tool_policy", "timeouts"}},
                "tool_policy": v.tool_policy.__dict__,
                "timeouts": v.timeouts.__dict__,
            }
            for k, v in registry.servers.items()
        },
        "path": str(registry.path),
    }
    return json.dumps(payload, indent=2)
