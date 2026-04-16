"""Tests for the registry management API."""

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
from lmcp.management import (
    build_registry_view,
    check_management_auth,
    validate_patch,
    apply_patch,
    _merge_patch,
    _compute_changes,
)
from lmcp.audit import AuditLogger
from lmcp.events import EventBus, BusEvent


def _make_registry(tmp: Path, management_token: str | None = "mgmt-secret") -> Registry:
    # Write a minimal registry.yaml so apply can read it
    yaml_content = """
lmcp:
  host: 127.0.0.1
  port: 7345
  audit_log: logs/audit.log
  loopback_only: true
clients:
  vscode:
    token: "real-token-abc"
    allow_servers:
      - test-server
servers:
  test-server:
    transport: stdio
    command: echo
    args:
      - hello
"""
    registry_path = tmp / "registry.yaml"
    registry_path.write_text(yaml_content, encoding="utf-8")

    return Registry(
        path=registry_path,
        lmcp=LmcpSettings(management_token=management_token),
        clients={
            "vscode": ClientConfig(
                client_id="vscode",
                token="real-token-abc",
                allow_servers=["test-server"],
            ),
        },
        servers={
            "test-server": ServerConfig(
                server_id="test-server",
                transport="stdio",
                command="echo",
                args=["hello"],
                tool_policy=ToolPolicy(),
                timeouts=ServerTimeouts(),
            ),
        },
    )


# --- Auth ---

def test_management_auth_disabled() -> None:
    reg = Registry(
        path=Path("."),
        lmcp=LmcpSettings(management_token=None),
        clients={}, servers={},
    )
    allowed, err = check_management_auth(reg, "any-token")
    assert not allowed
    assert err == "management_disabled"


def test_management_auth_wrong_token() -> None:
    reg = Registry(
        path=Path("."),
        lmcp=LmcpSettings(management_token="correct"),
        clients={}, servers={},
    )
    allowed, err = check_management_auth(reg, "wrong")
    assert not allowed
    assert err == "management_unauthorized"


def test_management_auth_correct() -> None:
    reg = Registry(
        path=Path("."),
        lmcp=LmcpSettings(management_token="correct"),
        clients={}, servers={},
    )
    allowed, err = check_management_auth(reg, "correct")
    assert allowed
    assert err == ""


# --- View ---

def test_view_redacts_tokens() -> None:
    with tempfile.TemporaryDirectory() as td:
        reg = _make_registry(Path(td))
        view = build_registry_view(reg)
        assert "token" not in view["clients"]["vscode"]
        assert view["clients"]["vscode"]["token_status"] == "set"


def test_view_includes_server_fields() -> None:
    with tempfile.TemporaryDirectory() as td:
        reg = _make_registry(Path(td))
        view = build_registry_view(reg)
        server = view["servers"]["test-server"]
        assert server["transport"] == "stdio"
        assert server["command"] == "echo"
        assert server["args"] == ["hello"]
        assert "tool_policy" in server
        assert "timeouts" in server


def test_view_excludes_management_token() -> None:
    with tempfile.TemporaryDirectory() as td:
        reg = _make_registry(Path(td))
        view = build_registry_view(reg)
        assert "management_token" not in view["lmcp"]


# --- Merge ---

def test_merge_patch_adds_client() -> None:
    current = {"clients": {"a": {"token": "x", "allow_servers": []}}, "servers": {}}
    patch = {"clients": {"b": {"token": "y", "allow_servers": []}}}
    merged = _merge_patch(current, patch)
    assert "a" in merged["clients"]
    assert "b" in merged["clients"]


def test_merge_patch_removes_client() -> None:
    current = {"clients": {"a": {"token": "x"}, "b": {"token": "y"}}, "servers": {}}
    patch = {"clients": {"b": None}}
    merged = _merge_patch(current, patch)
    assert "a" in merged["clients"]
    assert "b" not in merged["clients"]


def test_merge_patch_removes_server_cascades() -> None:
    current = {
        "clients": {"c1": {"token": "x", "allow_servers": ["s1", "s2"]}},
        "servers": {"s1": {"transport": "stdio"}, "s2": {"transport": "http"}},
    }
    patch = {"servers": {"s1": None}}
    merged = _merge_patch(current, patch)
    assert "s1" not in merged["servers"]
    assert merged["clients"]["c1"]["allow_servers"] == ["s2"]


