# Agentic Workspace API

HTTP/SSE API of the workspace backend (`src/backend/api.py` +
`src/backend/service.py`). It serves the three-pane workspace UI — chat,
directory tree, file editor — and is designed to be merged into the
biocypher-components-registry stack later.

All routes live under a configurable prefix, default:

```
/agent/api/v1
```

Run the server:

```bash
pip install -e ".[server]"        # or: uv sync (dev group includes it)
uvicorn backend.api:create_app --factory --port 8100
# or: python -m backend.api
```

## Interactive docs (Swagger)

FastAPI serves the generated OpenAPI docs automatically — with the server
running, open:

- `http://127.0.0.1:8100/docs` — Swagger UI (interactive, try-it-out)
- `http://127.0.0.1:8100/redoc` — ReDoc (read-only)
- `http://127.0.0.1:8100/openapi.json` — raw OpenAPI schema

The docs pages live at the app root, not under the `/agent/api/v1` prefix —
the prefix is part of each route's path, not a mount point. Each route's
error status codes (401/409/428/…) are documented in the generated schema
and match the tables in this file.

Two caveats when trying routes out in Swagger UI:

1. There is no "Authorize" button (auth is a plain header parameter, not an
   OpenAPI security scheme). For each request, fill the `authorization` field
   with `Bearer <session_token>` — the word `Bearer`, a space, then the token
   from `POST /sessions` — or put the bare token in the `token` query field.
2. Do not execute `GET .../events` from Swagger UI: it is an infinite SSE
   stream and the UI waits for the response to complete. Use `curl -N` (see
   the example below) or the frontend's SSE reader instead.

## Security model — read before deploying

**Run this service only inside the hardened container.** Sessions expose a
`run_command` tool to the model: arbitrary shell in the server's context. The
path confinement on file tools and file routes does *not* apply to shell
commands — on a bare host, a prompt-injected turn can read anything the
server's user can read and exfiltrate it. The docker-compose setup (read-only
root, non-root user, cap-drop, resource limits) is the intended boundary;
per-session sibling containers are the multi-tenant answer (deployment.md).

Consequences of the current single-process design:

- **Sessions are not isolated from each other at the shell level.** File
  tools and file routes are confined per workspace, but one session's
  `run_command` can read another session's workspace directory. Acceptable
  for a single-user demo; multi-tenant use requires per-session containers.
- **`POST /sessions` is unauthenticated and uncapped.** Anyone who can reach
  the port can create sessions (each: MCP connection + directory + task) —
  resource exhaustion is trivial. There is also no request-body limit and no
  workspace disk quota. Until the registry merge adds user auth: bind to
  localhost or a private network, front with a reverse proxy that sets
  `client_max_body_size` and rate limits, and monitor disk usage.
- **TLS is a deployment requirement, not built in.** The BYOK key travels in
  a request body and the session token in a header; both need HTTPS
  termination (nginx) on anything but localhost. The `?token=` query form
  additionally lands in access logs — prefer the `Authorization` header via
  a `fetch()`-based SSE reader; reserve `?token=` for quick local debugging.

## Concepts

A **session** is one workspace: its own directory on disk, its own MCP
connection, its own conversation history, and its own user-supplied Anthropic
credential (BYOK). Sessions are independent; deleting a session destroys all
of it.

A **turn** is one user message plus everything the agent does in response
(thinking, tool calls, file edits, shell commands) until it produces its final
answer. Turns are serialized per session — one at a time.

The model only ever sees truncated tool results (`MCP_RESULT_MAX_CHARS`,
default 20000); full results stay in the backend. SSE events carry at most a
500-char preview of each tool result.

Two knowable limits: conversation history grows unbounded with the session
(memory server-side, input tokens per turn — prompt caching softens the cost
but not the growth), so prefer fresh sessions over very long ones. And
`turn_error`/`session_error` messages contain raw exception text; they are
only delivered to the session's own event stream.

## Authentication

`POST /sessions` returns a `session_token`. Every `/sessions/{id}/...` request
must present it, either as

```
Authorization: Bearer <session_token>
```

or as a `?token=<session_token>` query parameter. The query form exists for
browser-native `EventSource`, which cannot set headers; prefer the header (a
`fetch()`-based SSE reader can set it).

Unknown session ids and wrong tokens both return **401** with the same body,
so session ids cannot be enumerated.

## Endpoints

### Session lifecycle

#### `POST /sessions`

Creates a session: allocates the workspace directory and opens the MCP
connection.

