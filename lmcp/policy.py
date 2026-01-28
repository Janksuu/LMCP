from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .config import ClientConfig, ServerConfig, ToolPolicy


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str


def authenticate_client(client: ClientConfig | None, provided_token: str | None) -> PolicyDecision:
    if client is None:
        return PolicyDecision(False, "unknown_client")
    if not client.token:
        return PolicyDecision(False, "client_missing_token")
    if not provided_token:
        return PolicyDecision(False, "missing_token")
    if client.token != provided_token:
        return PolicyDecision(False, "invalid_token")
    return PolicyDecision(True, "ok")


def authorize_server(client: ClientConfig, server_id: str) -> PolicyDecision:
    if server_id not in set(client.allow_servers):
        return PolicyDecision(False, "server_not_allowed")
    return PolicyDecision(True, "ok")


def authorize_tool(tool_policy: ToolPolicy, tool_name: str) -> PolicyDecision:
    if tool_policy.mode == "deny_all":
        return PolicyDecision(False, "tool_policy_deny_all")
    if tool_policy.mode == "allow_all":
        if tool_name in set(tool_policy.deny_tools):
            return PolicyDecision(False, "tool_denied")
        return PolicyDecision(True, "ok")
    if tool_policy.mode == "allow_list":
        if tool_name in set(tool_policy.allow_tools):
            return PolicyDecision(True, "ok")
        return PolicyDecision(False, "tool_not_in_allow_list")
    return PolicyDecision(False, f"unknown_tool_policy_mode:{tool_policy.mode}")

