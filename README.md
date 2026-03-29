# LMCP

[![SafeSkill 92/100](https://img.shields.io/badge/SafeSkill-92%2F100_Verified%20Safe-brightgreen)](https://safeskill.dev/scan/janksuu-lmcp)

**Local MCP Control Plane** — A governance layer for Model Context Protocol servers.

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   VS Code   │     │    Codex    │     │Claude Desktop│
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       └───────────────────┼───────────────────┘
                           │  token + client_id
                           ▼
                    ┌─────────────┐
                    │    LMCP     │
                    │  ─────────  │
                    │  Registry   │
                    │  Auth/Policy│
                    │  Audit Log  │
                    └──────┬──────┘
                           │  authorized only
       ┌───────────────────┼───────────────────┐
       │                   │                   │
       ▼                   ▼                   ▼
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ Ollama MCP  │     │ ComfyUI MCP │     │Playwright MCP│
│   (stdio)   │     │   (HTTP)    │     │  (Docker)   │
└─────────────┘     └─────────────┘     └─────────────┘
```

---

## The Problem

Running multiple AI clients against multiple MCP tool servers creates a fragmentation problem:

- VS Code, Codex, and Claude Desktop each have their own config format and discovery mechanism
- Every new client means re-registering every server in a different config file
- No shared policy: granting a client access in one place doesn't affect anything else
- No audit trail: there is no record of which client called which tool, or whether it was allowed

The natural response is to wire each client directly to each server. This works until a token changes, a server moves, or a client accumulates access it shouldn't have.

## What LMCP Does

LMCP provides a single local endpoint that all MCP clients connect to. The registry defines what servers exist. Per-client allowlists define what each client is permitted to reach. Every access decision — allowed or denied — is written to an append-only audit log.

This is not a proxy that routes traffic. It is a governance layer that decides whether traffic should be routed, and records every decision it makes.

---

## Design Invariants

These are the properties LMCP will not trade away:

- **Loopback binding by default** — LMCP binds to `127.0.0.1`. Remote access requires explicit configuration and a deliberate opt-in.
- **Explicit registration** — Nothing is discovered automatically. If a server is not in the registry, LMCP does not know it exists.
- **Per-client allowlists** — Clients only reach servers they are explicitly granted. A new client has no access until access is granted.
- **Append-only audit log** — Every authentication and authorization decision is written once and never modified or deleted.
- **Policy as access, not intent** — LMCP decides *whether* a tool call happens. It never decides *why*, or what to do next.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the rationale behind each of these decisions.

---

## Quick Start

**1. Install dependencies**

```bash
cd lmcp_v2
pip install -r requirements.txt
```

**2. Configure your registry**

Copy the example and edit:

```bash
cp config/registry.example.yaml config/registry.yaml
```

Add your servers and set client tokens:

```yaml
clients:
  vscode:
    token: "your-secure-token-here"
    allow_servers: ["ollama-mcp", "comfyui-mcp"]

servers:
  ollama-mcp:
    transport: stdio
    command: npx
    args: ["-y", "ollama-mcp-server"]
    env:
      OLLAMA_HOST: "http://127.0.0.1:11434"
    timeouts:
      initialize_s: 30
      tools_list_s: 30
      tools_call_s: 300
      retry_on_timeout: 1
      retry_backoff_s: 1.5
    tool_policy:
      mode: allow_all
```

**3. Validate your configuration**

```bash
python -m lmcp.daemon --registry config/registry.yaml --validate-registry
```

**4. Start LMCP**

```bash
python -m lmcp.daemon --registry config/registry.yaml --serve-http
```

**5. Verify it is running**

```bash
python -m lmcp.daemon --registry config/registry.yaml --status
```

**6. Connect your client**

Point your MCP client at:
```
http://127.0.0.1:7345/mcp?client_id=vscode&token=your-secure-token-here
```

---

## Configuration

### Registry Format

LMCP uses a single YAML registry file. All configuration lives here: the daemon settings, every registered client, and every registered server.

```yaml
lmcp:
  host: 127.0.0.1
  port: 7345
  audit_log: logs/audit.log
  loopback_only: true

clients:
  vscode:
    token: "your-token"
    allow_servers: ["ollama-mcp"]

servers:
  ollama-mcp:
    transport: stdio
    command: npx
    args: ["-y", "ollama-mcp-server"]
    env:
      OLLAMA_HOST: "http://127.0.0.1:11434"
    tool_policy:
      mode: allow_all
```

See `config/registry.example.yaml` for a full example with multiple server types.

### Server Transports

| Transport | Config Fields | Use Case |
|-----------|--------------|----------|
| `stdio` | `command`, `args`, `env` | Local MCP servers launched as child processes |
| `http` | `url`, `headers` | HTTP/SSE MCP servers already running |

### Timeouts and Retries

Each server can override LMCP timeout behavior:

```yaml
servers:
  comfyui-mcp:
    transport: http
    url: "http://127.0.0.1:9000/mcp"
    timeouts:
      tools_list_s: 20
      tools_call_s: 600
      retry_on_timeout: 1
      retry_backoff_s: 2
```

| Key | Meaning | Default (`stdio`) | Default (`http`) |
|-----|---------|-------------------|------------------|
| `initialize_s` | Timeout for MCP `initialize` | `90` | not used |
| `tools_list_s` | Timeout for `tools/list` | `90` | `60` |
| `tools_call_s` | Timeout for `tools/call` | `180` | `300` |
| `retry_on_timeout` | Retries after timeout | `0` | `0` |
| `retry_backoff_s` | Wait between retries | `1` | `1` |

Retry behavior is intentionally conservative:
- Retries apply to `initialize` and `tools/list`.
- `tools/call` is **not** auto-retried to avoid duplicate side effects.

### Rate Limiting

Optional per-client request throttling using an in-memory token bucket:

```yaml
lmcp:
  rate_limit_rpm: 60           # Global default (requests per minute)

clients:
  vscode:
    token: "..."
    allow_servers: [...]
    rate_limit_rpm: 120        # Per-client override
```

| Setting | Scope | Effect |
|---------|-------|--------|
| `lmcp.rate_limit_rpm` | Global | Default limit for clients that do not set their own |
| `clients.<id>.rate_limit_rpm` | Per-client | Overrides the global default for this client |

- Per-client takes precedence over global.
- If neither is set, the client is unlimited.
- Exceeding the limit returns MCP error `-32009` (`rate_limited`).
- Rate-limited requests are recorded in the audit log.
- State is in-memory and resets when the daemon restarts.

### Tool Policies

Per-server control over which tools clients can call:

| Mode | Behavior |
|------|----------|
| `allow_all` | All tools accessible |
| `deny_all` | No tools accessible (server registered but gated) |
| `allow_list` | Only tools listed in `tools` are accessible |

---

## Operation

### Status and Inspection

```bash
# Human-readable status summary
python -m lmcp.daemon --registry config/registry.yaml --status

# Machine-readable status (JSON)
python -m lmcp.daemon --registry config/registry.yaml --status-json
```

Status output includes registered clients and their allowed servers, registered servers and transport type, per-server timeout settings, and the most recent audit log entries.

### Web Status Panel (Optional)

When the daemon is running, a read-only status panel is available at:

```
http://127.0.0.1:7345/ui
```

The panel shows three views: registered servers (name, transport, status), registered clients (ID, allowed servers), and recent audit entries (timestamp, client, server, action, result). It is read-only — no actions can be taken from the UI.

---

## HTTP API

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/status` | GET | Daemon status (JSON) |
| `/ui` | GET | Read-only status panel |
| `/describe` | GET | Daemon configuration |
| `/auth-check` | GET | Verify client credentials |
| `/server-check` | GET | Verify server access |
| `/mcp` | POST | MCP protocol bridge |

### MCP Protocol Support

The `/mcp` endpoint accepts standard MCP JSON-RPC. Authentication is via query parameters or headers.

**Query parameters:** `?client_id=vscode&token=your-token`

**Headers:** `X-Client-Id: vscode` and `X-Lmcp-Token: your-token`

Supported methods:

- `initialize` — Protocol handshake
- `tools/list` — Aggregated tool discovery across allowed servers
- `tools/call` — Proxied tool execution with policy enforcement

### Example: List Tools

```bash
curl -X POST http://127.0.0.1:7345/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {
      "client_id": "vscode",
      "token": "your-token"
    }
  }'
```

---

## Full CLI Reference

```bash
# Start HTTP server
python -m lmcp.daemon --registry config/registry.yaml --serve-http

# Human-readable status summary
python -m lmcp.daemon --registry config/registry.yaml --status

# Machine-readable status (JSON)
python -m lmcp.daemon --registry config/registry.yaml --status-json

# Validate registry configuration
python -m lmcp.daemon --registry config/registry.yaml --validate-registry

# Print parsed configuration
python -m lmcp.daemon --registry config/registry.yaml --print-config

# Run self-test (auth + policy checks)
python -m lmcp.daemon --registry config/registry.yaml --self-test

# Test stdio server connection
python -m lmcp.daemon --registry config/registry.yaml --stdio-test ollama-mcp

# Test HTTP server connection
python -m lmcp.daemon --registry config/registry.yaml --http-test comfyui-mcp
```

---

## VS Code Integration

Add LMCP as an MCP server in your VS Code workspace:

**`.vscode/mcp.json`**
```json
{
  "servers": {
    "lmcp": {
      "type": "http",
      "url": "http://127.0.0.1:7345/mcp?client_id=vscode&token=YOUR_TOKEN"
    }
  }
}
```

In VS Code Agent mode, all servers in your LMCP registry become available through the single LMCP endpoint. Access is governed by the `allow_servers` list for the `vscode` client.

---

## Security

LMCP is designed so the secure behavior is the default:

- **Loopback only** — Binds to `127.0.0.1` unless explicitly configured otherwise
- **Token authentication** — Every client requires a valid token; no anonymous access
- **Server allowlists** — Clients access only servers they are explicitly granted
- **Tool policies** — Per-server control over which tools are reachable
- **Audit logging** — Every authentication and authorization decision is recorded

### What LMCP Does NOT Do

- No remote network access by default
- No automatic server discovery or registration
- No agent orchestration or planning
- No persistent memory or cross-request state
- No intent inference

LMCP is access control infrastructure. It is not an AI system.

---

## Requirements

- Python 3.10+
- `pyyaml >= 6.0`
- `jsonschema >= 4.20.0`

For MCP servers that use `npx`:
- Node.js 20+

See [docs/requirements.md](docs/requirements.md) for full setup details.

---

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — Design decisions, invariants, and threat model
- [CHANGELOG.md](CHANGELOG.md) — Version history
- [docs/requirements.md](docs/requirements.md) — Dependencies and setup
- [docs/testing.md](docs/testing.md) — Validation procedures

---

## About

LMCP is developed by **Quincy Perry** as part of the **DigitalSynth Atelier** ecosystem — tools for human-governed AI workflows.

---

## License

MIT — see [LICENSE](LICENSE)
