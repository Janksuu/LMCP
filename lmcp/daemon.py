from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .audit import AuditEvent, AuditLogger
from .config import Registry, load_registry, registry_to_json, validate_registry_file
from .policy import authenticate_client, authorize_server
from .http_mcp import HttpMcpError, http_call_tool, http_tools_list
from .stdio_mcp import (
    McpProtocolError,
    initialize_and_list_tools,
    initialize_and_call_tool,
    spawn_stdio_server,
)


@dataclass
class LmcpDaemon:
    registry: Registry
    audit: AuditLogger

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


def _collect_tools_for_server(daemon: LmcpDaemon, server_id: str) -> list[dict[str, Any]]:
    server = daemon.registry.servers.get(server_id)
    if not server:
        return []
    if server.transport == "http":
        try:
            result = http_tools_list(server)
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
            result = initialize_and_list_tools(session)
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
                        "service": "lmcp-v0",
                        "host": daemon.registry.lmcp.host,
                        "port": daemon.registry.lmcp.port,
                    },
                )
                return

            if path == "/describe":
                _json_response(self, 200, {"ok": True, "describe": daemon.describe()})
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
                    "hints": ["/health", "/describe", "/auth-check", "/server-check"],
                },
            )

        def do_POST(self) -> None:  # noqa: N802 - stdlib name
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
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
                        result = http_call_tool(server, actual_tool, arguments)
                        _json_response(self, 200, {"jsonrpc": "2.0", "id": request_id, "result": result.get("result", result)})
                        return
                    if server.transport == "stdio":
                        session = spawn_stdio_server(server)
                        try:
                            result = initialize_and_call_tool(session, actual_tool, arguments)
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
    parser = argparse.ArgumentParser(description="LMCP v0 daemon skeleton")
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
        help="Start a minimal LMCP HTTP surface for testing",
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
    return parser.parse_args()


def _resolve_audit_path(registry: Registry) -> Path:
    registry_dir = registry.path.parent
    repo_root = registry_dir.parent
    return (repo_root / registry.lmcp.audit_log).resolve()


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
    audit = AuditLogger(_resolve_audit_path(registry))
    daemon = LmcpDaemon(registry=registry, audit=audit)

    if args.print_config:
        print(registry_to_json(registry))
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
        print(f"LMCP HTTP test surface starting on http://{host}:{port}")
        print("Available endpoints: /health, /describe, /auth-check, /server-check")
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
            result = initialize_and_list_tools(session)
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
            try:
                from mcp.types import LATEST_PROTOCOL_VERSION  # type: ignore
                protocol_version = str(LATEST_PROTOCOL_VERSION)
            except Exception:
                protocol_version = "2025-11-25"
            # initialize
            initialize_params = {
                "protocolVersion": protocol_version,
                "clientInfo": {"name": "lmcp-v0", "version": "0.1.0"},
                "capabilities": {},
            }
            init_response = session.request(
                "initialize", initialize_params, request_id=1, timeout_s=90.0
            )
            session.notify("initialized", {})
            call_params = {"name": args.tool, "arguments": tool_args}
            call_response = session.request(
                "tools/call", call_params, request_id=2, timeout_s=180.0
            )
            print(json.dumps({"initialize": init_response, "call": call_response}, indent=2))
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
            result = http_tools_list(server)
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
            result = http_call_tool(server, args.tool, tool_args)
            print(json.dumps(result, indent=2))
            return 0
        except HttpMcpError as exc:
            print(f"LMCP http call error: {exc}")
            return 3

    print("LMCP v0 daemon skeleton")
    print(daemon.describe())
    print("Next step: add MCP transport proxy layer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
