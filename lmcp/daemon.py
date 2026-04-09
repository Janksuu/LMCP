from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import queue
import shutil
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .audit import AuditEvent, AuditLogger
from .config import Registry, check_registry_permissions, check_remote_mode, load_registry, registry_to_json, validate_registry_file
from .events import BusEvent, EventBus
from .management import check_management_auth, build_registry_view, validate_patch, apply_patch
from .policy import authenticate_client, authorize_server
from .http_mcp import HttpMcpError, http_call_tool, http_tools_list
from .stdio_mcp import (
    McpProtocolError,
    initialize_and_list_tools,
    initialize_and_call_tool,
    spawn_stdio_server,
)

STATUS_VERSION = 2

STATUS_REQUIRED_FIELDS = frozenset({
    "status_version",
    "service",
    "host",
    "port",
    "loopback_only",
    "uptime_s",
    "registry_path",
    "audit_log_path",
    "clients",
    "servers",
    "recent_audit_entries",
})

STATUS_CLIENT_REQUIRED_FIELDS = frozenset({
    "client_id",
    "token_status",
    "allow_servers",
    "rate_limit_rpm",
})

STATUS_SERVER_REQUIRED_FIELDS = frozenset({
    "server_id",
    "transport",
    "target",
    "available_hint",
    "tool_policy_mode",
    "timeouts",
})


class _TokenBucket:
    """In-memory token bucket rate limiter. Refills continuously. Thread-safe."""

    def __init__(self, rpm: int) -> None:
        self.rpm = rpm
        self.tokens = float(rpm)
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.rpm, self.tokens + elapsed * (self.rpm / 60.0))
            self.last_refill = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


