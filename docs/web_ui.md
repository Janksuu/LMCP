# Web UI Specification

The LMCP web UI is a management surface served at `GET /ui`. It replaces
the current read-only status dashboard with a registry editor and live
monitoring view when management auth is enabled.

## Design Principles

- **Single page.** One HTML page served by the daemon. No build step,
  no bundler, no framework install. Inline JS + CSS.
- **Two modes.** Read-only when management is disabled (shows /status and
  /events only). Full management when management_token is configured and
  the operator authenticates.
- **Patch-only edits.** Every save sends a patch via /registry/apply.
  No full config replacement.
- **Diff before write.** Every save shows what will change before applying.
- **No raw tokens.** Client tokens shown as status badges (empty /
  placeholder / set). New tokens entered through a masked input field
  and sent directly in the patch.

---

## Layout

```
+------------------------------------------------------------------+
|  LMCP Management                    [status: running] [uptime]   |
+------------------------------------------------------------------+
|                                                                  |
|  +------------------------------------------------------------+ |
|  |                  Permission Matrix                          | |
|  |                                                             | |
|  |              | ollama | comfyui | figshare | playwright |   | |
|  |  vscode      |  [x]   |  [x]    |   [x]    |    [x]     |   | |
|  |  claude-desk |  [x]   |  [ ]    |   [ ]    |    [ ]     |   | |
|  |  codex       |  [ ]   |  [ ]    |   [ ]    |    [ ]     |   | |
|  |                                                             | |
|  +------------------------------------------------------------+ |
|                                                                  |
|  +---------------------------+  +------------------------------+ |
|  |  Clients                  |  |  Servers                     | |
|  |                           |  |                              | |
|  |  > vscode                 |  |  > ollama-mcp                | |
|  |    token: [set]           |  |    transport: stdio           | |
|  |    rpm: 120               |  |    command: npx               | |
|  |    servers: 4             |  |    args: -y ollama-mcp-server | |
|  |                           |  |    env: OLLAMA_HOST=...       | |
|  |  > claude-desktop         |  |    policy: allow_all          | |
|  |    token: [set]           |  |    timeout: 300s call         | |
|  |    rpm: (global)          |  |                              | |
|  |    servers: 1             |  |  > comfyui-mcp               | |
|  |                           |  |    transport: http            | |
|  |  [+ Add Client]           |  |    url: 127.0.0.1:9000       | |
|  +---------------------------+  |    policy: allow_all          | |
|                                 |                              | |
|                                 |  [+ Add Server]              | |
|                                 +------------------------------+ |
|                                                                  |
|  +------------------------------------------------------------+ |
|  |  Pending Changes                        [Validate] [Apply]  | |
|  |                                                             | |
|  |  (no changes)                                               | |
|  |                                                             | |
|  +------------------------------------------------------------+ |
|                                                                  |
|  +------------------------------------------------------------+ |
|  |  Live Events                              [filter: all  v]  | |
|  |                                                             | |
|  |  12:00:01  client_auth  vscode         allowed  token_match | |
|  |  12:00:02  server_auth  vscode>ollama  allowed  on_list     | |
|  |  12:00:05  rate_limited codex          denied   exceeded    | |
|  |  12:00:10  config_change               applied  mgmt_apply  | |
|  |                                                             | |
|  +------------------------------------------------------------+ |
+------------------------------------------------------------------+
```

---

## Two-Mode Behavior

### Read-Only Mode (management disabled)

When `management_token` is not set, `/ui` shows:
- Header bar with daemon status and uptime (from /status)
- Live Events panel (from /events SSE)
- Static display of clients and servers (from /status, not /registry/view)
- No permission matrix, no editors, no apply button

This preserves the current /ui behavior with better styling.

### Management Mode (authenticated)

When `management_token` is set and the operator provides it:
- Full layout as shown above
- Permission matrix, client/server editors, pending changes
- All management API endpoints accessible

The UI prompts for the management token only when management endpoints
return `403 management_unauthorized`. If they return
`403 management_disabled`, the UI shows "Management is not enabled"
with no prompt.

---

## Panels

### 1. Header Bar

| Element | Source | Behavior |
|---------|--------|----------|
| Title | Static | "LMCP Management" |
| Status badge | /status `service` + `uptime_s` | Green when daemon is running |
| Mode indicator | Management auth state | "Read-only" or "Management" |

### 2. Permission Matrix (Management Mode Only)

The core visual. Rows are clients, columns are servers. Each cell is a
checkbox showing whether that client has the server in `allow_servers`.

| Element | Source | Behavior |
|---------|--------|----------|
| Client rows | /registry/view `clients` | One row per client |
| Server columns | /registry/view `servers` | One column per server |
| Checkbox state | `client.allow_servers` | Checked = server in list |
| Toggle | User action | Adds to pending changes (not applied immediately) |

Toggling a checkbox does NOT write immediately. It adds the change to
the pending changes panel. Apply writes all pending changes at once.

### 3. Clients Panel

Expandable list of registered clients.

