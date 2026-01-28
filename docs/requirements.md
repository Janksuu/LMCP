# Requirements

## Core Dependencies

### Python

- **Python 3.10+** (tested on 3.12 and 3.14)
- PyYAML
- jsonschema

Install from project root:

```bash
pip install -r requirements.txt
```

Verify:

```bash
python --version
python -m pip show pyyaml jsonschema
```

### Node.js (for npx-based MCP servers)

Many MCP servers use `npx` for installation-free execution.

- **Node.js 20+** (includes npm and npx)

Verify:

```bash
node -v
npm -v
npx -v
```

> **Note**: Node.js location doesn't matter as long as `node`, `npm`, and `npx` are in your PATH. Restart your terminal after installation.

---

## MCP Server Dependencies

These are not required for LMCP itself, but for specific MCP servers you want to use.

### Ollama MCP Server

Requires Ollama running locally:

- Ollama Desktop or CLI
- API endpoint at `http://127.0.0.1:11434`

Verify:

```bash
curl http://127.0.0.1:11434/api/tags
```

### ComfyUI MCP Server

Requires ComfyUI with MCP server:

- ComfyUI running locally
- MCP server endpoint at `http://127.0.0.1:9000/mcp`

### Docker-based MCP Servers

Requires Docker:

- Docker Desktop or Docker Engine
- Docker daemon running

Verify:

```bash
docker --version
docker ps
```

---

## Optional Tools

### MCP CLI (for debugging)

```bash
pip install "mcp[cli]"
```

### uv (faster Python package management)

```bash
pip install uv
```

---

## Quick Validation

After setup, validate your LMCP installation:

```bash
# Check registry loads correctly
python -m lmcp.daemon --registry config/registry.yaml --validate-registry

# Run self-test
python -m lmcp.daemon --registry config/registry.yaml --self-test
```
