"""Regression test for the /status API contract.

Ensures required fields are present in the status payload. If this test fails,
it means a required field was removed or renamed -- update the contract docs
and bump STATUS_VERSION before fixing.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from lmcp.config import (
    ClientConfig,
    LmcpSettings,
    Registry,
    ServerConfig,
    ServerTimeouts,
    ToolPolicy,
)
from lmcp.daemon import (
    STATUS_VERSION,
    STATUS_REQUIRED_FIELDS,
    STATUS_CLIENT_REQUIRED_FIELDS,
    STATUS_SERVER_REQUIRED_FIELDS,
    _build_status_payload,
)


def _make_registry(tmp: Path) -> Registry:
    return Registry(
        path=tmp / "registry.yaml",
        lmcp=LmcpSettings(),
        clients={
            "test_client": ClientConfig(
                client_id="test_client",
                token="test-token-value",
                allow_servers=["test_server"],
            ),
        },
        servers={
            "test_server": ServerConfig(
                server_id="test_server",
                transport="stdio",
                command="echo",
                args=["hello"],
                tool_policy=ToolPolicy(),
                timeouts=ServerTimeouts(),
            ),
        },
    )


def test_status_version_is_int() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        registry = _make_registry(tmp)
        payload = _build_status_payload(registry, tmp / "audit.log", limit=10)
        assert isinstance(payload["status_version"], int)
        assert payload["status_version"] == STATUS_VERSION


def test_top_level_required_fields() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        registry = _make_registry(tmp)
        payload = _build_status_payload(registry, tmp / "audit.log", limit=10)
        missing = STATUS_REQUIRED_FIELDS - set(payload.keys())
        assert not missing, f"Missing top-level fields: {missing}"


def test_client_required_fields() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        registry = _make_registry(tmp)
        payload = _build_status_payload(registry, tmp / "audit.log", limit=10)
        assert len(payload["clients"]) > 0, "Expected at least one client"
        for client in payload["clients"]:
            missing = STATUS_CLIENT_REQUIRED_FIELDS - set(client.keys())
            assert not missing, f"Client {client.get('client_id')!r} missing fields: {missing}"


def test_server_required_fields() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        registry = _make_registry(tmp)
        payload = _build_status_payload(registry, tmp / "audit.log", limit=10)
        assert len(payload["servers"]) > 0, "Expected at least one server"
        for server in payload["servers"]:
            missing = STATUS_SERVER_REQUIRED_FIELDS - set(server.keys())
            assert not missing, f"Server {server.get('server_id')!r} missing fields: {missing}"


def test_server_timeouts_fields() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        registry = _make_registry(tmp)
        payload = _build_status_payload(registry, tmp / "audit.log", limit=10)
        expected_keys = {"initialize_s", "tools_list_s", "tools_call_s", "retry_on_timeout", "retry_backoff_s"}
        for server in payload["servers"]:
            assert "timeouts" in server
            missing = expected_keys - set(server["timeouts"].keys())
            assert not missing, f"Server {server['server_id']!r} timeouts missing: {missing}"


def test_token_status_values() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        registry = _make_registry(tmp)
        # Override with all three token states
        registry.clients["empty"] = ClientConfig(client_id="empty", token="")
        registry.clients["placeholder"] = ClientConfig(client_id="placeholder", token="replace-me-123")
        registry.clients["real"] = ClientConfig(client_id="real", token="actual-secret")

        payload = _build_status_payload(registry, tmp / "audit.log", limit=10)
        statuses = {c["client_id"]: c["token_status"] for c in payload["clients"]}
        assert statuses["empty"] == "empty"
        assert statuses["placeholder"] == "placeholder"
        assert statuses["real"] == "set"
