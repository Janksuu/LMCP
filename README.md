# LMCP

**Local MCP Control Plane** — A unified access layer for Model Context Protocol servers.

LMCP solves MCP fragmentation by providing a single local endpoint that any MCP-capable client can connect to, with centralized registry, authentication, and policy enforcement.

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   VS Code   │     │    Codex    │     │Claude Desktop│
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       └───────────────────┼───────────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │    LMCP     │
                    │  ─────────  │
                    │  Registry   │
                    │  Auth/Policy│
                    │  Audit Log  │
                    └──────┬──────┘
                           │
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

MCP servers get locked into whichever client configures them:

- VS Code workspace MCP is invisible to Docker gateway
- Docker MCP gateway is invisible to Claude Desktop
- Every client has its own config format and discovery mechanism

You end up with duplicated configs, invisible servers, and ad-hoc wiring.

## The Solution

LMCP provides:

- **Single endpoint** — All clients talk to `127.0.0.1:7345/mcp`
- **Unified registry** — Configure servers once, access from anywhere
- **Transport normalization** — stdio and HTTP servers look identical to clients
- **Per-client authentication** — Token-based access control
- **Policy enforcement** — Server allowlists and tool filtering
- **Audit logging** — Full trail of who accessed what

---

## Quick Start

**1. Install dependencies**

```bash
cd lmcp_v1
pip install -r requirements.txt
```

**2. Configure your registry**

Copy the example and customize:

```bash
cp config/registry.example.yaml config/registry.yaml
```

Edit `config/registry.yaml` — add your servers and set client tokens:

```yaml
clients:
  vscode:
    token: "your-secure-token-here"
    allow_servers: ["ollama-mcp", "comfyui-mcp"]
```

**3. Start LMCP**

```bash
python -m lmcp.daemon --registry config/registry.yaml --serve-http
```

**4. Connect your client**

Point your MCP client at:
```
http://127.0.0.1:7345/mcp?client_id=vscode&token=your-secure-token-here
```

---

## Configuration

### Registry Format

LMCP uses a single YAML registry file:

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

### Server Types

| Transport | Config | Use Case |
|-----------|--------|----------|
| `stdio` | `command`, `args`, `env` | Local CLI-based MCP servers |
| `http` | `url`, `headers` | HTTP/SSE MCP servers |

### Tool Policies

Control which tools are accessible per server:

| Mode | Behavior |
|------|----------|
| `allow_all` | All tools accessible (default) |
| `deny_all` | No tools accessible |
| `allow_list` | Only specified tools accessible |

---

## HTTP API

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/describe` | GET | Daemon configuration |
| `/auth-check` | GET | Verify client credentials |
| `/server-check` | GET | Verify server access |
| `/mcp` | POST | MCP protocol bridge |

### MCP Protocol Support

The `/mcp` endpoint supports standard MCP methods:

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

## CLI Commands

LMCP includes testing and validation commands:

```bash
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

# Start HTTP server
python -m lmcp.daemon --registry config/registry.yaml --serve-http
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

Then in VS Code Agent mode, you can access all servers configured in your LMCP registry.

---

## Security

LMCP is designed with security defaults:

- **Loopback only** — Binds to `127.0.0.1` by default
- **Token authentication** — Every client requires a valid token
- **Server allowlists** — Clients can only access explicitly permitted servers
- **Tool policies** — Fine-grained control over which tools are accessible
- **Audit logging** — All authentication and authorization decisions are logged

### What LMCP Does NOT Do

- No remote network access by default
- No automatic server discovery
- No agent orchestration or planning
- No persistent memory or state
- No intent inference

LMCP is access control infrastructure, not an AI system.

---

## Requirements

- Python 3.10+
- PyYAML
- jsonschema

For MCP servers that use npx:
- Node.js 20+

See [docs/requirements.md](docs/requirements.md) for detailed setup.

---

## Documentation

- [Architecture](docs/architecture.md) — System design and components
- [Requirements](docs/requirements.md) — Dependencies and setup
- [Testing](docs/testing.md) — Validation procedures

---

## About

This project is developed by **Quincy Perry** and is part of the **DigitalSynth Atelier** ecosystem — a studio focused on human-centered AI systems, governance, and creative tooling.

---

## License

MIT — see [LICENSE](LICENSE)