@dataclass
class LmcpDaemon:
    registry: Registry
    audit: AuditLogger
    event_bus: EventBus | None = None

    def __post_init__(self) -> None:
        self._rate_limiters: dict[str, _TokenBucket] = {}
        self._rate_limiters_lock = threading.Lock()
        self._started_at = time.monotonic()

    def uptime_seconds(self) -> float:
        return round(time.monotonic() - self._started_at, 1)

    def check_rate_limit(self, client_id: str) -> bool:
        """Return True if request is allowed, False if rate-limited."""
        client = self.registry.clients.get(client_id)
        if not client:
            return True
        rpm = client.rate_limit_rpm or self.registry.lmcp.rate_limit_rpm
        if rpm is None:
            return True
        with self._rate_limiters_lock:
            if client_id not in self._rate_limiters:
                self._rate_limiters[client_id] = _TokenBucket(rpm)
            bucket = self._rate_limiters[client_id]
        return bucket.allow()

    def describe(self) -> dict[str, Any]:
        return {
            "host": self.registry.lmcp.host,
            "port": self.registry.lmcp.port,
            "clients": list(self.registry.clients.keys()),
            "servers": list(self.registry.servers.keys()),
            "loopback_only": self.registry.lmcp.loopback_only,
            "registry_path": str(self.registry.path),
        }

    def authenticate(self, client_id: str, token: str | None) -> bool:
        client = self.registry.clients.get(client_id)
        decision = authenticate_client(client, token)
        self.audit.write(
            AuditEvent(
                event="client_auth",
                client_id=client_id,
                allowed=decision.allowed,
                reason=decision.reason,
            )
        )
        return decision.allowed

    def authorize(self, client_id: str, server_id: str) -> bool:
        client = self.registry.clients.get(client_id)
        if client is None:
            self.audit.write(
                AuditEvent(
                    event="server_auth",
                    client_id=client_id,
                    server_id=server_id,
                    allowed=False,
                    reason="unknown_client",
                )
            )
            return False
        decision = authorize_server(client, server_id)
        self.audit.write(
            AuditEvent(
                event="server_auth",
                client_id=client_id,
                server_id=server_id,
                allowed=decision.allowed,
                reason=decision.reason,
            )
        )
        return decision.allowed


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler: BaseHTTPRequestHandler, status: int, html: str) -> None:
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _extract_query(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    parsed = urlparse(handler.path)
    raw = parse_qs(parsed.query)
    result: dict[str, str] = {}
    for key, values in raw.items():
        if not values:
            continue
        result[key] = values[0]
    return result


def _extract_auth(handler: BaseHTTPRequestHandler) -> tuple[str | None, str | None]:
    query = _extract_query(handler)
    client_id = query.get("client_id")
    token = query.get("token")
    if not client_id:
        client_id = handler.headers.get("x-lmcp-client-id")
    if not token:
        token = handler.headers.get("x-lmcp-token")
    return client_id, token


def _mcp_error(handler: BaseHTTPRequestHandler, request_id: Any, code: int, message: str) -> None:
    _json_response(
        handler,
        200,
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
    )


def _server_timeout_seconds(server: Any, timeout_key: str) -> float:
    default_timeouts = {
        "stdio": {"initialize_s": 90.0, "tools_list_s": 90.0, "tools_call_s": 180.0},
        "http": {"initialize_s": 60.0, "tools_list_s": 60.0, "tools_call_s": 300.0},
    }
    default_value = default_timeouts.get(server.transport, {}).get(timeout_key, 60.0)
    configured_value = getattr(getattr(server, "timeouts", None), timeout_key, None)
    if configured_value is None:
        return float(default_value)
    try:
        return max(0.001, float(configured_value))
    except (TypeError, ValueError):
        return float(default_value)


def _server_retry_on_timeout(server: Any) -> int:
    configured_value = getattr(getattr(server, "timeouts", None), "retry_on_timeout", 0)
    try:
        return max(0, int(configured_value))
    except (TypeError, ValueError):
        return 0


def _server_retry_backoff_seconds(server: Any) -> float:
    configured_value = getattr(getattr(server, "timeouts", None), "retry_backoff_s", 1.0)
    try:
        return max(0.001, float(configured_value))
    except (TypeError, ValueError):
        return 1.0


def _collect_tools_for_server(daemon: LmcpDaemon, server_id: str) -> list[dict[str, Any]]:
    server = daemon.registry.servers.get(server_id)
    if not server:
        return []
    if server.transport == "http":
        try:
            result = http_tools_list(
                server,
                timeout_s=_server_timeout_seconds(server, "tools_list_s"),
                retry_on_timeout=_server_retry_on_timeout(server),
                retry_backoff_s=_server_retry_backoff_seconds(server),
            )
            return result.get("result", {}).get("tools", []) or []
        except HttpMcpError:
            return []
    if server.transport == "stdio":
        try:
            session = spawn_stdio_server(server)
        except FileNotFoundError:
            return []
        except Exception:
            return []
        try:
            result = initialize_and_list_tools(
                session,
                initialize_timeout_s=_server_timeout_seconds(server, "initialize_s"),
                tools_list_timeout_s=_server_timeout_seconds(server, "tools_list_s"),
                retry_on_timeout=_server_retry_on_timeout(server),
                retry_backoff_s=_server_retry_backoff_seconds(server),
            )
            return result.get("tools_list", {}).get("result", {}).get("tools", []) or []
        except Exception:
            return []
        finally:
            session.close()
    return []


def _make_handler(daemon: LmcpDaemon) -> type[BaseHTTPRequestHandler]:
    class LmcpHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # silence default stdout logging
            return

        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")

            if path == "" or path == "/health":
                _json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "service": "lmcp-v2",
                        "host": daemon.registry.lmcp.host,
                        "port": daemon.registry.lmcp.port,
                    },
                )
                return

            if path == "/describe":
                _json_response(self, 200, {"ok": True, "describe": daemon.describe()})
                return

            if path == "/status":
                query = _extract_query(self)
                try:
                    limit = max(int(query.get("limit", "10")), 0)
                except ValueError:
                    _json_response(self, 400, {"ok": False, "error": "invalid_limit"})
                    return
                payload = _build_status_payload(
                    registry=daemon.registry,
                    audit_path=_resolve_audit_path(daemon.registry),
                    limit=limit,
                    daemon=daemon,
                )
                _json_response(self, 200, {"ok": True, "status": payload})
                return

            if path == "/ui":
                ui_path = Path(__file__).resolve().parent / "ui.html"
                try:
                    html = ui_path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    _json_response(self, 500, {"ok": False, "error": "ui_file_missing"})
                    return
                _html_response(self, 200, html)
                return

            if path == "/events":
                if daemon.event_bus is None:
                    _json_response(self, 503, {"ok": False, "error": "event_bus_not_available"})
                    return
                query = _extract_query(self)
                event_type_filter = query.get("event_type")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                eq: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=256)

                def _on_event(event: BusEvent) -> None:
                    try:
                        eq.put_nowait(event.to_dict())
                    except queue.Full:
                        pass  # best-effort: drop if subscriber is too slow

                sub_id = daemon.event_bus.subscribe(_on_event)
                try:
                    while True:
                        try:
                            payload = eq.get(timeout=30)
                        except queue.Empty:
                            # Send SSE comment as keepalive
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        if payload is None:
                            break
                        if event_type_filter and payload.get("event_type") != event_type_filter:
                            continue
                        sse_event = payload.get("event_type", "message")
                        sse_data = json.dumps(payload, ensure_ascii=False)
                        self.wfile.write(f"event: {sse_event}\ndata: {sse_data}\n\n".encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # client disconnected
                finally:
                    daemon.event_bus.unsubscribe(sub_id)
                return

            if path == "/registry/view":
                mgmt_token = self.headers.get("x-lmcp-management-token")
                allowed, err_code = check_management_auth(daemon.registry, mgmt_token)
                if not allowed:
                    _json_response(self, 403, {"ok": False, "error": err_code})
                    return
                view = build_registry_view(daemon.registry)
                _json_response(self, 200, {"ok": True, "registry": view})
                return

            if path == "/auth-check":
                query = _extract_query(self)
                client_id = query.get("client_id", "")
                token = query.get("token")
                allowed = daemon.authenticate(client_id, token)
                _json_response(
                    self,
                    200 if allowed else 401,
                    {"ok": allowed, "client_id": client_id, "reason": "ok" if allowed else "denied"},
                )
                return

            if path == "/server-check":
                query = _extract_query(self)
                client_id = query.get("client_id", "")
                token = query.get("token")
                server_id = query.get("server_id", "")

                if not daemon.authenticate(client_id, token):
                    _json_response(
                        self,
                        401,
                        {"ok": False, "client_id": client_id, "server_id": server_id, "reason": "auth_failed"},
                    )
                    return

                allowed = daemon.authorize(client_id, server_id)
                _json_response(
                    self,
                    200 if allowed else 403,
                    {
                        "ok": allowed,
                        "client_id": client_id,
                        "server_id": server_id,
                        "reason": "ok" if allowed else "server_not_allowed",
                    },
                )
                return

            _json_response(
                self,
                404,
                {
                    "ok": False,
                    "error": "not_found",
                    "path": parsed.path,
                    "hints": ["/health", "/describe", "/status", "/ui", "/auth-check", "/server-check"],
                },
            )

        def do_POST(self) -> None:  # noqa: N802 - stdlib name
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")

            # --- Management endpoints ---
            if path in ("/registry/validate", "/registry/apply"):
                mgmt_token = self.headers.get("x-lmcp-management-token")
                allowed, err_code = check_management_auth(daemon.registry, mgmt_token)
                if not allowed:
                    _json_response(self, 403, {"ok": False, "error": err_code})
                    return
                content_length = int(self.headers.get("content-length", "0"))
                body = self.rfile.read(content_length) if content_length > 0 else b""
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except Exception:
                    _json_response(self, 400, {"ok": False, "error": "invalid_json"})
                    return
                patch = payload.get("patch")
                if not isinstance(patch, dict):
                    _json_response(self, 400, {"ok": False, "error": "missing_patch", "message": "Request body must include a 'patch' object."})
                    return

                if path == "/registry/validate":
                    result = validate_patch(daemon.registry.path, patch)
                    _json_response(self, 200, {"ok": True, **result})
                    return

                if path == "/registry/apply":
                    result = apply_patch(
                        registry=daemon.registry,
                        patch=patch,
                        audit=daemon.audit,
                        event_bus=daemon.event_bus,
                    )
                    if result.get("error") == "apply_in_progress":
                        _json_response(self, 409, {"ok": False, "error": "apply_in_progress", "message": "Another apply is in progress. Try again."})
                        return
                    if result.get("error") == "write_failed":
                        _json_response(self, 500, {"ok": False, "error": "write_failed", "message": result.get("message", "Write failed.")})
                        return
                    # Clear rate limiters on successful apply so new RPM values take effect
                    if result.get("applied"):
                        with daemon._rate_limiters_lock:
                            daemon._rate_limiters.clear()
                    _json_response(self, 200, {"ok": True, **result})
                    return

            # --- MCP endpoint ---
            if path != "/mcp":
                _json_response(
                    self,
                    404,
                    {"ok": False, "error": "not_found", "path": parsed.path},
                )
                return

            content_length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(content_length) if content_length > 0 else b""
            try:
                request_payload = json.loads(body.decode("utf-8") or "{}")
            except Exception:
                _json_response(self, 400, {"error": "invalid_json"})
                return

            request_id = request_payload.get("id")
            method = request_payload.get("method")
            params = request_payload.get("params") or {}

            client_id, token = _extract_auth(self)
            if not client_id or not token or not daemon.authenticate(client_id, token):
                _mcp_error(self, request_id, -32001, "unauthorized")
                return

            if not daemon.check_rate_limit(client_id):
                daemon.audit.write(
                    AuditEvent(
                        event="rate_limited",
                        client_id=client_id,
                        allowed=False,
                        reason="rate_limit_exceeded",
                    )
                )
                _mcp_error(self, request_id, -32009, "rate_limited")
                return

            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "lmcp", "version": "0.1.0"},
                }
                _json_response(self, 200, {"jsonrpc": "2.0", "id": request_id, "result": result})
                return

            if method == "tools/list":
                tools: list[dict[str, Any]] = []
                for server_id in daemon.registry.clients[client_id].allow_servers:
                    for tool in _collect_tools_for_server(daemon, server_id):
                        tool_name = tool.get("name")
                        if not tool_name:
                            continue
                        tools.append(
                            {
                                "name": f"{server_id}.{tool_name}",
                                "description": f"[{server_id}] {tool.get('description', '')}".strip(),
                                "inputSchema": tool.get("inputSchema", {}),
                            }
                        )
                _json_response(self, 200, {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}})
                return

            if method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {}) or {}
                if "." not in tool_name:
                    _mcp_error(self, request_id, -32602, "tool name must be prefixed with server_id")
                    return
                server_id, actual_tool = tool_name.split(".", 1)
                if not daemon.authorize(client_id, server_id):
                    _mcp_error(self, request_id, -32003, "server_not_allowed")
                    return
                server = daemon.registry.servers.get(server_id)
                if not server:
                    _mcp_error(self, request_id, -32004, "unknown_server")
                    return
                try:
                    if server.transport == "http":
                        result = http_call_tool(
                            server,
                            actual_tool,
                            arguments,
                            timeout_s=_server_timeout_seconds(server, "tools_call_s"),
                        )
                        _json_response(self, 200, {"jsonrpc": "2.0", "id": request_id, "result": result.get("result", result)})
                        return
                    if server.transport == "stdio":
                        session = spawn_stdio_server(server)
                        try:
                            result = initialize_and_call_tool(
                                session,
                                actual_tool,
                                arguments,
                                initialize_timeout_s=_server_timeout_seconds(server, "initialize_s"),
                                tools_call_timeout_s=_server_timeout_seconds(server, "tools_call_s"),
                                retry_on_timeout=_server_retry_on_timeout(server),
                                retry_backoff_s=_server_retry_backoff_seconds(server),
                            )
                            _json_response(self, 200, {"jsonrpc": "2.0", "id": request_id, "result": result.get("call", result)})
                            return
                        finally:
                            session.close()
                    _mcp_error(self, request_id, -32005, "unsupported_transport")
                    return
                except (McpProtocolError, HttpMcpError) as exc:
                    _mcp_error(self, request_id, -32010, f"tool_call_failed:{exc}")
                    return

            _mcp_error(self, request_id, -32601, "method_not_found")
            return

    return LmcpHandler


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LMCP v2 daemon")
    parser.add_argument(
        "--registry",
        default="config/registry.yaml",
        help="Path to LMCP registry yaml (default: config/registry.yaml)",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print parsed registry as JSON and exit",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run a tiny auth/allowlist self-test and exit",
    )
    parser.add_argument(
        "--serve-http",
        action="store_true",
        help="Start the LMCP HTTP surface",
    )
    parser.add_argument(
        "--stdio-test",
        default="",
        help="Spawn a stdio MCP server by id and list tools (e.g., --stdio-test ollama-mcp)",
    )
    parser.add_argument(
        "--stdio-call",
        default="",
        help="Spawn a stdio MCP server by id and call a tool (e.g., --stdio-call ollama-mcp)",
    )
    parser.add_argument(
        "--tool",
        default="",
        help="Tool name to call when using --stdio-call (e.g., --tool list)",
    )
    parser.add_argument(
        "--args-json",
        default="{}",
        help="JSON object of tool arguments for --stdio-call (default: {})",
    )
    parser.add_argument(
        "--args-file",
        default="",
        help="Path to a JSON file containing tool arguments (overrides --args-json)",
    )
    parser.add_argument(
        "--http-test",
        default="",
        help="Call tools/list on an HTTP/SSE MCP server by id (e.g., --http-test comfyui-mcp-example)",
    )
    parser.add_argument(
        "--http-call",
        default="",
        help="Call tools/call on an HTTP/SSE MCP server by id (e.g., --http-call comfyui-mcp-example)",
    )
    parser.add_argument(
        "--validate-registry",
        action="store_true",
        help="Validate the registry file against schema and guardrails",
    )
    parser.add_argument(
        "--print-schema",
        action="store_true",
        help="Print the LMCP registry JSON schema and exit",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print human-readable LMCP status (clients, servers, recent audit entries) and exit",
    )
    parser.add_argument(
        "--status-json",
        action="store_true",
        help="Print JSON LMCP status payload and exit",
    )
    parser.add_argument(
        "--status-limit",
        type=int,
        default=10,
        help="Number of recent audit entries to include in status output (default: 10)",
    )
    return parser.parse_args()


