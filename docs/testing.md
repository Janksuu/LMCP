# Testing Guide

## Prerequisites

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Configure your registry:
   - Copy `config/registry.example.yaml` to `config/registry.yaml`
   - Set secure tokens for each client
   - Configure your MCP servers

---

## CLI Test Commands

### Configuration Validation

```bash
# Validate registry schema and constraints
python -m lmcp.daemon --registry config/registry.yaml --validate-registry

# Print parsed configuration
python -m lmcp.daemon --registry config/registry.yaml --print-config

# Run authentication and policy self-test
python -m lmcp.daemon --registry config/registry.yaml --self-test
```

### Server Connection Tests

```bash
# Test stdio MCP server (spawns server, lists tools)
python -m lmcp.daemon --registry config/registry.yaml --stdio-test <server-id>

# Test HTTP MCP server (connects, lists tools)
python -m lmcp.daemon --registry config/registry.yaml --http-test <server-id>
```

### Tool Execution Tests

```bash
# Call a tool on a stdio server
python -m lmcp.daemon --registry config/registry.yaml \
  --stdio-call <server-id> --tool <tool-name> --args-json '{}'

# Call a tool with arguments from file
python -m lmcp.daemon --registry config/registry.yaml \
  --stdio-call <server-id> --tool <tool-name> --args-file config/examples/args.json
```

### HTTP Server Mode

```bash
# Start LMCP HTTP server
python -m lmcp.daemon --registry config/registry.yaml --serve-http
```

Server runs on `http://127.0.0.1:7345` by default.

---

## VS Code Integration Test

1. Start LMCP:
   ```bash
   python -m lmcp.daemon --registry config/registry.yaml --serve-http
   ```

2. Add to your VS Code workspace (`.vscode/mcp.json`):
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

3. Restart VS Code and open Agent mode

4. Test with: "Use the lmcp server and list tools"

---

## Success Criteria

LMCP is working correctly when:

- [ ] Registry loads without errors
- [ ] `--validate-registry` passes
- [ ] `--self-test` shows successful auth/policy checks
- [ ] stdio servers can be spawned and tools listed
- [ ] HTTP servers can be connected and tools listed
- [ ] Tool calls execute and return results
- [ ] Audit log records all decisions

---

## Audit Log

Check the audit log for authentication and authorization records:

```bash
cat logs/audit.log
```

Each line is a JSON object with:
- `timestamp` — ISO 8601 timestamp
- `event_type` — `client_auth` or `server_auth`
- `client_id` — Client identifier
- `allowed` — Boolean result
- `reason` — Decision explanation

---

## Troubleshooting

### "Token validation failed"

- Check that your token in the request matches the registry
- Tokens cannot be placeholder values like "replace-me"

### "Server not in allowlist"

- Verify the server ID exists in your registry
- Check the client's `allow_servers` list includes this server

### "stdio server failed to start"

- Verify the command exists (e.g., `npx` in PATH)
- Check that required services are running (e.g., Ollama)
- Review the error message for missing dependencies

### "HTTP server connection failed"

- Verify the server URL is correct
- Check that the MCP server is running
- Test the endpoint directly with curl
