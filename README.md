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


Three layers to track:
1. Storage — where bytes live on disk
2. Wire — what crosses between client / LLM / MCP server
3. Context — what ends up in the model's input tokens (you pay for this)

Data files sit on the BioCypher MCP server. Tools (get_available_workflows, schema lookups, query execution) read those files server-side. Only the tool result crosses the wire — typically small JSON. Bytes never hit the LLM unless the tool literally returns them.

Per-SDK behavior                                                                                                                                                                                                                                                                                                     
           
  Copilot SDK (gh_sdk.py style) — npx bridge speaks stdio MCP, can mount local MCP servers alongside remote. Want local CSV processing? Spin up a local MCP server (Python fastmcp, e.g.) that reads disk, register it next to BioCypher in mcp_servers. Bridge handles both. Data stays local.                        
   
  Anthropic native remote MCP (claude_sdk.py) — Anthropic connects to URL-based MCP servers. Cannot reach your localhost. For local files, route through:                                                                                                                                                              
  - Files API + Code Execution tool (sandboxed, server-side analysis), or
  - Run a manual tool runner where YOUR code reads the file and returns extracts (Pattern 9 from the docs — orchestrator holds the data, sends results as tool_result).                                                                                                                                                
                                                                                                                                                                       
  Vercel AI SDK — backend code holds the file; you write a tool that does fs.readFile + extraction, returns extract. Streaming UI shows progress. No native MCP, so you'd HTTP-call BioCypher directly or proxy through.                                                                                               
                                                                                                                                                                                                                                                                                                                       
  Custom tool runner (manual loop) — most flexible. You decide: read full file, chunk it, summarize before sending, page through a SQL cursor, whatever. Only the chunks you choose enter context.           

  Patterns for big results

  When a BioCypher tool returns 50MB of graph data:

  1. Tool returns file_id, not bytes
     → Claude calls a follow-up tool: summarize(file_id)
     → Or: Code Execution tool loads it, runs analysis, returns summary

  2. Pagination / cursor in tool schema
     → Tool: query(cypher, page=0, page_size=100)
     → Claude iterates, only relevant page enters context

  3. Programmatic Tool Calling (PTC, Anthropic beta)
     → Claude writes Python that calls tools as functions
     → Intermediate results stay in sandbox; only final output to context
     → Best when chaining many tool calls over big intermediates

  4. Filtering at source
     → Tool args force narrow result (entity_type, limit, fields)
     → Don't return what model doesn't need

  Practical for this repo

  If BioCypher MCP exposes a workflow that processes a file you provide:
  - Hosted file: upload it via Anthropic Files API, pass file_id in the tool call (if MCP tool schema accepts it). Anthropic streams bytes to MCP server.
  - Your local file: easier path is a local sidecar MCP server (Copilot SDK pattern) than going through Files API.
  - Output too big: ask BioCypher MCP team to add a "write to file_id, return ref" mode, then layer Code Execution for analysis.

  Rule of thumb: never let raw data round-trip through the LLM context if you can avoid it. Filter / summarize / reference-by-ID at the boundary.


Biocypher-mcp with gh copilot sdk:
keeps timing out

configs:
- schema not placed in correct folder
- currently lacks core properties for each entity type:
   - represented_as: node or edge
   - preferred_id: main identifier field
   - input_label: field name from your data (must match adapter output)
- make sure to use uniprot id if present
- fails to find all interactions
- fails to find all properties of interactions
- creates a list and not a dict for properties

- biocypher schema not mentioned at all

adapter creation:
- enum classes not required in the simple adapter example

adapter validation:
- pytest not running

Documentation and Maintenance:
- also include docstrings per default

comparing gh copilot sdk and python ui: no duplicated entries with gh copilot, due to bc settings?
