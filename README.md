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

### Env vars

- `ANTHROPIC_API_KEY` — required (any non-empty value for local models)
- `ANTHROPIC_BASE_URL` — optional, e.g. `http://localhost:4000` for LiteLLM
- `CLAUDE_MODEL` — default `claude-opus-4-8`
- `BIOCYPHER_MCP_URL` — default `https://mcp.biocypher.org/mcp`
- `BIOCYPHER_MCP_AUTH_HEADER` — optional, e.g. `Bearer <token>`
- `MCP_RESULT_MAX_CHARS` — default `20000`; cap on result chars reaching context

### Frontend integration

`client_loop.py` is a CLI chat loop (matching `claude_sdk.py`/`gh_sdk.py`). For
the three-pane workspace UI, the same `main()` structure drops into FastAPI:
hold the MCP `ClientSession` open per workspace session and forward the runner's
messages/tool events as SSE instead of printing them.