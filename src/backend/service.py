"""Session service: the client-side tool loop lifted into per-session actors.

Each workspace session owns:

- a workspace directory (file tools and run_command are confined to it),
- a conversation history with the snapshot/rollback pattern per turn,
- an MCP connection living in a dedicated actor task (the asyncio context
  managers must be entered and exited by the same task — see PLAN.md 3.2),
- optionally a user-supplied Anthropic key/token (BYOK, held in memory only).

Turns are serialized per session: the actor consumes one message at a time
from the session inbox. Progress is fanned out to SSE subscribers as events;
full tool results never leave this process (only truncated text enters model
context, and events carry a short preview).

``backend.api`` is the HTTP face over :class:`SessionManager`.
"""

import asyncio
import contextlib
import os
import secrets
import shutil
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
from anthropic import AsyncAnthropic
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from backend import client_loop as cl

# Seconds to wait for the MCP connection when creating a session.
READY_TIMEOUT = float(os.getenv("AGENT_SESSION_READY_TIMEOUT", "30"))
# Chars of each tool result included in the tool_result SSE event.
EVENT_PREVIEW_CHARS = 500


class SessionStartupError(Exception):
    """MCP connection could not be established for a new session."""


@asynccontextmanager
async def connect_mcp(url: str, headers: dict[str, str]):
    """Default MCP connector; tests inject a fake with the same shape."""
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            yield mcp


