# LMCP Architecture

LMCP is a local governance layer for MCP tool servers. This document explains the design decisions behind it — not just what it does, but why it is built the way it is.

---

## System Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                          LMCP Daemon                             │
│                        127.0.0.1:7345                            │
│                                                                  │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────┐  │
│  │   Registry   │   │    Policy    │   │    Audit Logger      │  │
│  │   Loader     │   │    Engine    │   │  (append-only JSONL) │  │
│  └──────┬───────┘   └──────┬───────┘   └──────────┬───────────┘  │
│         │                  │                      │              │
│         └──────────────────┼──────────────────────┘              │
│                            │                                     │
│                            ▼                                     │
│                ┌───────────────────────┐                         │
│                │      HTTP Server      │  /health /status /ui    │
│                │    /mcp  /auth-check  │  /describe /mcp         │
│                └───────────┬───────────┘                         │
│                            │                                     │
│           ┌────────────────┼────────────────┐                    │
│           ▼                ▼                ▼                    │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐            │
│  │stdio Adapter │  │ HTTP Adapter │  │  (future)    │            │
│  │(subprocess)  │  │ (HTTP/SSE)   │  │              │            │
│  └──────────────┘  └──────────────┘  └──────────────┘            │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
        │                    │                   │
        ▼                    ▼                   ▼
  stdio servers         HTTP servers        Docker/other
  (spawned on demand)   (already running)   (via stdio)