def test_merge_patch_updates_field() -> None:
    current = {
        "clients": {"a": {"token": "x", "allow_servers": ["s1"]}},
        "servers": {"s1": {"transport": "stdio"}},
    }
    patch = {"clients": {"a": {"allow_servers": ["s1", "s2"]}}}
    merged = _merge_patch(current, patch)
    assert merged["clients"]["a"]["allow_servers"] == ["s1", "s2"]
    assert merged["clients"]["a"]["token"] == "x"  # preserved


def test_merge_patch_deep_merges_nested_dicts() -> None:
    """Regression: shallow merge would clobber unrelated nested fields.
    Patching tool_policy.allow_tools must preserve tool_policy.mode."""
    current = {
        "clients": {},
        "servers": {
            "s1": {
                "transport": "stdio",
                "tool_policy": {
                    "mode": "allow_list",
                    "allow_tools": ["old_tool"],
                    "deny_tools": ["bad_tool"],
                },
            },
        },
    }
    patch = {"servers": {"s1": {"tool_policy": {"allow_tools": ["new_tool"]}}}}
    merged = _merge_patch(current, patch)
    # allow_tools replaced
    assert merged["servers"]["s1"]["tool_policy"]["allow_tools"] == ["new_tool"]
    # mode preserved (not clobbered)
    assert merged["servers"]["s1"]["tool_policy"]["mode"] == "allow_list"
    # deny_tools preserved (not clobbered)
    assert merged["servers"]["s1"]["tool_policy"]["deny_tools"] == ["bad_tool"]
    # transport preserved
    assert merged["servers"]["s1"]["transport"] == "stdio"


def test_merge_patch_deep_merges_timeouts() -> None:
    """Patching one timeout field must preserve the others."""
    current = {
        "clients": {},
        "servers": {
            "s1": {
                "transport": "stdio",
                "timeouts": {
                    "initialize_s": 30,
                    "tools_list_s": 30,
                    "tools_call_s": 300,
                    "retry_on_timeout": 1,
                },
            },
        },
    }
    patch = {"servers": {"s1": {"timeouts": {"tools_call_s": 600}}}}
    merged = _merge_patch(current, patch)
    assert merged["servers"]["s1"]["timeouts"]["tools_call_s"] == 600
    assert merged["servers"]["s1"]["timeouts"]["initialize_s"] == 30
    assert merged["servers"]["s1"]["timeouts"]["tools_list_s"] == 30
    assert merged["servers"]["s1"]["timeouts"]["retry_on_timeout"] == 1


# --- Changes ---

def test_compute_changes_detects_add() -> None:
    current = {"clients": {}, "servers": {}}
    merged = {"clients": {"new": {"token": "x"}}, "servers": {}}
    changes = _compute_changes(current, merged)
    assert "new" in changes["clients_added"]


def test_compute_changes_detects_remove() -> None:
    current = {"clients": {"old": {"token": "x"}}, "servers": {}}
    merged = {"clients": {}, "servers": {}}
    changes = _compute_changes(current, merged)
    assert "old" in changes["clients_removed"]


# --- Validate ---

def test_validate_valid_patch() -> None:
    with tempfile.TemporaryDirectory() as td:
        reg = _make_registry(Path(td))
        result = validate_patch(reg.path, {"clients": {"vscode": {"allow_servers": ["test-server"]}}})
        assert result["valid"] is True
        assert result["errors"] == []


def test_validate_invalid_patch() -> None:
    with tempfile.TemporaryDirectory() as td:
        reg = _make_registry(Path(td))
        result = validate_patch(reg.path, {"clients": {"vscode": {"allow_servers": ["nonexistent"]}}})
        assert result["valid"] is False
        assert any("nonexistent" in e for e in result["errors"])


# --- Apply ---

