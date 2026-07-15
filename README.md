# agentic-workspace
Agentic workspace prototype for BioCypher registry

- claude sdk: claude has skill with this that will be loaded automatically in the chat
- gh copilot sdk: seems more mature on the javascript side
- client-side loop (`src/backend/client_loop.py`): backend is the MCP client, provider-swappable — see below

## Client-side tool loop (`src/backend/client_loop.py`)

Chat with an LLM + BioCypher MCP where **this backend is the MCP client**. It
connects directly to `https://mcp.biocypher.org/mcp` over streamable HTTP,
executes every tool call itself, and hands only the (optionally truncated) tool
result to the model. The Anthropic remote-MCP connector is not used — Anthropic
never contacts the MCP server. This is what `claude_sdk.py` does *not* do:
there, Anthropic's servers connect to the MCP endpoint and full tool results
round-trip through Anthropic.

The LLM provider is a config switch, same code path for both:

- **Anthropic**: set `ANTHROPIC_API_KEY`.
- **Local model**: run an Anthropic-compatible endpoint (LiteLLM proxy, or
  llama.cpp's `/v1/messages`) and set `ANTHROPIC_BASE_URL` + any non-empty
  `ANTHROPIC_API_KEY`. Nothing leaves your infrastructure.

Data minimization: `make_tool` caps each result at `MCP_RESULT_MAX_CHARS`
(default 20000) before it enters model context — the full result stays in this
process. Only what the model actually reads reaches the provider, so with a
local model that is zero and with Anthropic it is bounded to what you allow.

### Install & run

```
conda activate aw               # or: pip install -e .   (pulls anthropic[mcp])

# MCP connectivity check — no LLM key needed:
python src/backend/client_loop.py --list-tools

# Anthropic:
ANTHROPIC_API_KEY=sk-... python src/backend/client_loop.py

# Local model (start LiteLLM/llama.cpp first):
ANTHROPIC_API_KEY=dummy ANTHROPIC_BASE_URL=http://localhost:4000 python src/backend/client_loop.py
```

### Using docker-compose

Place your Anthropic API key inside `secrets/anthropic_api_key` (no quotes;
a trailing newline is fine):
```bash
mkdir -p secrets && printf '%s' "$ANTHROPIC_API_KEY" > secrets/anthropic_api_key
```
Then build the image and start the service using
```bash
docker compose build
docker compose run --rm agent
```
Compose builds and tags the image (`biocypher-agent`) itself and mounts the
secret at `/run/secrets/anthropic_api_key`, which the container reads via
`ANTHROPIC_API_KEY_FILE` — the key never enters the container environment.
This setup is fairly safe for local single-user use. Any `run_command` runs inside the container as a non-root agent user at `/workspace` and the host filesystem is untouchable. The root filesystem is read-only, and there are pids/mem/cpu limits. The API key is read once at startup and then removed from the environment.

### Env vars

- `ANTHROPIC_API_KEY` — required (any non-empty value for local models)
- `ANTHROPIC_API_KEY_FILE` — alternative to `ANTHROPIC_API_KEY`: path to a file
  holding the key; read once at startup and deleted best-effort, so the key
  never sits in the process environment. Preferred for containers (used by
  `docker-compose.yml`); wins over the env var, which is scrubbed either way
- `ANTHROPIC_BASE_URL` — optional, e.g. `http://localhost:4000` for LiteLLM
- `CLAUDE_MODEL` — default `claude-opus-4-8`
- `BIOCYPHER_MCP_URL` — default `https://mcp.biocypher.org/mcp`
- `BIOCYPHER_MCP_AUTH_HEADER` — optional, e.g. `Bearer <token>`
- `BIOCYPHER_MCP_AUTH_HEADER_FILE` — file variant, same semantics as
  `ANTHROPIC_API_KEY_FILE`
- `MCP_RESULT_MAX_CHARS` — default `20000`; cap on result chars reaching context
- `FILE_TOOLS_ROOT` — default cwd; root dir the read/write/edit file tools are
  confined to (`/workspace` in the container)

### Frontend integration

`client_loop.py` is a CLI chat loop (matching `claude_sdk.py`/`gh_sdk.py`). For
the three-pane workspace UI, the same `main()` structure drops into FastAPI:
hold the MCP `ClientSession` open per workspace session and forward the runner's
messages/tool events as SSE instead of printing them.
