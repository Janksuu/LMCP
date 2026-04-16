# Changelog

All notable changes to LMCP are documented in this file.

## v3.0.2 - 2026-04-16

### Fixed
- **Deep-merge patches**: nested fields in client/server patches now merge
  recursively instead of clobbering. Patching `tool_policy.allow_tools`
  preserves `mode` and `deny_tools`. Patching one timeout field preserves
  the others. (Previously, a partial patch could silently lose policy
  fields.)
- **Reload failure surfaced**: when the on-disk write succeeds but the
  in-memory reload fails, `apply` now returns `reload_failed: true` with
  a warning and audits the failure. Previously the error was silently
  swallowed and operators had no signal that auth was broken.
- **Backup/write failures audited**: `config_change` events with
  `allowed: false` are now emitted on backup-failure and write-failure
  paths. Previously only validation failures were audited.
- **Subprocess spawn amplification**: `tools/list` now caches per-server
  results for 30 seconds. An authenticated client can no longer DoS the
  host by spawning N subprocesses per request. Cache is invalidated on
  config apply.
- **tools/list crash on concurrent reload**: replaced direct dict subscript
  with `.get()`. If a client is removed between authenticate and tools/list
  by a concurrent apply, the handler returns 401 instead of 500.
- **Malformed Content-Length**: handlers now return 400 `invalid_content_length`
  instead of crashing with 500 when the header is non-integer or negative.
- **UI defense-in-depth XSS**: status fields (`token_status`, `transport`,
  `tool_policy_mode`, `rate_limit_rpm`, timeout values) now go through
  `esc()` before interpolation. Not currently exploitable, but removes
  reliance on server-side type contracts.

### Added
- 14 regression tests total for v3.0.2 fixes (52/52 tests passing):
  - 2 for deep-merge patch behavior
  - 2 for reload-failure and backup-failure handling
  - 4 for tools cache (hit, miss, TTL expiry, invalidation)
  - 6 for Content-Length parsing (valid, zero, missing, malformed, negative, empty)

---

## v3.0.1 - 2026-04-15

### Security
- **Probe rate limiting**: `/auth-check` and `/server-check` now rate-limited
  at 10 rpm. Prevents brute force token enumeration and audit log flooding.
- **SSE subscriber cap**: max 50 concurrent `/events` connections. Returns
  503 when full. Prevents memory exhaustion.
- **Constant-time token comparison**: client and management token checks
  use `hmac.compare_digest()` instead of `==`. Prevents timing side-channel.
- **XSS fix**: `esc()` in management UI now escapes single and double quotes.
  Prevents attribute injection via crafted client/server IDs.
- **Information disclosure reduction**: `/describe` no longer exposes client
  IDs, server IDs, or registry path (now shows counts only). `/status` no
  longer exposes `registry_path`, `audit_log_path`, or server `target`
  (command line). These are available through `/registry/view` with
  management auth.
- **Backup files gitignored**: `config/*.bak` added to `.gitignore`.

### Added
- `SECURITY.md`: security model, accepted risks, audit history, configuration guide.
- `STATUS_VERSION` bumped to 3 (fields removed from public `/status`).

---

## v3.0.0 - 2026-04-08

### Added
- **Internal event model** (`lmcp/events.py`): typed `BusEvent` with
  `event_type`, `event_version`, `timestamp`, `payload`. Thread-safe
  in-memory `EventBus` with subscribe/publish. Best-effort delivery.
- **SSE endpoint** (`GET /events`): live Server-Sent Events stream from
  the event bus. Per-client thread with bounded queue, 30s keepalive,
  optional `?event_type=` filter. Subscriber cleanup on disconnect.
- **Management API** (`lmcp/management.py`): three endpoints for registry
  editing through HTTP:
  - `GET /registry/view` -- authenticated operator view (tokens redacted)
  - `POST /registry/validate` -- dry-run patch validation
  - `POST /registry/apply` -- atomic backup + write + reload
- **Management auth**: separate `management_token` in registry config,
  header-only (`X-Lmcp-Management-Token`), disabled by default.
- **Web management UI** (`lmcp/ui.html`): replaces the read-only dashboard.
  Two modes: read-only (when management disabled) and full management
  (permission matrix, client/server panels, pending changes with
  validate/apply workflow, live SSE events). Dark theme, vanilla JS.
- **Launcher**: `python -m lmcp` via `__main__.py`. `start_lmcp.ps1` for
  Windows. Daemon prompts to open browser at launch.
- **Audit-to-event wiring**: every audit write emits a `BusEvent` through
  the event bus. `config_change` event type for management actions.
- **Patch-only config editing**: no full replacement mode. Patches merge
  into current config, preserving tokens for unchanged clients. Server
  removal cascades to client allowlists.