```

---

## Design Decisions

### Explicit Registration Over Auto-Discovery

LMCP does not discover servers automatically. Every server must be registered in `registry.yaml` before LMCP knows it exists.

**Why:** Auto-discovery makes the registry implicit. If LMCP could detect running services on its own, the configuration file would no longer be the authoritative record of what is accessible. With explicit registration, `registry.yaml` is a complete, auditable statement of what LMCP can reach. You can read it and know exactly what is reachable. Nothing is running that is not written down.

**What this means in practice:** Adding a new MCP server requires editing the registry. This is intentional friction — it makes access a deliberate act.

---

### Loopback Binding by Default

LMCP binds to `127.0.0.1` by default. It will not accept connections from other machines on the network unless explicitly configured to do so via `loopback_only: false`.

**Why:** MCP tool servers are local services. Exposing the governance layer to the network creates attack surface without corresponding benefit. The default is the secure option. Remote access requires a deliberate configuration change and a reason.

**What this means in practice:** An attacker on the local network cannot reach LMCP without first compromising the host operating system.

---

### Per-Client Allowlists Over Global Policy

Each registered client has its own `allow_servers` list. A client can only reach servers that appear on its list. There is no global "any authenticated client can reach any server" mode.

**Why:** Different clients have different roles. An IDE integration should not share tool access with an automation script. Global policy grants everyone the same surface — per-client allowlists apply least privilege at the identity level without requiring per-call configuration.

**What this means in practice:** Adding a new client to the registry grants it zero access by default. Access must be explicitly assigned.

---

### Append-Only Audit Log

The audit log is written in JSONL format, one entry per line. Entries are appended. Nothing is ever edited or deleted.

**Why:** A log that can be modified is not an audit trail — it is a history you are choosing to believe. Append-only guarantees that the record of what happened is tamper-resistant by construction. Log rotation is handled by moving or archiving the file, never by editing it.

**What this means in practice:** Every authentication and authorization decision — including denials — is permanently recorded with a timestamp. If something was called, it is in the log.

---

### Three-Tier Authorization

Every request passes through three independent gates in sequence:

1. **Client authentication** — Is this `client_id` + `token` a valid registered client?
2. **Server authorization** — Is the target server on this client's `allow_servers` list?
3. **Tool authorization** — Does the server's `tool_policy` permit this tool to be called?

**Why:** The three gates are independent concerns. Passing one gate should not implicitly pass the others. This means you can grant a client access to a server while still blocking specific tools on that server, without any special-case logic.

**What this means in practice:** A client that has a valid token but requests a server not on its allowlist is denied at gate 2, and that denial is logged.

---

### Policy as Access, Not Intent

LMCP decides whether a tool call is authorized. It does not decide why the client is making the call, what the client should do instead, or what the appropriate next action is.

**Why:** Access control and intent inference are separate problems. Mixing them would conflate infrastructure with agent behavior. LMCP's role is narrowly defined: check credentials, check allowlists, proxy or deny, log the result.

**What this means in practice:** LMCP has no planner, no memory, and no state beyond what is in the registry and the audit log.

---

## Threat Model

### What LMCP Assumes

- All clients are processes running on the local host, authorized to run by the host OS
- The registry file is stored on the local filesystem and protected by OS-level file permissions
- The host itself is not compromised (local threat model, not network threat model)

### What LMCP Does Not Assume

- Clients are inherently trustworthy relative to each other — token authentication enforces client identity
- All registered servers are equally sensitive — tool policies enforce per-server access granularity

### What Is Out of Scope

- **Compromised host OS** — If the host is compromised, LMCP's security guarantees do not hold. This is out of scope.
- **Encrypted transport** — Loopback connections do not traverse the network. TLS on `127.0.0.1` provides no meaningful security benefit for the local threat model.
- **Client code integrity** — LMCP assumes the client process is what it claims to be. It does not verify the binary or its behavior.
- **Token storage security** — Tokens are stored in plaintext in `registry.yaml`. The security of tokens depends on OS-level file permission controls. Hashed token storage is a planned future improvement.

---

## Components

### Registry Loader (`lmcp/config.py`)

Reads `registry.yaml`, validates it against a JSON schema, and returns typed configuration objects. Enforces structural constraints at load time — invalid configurations fail before the daemon starts.

### Policy Engine (`lmcp/policy.py`)

Stateless functions implementing the three-tier authorization model. Takes a registry, a client identity, a server ID, and an optional tool name. Returns an allow/deny decision with a reason code. Has no side effects — logging is handled by the caller.

### Audit Logger (`lmcp/audit.py`)

Writes `AuditEvent` records to a JSONL file. Append-only. The audit logger is the only component that writes to the log file. No other component reads from it at runtime.

### HTTP Server (`lmcp/daemon.py`)

Binds to the configured address and handles incoming requests. Orchestrates the policy engine and transport adapters. Routes `/mcp` requests through authentication, authorization, and proxying. Serves `/status`, `/ui`, and diagnostic endpoints.

### stdio Adapter (`lmcp/stdio_mcp.py`)

Spawns local MCP server processes as child processes on demand. Manages subprocess lifecycle and handles JSON-RPC over stdin/stdout. Supports both `content-length` framing (standard MCP) and `newline`-delimited framing (some server implementations).

### HTTP Adapter (`lmcp/http_mcp.py`)

Proxies requests to HTTP/SSE MCP servers that are already running. Parses Server-Sent Events and manages request timeouts.

---

## Request Flow

```
Client → POST /mcp?client_id=X&token=Y
  │
  ├─ 1. Extract client_id and token from request
  ├─ 2. Policy: authenticate_client(registry, client_id, token)
  │      → denied: 401, logged, return
  │
  ├─ 3. Route by method:
  │
  │   tools/list:
  │      ├─ For each server in client's allow_servers:
  │      │    ├─ Policy: authorize_server(...)  → skip if denied
  │      │    └─ Adapter: fetch tools from server
  │      └─ Return merged tool list
  │
  │   tools/call:
  │      ├─ Policy: authorize_server(registry, client_id, server_id)
  │      │    → denied: 403, logged, return
  │      ├─ Policy: authorize_tool(server_config, tool_name)
  │      │    → denied: 403, logged, return
  │      └─ Adapter: proxy call to server, return response
  │
  └─ 4. Log decision (allowed or denied) to audit trail
```

---

## Non-Goals

These are out of scope by design, not by omission:

| What | Why it is out of scope |
|------|----------------------|
| Agent orchestration | LMCP enforces access. Planning what to do is a different problem. |
| Intent inference | LMCP does not interpret why a tool is being called. |
| Automatic server discovery | Implicit registration undermines the auditability of the registry. |
| Persistent cross-request memory | LMCP is stateless between requests by design. |
| Remote network access by default | Local-first is the secure default. Network access is opt-in. |
| UI control actions | Status inspection and access management are different threat surfaces. |

---

## File Structure

```
LMCP/
├── lmcp/
│   ├── daemon.py        # HTTP server, routing, orchestration
│   ├── config.py        # Registry loader and typed config objects
│   ├── policy.py        # Three-tier authorization logic
│   ├── audit.py         # Append-only audit logger
│   ├── stdio_mcp.py     # stdio transport adapter
│   └── http_mcp.py      # HTTP/SSE transport adapter
├── config/
│   ├── registry.example.yaml   # Example registry (safe for version control)
│   └── registry.schema.json    # JSON schema for registry validation
├── docs/
│   ├── requirements.md
│   └── testing.md
├── README.md
├── ARCHITECTURE.md      # This file
├── CHANGELOG.md
└── requirements.txt
```
