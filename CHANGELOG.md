# Changelog

All notable changes to LMCP are documented in this file.

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