- **201**
  ```json
  {
    "session_id": "e53d64ad54dc43f0aa3e321dc7f0f7d4",
    "session_token": "i_sFZ6s1NAoBB...",
    "model": "claude-opus-4-8",
    "tools": [
      {"name": "get_available_workflows", "description": "Main entry point tool ..."},
      {"name": "write_file", "description": "Create or overwrite a text file with the given content."}
    ]
  }
  ```
- **502** — the MCP server could not be reached (session is not created).

The `session_token` is shown exactly once; store it client-side for the
session's lifetime.

#### `GET /sessions/{id}`

Session state — useful after a reconnect.

- **200**
  ```json
  {
    "session_id": "e53d...",
    "model": "claude-opus-4-8",
    "has_key": true,
    "busy": false,
    "error": null,
    "tools": [ ... ]
  }
  ```

`error` is non-null when the session's MCP connection died; such a session
only serves file routes and should be deleted.

#### `DELETE /sessions/{id}`

Ends the session: closes the MCP connection, drops the history, deletes the
workspace directory, forgets the key.

- **204**

### Key upload (BYOK)

#### `POST /sessions/{id}/key`

Attach the user's Anthropic credential to the session. The UI collects it in
the chat pane (masked input) but **must** send it here — never as a chat
message; anything in a chat message enters model context, history, and
provider logs. The backend holds the key in memory only and destroys it with
the session.

Body — one of:

```json
{"api_key": "sk-ant-..."}
{"auth_token": "<bearer token, e.g. for a gateway>"}
```

- **204** — stored.
- **400** — neither field given.

Calling it again replaces the credential.

### Chat

#### `POST /sessions/{id}/messages`

Append a user turn and start the tool loop. Returns immediately; progress
arrives on the events stream.

Body:

```json
{"content": "Create a BioCypher adapter for UniProt."}
```

- **202** — `{"turn_id": "1f0c..."}`
- **400** — empty `content`.
- **409** — a turn is already running; wait for `turn_done`/`turn_error`.
- **428** — no key set yet (`POST .../key` first).
- **502** — session unusable (MCP connection died).

If the turn fails mid-way (provider error, interrupt), the whole partial turn
is rolled back from history — the user simply retries.

#### `POST /sessions/{id}/interrupt`

Cancel the running turn (or a turn that was accepted but has not started
yet). History rolls back to the pre-turn snapshot and a `turn_error` event
with message `"interrupted"` is emitted.

- **202** — `{"status": "interrupting"}`
- **409** — no turn is running or queued.

#### `GET /sessions/{id}/events`

Server-sent events stream (`text/event-stream`). The first event is always a
snapshot:

```
event: session_state
data: {"has_key": false, "busy": false, "error": null}
```

then live events follow, each with an incrementing `id:`:

```
event: text_delta
id: 42
data: {"text": "Let me check the available workflows."}
```

A comment line `: heartbeat` is sent after 15 idle seconds to keep proxies
from closing the stream.

| Event | Data | Meaning |
|---|---|---|
| `session_state` | `{has_key, busy, error}` | snapshot on (re)connect |
| `turn_started` | `{turn_id}` | turn accepted by the worker |
| `thinking_started` | `{}` | model is in a thinking block (show a marker) |
| `text_delta` | `{text}` | assistant text, streamed |
| `tool_call` | `{name, args}` | the model invoked a tool (render as a chip) |
| `tool_result` | `{name, is_error, chars, preview}` | tool finished; `preview` is the first 500 chars, `chars` the full length that entered model context |
| `usage` | `{input, cache_read, cache_write, output}` | token usage of one API call within the turn |
| `fs_changed` | `{paths}` | workspace changed (agent write/edit, any `run_command` — empty-string path means "anything may have changed" — or an editor `PUT`); refresh the tree and open files |
| `turn_done` | `{turn_id}` | turn finished; the session accepts the next message |
| `turn_error` | `{turn_id, message}` | turn failed or was interrupted; history rolled back |
| `session_error` | `{message}` | MCP connection died; session is unusable |
| `session_closed` | `{}` | session was deleted; the stream ends after this event |

Multiple subscribers are allowed (e.g. two browser tabs); events are fanned
out to all of them. There is no replay — connect to the stream before sending
messages — and the `id:` field is informational only (`Last-Event-ID` on
reconnect is not honored). Each subscriber buffers at most 1000 events; a
consumer that stalls longer than that loses the oldest events.

### Files (directory pane + editor)

All `path` parameters are relative to the session workspace. Absolute paths
and `..` are rejected with **400**; symlink escapes are blocked server-side.

#### `GET /sessions/{id}/files?path=<dir>`

