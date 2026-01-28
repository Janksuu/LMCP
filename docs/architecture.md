# LMCP Architecture

LMCP is a standalone local daemon that provides unified access to MCP servers.

## Overview

```
┌─────────────────────────────────────────────────────────────┐
│                         LMCP Daemon                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │  Registry   │  │   Policy    │  │    Audit Logger     │  │
│  │   Loader    │  │   Engine    │  │                     │  │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘  │
│         │                │                    │             │
│         └────────────────┼────────────────────┘             │
│                          │                                  │
│                          ▼                                  │
│              ┌───────────────────────┐                      │
│              │    HTTP API Server    │                      │
│              │   (127.0.0.1:7345)    │                      │
│              └───────────┬───────────┘                      │
│                          │                                  │
│         ┌────────────────┼────────────────┐                 │
│         ▼                ▼                ▼                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │stdio Adapter│  │HTTP Adapter │  │  (future)   │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Core Components

### Registry Loader

- Reads `registry.yaml` configuration
- Validates against JSON schema
- Enforces security constraints (loopback-only, valid tokens)
- Provides typed configuration objects

### Policy Engine

Three-level authorization:

1. **Client Authentication** — Verify `client_id` + `token`
2. **Server Authorization** — Check client's `allow_servers` list
3. **Tool Authorization** — Apply server's `tool_policy`

### Audit Logger

- Append-only JSONL format
- Records all authentication/authorization decisions
- Includes timestamps, client IDs, server IDs, and reasons

### Transport Adapters

**stdio Adapter**
- Spawns local MCP server processes
- Manages subprocess lifecycle
- Handles JSON-RPC over stdin/stdout
- Supports content-length and newline-delimited framing

**HTTP Adapter**
- Proxies to HTTP/SSE MCP servers
- Handles Server-Sent Events parsing
- Manages request timeouts

## Request Flow

1. Client sends request to `/mcp` endpoint
2. LMCP extracts `client_id` and `token` from request
3. Policy engine authenticates client
4. For `tools/list`: aggregates tools from all allowed servers
5. For `tools/call`: authorizes server + tool, then proxies request
6. Response returned to client
7. Decision logged to audit trail

## Design Principles

### What LMCP Does

- Unified endpoint for MCP clients
- Centralized registry and configuration
- Authentication and authorization
- Transport normalization (stdio ↔ HTTP)
- Audit logging

### What LMCP Does NOT Do

- No intent inference
- No planning or orchestration
- No agent coordination
- No persistent memory
- No automatic discovery

LMCP is infrastructure for access control, not an AI system.
