"""Registry management API for LMCP.

Three operations: view (read), validate (dry-run), apply (write).
All management endpoints require a separate management_token.
"""

from __future__ import annotations

import copy
import shutil
import threading
from pathlib import Path
from typing import Any

from .audit import AuditEvent, AuditLogger
from .config import Registry, validate_registry_data
from .events import BusEvent, EventBus


_write_lock = threading.Lock()

# Settings that require a daemon restart (cannot be reloaded live).
_NON_RELOADABLE = frozenset({"host", "port", "audit_log", "loopback_only"})


def check_management_auth(
    registry: Registry,
    token: str | None,
) -> tuple[bool, str]:
    """Check management token. Returns (allowed, error_code)."""
    mgmt_token = registry.lmcp.management_token
    if not mgmt_token:
        return False, "management_disabled"
    if token != mgmt_token:
        return False, "management_unauthorized"
    return True, ""


def build_registry_view(registry: Registry) -> dict[str, Any]:
    """Build the operator view of the registry (tokens redacted)."""
    clients = {}
    for cid, client in sorted(registry.clients.items()):
        token = client.token.strip()
        token_status = (
            "empty" if token == ""
            else "placeholder" if token.lower().startswith("replace-me")
            else "set"
        )
        clients[cid] = {
            "token_status": token_status,
            "allow_servers": list(client.allow_servers),
            "rate_limit_rpm": client.rate_limit_rpm or registry.lmcp.rate_limit_rpm,
        }

    servers = {}
    for sid, server in sorted(registry.servers.items()):
        servers[sid] = {
            "transport": server.transport,
            "command": server.command,
            "args": list(server.args),
            "url": server.url,
            "env": dict(server.env),
            "cwd": server.cwd,
            "headers": dict(server.headers),
            "stdio_mode": server.stdio_mode,
            "tool_policy": {
                "mode": server.tool_policy.mode,
                "allow_tools": list(server.tool_policy.allow_tools),
                "deny_tools": list(server.tool_policy.deny_tools),
            },
            "timeouts": {
                "initialize_s": server.timeouts.initialize_s,
                "tools_list_s": server.timeouts.tools_list_s,
                "tools_call_s": server.timeouts.tools_call_s,
                "retry_on_timeout": server.timeouts.retry_on_timeout,
                "retry_backoff_s": server.timeouts.retry_backoff_s,
            },
        }

    return {
        "lmcp": {
            "host": registry.lmcp.host,
            "port": registry.lmcp.port,
            "audit_log": registry.lmcp.audit_log,
            "loopback_only": registry.lmcp.loopback_only,
            "rate_limit_rpm": registry.lmcp.rate_limit_rpm,
        },
        "clients": clients,
        "servers": servers,
    }


def _read_current_yaml(registry_path: Path) -> dict[str, Any]:
    """Read the current registry YAML from disk."""
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyYAML is required") from exc
    return yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}