List one directory level (directories first, then files, both sorted).
Omit `path` (or pass `""`) for the workspace root.

- **200**
  ```json
  {
    "path": "",
    "entries": [
      {"name": "adapter", "path": "adapter", "is_dir": true},
      {"name": "README.md", "path": "README.md", "is_dir": false}
    ]
  }
  ```
- **404** — not a directory.

Build the tree by fetching levels lazily as the user expands them.

#### `GET /sessions/{id}/file?path=<file>`

- **200** — `{"path": "a.txt", "content": "...", "etag": "\"c8bfeab3...\""}`
- **404** — no such file.
- **415** — not a text file.

Keep the `etag`; the editor sends it back on save.

#### `PUT /sessions/{id}/file?path=<file>`

Create or overwrite a file. Parent directories are created as needed.

Body: `{"content": "..."}`. Optional header: `If-Match: <etag>`.

- **200** — `{"path": "a.txt", "etag": "<new etag>"}`
- **409** — `If-Match` given and the file changed since it was loaded (the
  agent or another editor wrote it), or the file was deleted, or the path is
  a directory. Re-fetch, show a conflict banner, let the user decide.

Send `If-Match` when saving an opened file; omit it when creating a new one.
A successful `PUT` also emits `fs_changed` to all event subscribers.

Filesystem errors (a parent path component is an existing file, permissions)
also return **409** with the OS error message. The `If-Match` check is atomic
against the agent's file tools, but not against a shell command
(`run_command`) writing the same file at the same instant — a lost update
there is theoretically possible and surfaces as the next `fs_changed`.

#### `DELETE /sessions/{id}/file?path=<path>`

Delete a file or directory (recursively).

- **204** — deleted (emits `fs_changed`).
- **400** — refusing to delete the workspace root.
- **404** — no such file or directory.

## Status code summary

| Code | Meaning here |
|---|---|
| 400 | invalid input: bad path, empty message, key body without a key |
| 401 | missing/wrong session token, or unknown session id |
| 409 | conflict: turn already running, stale `If-Match`, no turn to interrupt, filesystem error on a file route |
| 415 | binary file requested as text |
| 428 | no API key set for the session yet |
| 502 | MCP server unreachable / session's MCP connection died |

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_API_PREFIX` | `/agent/api/v1` | route prefix (registry nginx convention) |
| `AGENT_API_HOST` / `AGENT_API_PORT` | `127.0.0.1` / `8100` | bind address for `python -m backend.api` |
| `AGENT_WORKSPACES_ROOT` | `./workspaces` | parent dir of per-session workspaces |
| `AGENT_SESSION_READY_TIMEOUT` | `30` | seconds to wait for MCP on session create |
| `BIOCYPHER_MCP_URL` | `https://mcp.biocypher.org/mcp` | MCP server |
| `BIOCYPHER_MCP_AUTH_HEADER[_FILE]` | — | MCP auth header, read once at service start |
| `CLAUDE_MODEL` | `claude-opus-4-8` | model for all sessions |
| `ANTHROPIC_BASE_URL` | — | Anthropic-compatible endpoint (LiteLLM, llama.cpp) |
| `CLAUDE_THINKING` | auto | `adaptive`/`off` override |
| `MCP_RESULT_MAX_CHARS` | `20000` | tool-result cap before model context |

Note: the Anthropic key is *not* configured via environment — each session
receives its own key through `POST .../key`.

## Example: full session with curl

```bash
B=http://127.0.0.1:8100/agent/api/v1

# 1. create a session
CREATED=$(curl -s -X POST $B/sessions)
SID=$(echo "$CREATED" | jq -r .session_id)
TOK=$(echo "$CREATED" | jq -r .session_token)
AUTH="Authorization: Bearer $TOK"

# 2. watch events (separate terminal)
curl -sN "$B/sessions/$SID/events?token=$TOK"

# 3. upload the key, then chat
curl -s -X POST $B/sessions/$SID/key -H "$AUTH" -H "Content-Type: application/json" \
     -d '{"api_key": "sk-ant-..."}' -o /dev/null
curl -s -X POST $B/sessions/$SID/messages -H "$AUTH" -H "Content-Type: application/json" \
     -d '{"content": "What BioCypher workflows are available?"}'

# 4. browse and edit the workspace
curl -s "$B/sessions/$SID/files" -H "$AUTH"
curl -s -X PUT "$B/sessions/$SID/file?path=notes.md" -H "$AUTH" \
     -H "Content-Type: application/json" -d '{"content": "# Notes"}'

# 5. clean up
curl -s -X DELETE $B/sessions/$SID -H "$AUTH"
```
