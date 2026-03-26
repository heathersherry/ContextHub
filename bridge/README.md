# ContextHub OpenClaw Plugin

OpenClaw context-engine plugin that connects ContextHub (enterprise context
management) to the OpenClaw agent runtime.

## Architecture

```
OpenClaw Runtime (TS)
  ├─ ContextHubBridge (bridge/src/bridge.ts)    ← ContextEngine lifecycle
  │    └─ HTTP ──► Python Sidecar (bridge/src/sidecar.py)
  │                  └─ ContextHubContextEngine (plugins/openclaw/)
  │                       └─ ContextHubClient (sdk/)
  │                            └─ ContextHub Server API (:8000)
  │
  └─ 7 Agent Tools (bridge/src/tools.ts)        ← ls/read/grep/stat/store/promote/skill_publish
       └─ HTTP ──► Python Sidecar /dispatch
```

## Prerequisites

- **ContextHub Server** running on `:8000` (PostgreSQL + pgvector + FastAPI)
- **Python 3.11+** with `httpx`, `fastapi`, `uvicorn` for the sidecar
- **Node.js ≥ 22.12** (required by OpenClaw)
- **pnpm** for OpenClaw monorepo

## Quick Start

### 1. Start ContextHub

```bash
cd ContextHub
docker-compose up -d          # PostgreSQL + pgvector
alembic upgrade head          # Run migrations
uvicorn contexthub.main:app --port 8000
```

### 2. Start the Python sidecar

```bash
cd ContextHub
python bridge/src/sidecar.py --port 9100 --contexthub-url http://localhost:8000
```

The sidecar supports multi-agent mode: pass `X-Agent-Id` header per request
to use different ContextHub agent identities.

### 3. Build and install the plugin in OpenClaw

```bash
# Build the TS bridge
cd ContextHub/bridge
npm install
npm run build

# Install as a local OpenClaw plugin (link mode for development)
cd /path/to/openclaw
pnpm openclaw plugins install -l /path/to/ContextHub/bridge
```

### 4. Configure OpenClaw

Add to your `~/.openclaw/openclaw.json`:

```json5
{
  plugins: {
    slots: {
      contextEngine: "contexthub"
    },
    entries: {
      contexthub: {
        enabled: true,
        config: {
          sidecarUrl: "http://localhost:9100"
        }
      }
    }
  }
}
```

### 5. Run OpenClaw

```bash
cd /path/to/openclaw
pnpm openclaw
```

## What the plugin provides

### Context Engine lifecycle

| Method      | Behavior |
|-------------|----------|
| `assemble`  | Auto-recall: searches ContextHub for context relevant to the current conversation and injects it via `systemPromptAddition` |
| `afterTurn` | Auto-capture: extracts reusable facts from assistant responses and writes them as private memories in ContextHub |
| `compact`   | Delegates to OpenClaw's built-in runtime compaction (`delegateCompactionToRuntime`) |
| `ingest`    | No-op (ContextHub does not manage conversation history) |

### Agent tools

| Tool | Description |
|------|-------------|
| `ls` | List contexts under a URI prefix |
| `read` | Read a context by URI |
| `grep` | Semantic search across ContextHub |
| `stat` | Get context metadata |
| `contexthub_store` | Write or update a context |
| `contexthub_promote` | Promote private memory to team scope |
| `contexthub_skill_publish` | Publish a new skill version |

## Multi-agent testing

To simulate multiple agents, run one sidecar per agent identity:

```bash
python bridge/src/sidecar.py --port 9100 --agent-id query-agent
python bridge/src/sidecar.py --port 9101 --agent-id analysis-agent
```

Or use the dynamic `X-Agent-Id` header — the sidecar lazily creates separate
SDK clients per agent identity.

## Verifying the integration

```bash
# Check sidecar health
curl http://localhost:9100/health

# List available tools
curl http://localhost:9100/tools

# Verify context engine is active
cd /path/to/openclaw && pnpm openclaw doctor
```