| Field | Source | Editable | Notes |
|-------|--------|----------|-------|
| client_id | /registry/view | No | Identifier |
| token_status | /registry/view | No | Badge: empty / placeholder / set |
| Set new token | User input | Yes | Masked input, included in patch on apply |
| allow_servers | /registry/view | Yes | Via matrix or multi-select |
| rate_limit_rpm | /registry/view | Yes | Number input. Empty = null (unlimited) |

**Add Client:** form with client_id, token, initial allowed servers.
**Remove Client:** confirmation dialog. Added to pending changes.

### 4. Servers Panel

Expandable list of registered servers. All fields from the registry are
displayed. Fields the UI can edit are marked below.

| Field | Source | Editable | Notes |
|-------|--------|----------|-------|
| server_id | /registry/view | No | Identifier |
| transport | /registry/view | No | Set at creation only |
| command | /registry/view | Display | stdio servers |
| args | /registry/view | Display | Shown as joined string |
| url | /registry/view | Display | http servers |
| env | /registry/view | Display | Key-value pairs (may contain secrets) |
| cwd | /registry/view | Display | Working directory |
| headers | /registry/view | Display | Key-value pairs (may contain secrets) |
| stdio_mode | /registry/view | Display | newline or content-length |
| tool_policy.mode | /registry/view | Yes | Dropdown: allow_all / deny_all / allow_list |
| tool_policy.allow_tools | /registry/view | Display | Shown when mode = allow_list |
| tool_policy.deny_tools | /registry/view | Display | Shown when mode has deny entries |
| timeouts | /registry/view | Yes | Number inputs per timeout field |

Display-only fields are shown so the operator has full visibility, but
editing them requires direct YAML editing. This avoids the UI needing to
handle complex field types (args arrays, env maps) and prevents
accidental data loss on fields the UI doesn't fully understand.

**Add Server:** form with server_id, transport, command/url, basic
tool_policy. Advanced fields (args, env, cwd, headers) are set via YAML.
**Remove Server:** confirmation dialog. Cascade: also removes from all
client `allow_servers` lists. Added to pending changes.

### 5. Pending Changes Panel

Shows what will be written when Apply is clicked.

| Element | Source | Behavior |
|---------|--------|----------|
| Change list | Local state | Accumulated from matrix/panel edits |
| Changes summary | Compare current vs proposed | clients/servers added/modified/removed |
| Validate button | POST /registry/validate | Shows errors inline if any |
| Apply button | POST /registry/apply | Sends patch, shows result |
| Discard button | Local state | Clears all pending changes |

**Workflow:**
1. User makes edits (matrix checkboxes, panel fields)
2. Changes accumulate in pending panel
3. User clicks Validate -- errors shown if any
4. User clicks Apply -- changes summary shown, confirmation prompt
5. Apply calls POST /registry/apply with patch
6. On success: pending changes cleared, UI refreshes from /registry/view
7. On failure: errors displayed, pending changes preserved

### 6. Live Events Panel

Subscribes to `GET /events` via EventSource (SSE).

| Element | Source | Behavior |
|---------|--------|----------|
| Event stream | /events SSE | Auto-scrolling, newest at bottom |
| Filter dropdown | Local state | Filter by event_type |
| Event row | SSE `data` field | Parsed from BusEvent wrapper |

Each SSE message arrives as:
```
event: <event_type>
data: {"event_type": "...", "event_version": 1, "timestamp": "...", "payload": {...}}
```

The UI extracts display fields from `payload`:
- `payload.client_id` -> client column
- `payload.server_id` -> server column
- `payload.allowed` -> allowed column
- `payload.reason` -> reason column

`config_change` events are highlighted differently (management action).

---

## Data Sources

| Endpoint | Used By | Mode | Purpose |
|----------|---------|------|---------|
| GET /status | Header bar, read-only view | Both | Uptime, daemon state |
| GET /events (SSE) | Live Events panel | Both | Real-time event stream |
| GET /registry/view | Matrix, Clients, Servers | Management | Editable config |
| POST /registry/validate | Pending Changes | Management | Pre-apply validation |
| POST /registry/apply | Pending Changes | Management | Write config |

---

## Interaction Model

- **No auto-save.** Every change is staged in pending changes.
- **Batch apply.** Multiple edits are applied as one atomic patch.
- **Optimistic refresh.** After apply, the UI fetches /registry/view to
  confirm the new state.
- **SSE reconnect.** If the /events connection drops, the UI reconnects
  with a 5-second backoff.
- **Management auth prompt.** Only shown when management endpoints return
  `403 management_unauthorized`. Token stored in sessionStorage (cleared
  on tab close). If `403 management_disabled`, show info message instead.

---

## Technology

- Inline HTML/CSS/JS served by the daemon at GET /ui
- No build step, no npm, no bundler
- Vanilla JS with fetch() and EventSource
- CSS grid for layout
- Dark theme consistent with current /ui styling

---

## Out of Scope (v3)

- Editing lmcp settings (host, port, audit_log, loopback_only) through the UI.
  These require daemon restart and are better managed via YAML.
- Editing complex server fields (args, env, cwd, headers, stdio_mode) through
  the UI. Displayed for visibility, edited via YAML.
- Tool-level allow_tools / deny_tools editing (no tool enumeration endpoint).
- User/role management (single operator, single management token).
- Registry version history / undo (backup file exists but no UI for it).
- Mobile layout optimization.
