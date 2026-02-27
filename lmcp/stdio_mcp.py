from __future__ import annotations

from dataclasses import dataclass
import json
import os
import subprocess
import threading
import time
from queue import Queue, Empty
from typing import Any

from .config import ServerConfig


class McpProtocolError(RuntimeError):
    pass


def _is_timeout_error(exc: Exception) -> bool:
    return isinstance(exc, McpProtocolError) and str(exc).startswith("read_timeout:")


def _encode_message(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def _encode_newline_message(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _read_headers(stream: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if line is None or line == b"":
            raise McpProtocolError("unexpected_eof_reading_headers")
        if line in (b"\r\n", b"\n"):
            break
        try:
            text = line.decode("ascii", errors="strict").strip()
        except Exception as exc:  # pragma: no cover - defensive
            raise McpProtocolError("invalid_header_encoding") from exc
        if ":" not in text:
            continue
        key, value = text.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers


def _read_message(stream: Any) -> dict[str, Any]:
    headers = _read_headers(stream)
    content_length_raw = headers.get("content-length")
    if not content_length_raw:
        raise McpProtocolError("missing_content_length")
    try:
        content_length = int(content_length_raw)
    except ValueError as exc:
        raise McpProtocolError("invalid_content_length") from exc
    body = stream.read(content_length)
    if body is None or len(body) != content_length:
        raise McpProtocolError("unexpected_eof_reading_body")
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise McpProtocolError("invalid_json_body") from exc


def _read_newline_message(stream: Any) -> dict[str, Any]:
    line = stream.readline()
    if line is None or line == b"":
        raise McpProtocolError("unexpected_eof_reading_line")
    try:
        text = line.decode("utf-8", errors="replace").strip()
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpProtocolError("invalid_json_line") from exc


@dataclass
class StdioMcpSession:
    process: subprocess.Popen[bytes]
    stdio_mode: str = "newline"

    def _read_message_with_timeout(self, timeout_s: float) -> dict[str, Any]:
        if not self.process.stdout:
            raise McpProtocolError("process_stdout_unavailable")
        queue: Queue[dict[str, Any] | Exception] = Queue(maxsize=1)

        def _reader() -> None:
            try:
                if self.stdio_mode == "newline":
                    queue.put(_read_newline_message(self.process.stdout))
                else:
                    queue.put(_read_message(self.process.stdout))
            except Exception as exc:  # pragma: no cover - defensive
                queue.put(exc)

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()
        try:
            item = queue.get(timeout=timeout_s)
        except Empty as exc:
            exit_code = self.process.poll()
            stderr_text = ""
            if exit_code is not None and self.process.stderr:
                try:
                    stderr_text = self.process.stderr.read().decode("utf-8", errors="replace")
                except Exception:
                    stderr_text = ""
            if exit_code is not None:
                detail = f"read_timeout:{timeout_s}s; process_exited:{exit_code}"
                if stderr_text.strip():
                    detail += f"; stderr:{stderr_text.strip()[:800]}"
                raise McpProtocolError(detail) from exc
            raise McpProtocolError(f"read_timeout:{timeout_s}s") from exc
        if isinstance(item, Exception):
            raise item
        return item

    def request(
        self,
        method: str,
        params: dict[str, Any] | None,
        request_id: int,
        timeout_s: float = 60.0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        wire = _encode_newline_message(payload) if self.stdio_mode == "newline" else _encode_message(payload)
        if not self.process.stdin or not self.process.stdout:
            raise McpProtocolError("process_stdio_unavailable")
        self.process.stdin.write(wire)
        self.process.stdin.flush()
        response = self._read_message_with_timeout(timeout_s=timeout_s)
        return response

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        wire = _encode_newline_message(payload) if self.stdio_mode == "newline" else _encode_message(payload)
        if not self.process.stdin:
            raise McpProtocolError("process_stdin_unavailable")
        self.process.stdin.write(wire)
        self.process.stdin.flush()

    def close(self) -> None:
        try:
            self.process.terminate()
        except Exception:
            pass
        try:
            self.process.kill()
        except Exception:
            pass


def spawn_stdio_server(server: ServerConfig) -> StdioMcpSession:
    if server.transport != "stdio":
        raise McpProtocolError(f"server_not_stdio:{server.server_id}")
    if not server.command:
        raise McpProtocolError(f"missing_command:{server.server_id}")

    env = os.environ.copy()
    env.update(server.env)

    process = subprocess.Popen(
        [server.command, *server.args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=server.cwd or None,
        env=env,
        bufsize=0,
    )
    return StdioMcpSession(process=process, stdio_mode=server.stdio_mode)


def _request_with_timeout_retries(
    session: StdioMcpSession,
    method: str,
    params: dict[str, Any] | None,
    request_id: int,
    timeout_s: float,
    retry_on_timeout: int,
    retry_backoff_s: float,
) -> dict[str, Any]:
    attempts = max(0, int(retry_on_timeout)) + 1
    for attempt_index in range(attempts):
        try:
            return session.request(method, params, request_id=request_id, timeout_s=timeout_s)
        except Exception as exc:
            if not _is_timeout_error(exc) or attempt_index >= attempts - 1:
                raise
            backoff = max(0.0, float(retry_backoff_s))
            if backoff > 0:
                time.sleep(backoff)
    raise McpProtocolError("request_retry_exhausted")


def initialize_and_list_tools(
    session: StdioMcpSession,
    initialize_timeout_s: float = 90.0,
    tools_list_timeout_s: float = 90.0,
    retry_on_timeout: int = 0,
    retry_backoff_s: float = 1.0,
) -> dict[str, Any]:
    try:
        from mcp.types import LATEST_PROTOCOL_VERSION  # type: ignore
        protocol_version = str(LATEST_PROTOCOL_VERSION)
    except Exception:
        protocol_version = "2025-11-25"
    initialize_params = {
        "protocolVersion": protocol_version,
        "clientInfo": {"name": "lmcp-v0", "version": "0.1.0"},
        "capabilities": {},
    }
    init_response = _request_with_timeout_retries(
        session,
        "initialize",
        initialize_params,
        request_id=1,
        timeout_s=initialize_timeout_s,
        retry_on_timeout=retry_on_timeout,
        retry_backoff_s=retry_backoff_s,
    )
    session.notify("initialized", {})
    tools_response = _request_with_timeout_retries(
        session,
        "tools/list",
        {},
        request_id=2,
        timeout_s=tools_list_timeout_s,
        retry_on_timeout=retry_on_timeout,
        retry_backoff_s=retry_backoff_s,
    )
    return {"initialize": init_response, "tools_list": tools_response}


def initialize_and_call_tool(
    session: StdioMcpSession,
    tool_name: str,
    arguments: dict[str, Any],
    initialize_timeout_s: float = 90.0,
    tools_call_timeout_s: float = 180.0,
    retry_on_timeout: int = 0,
    retry_backoff_s: float = 1.0,
) -> dict[str, Any]:
    try:
        from mcp.types import LATEST_PROTOCOL_VERSION  # type: ignore
        protocol_version = str(LATEST_PROTOCOL_VERSION)
    except Exception:
        protocol_version = "2025-11-25"

    initialize_params = {
        "protocolVersion": protocol_version,
        "clientInfo": {"name": "lmcp-v0", "version": "0.1.0"},
        "capabilities": {},
    }
    init_response = _request_with_timeout_retries(
        session,
        "initialize",
        initialize_params,
        request_id=1,
        timeout_s=initialize_timeout_s,
        retry_on_timeout=retry_on_timeout,
        retry_backoff_s=retry_backoff_s,
    )
    session.notify("initialized", {})
    call_params = {"name": tool_name, "arguments": arguments}
    call_response = session.request("tools/call", call_params, request_id=2, timeout_s=tools_call_timeout_s)
    return {"initialize": init_response, "call": call_response}
