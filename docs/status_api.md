# /status API Contract

The `GET /status` endpoint returns a JSON payload describing the current state of the
LMCP daemon. This document defines the contract that consumers (CLI, /ui, future native
clients) can depend on.

## Versioning

The payload includes a `status_version` integer. When required fields are added, removed,
or their types change, `status_version` is incremented. Consumers should check this field
and handle unknown versions gracefully.

Current version: **2**

## Endpoint

```
GET /status
GET /status?limit=N   (limit recent audit entries, default 20)
```

Returns `200 OK` with `Content-Type: application/json`.
Returns `400 Bad Request` if `limit` is not a valid positive integer.

## Required Fields

### Top-level

| Field | Type | Description |
|-------|------|-------------|
| `status_version` | `int` | Contract version. Incremented on breaking changes. |
| `service` | `string` | Service identifier. Currently `"lmcp-v3"`. |
| `host` | `string` | Bound host address. |
| `port` | `int` | Bound port number. |
| `loopback_only` | `bool` | Whether the daemon is restricted to loopback. |
| `uptime_s` | `float\|null` | Daemon uptime in seconds. `null` when queried via CLI (no running daemon). |
| `registry_path` | `string` | Absolute path to the loaded registry file. |
| `audit_log_path` | `string` | Absolute path to the audit log file. |
| `clients` | `array` | List of client objects (see below). |
| `servers` | `array` | List of server objects (see below). |
| `recent_audit_entries` | `array` | Recent audit log entries (newest last). |

### Client object

Each entry in `clients`:

| Field | Type | Description |
|-------|------|-------------|
| `client_id` | `string` | Unique client identifier. |
| `token_status` | `string` | One of `"empty"`, `"placeholder"`, `"set"`. |
| `allow_servers` | `array[string]` | Server IDs this client is permitted to reach. |
| `rate_limit_rpm` | `int\|null` | Effective rate limit (per-client or global fallback). `null` if unlimited. |

### Server object

Each entry in `servers`:

| Field | Type | Description |
|-------|------|-------------|
| `server_id` | `string` | Unique server identifier. |
| `transport` | `string` | `"stdio"` or `"http"`. |
| `target` | `string` | Command string (stdio) or URL (http). |
| `available_hint` | `bool` | Whether the server binary/URL appears reachable. |
| `tool_policy_mode` | `string` | `"allow_all"`, `"deny_all"`, or `"allow_list"`. |
| `timeouts` | `object` | Resolved timeout configuration (see below). |

### Timeouts object

Each `timeouts` in a server entry:

| Field | Type | Description |
|-------|------|-------------|
| `initialize_s` | `float` | Timeout for MCP initialize. |
| `tools_list_s` | `float` | Timeout for tools/list. |
| `tools_call_s` | `float` | Timeout for tools/call. |
| `retry_on_timeout` | `int` | Number of retries after timeout. |
| `retry_backoff_s` | `float` | Backoff interval between retries. |

### Audit entry object

Each entry in `recent_audit_entries`:

| Field | Type | Description |
|-------|------|-------------|
| `event` | `string` | Event type (e.g. `client_auth`, `rate_limited`). |
| `client_id` | `string\|null` | Client that triggered the event. |
| `server_id` | `string\|null` | Target server, if applicable. |
| `tool_name` | `string\|null` | Target tool, if applicable. |
| `allowed` | `bool\|null` | Whether the request was allowed. |
| `reason` | `string\|null` | Reason code for the decision. |
| `detail` | `object\|null` | Additional detail, if any. |
| `ts` | `string` | ISO 8601 UTC timestamp. |

Malformed log lines appear as `{"raw": "...", "error": "invalid_json"}`.

## Example Payload

```json
{
  "status_version": 2,
  "service": "lmcp-v3",
  "host": "127.0.0.1",
  "port": 7345,
  "loopback_only": true,
  "uptime_s": 3421.7,
  "registry_path": "/home/user/lmcp/config/registry.yaml",
  "audit_log_path": "/home/user/lmcp/logs/audit.log",
  "clients": [
    {
      "client_id": "vscode",
      "token_status": "set",
      "allow_servers": ["ollama-mcp", "comfyui-mcp"],
      "rate_limit_rpm": 120
    }
  ],
  "servers": [
    {
      "server_id": "ollama-mcp",
      "transport": "stdio",
      "target": "npx -y ollama-mcp-server",
      "available_hint": true,
      "tool_policy_mode": "allow_all",
      "timeouts": {
        "initialize_s": 30.0,
        "tools_list_s": 30.0,
        "tools_call_s": 300.0,
        "retry_on_timeout": 1,
        "retry_backoff_s": 1.5
      }
    }
  ],
  "recent_audit_entries": [
    {
      "event": "client_auth",
      "client_id": "vscode",
      "server_id": null,
      "tool_name": null,
      "allowed": true,
      "reason": "token_match",
      "detail": null,
      "ts": "2026-03-27T12:00:00.000000+00:00"
    }
  ]
}
```

## Stability Guarantee

Fields listed above are required and will not be removed without incrementing
`status_version`. New fields may be added without a version bump. Consumers
should ignore unknown fields.