- **Daemon reload**: clients, servers, rate limits, and management token
  reload without restart. Host/port/audit_log require restart (response
  includes `restart_required: true`).
- **Documentation**: `docs/management_api.md` (API contract),
  `docs/web_ui.md` (UI specification), `docs/status_api.md` (updated).

### Changed
- `AuditLogger` accepts optional `EventBus` for event emission.
- `LmcpSettings` adds `management_token` field.
- `validate_registry_data()` adds `skip_token_validation` parameter.
- `registry.schema.json` allows `null` for `rate_limit_rpm`, adds
  `management_token` under `lmcp`.
- `EVENT_TYPES` expanded: `config_change` added.
- `/ui` now serves `lmcp/ui.html` from disk instead of inline HTML.
- Rate limiter buckets cleared on successful config apply.

### Tests
- 38 tests across 3 test files (events: 14, management: 18, status: 6).

---

## v2.1.1 - 2026-03-30

### Added
- `/status` API contract: `status_version` field (currently 2), documented JSON
  shape in `docs/status_api.md`, example payload, stability guarantee.
- Operator state fields in `/status`: `uptime_s` (daemon uptime), `rate_limit_rpm`
  per client (effective limit or null if unlimited).
- `STATUS_REQUIRED_FIELDS`, `STATUS_CLIENT_REQUIRED_FIELDS`,
  `STATUS_SERVER_REQUIRED_FIELDS` exported for contract enforcement.
- Regression test suite: `tests/test_status_contract.py` (6 tests).

### Fixed
- Audit event timestamps: `ts` field was evaluated once at import time, causing all
  events in the same process to share one timestamp. Now uses `field(default_factory)`.
- Thread safety: `_TokenBucket`, `_rate_limiters` dict, and `AuditLogger.write()` are
  now protected by locks for `ThreadingHTTPServer` concurrency.
- POSIX permission check now includes group/other writable bits, not just readable.
- README: `/mcp` auth example corrected (auth via query params or headers, not
  JSON-RPC params). Header name corrected to `X-Lmcp-Client-Id`.
- README: Quick Start path corrected from `lmcp_v2` to `LMCP`.

---

## v2.1.0 - 2026-03-27

### Added
- Registry file permission check on startup (POSIX only): warns to stderr if
  `registry.yaml` is readable by group or other users, with `chmod 600` instruction.
  No-ops on Windows, where ACLs govern file access.
- Remote mode hardening: warns to stderr at startup when `loopback_only` is disabled
  and the bound host is not a loopback address. Reminds operator to verify token
  strength and that network exposure is intentional.
- Per-client rate limiting via in-memory token bucket. Configurable per-client
  (`rate_limit_rpm` in client config) with optional global default (`rate_limit_rpm`
  under `lmcp` settings). Returns MCP error -32009 (rate_limited) when exceeded.
  Rate-limited requests are logged to the audit trail. Unlimited by default.

---

## v2.0.0 - 2026-02-19

### Added
- CLI status surfaces in `lmcp.daemon`:
  - `--status` (human-readable operational summary)
  - `--status-json` (machine-readable status payload)
  - `--status-limit` (recent audit entry window)
- Read-only HTTP observability endpoints:
  - `GET /status` (JSON status payload)
  - `GET /ui` (read-only status dashboard with servers, clients, and recent audit entries)
- Operator signal for client token configuration state (`empty`, `placeholder`, `set`).
- Per-server timeout and retry configuration via optional `timeouts:` block in registry:
  - `initialize_s`, `tools_list_s`, `tools_call_s` — per-phase timeout overrides
  - `retry_on_timeout`, `retry_backoff_s` — retry control for initialize and tools/list
  - Safe transport defaults apply when not configured (stdio: 90s init/list, 180s call; http: 60s list, 300s call)
  - `tools/call` is intentionally not auto-retried to prevent duplicate side effects
- Timeout settings surfaced in `--status`, `--status-json`, and `GET /status` output.

### Changed
- `/health` response now reports `service: lmcp-v2`.
- HTTP startup banner wording normalized from "test surface" to "HTTP surface".
- Startup endpoint list updated to include `/status`, `/ui`, and `/mcp`.
- Root documentation expanded for v2:
  - architecture-first README updates
  - root-level `ARCHITECTURE.md` as the canonical design reference

### Notes
- UI remains intentionally read-only in v2.
- Token hardening/refactor is deferred to v3 by design.

## v1.0.0 - 2026-01-28

### Baseline
- Local LMCP daemon with registry-driven policy enforcement.
- Client authentication via `client_id` + token.
- Per-client server allowlists and per-server tool policy modes.
- MCP routing support for:
  - stdio servers
  - HTTP/SSE servers
- Core HTTP endpoints:
  - `/health`, `/describe`, `/auth-check`, `/server-check`, `/mcp`
- Audit logging and registry validation workflow.