def _resolve_audit_path(registry: Registry) -> Path:
    registry_dir = registry.path.parent
    repo_root = registry_dir.parent
    return (repo_root / registry.lmcp.audit_log).resolve()


def _server_command_available(command: str | None) -> bool:
    if not command:
        return False
    command_path = Path(command)
    if command_path.is_absolute():
        return command_path.exists()
    return shutil.which(command) is not None


def _read_recent_audit_entries(audit_path: Path, limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not audit_path.exists():
        return []
    try:
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    entries: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            entries.append({"raw": line, "error": "invalid_json"})
            continue
        if isinstance(payload, dict):
            entries.append(payload)
    return entries


def _build_status_payload(
    registry: Registry,
    audit_path: Path,
    limit: int,
    daemon: LmcpDaemon | None = None,
) -> dict[str, Any]:
    clients = []
    for client_id, client in sorted(registry.clients.items()):
        token = client.token.strip()
        token_status = (
            "empty"
            if token == ""
            else "placeholder"
            if token.lower().startswith("replace-me")
            else "set"
        )
        clients.append(
            {
                "client_id": client_id,
                "token_status": token_status,
                "allow_servers": list(client.allow_servers),
                "rate_limit_rpm": client.rate_limit_rpm or registry.lmcp.rate_limit_rpm,
            }
        )

    servers = []
    for server_id, server in sorted(registry.servers.items()):
        if server.transport == "stdio":
            target = " ".join([server.command or "", *server.args]).strip()
            available_hint = _server_command_available(server.command)
        else:
            target = server.url or ""
            available_hint = bool(server.url)

        servers.append(
            {
                "server_id": server_id,
                "transport": server.transport,
                "target": target,
                "available_hint": available_hint,
                "tool_policy_mode": server.tool_policy.mode,
                "timeouts": {
                    "initialize_s": _server_timeout_seconds(server, "initialize_s"),
                    "tools_list_s": _server_timeout_seconds(server, "tools_list_s"),
                    "tools_call_s": _server_timeout_seconds(server, "tools_call_s"),
                    "retry_on_timeout": _server_retry_on_timeout(server),
                    "retry_backoff_s": _server_retry_backoff_seconds(server),
                },
            }
        )

    return {
        "status_version": STATUS_VERSION,
        "service": "lmcp-v2",
        "host": registry.lmcp.host,
        "port": registry.lmcp.port,
        "loopback_only": registry.lmcp.loopback_only,
        "uptime_s": daemon.uptime_seconds() if daemon else None,
        "registry_path": str(registry.path),
        "audit_log_path": str(audit_path),
        "clients": clients,
        "servers": servers,
        "recent_audit_entries": _read_recent_audit_entries(audit_path, limit=limit),
    }


def _print_status_human(payload: dict[str, Any]) -> None:
    print("LMCP status")
    uptime = payload.get("uptime_s")
    uptime_str = f"  uptime: {uptime}s" if uptime is not None else ""
    print(
        f"- service: {payload.get('service')}  host: {payload.get('host')}  port: {payload.get('port')}  loopback_only: {payload.get('loopback_only')}{uptime_str}"
    )
    print(f"- registry: {payload.get('registry_path')}")
    print(f"- audit_log: {payload.get('audit_log_path')}")

    print("\nClients:")
    clients = payload.get("clients", []) or []
    if not clients:
        print("- none")
    for client in clients:
        allow_servers = ", ".join(client.get("allow_servers", []))
        rpm = client.get("rate_limit_rpm")
        rpm_str = f"  rpm={rpm}" if rpm is not None else ""
        print(
            f"- {client.get('client_id')}  token={client.get('token_status')}  allow=[{allow_servers}]{rpm_str}"
        )

    print("\nServers:")
    servers = payload.get("servers", []) or []
    if not servers:
        print("- none")
    for server in servers:
        print(
            f"- {server.get('server_id')}  transport={server.get('transport')}  available_hint={server.get('available_hint')}  policy={server.get('tool_policy_mode')}"
        )
        print(f"  target: {server.get('target')}")
        timeouts = server.get("timeouts", {}) or {}
        print(
            f"  timeouts: initialize={timeouts.get('initialize_s')}s list={timeouts.get('tools_list_s')}s call={timeouts.get('tools_call_s')}s retry_on_timeout={timeouts.get('retry_on_timeout')} backoff={timeouts.get('retry_backoff_s')}s"
        )

    print("\nRecent audit entries:")
    recent_entries = payload.get("recent_audit_entries", []) or []
    if not recent_entries:
        print("- none")
    for entry in recent_entries:
        if "raw" in entry:
            print(f"- invalid entry: {entry.get('raw')}")
            continue
        print(
            f"- {entry.get('ts', '?')}  event={entry.get('event')}  client={entry.get('client_id')}  server={entry.get('server_id')}  allowed={entry.get('allowed')}  reason={entry.get('reason')}"
        )


def run() -> int:
    args = _parse_args()
    if args.print_schema:
        schema_path = Path(__file__).resolve().parent.parent / "config" / "registry.schema.json"
        print(schema_path.read_text(encoding="utf-8"))
        return 0

    if args.validate_registry:
        errors = validate_registry_file(args.registry)
        if errors:
            print("LMCP registry validation failed:")
            for err in errors:
                print(f"- {err}")
            return 2
        print("LMCP registry validation passed.")
        return 0

    registry = load_registry(args.registry)
    for _warn in check_registry_permissions(registry.path):
        print(f"WARNING: {_warn}", file=sys.stderr)
    for _warn in check_remote_mode(registry.lmcp):
        print(f"WARNING: {_warn}", file=sys.stderr)
    event_bus = EventBus()
    audit = AuditLogger(_resolve_audit_path(registry), event_bus=event_bus)
    daemon = LmcpDaemon(registry=registry, audit=audit, event_bus=event_bus)

    if args.print_config:
        print(registry_to_json(registry))
        return 0

    if args.status or args.status_json:
        status_payload = _build_status_payload(
            registry=registry,
            audit_path=_resolve_audit_path(registry),
            limit=max(args.status_limit, 0),
        )
        if args.status_json:
            print(json.dumps(status_payload, indent=2))
        else:
            _print_status_human(status_payload)
        return 0

    if args.self_test:
        print("LMCP self-test starting...")
        sample_client = next(iter(registry.clients.values()))
        auth_ok = daemon.authenticate(sample_client.client_id, sample_client.token)
        print(f"auth({sample_client.client_id}) => {auth_ok}")
        for server_id in sample_client.allow_servers:
            allow_ok = daemon.authorize(sample_client.client_id, server_id)
            print(f"allow({sample_client.client_id} -> {server_id}) => {allow_ok}")
        print("LMCP self-test complete.")
        return 0

    if args.serve_http:
        host = registry.lmcp.host
        port = registry.lmcp.port
        ui_url = f"http://{host}:{port}/ui"
        print(f"LMCP HTTP surface starting on http://{host}:{port}")
        print("Available endpoints: /health, /describe, /status, /ui, /events, /auth-check, /server-check, /mcp")
        try:
            open_ui = input(f"Open UI in browser? ({ui_url}) [Y/n] ").strip().lower()
        except EOFError:
            open_ui = "n"
        if open_ui in ("", "y", "yes"):
            import webbrowser
            webbrowser.open(ui_url)
        server = ThreadingHTTPServer((host, port), _make_handler(daemon))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("LMCP HTTP surface stopped.")
        finally:
            server.server_close()
        return 0

    if args.stdio_test:
        server_id = args.stdio_test
        server = registry.servers.get(server_id)
        if server is None:
            print(f"LMCP stdio test: unknown server_id '{server_id}'")
            return 2
        if server.transport != "stdio":
            print(f"LMCP stdio test: server '{server_id}' is not stdio")
            return 2
        print(f"LMCP stdio test: spawning '{server_id}' via: {server.command} {' '.join(server.args)}")
        try:
            session = spawn_stdio_server(server)
            result = initialize_and_list_tools(
                session,
                initialize_timeout_s=_server_timeout_seconds(server, "initialize_s"),
                tools_list_timeout_s=_server_timeout_seconds(server, "tools_list_s"),
                retry_on_timeout=_server_retry_on_timeout(server),
                retry_backoff_s=_server_retry_backoff_seconds(server),
            )
            print(json.dumps(result, indent=2))
            return 0
        except McpProtocolError as exc:
            print(f"LMCP stdio test protocol error: {exc}")
            return 3
        except FileNotFoundError:
            print("LMCP stdio test failed: command not found on PATH.")
            return 4
        finally:
            try:
                session.close()  # type: ignore[name-defined]
            except Exception:
                pass

    if args.stdio_call:
        server_id = args.stdio_call
        server = registry.servers.get(server_id)
        if server is None:
            print(f"LMCP stdio call: unknown server_id '{server_id}'")
            return 2
        if server.transport != "stdio":
            print(f"LMCP stdio call: server '{server_id}' is not stdio")
            return 2
        if not args.tool:
            print("LMCP stdio call: missing --tool")
            return 2
        try:
            if args.args_file:
                tool_args = json.loads(Path(args.args_file).read_text(encoding="utf-8"))
            else:
                tool_args = json.loads(args.args_json or "{}")
            if not isinstance(tool_args, dict):
                print("LMCP stdio call: --args-json must decode to an object")
                return 2
        except json.JSONDecodeError as exc:
            print(f"LMCP stdio call: invalid --args-json: {exc}")
            return 2
        except OSError as exc:
            print(f"LMCP stdio call: unable to read --args-file: {exc}")
            return 2
        print(
            f"LMCP stdio call: spawning '{server_id}' via: {server.command} {' '.join(server.args)}"
        )
        session = None
        try:
            session = spawn_stdio_server(server)
            result = initialize_and_call_tool(
                session,
                args.tool,
                tool_args,
                initialize_timeout_s=_server_timeout_seconds(server, "initialize_s"),
                tools_call_timeout_s=_server_timeout_seconds(server, "tools_call_s"),
                retry_on_timeout=_server_retry_on_timeout(server),
                retry_backoff_s=_server_retry_backoff_seconds(server),
            )
            print(json.dumps(result, indent=2))
            return 0
        except McpProtocolError as exc:
            print(f"LMCP stdio call protocol error: {exc}")
            return 3
        except FileNotFoundError:
            print("LMCP stdio call failed: command not found on PATH.")
            return 4
        finally:
            try:
                if session:
                    session.close()
            except Exception:
                pass

    if args.http_test:
        server_id = args.http_test
        server = registry.servers.get(server_id)
        if server is None:
            print(f"LMCP http test: unknown server_id '{server_id}'")
            return 2
        if server.transport != "http":
            print(f"LMCP http test: server '{server_id}' is not http")
            return 2
        if not server.url:
            print(f"LMCP http test: server '{server_id}' missing url")
            return 2
        print(f"LMCP http test: {server_id} -> {server.url}")
        try:
            result = http_tools_list(
                server,
                timeout_s=_server_timeout_seconds(server, "tools_list_s"),
                retry_on_timeout=_server_retry_on_timeout(server),
                retry_backoff_s=_server_retry_backoff_seconds(server),
            )
            print(json.dumps(result, indent=2))
            return 0
        except HttpMcpError as exc:
            print(f"LMCP http test error: {exc}")
            return 3

    if args.http_call:
        server_id = args.http_call
        server = registry.servers.get(server_id)
        if server is None:
            print(f"LMCP http call: unknown server_id '{server_id}'")
            return 2
        if server.transport != "http":
            print(f"LMCP http call: server '{server_id}' is not http")
            return 2
        if not server.url:
            print(f"LMCP http call: server '{server_id}' missing url")
            return 2
        if not args.tool:
            print("LMCP http call: missing --tool")
            return 2
        try:
            if args.args_file:
                tool_args = json.loads(Path(args.args_file).read_text(encoding="utf-8"))
            else:
                tool_args = json.loads(args.args_json or "{}")
            if not isinstance(tool_args, dict):
                print("LMCP http call: --args-json must decode to an object")
                return 2
        except json.JSONDecodeError as exc:
            print(f"LMCP http call: invalid --args-json: {exc}")
            return 2
        except OSError as exc:
            print(f"LMCP http call: unable to read --args-file: {exc}")
            return 2
        print(f"LMCP http call: {server_id} -> {server.url} -> {args.tool}")
        try:
            result = http_call_tool(
                server,
                args.tool,
                tool_args,
                timeout_s=_server_timeout_seconds(server, "tools_call_s"),
            )
            print(json.dumps(result, indent=2))
            return 0
        except HttpMcpError as exc:
            print(f"LMCP http call error: {exc}")
            return 3

    print("LMCP v2 daemon")
    print(daemon.describe())
    print("Use --help for available commands.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
