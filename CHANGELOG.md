# Changelog

All notable changes to LMCP are documented in this file.

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
