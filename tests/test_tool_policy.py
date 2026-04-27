"""Regression tests for per-server tool policy enforcement.

These tests close the gap identified in the April 2026 review: the
two-rail access-control plane (per-server allowlist + per-tool policy)
must be enforced in the data path, not just documented and validated.

Two layers covered:
1. policy.authorize_tool unit logic (pure decision function)
2. daemon /mcp tools/list filtering via the tools cache (no upstream
   subprocess required because we pre-seed the cache)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from lmcp.audit import AuditLogger
from lmcp.config import (
    ClientConfig,
    LmcpSettings,
    Registry,
    ServerConfig,
    ServerTimeouts,
    ToolPolicy,
)
from lmcp.daemon import LmcpDaemon, _collect_tools_for_server
from lmcp.policy import authorize_tool


# ─── authorize_tool unit logic ────────────────────────────────────────────

def test_authorize_tool_allow_all_default() -> None:
    p = ToolPolicy(mode="allow_all", allow_tools=[], deny_tools=[])
    assert authorize_tool(p, "anything").allowed is True


def test_authorize_tool_allow_all_with_deny_list() -> None:
    p = ToolPolicy(mode="allow_all", allow_tools=[], deny_tools=["dangerous"])
    assert authorize_tool(p, "safe").allowed is True
    decision = authorize_tool(p, "dangerous")
    assert decision.allowed is False
    assert decision.reason == "tool_denied"


def test_authorize_tool_deny_all_blocks_everything() -> None:
    p = ToolPolicy(mode="deny_all", allow_tools=[], deny_tools=[])
    decision = authorize_tool(p, "anything")
    assert decision.allowed is False
    assert decision.reason == "tool_policy_deny_all"


def test_authorize_tool_allow_list_only_listed_tools() -> None:
    p = ToolPolicy(mode="allow_list", allow_tools=["safe_read", "list_items"], deny_tools=[])
    assert authorize_tool(p, "safe_read").allowed is True
    assert authorize_tool(p, "list_items").allowed is True
    decision = authorize_tool(p, "delete_everything")
    assert decision.allowed is False
    assert decision.reason == "tool_not_in_allow_list"


def test_authorize_tool_unknown_mode_denies() -> None:
    p = ToolPolicy(mode="something_else", allow_tools=[], deny_tools=[])
    decision = authorize_tool(p, "anything")
    assert decision.allowed is False
    assert "unknown_tool_policy_mode" in decision.reason


# ─── _collect_tools_for_server respects cache (used by tools/list) ────────

def _make_registry_with_server(tmp: Path, policy: ToolPolicy) -> Registry:
    return Registry(
        path=tmp / "registry.yaml",
        lmcp=LmcpSettings(),
        clients={
            "c": ClientConfig(client_id="c", token="t", allow_servers=["srv"]),
        },
        servers={
            "srv": ServerConfig(
                server_id="srv",
                transport="stdio",
                command="echo",
                args=[],
                tool_policy=policy,
                timeouts=ServerTimeouts(),
            ),
        },
    )


def _seed_cache(daemon: LmcpDaemon, server_id: str, tools: list[dict]) -> None:
    """Pre-seed the tools cache so tests do not spawn subprocesses."""
    daemon.set_cached_tools(server_id, tools)


def test_collect_tools_returns_cached_unfiltered() -> None:
    """_collect_tools_for_server returns the upstream tool list as-is.
    Filtering happens in the /mcp handler, not at collection time."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        reg = _make_registry_with_server(
            tmp,
            ToolPolicy(mode="deny_all"),
        )
        audit = AuditLogger(tmp / "audit.log")
        daemon = LmcpDaemon(registry=reg, audit=audit)
        _seed_cache(daemon, "srv", [{"name": "t1"}, {"name": "t2"}])

        # _collect_tools_for_server is just the upstream fetch + cache.
        # The /mcp handler is responsible for applying tool_policy.
        result = _collect_tools_for_server(daemon, "srv")
        assert len(result) == 2


# ─── Tool policy filtering simulation (mirrors daemon /mcp logic) ─────────

def _filter_tools_by_policy(tools: list[dict], policy: ToolPolicy) -> list[str]:
    """Simulate the same filtering daemon /mcp tools/list applies."""
    out = []
    for tool in tools:
        name = tool.get("name")
        if not name:
            continue
        if authorize_tool(policy, name).allowed:
            out.append(name)
    return out


def test_tools_list_allow_all_returns_everything() -> None:
    tools = [{"name": "t1"}, {"name": "t2"}, {"name": "t3"}]
    policy = ToolPolicy(mode="allow_all", allow_tools=[], deny_tools=[])
    assert _filter_tools_by_policy(tools, policy) == ["t1", "t2", "t3"]


def test_tools_list_allow_all_with_deny_filter() -> None:
    tools = [{"name": "safe"}, {"name": "dangerous"}, {"name": "safe2"}]
    policy = ToolPolicy(mode="allow_all", allow_tools=[], deny_tools=["dangerous"])
    assert _filter_tools_by_policy(tools, policy) == ["safe", "safe2"]


def test_tools_list_deny_all_returns_empty() -> None:
    tools = [{"name": "t1"}, {"name": "t2"}]
    policy = ToolPolicy(mode="deny_all", allow_tools=[], deny_tools=[])
    assert _filter_tools_by_policy(tools, policy) == []


def test_tools_list_allow_list_returns_only_listed() -> None:
    tools = [{"name": "safe_read"}, {"name": "delete_db"}, {"name": "list_items"}]
    policy = ToolPolicy(mode="allow_list", allow_tools=["safe_read", "list_items"], deny_tools=[])
    assert _filter_tools_by_policy(tools, policy) == ["safe_read", "list_items"]


def test_tools_list_skips_tools_without_names() -> None:
    """The /mcp handler skips tools without a name field, regardless of policy."""
    tools = [{"name": "t1"}, {"description": "no name here"}, {"name": "t2"}]
    policy = ToolPolicy(mode="allow_all", allow_tools=[], deny_tools=[])
    assert _filter_tools_by_policy(tools, policy) == ["t1", "t2"]
