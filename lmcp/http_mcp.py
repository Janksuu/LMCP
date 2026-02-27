from __future__ import annotations

import json
import socket
import time
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from .config import ServerConfig


class HttpMcpError(RuntimeError):
    pass


def _parse_sse_response(text: str) -> dict[str, Any]:
    lines = text.replace("\r\n", "\n").split("\n")
    for line in lines:
        line = line.strip()
        if line.startswith("data: "):
            payload = line[6:]
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                continue
    raise HttpMcpError("sse_no_json_data")


def _make_request(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, method="POST")
    for key, value in headers.items():
        req.add_header(key, value)

    try:
        with request.urlopen(req, timeout=timeout_s) as resp:
            content_type = resp.headers.get("content-type", "")
            text = resp.read().decode("utf-8", errors="replace")
            if "text/event-stream" in content_type:
                return _parse_sse_response(text)
            return json.loads(text)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HttpMcpError(f"http_error:{exc.code}:{detail[:500]}") from exc
    except TimeoutError as exc:
        raise HttpMcpError(f"timeout_error:{exc}") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
            raise HttpMcpError(f"timeout_error:{exc}") from exc
        raise HttpMcpError(f"url_error:{exc}") from exc


def _request_with_timeout_retries(
    make_request: Any,
    retry_on_timeout: int,
    retry_backoff_s: float,
) -> dict[str, Any]:
    attempts = max(0, int(retry_on_timeout)) + 1
    for attempt_index in range(attempts):
        try:
            return make_request()
        except HttpMcpError as exc:
            is_timeout = str(exc).startswith("timeout_error:")
            if (not is_timeout) or attempt_index >= attempts - 1:
                raise
            backoff = max(0.0, float(retry_backoff_s))
            if backoff > 0:
                time.sleep(backoff)
    raise HttpMcpError("timeout_error:retry_exhausted")


def http_tools_list(
    server: ServerConfig,
    timeout_s: float | None = None,
    retry_on_timeout: int = 0,
    retry_backoff_s: float = 1.0,
) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    headers.update(server.headers or {})
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    effective_timeout_s = float(timeout_s) if timeout_s is not None else 60.0
    return _request_with_timeout_retries(
        lambda: _make_request(server.url or "", headers, payload, timeout_s=effective_timeout_s),
        retry_on_timeout=retry_on_timeout,
        retry_backoff_s=retry_backoff_s,
    )


def http_call_tool(
    server: ServerConfig,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_s: float | None = None,
) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    headers.update(server.headers or {})
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    effective_timeout_s = float(timeout_s) if timeout_s is not None else 300.0
    return _make_request(server.url or "", headers, payload, timeout_s=effective_timeout_s)
