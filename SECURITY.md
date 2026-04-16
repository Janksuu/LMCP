# Security

## Security Model

LMCP operates under a **local threat model**. It is designed for a single
operator running AI tool servers on their own machine. The security boundary
is the host operating system.

### What LMCP Protects Against

- **Unauthorized tool access**: per-client tokens and server allowlists
- **Token enumeration**: rate-limited probe endpoints (10 rpm)
- **Timing attacks**: constant-time token comparison (hmac.compare_digest)
- **Audit tampering**: append-only JSONL log, never modified by the daemon
- **XSS in management UI**: all user-controlled values escaped before rendering
- **Config corruption**: atomic backup-then-write, write lock prevents races
- **SSE resource exhaustion**: max 50 concurrent subscribers

### What LMCP Does NOT Protect Against

- **Compromised host OS**: if the host is compromised, LMCP's guarantees
  do not hold. Tokens, registry, and audit log are on the local filesystem.
- **Network eavesdropping**: loopback traffic is unencrypted. When
  `loopback_only: false`, tokens travel in cleartext over HTTP.
- **Management token compromise**: the management token is root access.
  An attacker with it can add servers with arbitrary commands, modify
  client permissions, and rotate the management token itself.
- **Disk-level token theft**: tokens are stored in plaintext in
  `registry.yaml`. File permissions are the primary protection.
  Token hashing is planned for a future release.

## Accepted Risks

| Risk | Severity | Rationale |
|------|----------|-----------|
| Plaintext tokens on disk | High | Local threat model; file permissions are the gate. Token hashing (bcrypt) is planned. |
| Management token = root access | High | By design. The management token controls the registry. Treat it like a root password. |
| Post-auth RCE via server addition | Critical (post-auth) | An operator with management access can add servers with arbitrary commands. This is intentional -- the operator controls what servers exist. |
| Backup files contain old tokens | Medium | `.bak` files are created on registry apply. They are gitignored but exist on disk. |
| No TLS on loopback | Low | Loopback traffic does not traverse the network. TLS adds no meaningful security for 127.0.0.1. |

## Audit History

| Date | Scope | Findings | Fixes |
|------|-------|----------|-------|
| 2026-04-15 | Full v3 audit (6 phases) | 12 findings (1 critical post-auth, 3 high, 5 medium, 3 low) | All critical/high fixed in same release |

### Findings Summary

**Fixed:**
- Probe endpoint brute force: /auth-check and /server-check now rate-limited (10 rpm)
- SSE connection exhaustion: max 50 subscribers enforced
- Information disclosure: /describe and /status no longer expose client IDs, server commands, or file paths
- Timing attack: token comparison uses hmac.compare_digest
- XSS: single-quote breakout in UI matrix fixed (esc() now escapes quotes)
- Backup file leak: .bak files now gitignored

**Accepted (documented):**
- Management token self-rotation (by design)
- Post-auth RCE via server addition (by design)
- Plaintext token storage (planned fix: bcrypt hashing)

## Reporting Vulnerabilities

If you find a security issue in LMCP, please report it by opening a
GitHub issue at https://github.com/Janksuu/LMCP/issues with the label
"security". For sensitive issues, contact the maintainer directly.

## Security Configuration

### Enable Management Auth

```yaml
lmcp:
  management_token: "your-strong-management-secret"
```

Without this, management endpoints are disabled and the UI is read-only.

### Restrict File Permissions (POSIX)

```bash
chmod 600 config/registry.yaml
```

LMCP warns at startup if the registry is readable by group or other users.

### Rate Limiting

```yaml
lmcp:
  rate_limit_rpm: 60        # global default

clients:
  vscode:
    rate_limit_rpm: 120      # per-client override
```

Probe endpoints (/auth-check, /server-check) are always rate-limited at
10 rpm regardless of client configuration.