def _block_get(block, key, default=None):
    """Read a field from an SDK content block (object or plain dict)."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _tool_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            _block_get(c, "text", "")
            for c in content
            if _block_get(c, "type") == "text"
        )
    return str(content)


class Session:
    def __init__(self, workspace: Path, mcp_url: str, mcp_headers: dict[str, str]):
        self.id = uuid.uuid4().hex
        self.token = secrets.token_urlsafe(32)
        self.workspace = workspace
        self.mcp_url = mcp_url
        self.mcp_headers = mcp_headers
        self.history: list = []
        self.api_key: str | None = None
        self.auth_token: str | None = None
        self.tools: list = []
        self.tool_defs: list[dict] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.subscribers: set[asyncio.Queue] = set()
        self.ready = asyncio.Event()
        self.error: str | None = None
        self.busy = False
        self.turn_task: asyncio.Task | None = None
        self.actor: asyncio.Task | None = None
        self._seq = 0
        # Test seams; the API layer never touches these.
        self.client_factory = AsyncAnthropic
        self.mcp_connect = connect_mcp
        self.file_tools = cl.make_file_tools(
            get_root=lambda: self.workspace,
            # The service runs inside one Python environment (the container
            # venv); there is no interactive env prompt like in the CLI.
            get_exec_bin=lambda: Path(sys.executable).parent,
            on_fs_change=self._on_fs_change,
        )

    @property
    def has_key(self) -> bool:
        return bool(self.api_key or self.auth_token)

    def set_key(self, api_key: str | None, auth_token: str | None) -> None:
        self.api_key = api_key or None
        self.auth_token = auth_token or None

    # ---------------------------------------------------------- events

    def publish(self, event_type: str, **data) -> None:
        self._seq += 1
        event = {"seq": self._seq, "type": event_type, "data": data}
        for queue in list(self.subscribers):
            queue.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    def _on_fs_change(self, path: str) -> None:
        self.publish("fs_changed", paths=[path])

    # ----------------------------------------------------------- actor

    async def run_actor(self) -> None:
        """Own the MCP connection and consume the inbox, one turn at a time."""
        try:
            async with self.mcp_connect(self.mcp_url, self.mcp_headers) as mcp:
                listed = await mcp.list_tools()
                self.tools = [
                    cl.make_tool(t, mcp) for t in listed.tools
                ] + self.file_tools
                self.tool_defs = [
                    {
                        "name": t.name,
                        "description": (t.description or "").strip().splitlines()[0]
                        if t.description
                        else "",
                    }
                    for t in listed.tools
                ] + [
                    {
                        "name": t.name,
                        "description": (t.description or "").strip().splitlines()[0],
                    }
                    for t in self.file_tools
                ]
                self.ready.set()
                while True:
                    item = await self.inbox.get()
                    if item is None:
                        return
                    turn_id, content = item
                    self.turn_task = asyncio.create_task(
                        self._run_turn(turn_id, content)
                    )
                    try:
                        await self.turn_task
                    except asyncio.CancelledError:
                        # The turn was interrupted, not the actor; the turn
                        # already rolled back history and emitted turn_error.
                        pass
                    finally:
                        self.turn_task = None
                        self.busy = False
        except Exception as e:  # noqa: BLE001 — session becomes unusable, record why
            self.error = f"{type(e).__name__}: {e}"
            self.ready.set()
            self.publish("session_error", message=self.error)

    async def _run_turn(self, turn_id: str, content: str) -> None:
        self.publish("turn_started", turn_id=turn_id)
        snapshot = len(self.history)
        self.history.append({"role": "user", "content": content})
        client = self.client_factory(api_key=self.api_key, auth_token=self.auth_token)
        thinking = cl.thinking_config()
        runner = client.beta.messages.tool_runner(
            model=cl.MODEL,
            max_tokens=16000,
            system=cl.build_system_prompt(self.workspace, cl.RESULT_MAX_CHARS),
            tools=self.tools,
            messages=self.history,
            stream=True,
            cache_control={"type": "ephemeral"},
            **({"thinking": thinking} if thinking else {}),
        )
        try:
            async for stream in runner:
                async with stream:
                    await self._emit_stream(stream)
                    message = await stream.get_final_message()
                usage = message.usage
                self.publish(
                    "usage",
                    input=usage.input_tokens,
                    cache_read=usage.cache_read_input_tokens,
                    cache_write=usage.cache_creation_input_tokens,
                    output=usage.output_tokens,
                )
                self.history.append({"role": "assistant", "content": message.content})
                names_by_id = {}
                for block in message.content:
                    if _block_get(block, "type") == "tool_use":
                        names_by_id[_block_get(block, "id")] = _block_get(block, "name")
                        self.publish(
                            "tool_call",
                            name=_block_get(block, "name"),
                            args=_block_get(block, "input"),
                        )
                tool_response = await runner.generate_tool_call_response()
                if tool_response is not None:
                    self.history.append(tool_response)
                    for block in _block_get(tool_response, "content", []):
                        if _block_get(block, "type") != "tool_result":
                            continue
                        text = _tool_result_text(_block_get(block, "content", ""))
                        self.publish(
                            "tool_result",
                            name=names_by_id.get(_block_get(block, "tool_use_id")),
                            is_error=bool(_block_get(block, "is_error", False)),
                            chars=len(text),
                            preview=text[:EVENT_PREVIEW_CHARS],
                        )
            # busy flips before the terminal event so that a client reacting
            # to turn_done can immediately POST the next message without 409.
            self.busy = False
            self.publish("turn_done", turn_id=turn_id)
        except asyncio.CancelledError:
            del self.history[snapshot:]
            self.busy = False
            self.publish("turn_error", turn_id=turn_id, message="interrupted")
            raise
        except anthropic.APIError as e:
            # Same rationale as the CLI: a partial turn can leave a tool_use
            # without its tool_result, which would 400 on every next request.
            del self.history[snapshot:]
            self.busy = False
            self.publish("turn_error", turn_id=turn_id, message=str(e))

    async def _emit_stream(self, stream) -> None:
        thinking_marked = False
        async for event in stream:
            if event.type == "text":
                self.publish("text_delta", text=event.text)
            elif event.type == "thinking" and not thinking_marked:
                self.publish("thinking_started")
                thinking_marked = True

    def interrupt(self) -> bool:
        if self.turn_task is not None and not self.turn_task.done():
            self.turn_task.cancel()
            return True
        return False


class SessionManager:
    def __init__(
        self,
        workspaces_root: Path | str | None = None,
        mcp_url: str | None = None,
        mcp_headers: dict[str, str] | None = None,
        mcp_connect=None,
    ):
        self.workspaces_root = Path(
            workspaces_root or os.getenv("AGENT_WORKSPACES_ROOT", "workspaces")
        ).resolve()
        self.mcp_url = mcp_url or cl.MCP_URL
        # Read once for the whole service: read_secret scrubs the env var,
        # so per-session reads would only work for the first session.
        self.mcp_headers = cl.mcp_headers() if mcp_headers is None else mcp_headers
        self.mcp_connect = mcp_connect
        self.sessions: dict[str, Session] = {}

    async def create(self) -> Session:
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        session = Session(self.workspaces_root, self.mcp_url, self.mcp_headers)
        session.workspace = self.workspaces_root / session.id
        session.workspace.mkdir()
        if self.mcp_connect is not None:
            session.mcp_connect = self.mcp_connect
        session.actor = asyncio.create_task(session.run_actor())
        try:
            await asyncio.wait_for(session.ready.wait(), timeout=READY_TIMEOUT)
        except asyncio.TimeoutError:
            session.error = f"MCP connection timed out after {READY_TIMEOUT}s"
        if session.error:
            await self._teardown(session)
            raise SessionStartupError(session.error)
        self.sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    async def delete(self, session_id: str) -> bool:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return False
        await self._teardown(session)
        return True

    async def _teardown(self, session: Session) -> None:
        session.set_key(None, None)
        session.interrupt()
        if session.actor is not None and not session.actor.done():
            session.inbox.put_nowait(None)
            try:
                await asyncio.wait_for(session.actor, timeout=10)
            except (asyncio.TimeoutError, Exception):
                session.actor.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await session.actor
        shutil.rmtree(session.workspace, ignore_errors=True)

    async def shutdown(self) -> None:
        for session_id in list(self.sessions):
            await self.delete(session_id)