def _merge_patch(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a patch into the current config. Returns a new dict."""
    merged = copy.deepcopy(current)

    # Merge lmcp settings
    if "lmcp" in patch:
        merged_lmcp = merged.get("lmcp", {}) or {}
        for k, v in patch["lmcp"].items():
            merged_lmcp[k] = v
        merged["lmcp"] = merged_lmcp

    # Merge clients
    if "clients" in patch:
        merged_clients = merged.get("clients", {}) or {}
        for cid, cfg in patch["clients"].items():
            if cfg is None:
                merged_clients.pop(cid, None)
            elif cid in merged_clients:
                for k, v in cfg.items():
                    merged_clients[cid][k] = v
            else:
                merged_clients[cid] = cfg
        merged["clients"] = merged_clients

    # Merge servers (with cascade delete)
    if "servers" in patch:
        merged_servers = merged.get("servers", {}) or {}
        removed_servers = set()
        for sid, cfg in patch["servers"].items():
            if cfg is None:
                merged_servers.pop(sid, None)
                removed_servers.add(sid)
            elif sid in merged_servers:
                for k, v in cfg.items():
                    merged_servers[sid][k] = v
            else:
                merged_servers[sid] = cfg
        merged["servers"] = merged_servers

        # Cascade: remove deleted servers from all client allow_servers
        if removed_servers:
            for cid, cfg in (merged.get("clients", {}) or {}).items():
                if "allow_servers" in cfg:
                    cfg["allow_servers"] = [
                        s for s in cfg["allow_servers"] if s not in removed_servers
                    ]

    return merged


def _compute_changes(
    current: dict[str, Any],
    merged: dict[str, Any],
) -> dict[str, Any]:
    """Compute a summary of what changed between current and merged."""
    cur_clients = set((current.get("clients") or {}).keys())
    new_clients = set((merged.get("clients") or {}).keys())
    cur_servers = set((current.get("servers") or {}).keys())
    new_servers = set((merged.get("servers") or {}).keys())

    clients_added = sorted(new_clients - cur_clients)
    clients_removed = sorted(cur_clients - new_clients)
    clients_modified = sorted(
        cid for cid in cur_clients & new_clients
        if (current.get("clients") or {})[cid] != (merged.get("clients") or {})[cid]
    )

    servers_added = sorted(new_servers - cur_servers)
    servers_removed = sorted(cur_servers - new_servers)
    servers_modified = sorted(
        sid for sid in cur_servers & new_servers
        if (current.get("servers") or {})[sid] != (merged.get("servers") or {})[sid]
    )

    return {
        "clients_added": clients_added,
        "clients_modified": clients_modified,
        "clients_removed": clients_removed,
        "servers_added": servers_added,
        "servers_modified": servers_modified,
        "servers_removed": servers_removed,
    }


def _check_restart_required(current: dict[str, Any], merged: dict[str, Any]) -> bool:
    """Check if non-reloadable lmcp settings changed."""
    cur_lmcp = current.get("lmcp", {}) or {}
    new_lmcp = merged.get("lmcp", {}) or {}
    for key in _NON_RELOADABLE:
        if cur_lmcp.get(key) != new_lmcp.get(key):
            return True
    return False


def validate_patch(
    registry_path: Path,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Validate a patch against the current config. Returns result dict."""
    current = _read_current_yaml(registry_path)
    merged = _merge_patch(current, patch)
    errors = validate_registry_data(merged, skip_token_validation=True)
    if errors:
        return {"valid": False, "errors": errors, "changes_summary": None}
    changes = _compute_changes(current, merged)
    return {"valid": True, "errors": [], "changes_summary": changes}


def apply_patch(
    registry: Registry,
    patch: dict[str, Any],
    audit: AuditLogger,
    event_bus: EventBus | None = None,
) -> dict[str, Any]:
    """Apply a patch to the registry. Returns result dict.

    Acquires the write lock for the entire read-merge-validate-write sequence.
    """
    if not _write_lock.acquire(blocking=False):
        return {"applied": False, "error": "apply_in_progress"}

    try:
        registry_path = registry.path
        current = _read_current_yaml(registry_path)
        merged = _merge_patch(current, patch)
        errors = validate_registry_data(merged, skip_token_validation=True)

        if errors:
            audit.write(AuditEvent(
                event="config_change",
                allowed=False,
                reason="validation_failed",
                detail={"errors": errors},
            ))
            return {"applied": False, "errors": errors, "changes_summary": None}

        changes = _compute_changes(current, merged)
        restart_required = _check_restart_required(current, merged)

        # Backup
        backup_path = str(registry_path) + ".bak"
        try:
            shutil.copy2(str(registry_path), backup_path)
        except OSError:
            return {
                "applied": False,
                "error": "write_failed",
                "message": "Failed to create backup.",
            }

        # Write
        try:
            import yaml  # type: ignore
            yaml_str = yaml.dump(merged, default_flow_style=False, allow_unicode=True)
            registry_path.write_text(yaml_str, encoding="utf-8")
        except Exception:
            # Attempt restore
            try:
                shutil.copy2(backup_path, str(registry_path))
            except OSError:
                pass
            return {
                "applied": False,
                "error": "write_failed",
                "message": "Failed to write registry. Previous config preserved.",
            }

        # Reload reloadable config
        try:
            from .config import load_registry
            new_registry = load_registry(registry_path)
            registry.clients = new_registry.clients
            registry.servers = new_registry.servers
            registry.lmcp.rate_limit_rpm = new_registry.lmcp.rate_limit_rpm
            registry.lmcp.management_token = new_registry.lmcp.management_token
        except Exception:
            pass  # file is valid, daemon picks up on restart

        # Audit
        audit.write(AuditEvent(
            event="config_change",
            allowed=True,
            reason="management_apply",
            detail={**changes, "backup_path": backup_path},
        ))

        result: dict[str, Any] = {
            "applied": True,
            "backup_path": backup_path,
            "errors": [],
            "changes_summary": changes,
        }
        if restart_required:
            result["restart_required"] = True
        return result

    finally:
        _write_lock.release()