def test_apply_success() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        reg = _make_registry(tmp)
        bus = EventBus()
        received: list[BusEvent] = []
        bus.subscribe(lambda e: received.append(e))
        audit = AuditLogger(tmp / "audit.log", event_bus=bus)

        result = apply_patch(
            registry=reg,
            patch={"clients": {"vscode": {"allow_servers": []}}},
            audit=audit,
            event_bus=bus,
        )
        assert result["applied"] is True
        assert result["errors"] == []
        assert "vscode" in result["changes_summary"]["clients_modified"]
        assert (tmp / "registry.yaml.bak").exists()
        # Check audit event was emitted
        config_events = [e for e in received if e.event_type == "config_change"]
        assert len(config_events) >= 1


def test_apply_validation_failure() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        reg = _make_registry(tmp)
        audit = AuditLogger(tmp / "audit.log")

        result = apply_patch(
            registry=reg,
            patch={"clients": {"vscode": {"allow_servers": ["nonexistent"]}}},
            audit=audit,
        )
        assert result["applied"] is False
        assert len(result["errors"]) > 0


def test_apply_reloads_registry() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        reg = _make_registry(tmp)
        audit = AuditLogger(tmp / "audit.log")

        # Add a new client
        result = apply_patch(
            registry=reg,
            patch={"clients": {"new-client": {"token": "new-token", "allow_servers": []}}},
            audit=audit,
        )
        assert result["applied"] is True
        assert "new-client" in reg.clients


def test_apply_restart_required() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        reg = _make_registry(tmp)
        audit = AuditLogger(tmp / "audit.log")

        result = apply_patch(
            registry=reg,
            patch={"lmcp": {"port": 9999}},
            audit=audit,
        )
        assert result["applied"] is True
        assert result.get("restart_required") is True


def test_apply_reload_failure_surfaced(monkeypatch) -> None:
    """Regression: if in-memory reload fails after successful file write,
    apply_patch must return applied=true + reload_failed=true with a warning
    and emit an audit event with reload_error. Previously the failure was
    silently swallowed."""
    import lmcp.management as mgmt

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        reg = _make_registry(tmp)
        bus = EventBus()
        received: list[BusEvent] = []
        bus.subscribe(lambda e: received.append(e))
        audit = AuditLogger(tmp / "audit.log", event_bus=bus)

        # Force the reload step to fail by monkeypatching load_registry to raise.
        # This exercises the new reload-failure handling path.
        def _boom(path):
            raise RuntimeError("simulated reload failure")

        monkeypatch.setattr("lmcp.config.load_registry", _boom)

        result = apply_patch(
            registry=reg,
            patch={"clients": {"vscode": {"allow_servers": []}}},
            audit=audit,
            event_bus=bus,
        )

        # File write succeeded, so applied is true
        assert result["applied"] is True
        # But reload failed, so the new fields are set
        assert result.get("reload_failed") is True
        assert "reload_error" in result
        assert "warning" in result
        # Audit event records the reload failure
        config_events = [e for e in received if e.event_type == "config_change"]
        assert len(config_events) >= 1
        assert config_events[-1].payload.get("reason") == "management_apply_reload_failed"
        assert "reload_error" in config_events[-1].payload.get("detail", {})


def test_apply_backup_failure_audited(monkeypatch) -> None:
    """Regression: backup failure must emit a config_change audit event with
    allowed=false. Previously only validation failures were audited."""
    import shutil as shutil_mod

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        reg = _make_registry(tmp)
        bus = EventBus()
        received: list[BusEvent] = []
        bus.subscribe(lambda e: received.append(e))
        audit = AuditLogger(tmp / "audit.log", event_bus=bus)

        # Force backup to fail
        def _boom(src, dst):
            raise OSError("simulated backup failure")

        monkeypatch.setattr(shutil_mod, "copy2", _boom)

        result = apply_patch(
            registry=reg,
            patch={"clients": {"vscode": {"allow_servers": []}}},
            audit=audit,
            event_bus=bus,
        )

        assert result["applied"] is False
        assert result.get("error") == "write_failed"
        # Audit event recorded the backup failure
        config_events = [e for e in received if e.event_type == "config_change"]
        assert len(config_events) >= 1
        last = config_events[-1]
        assert last.payload.get("allowed") is False
        assert last.payload.get("reason") == "backup_failed"
