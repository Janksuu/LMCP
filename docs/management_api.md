# Management API Contract

The management API enables programmatic and UI-driven editing of the LMCP
registry. It treats `registry.yaml` as the single source of truth and
provides three operations: view, validate, and apply.

## Design Principles

- **Three endpoints, not CRUD.** The registry is one file, not a database.
  View it, validate a change, apply it.
- **Patch-only writes.** Edits are patches on top of the current config.
  There is no full-replacement mode. This avoids token-loss bugs (view
  redacts tokens, so a full round-trip would erase them).
- **Atomic writes.** Every apply acquires a lock, then reads the current
  file, merges, validates, backs up, and writes. No partial updates.
- **Management auth is separate from client auth.** A client token grants
  access to MCP tools. Management auth grants access to the registry itself.
  Disabled by default.
- **Audit everything.** Every config change and every denied management
  attempt produces an audit event.

---

## Implementation Prerequisites

Before these endpoints can be built, the following changes to the existing
codebase are required:

1. **Add `management_token` to `LmcpSettings`** (config.py) -- optional
   string field, default None.
2. **Add `management_token` to `registry.schema.json`** under `lmcp`
   properties. Type: `string`. Not required.
3. **Update `load_registry()`** to parse the new field.
4. **Update `validate_registry_data()`** to skip token checks when called
   in management context (patch mode does not require tokens for unchanged
   clients). Add a `skip_token_validation: bool = False` parameter.
5. **Allow `null` for `rate_limit_rpm` in schema** -- change type from
   `integer` to `["integer", "null"]` in both lmcp and client sections.
6. **Add `config_change` to `EVENT_TYPES`** in events.py.

---

## Authentication

Management endpoints require a separate management token when enabled.
This is NOT the same as a client token.

```yaml
# In registry.yaml
lmcp:
  host: 127.0.0.1
  port: 7345
  # ...
  management_token: "your-management-token"  # omit or empty to disable
```

When `management_token` is not set or empty, management endpoints return
`403` with error code `management_disabled`. The UI should show a
"Management is disabled" message, NOT a token prompt.

When set but the wrong token is provided, management endpoints return
`403` with error code `management_unauthorized`. The UI should show a
token prompt.

Management requests include the token via:
- Header: `X-Lmcp-Management-Token: <token>`
- Or query param: `?management_token=<token>`

---

## Access Model

The management surface has two tiers:

| Tier | Endpoints | Auth required |
|------|-----------|---------------|
| Public (read-only) | `/status`, `/events`, `/ui` (read-only view) | None |
| Management (read-write) | `/registry/view`, `/registry/validate`, `/registry/apply` | management_token |

When management is disabled, `/ui` remains accessible in read-only mode
(current behavior, consuming /status and /events). The management panels
(permission matrix, editors, pending changes) are hidden or disabled.

---

## Endpoints

### GET /registry/view

Returns the current registry in a UI-safe shape. Client tokens are
replaced with `token_status` (empty / placeholder / set). Server configs
include all fields (including `args`, `env`, `cwd`, `headers`,
`stdio_mode`) so the UI can display them and patch mode can preserve them.

**Auth:** management token required

**Response (200):**

```json
{
  "ok": true,
  "registry": {
    "lmcp": {
      "host": "127.0.0.1",
      "port": 7345,
      "audit_log": "logs/audit.log",
      "loopback_only": true,
      "rate_limit_rpm": null
    },
    "clients": {
      "vscode": {
        "token_status": "set",
        "allow_servers": ["ollama-mcp", "comfyui-mcp"],
        "rate_limit_rpm": 120
      }
    },
    "servers": {
      "ollama-mcp": {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "ollama-mcp-server"],
        "env": { "OLLAMA_HOST": "http://127.0.0.1:11434" },
        "cwd": null,
        "headers": {},
        "stdio_mode": "newline",
        "tool_policy": {
          "mode": "allow_all",
          "allow_tools": [],
          "deny_tools": []
        },
        "timeouts": {
          "initialize_s": 30,
          "tools_list_s": 30,
          "tools_call_s": 300,
          "retry_on_timeout": 1,
          "retry_backoff_s": 1.5
        }
      }
    }
  }
}
```

Notes:
- `lmcp.management_token` is never included in the response.
- `env` and `headers` may contain secrets. The UI should display them
  as opaque key-value pairs, not hide them.
- /status shows live daemon state (uptime, audit entries, available_hint).
  /registry/view shows the editable config (what the file contains).

---

### POST /registry/validate

Accepts a patch and returns validation results without writing anything.
This is a dry-run for /registry/apply.

**Auth:** management token required

**Request body:**

```json
{
  "patch": {
    "clients": {
      "vscode": {
        "allow_servers": ["ollama-mcp", "comfyui-mcp", "figshare"]
      }
    }
  }
}
```

The patch is merged into the current config, then validated. Token
validation is skipped for clients not included in the patch (existing
tokens are preserved, not re-checked).

**Response (200, validation passed):**

```json
{
  "ok": true,
  "valid": true,
  "errors": [],
  "changes_summary": {
    "clients_added": [],
    "clients_modified": ["vscode"],
    "clients_removed": [],
    "servers_added": [],
    "servers_modified": [],
    "servers_removed": []
  }
}
```

**Response (200, validation failed):**

```json
{
  "ok": true,
  "valid": false,
  "errors": [
    "client 'vscode' allows unknown server 'nonexistent'"
  ],
  "changes_summary": null
}
```

Note: the validate response does NOT return the normalized config.
Returning the full config would expose tokens. The changes_summary
is sufficient for the UI to show a diff preview.

