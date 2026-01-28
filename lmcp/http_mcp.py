from __future__ import annotations

import json
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
    except URLError as exc:
        raise HttpMcpError(f"url_error:{exc}") from exc


def http_tools_list(server: ServerConfig) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    headers.update(server.headers or {})
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    return _make_request(server.url or "", headers, payload)


def http_call_tool(
    server: ServerConfig, tool_name: str, arguments: dict[str, Any]
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
    return _make_request(server.url or "", headers, payload, timeout_s=300.0)

