"""Unit tests for backend.service — no network, fake MCP and fake Anthropic."""

import asyncio

import pytest

from backend.service import SessionManager, SessionStartupError
from fakes import (
    FakeRunner,
    FakeStream,
    fake_client_factory,
    fake_mcp_connect,
    final_message,
    text_event,
)


def make_manager(tmp_path, mcp_connect=fake_mcp_connect):
    return SessionManager(
        workspaces_root=tmp_path / "workspaces",
        mcp_headers={},
        mcp_connect=mcp_connect,
    )


async def collect_until_done(queue, timeout=5.0):
    events = []
    while True:
        event = await asyncio.wait_for(queue.get(), timeout)
        events.append(event)
        if event["type"] in ("turn_done", "turn_error", "session_error"):
            return events


async def run_turn(session, content):
    queue = session.subscribe()
    session.busy = True
    session.inbox.put_nowait(("turn-1", content))
    events = await collect_until_done(queue)
    session.unsubscribe(queue)
    return events


def test_create_and_delete_session(tmp_path):
    async def scenario():
        manager = make_manager(tmp_path)
        session = await manager.create()
        assert session.workspace.is_dir()
        names = [t["name"] for t in session.tool_defs]
        assert "get_phase_guidance" in names
        assert "run_command" in names
        assert manager.get(session.id) is session
        assert await manager.delete(session.id)
        assert not session.workspace.exists()
        assert manager.get(session.id) is None

    asyncio.run(scenario())


def test_create_session_mcp_failure(tmp_path):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def broken(url, headers):
        raise ConnectionError("no route to MCP")
        yield  # pragma: no cover

    async def scenario():
        manager = make_manager(tmp_path, mcp_connect=broken)
        with pytest.raises(SessionStartupError, match="no route to MCP"):
            await manager.create()
        assert not manager.sessions

    asyncio.run(scenario())


def test_turn_with_tool_call(tmp_path):
    from types import SimpleNamespace

    tool_use = SimpleNamespace(
        type="tool_use", id="tu_1", name="get_phase_guidance", input={"q": "x"}
    )
    tool_response = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu_1",
                "content": [{"type": "text", "text": "guidance text"}],
            }
        ],
    }
    turns = [
        (
            FakeStream([text_event("let me check")], final_message([tool_use])),
            tool_response,
        ),
        (
            FakeStream(
                [text_event("all done")],
                final_message([SimpleNamespace(type="text", text="all done")]),
            ),
            None,
        ),
    ]

    async def scenario():
        manager = make_manager(tmp_path)
        session = await manager.create()
        session.set_key("sk-test", None)
        session.client_factory = fake_client_factory(FakeRunner(turns))
        events = await run_turn(session, "hello")
        types = [e["type"] for e in events]
        assert types[0] == "turn_started"
        assert types[-1] == "turn_done"
        assert "text_delta" in types
        assert "tool_call" in types
        assert "tool_result" in types
        assert types.count("usage") == 2
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["data"]["name"] == "get_phase_guidance"
        assert result["data"]["preview"] == "guidance text"
        # user, assistant(tool_use), tool_result, assistant(final)
        assert len(session.history) == 4
        assert session.busy is False
        await manager.shutdown()

    asyncio.run(scenario())


def test_turn_api_error_rolls_back_history(tmp_path):
    turns = [
        (
            FakeStream([text_event("hi")], final_message([])),
            None,
        )
    ]

    async def scenario():
        manager = make_manager(tmp_path)
        session = await manager.create()
        session.set_key("sk-test", None)
        session.client_factory = fake_client_factory(FakeRunner(turns, error_at=0))
        events = await run_turn(session, "hello")
        assert events[-1]["type"] == "turn_error"
        assert "boom" in events[-1]["data"]["message"]
        assert session.history == []
        assert session.busy is False
        # The session survives a failed turn: run a working one after it.
        session.client_factory = fake_client_factory(FakeRunner(list(turns)))
        events = await run_turn(session, "again")
        assert events[-1]["type"] == "turn_done"
        assert len(session.history) == 2
        await manager.shutdown()

    asyncio.run(scenario())


def test_fs_change_event_from_file_tool(tmp_path):
    async def scenario():
        manager = make_manager(tmp_path)
        session = await manager.create()
        queue = session.subscribe()
        write_file = next(t for t in session.file_tools if t.name == "write_file")
        out = await write_file.call({"path": "a.txt", "content": "hi"})
        assert out == "wrote 2 chars to a.txt"
        assert (session.workspace / "a.txt").read_text() == "hi"
        event = queue.get_nowait()
        assert event["type"] == "fs_changed"
        assert event["data"]["paths"] == ["a.txt"]
        await manager.shutdown()

    asyncio.run(scenario())