---

### POST /registry/apply

Atomically validates, backs up, and writes the registry. Reloads
reloadable daemon config if validation passes.

**Auth:** management token required

**Request body:** same as /registry/validate

**Sequence (all steps inside the write lock):**

1. Acquire write lock. If already held, return 409 immediately.
2. Read current registry.yaml from disk (not cached state).
3. Merge patch into current config.
4. If a server is removed (`null`), also remove it from all client
   `allow_servers` lists (cascade delete).
5. Run `validate_registry_data()` with `skip_token_validation=True`
   for clients not in the patch. If errors, release lock and return.
6. Backup current `registry.yaml` to `registry.yaml.bak`.
   If backup fails, release lock and return 500.
7. Write merged config as YAML.
   If write fails, attempt to restore from backup, release lock, return 500.
8. Reload reloadable daemon config (see Daemon Reload section).
   If reload fails, log warning but do NOT roll back (file is valid,
   daemon will pick up changes on next restart).
9. Emit `config_change` audit event.
10. Emit `config_change_denied` audit event for failed attempts.
11. Release write lock.

**Response (200, applied):**

```json
{
  "ok": true,
  "applied": true,
  "backup_path": "config/registry.yaml.bak",
  "errors": [],
  "changes_summary": {
    "clients_added": [],
    "clients_modified": ["vscode"],
    "clients_removed": [],
    "servers_added": ["figshare"],
    "servers_modified": [],
    "servers_removed": []
  }
}
```

**Response (200, validation failed):**

```json
{
  "ok": true,
  "applied": false,
  "errors": ["..."],
  "changes_summary": null
}
```

**Response (409, write lock held):**

```json
{
  "ok": false,
  "error": "apply_in_progress",
  "message": "Another apply is in progress. Try again."
}
```

**Response (500, backup or write failure):**

```json
{
  "ok": false,
  "error": "write_failed",
  "message": "Failed to write registry. Previous config preserved."
}
```

---

## Patch Semantics

Patches merge into the current config:

- **Clients:** if a client_id exists in patch, its fields are merged.
  `allow_servers` replaces the full list (not appended). To add a new
  client, include the full client object with a token. To remove a
  client, set its value to `null`.
- **Servers:** same merge semantics. `null` removes the server AND
  removes it from all client `allow_servers` lists (cascade delete).
- **lmcp settings:** individual fields are overwritten. Omitted fields
  keep their current values. `host` and `port` changes are written to
  the file but do NOT take effect until daemon restart (see Daemon Reload).
- **Tokens:** patch can include new tokens. Omitted tokens are preserved
  from the current file. /registry/view never shows them, but
  /registry/apply reads them from the existing file when merging.

---

## Token Handling

- /registry/view replaces client tokens with `token_status` (empty/placeholder/set)
- /registry/view never includes `management_token`
- /registry/validate skips token validation for clients not in the patch
- /registry/apply preserves existing tokens when patch does not include them
- New tokens can be set through patch: `"token": "new-value"`
- The management API never returns raw tokens in any response

---

## Audit Events

Every apply attempt emits an audit event:

**Successful apply:**
```json
{
  "event": "config_change",
  "client_id": null,
  "server_id": null,
  "allowed": true,
  "reason": "management_apply",
  "detail": {
    "clients_added": [],
    "clients_modified": ["vscode"],
    "clients_removed": [],
    "servers_added": ["figshare"],
    "servers_modified": [],
    "servers_removed": [],
    "backup_path": "config/registry.yaml.bak"
  },
  "ts": "2026-04-08T12:00:00.000000+00:00"
}
```

**Failed or denied attempt:**
```json
{
  "event": "config_change",
  "allowed": false,
  "reason": "validation_failed",
  "detail": { "errors": ["..."] },
  "ts": "..."
}
```

These events publish through the EventBus. SSE subscribers on /events
receive them as:

```
event: config_change
data: {"event_type": "config_change", "event_version": 1, "timestamp": "...", "payload": {...}}
```

Note: `config_change` must be added to `EVENT_TYPES` in events.py.

---

## Daemon Reload

After a successful apply, the daemon reloads **reloadable** config:

| Setting | Reloadable | Notes |
|---------|-----------|-------|
| clients (tokens, allow_servers, rate_limit_rpm) | Yes | |
| servers (transport, command, url, policy, timeouts) | Yes | |
| lmcp.rate_limit_rpm (global) | Yes | Existing token buckets are cleared and recreated |
| lmcp.host | **No** | Requires daemon restart. Written to file but not applied live. |
| lmcp.port | **No** | Requires daemon restart. |
| lmcp.audit_log | **No** | Requires daemon restart. |
| lmcp.loopback_only | **No** | Requires daemon restart. |
| lmcp.management_token | Yes | Takes effect on next request |

When clients or rate limits change, the in-memory `_rate_limiters` dict
is cleared so new buckets are created with the updated RPM values.

Active connections and in-progress requests are not interrupted.

If a non-reloadable setting is changed, the apply response includes a
warning: `"restart_required": true`.

---

## Error Codes

| Status | Error | Meaning | UI behavior |
|--------|-------|---------|-------------|
| 200 | (none) | Success (check `valid` or `applied` field) | Refresh |
| 400 | `invalid_json` | Request body is not valid JSON | Show error |
| 403 | `management_disabled` | management_token not configured | Show "Management disabled" (NOT a token prompt) |
| 403 | `management_unauthorized` | Wrong management token | Show token prompt |
| 409 | `apply_in_progress` | Another apply is running | Retry message |
| 500 | `write_failed` | Backup or file write failed | Show error, preserve pending changes |
