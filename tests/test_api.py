"""API tests for backend.api — TestClient over fake MCP and fake Anthropic."""

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api import create_app
from backend.service import SessionManager
from fakes import (
    FakeRunner,
    FakeStream,
    fake_client_factory,
    fake_mcp_connect,
    final_message,
    text_event,
)

PREFIX = "/agent/api/v1"


@pytest.fixture
def manager(tmp_path):
    return SessionManager(
        workspaces_root=tmp_path / "workspaces",
        mcp_headers={},
        mcp_connect=fake_mcp_connect,
    )


@pytest.fixture
def client(manager):
    with TestClient(create_app(manager)) as test_client:
        yield test_client


@pytest.fixture
def session(client, manager):
    """A created session: (session_id, auth headers, Session object)."""
    created = client.post(f"{PREFIX}/sessions").json()
    headers = {"Authorization": f"Bearer {created['session_token']}"}
    return created["session_id"], headers, manager.get(created["session_id"])


def wait_not_busy(client, sid, headers, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get(f"{PREFIX}/sessions/{sid}", headers=headers).json()
        if not state["busy"]:
            return state
        time.sleep(0.02)
    raise AssertionError("turn did not finish in time")


# ------------------------------------------------------------- lifecycle


def test_create_session_returns_tools_and_token(client):
    response = client.post(f"{PREFIX}/sessions")
    assert response.status_code == 201
    body = response.json()
    assert body["session_id"] and body["session_token"]
    names = [t["name"] for t in body["tools"]]
    assert "get_phase_guidance" in names and "write_file" in names


def test_auth_required_and_checked(client, session):
    sid, headers, _ = session
    assert client.get(f"{PREFIX}/sessions/{sid}").status_code == 401
    bad = {"Authorization": "Bearer wrong-token"}
    assert client.get(f"{PREFIX}/sessions/{sid}", headers=bad).status_code == 401
    assert client.get(f"{PREFIX}/sessions/{sid}", headers=headers).status_code == 200
    # unknown session id gives the same 401, not a distinguishable 404
    assert client.get(f"{PREFIX}/sessions/nope", headers=headers).status_code == 401


def test_delete_session(client, manager, session):
    sid, headers, session_obj = session
    workspace = session_obj.workspace
    assert client.delete(f"{PREFIX}/sessions/{sid}", headers=headers).status_code == 204
    assert manager.get(sid) is None
    assert not workspace.exists()
    assert client.get(f"{PREFIX}/sessions/{sid}", headers=headers).status_code == 401


# ------------------------------------------------------------ key + chat


def test_message_requires_key_then_runs(client, session):
    sid, headers, session_obj = session
    url = f"{PREFIX}/sessions/{sid}/messages"
    assert client.post(url, headers=headers, json={"content": "hi"}).status_code == 428

    assert (
        client.post(
            f"{PREFIX}/sessions/{sid}/key", headers=headers, json={}
        ).status_code
        == 400
    )
    response = client.post(
        f"{PREFIX}/sessions/{sid}/key", headers=headers, json={"api_key": "sk-test"}
    )
    assert response.status_code == 204
    assert client.get(f"{PREFIX}/sessions/{sid}", headers=headers).json()["has_key"]

    turns = [
        (
            FakeStream(
                [text_event("hello back")],
                final_message([SimpleNamespace(type="text", text="hello back")]),
            ),
            None,
        )
    ]
    session_obj.client_factory = fake_client_factory(FakeRunner(turns))
    response = client.post(url, headers=headers, json={"content": "hi"})
    assert response.status_code == 202
    assert response.json()["turn_id"]
    wait_not_busy(client, sid, headers)
    assert len(session_obj.history) == 2

    assert client.post(url, headers=headers, json={"content": ""}).status_code == 400


def test_message_conflict_while_busy(client, session):
    sid, headers, session_obj = session
    session_obj.set_key("sk-test", None)
    session_obj.busy = True
    response = client.post(
        f"{PREFIX}/sessions/{sid}/messages", headers=headers, json={"content": "hi"}
    )
    assert response.status_code == 409
    session_obj.busy = False


def test_interrupt_without_turn(client, session):
    sid, headers, _ = session
    response = client.post(f"{PREFIX}/sessions/{sid}/interrupt", headers=headers)
    assert response.status_code == 409


def test_events_stream_snapshot_and_token_query(manager):
    # TestClient cannot cancel an infinite SSE response, so this test runs a
    # real uvicorn server in a thread and closes a real TCP connection.
    import socket
    import threading
    import time as time_mod

    import httpx
    import uvicorn

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(manager), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time_mod.monotonic() + 10
        while not server.started:
            assert time_mod.monotonic() < deadline, "uvicorn did not start"
            time_mod.sleep(0.02)
        base = f"http://127.0.0.1:{port}{PREFIX}"
        created = httpx.post(f"{base}/sessions", timeout=10).json()
        url = f"{base}/sessions/{created['session_id']}/events"
        lines = []
        with httpx.stream(
            "GET", url, params={"token": created["session_token"]}, timeout=10
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            for line in response.iter_lines():
                lines.append(line)
                if len(lines) >= 2:
                    break
        assert lines[0] == "event: session_state"
        assert '"has_key": false' in lines[1]
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# ----------------------------------------------------------------- files


def test_file_roundtrip_and_listing(client, session):
    sid, headers, _ = session
    put = client.put(
        f"{PREFIX}/sessions/{sid}/file",
        params={"path": "sub/a.txt"},
        headers=headers,
        json={"content": "hello"},
    )
    assert put.status_code == 200
    etag = put.json()["etag"]

    got = client.get(
        f"{PREFIX}/sessions/{sid}/file", params={"path": "sub/a.txt"}, headers=headers
    )
    assert got.status_code == 200
    assert got.json() == {"path": "sub/a.txt", "content": "hello", "etag": etag}

    listing = client.get(
        f"{PREFIX}/sessions/{sid}/files", params={"path": ""}, headers=headers
    ).json()
    assert listing["entries"] == [{"name": "sub", "path": "sub", "is_dir": True}]


def test_file_etag_conflict(client, session):
    sid, headers, session_obj = session
    url = f"{PREFIX}/sessions/{sid}/file"
    etag = client.put(
        url, params={"path": "a.txt"}, headers=headers, json={"content": "v1"}
    ).json()["etag"]

    # the agent changes the file behind the editor's back
    (session_obj.workspace / "a.txt").write_text("agent version")

    stale = client.put(
        url,
        params={"path": "a.txt"},
        headers={**headers, "If-Match": etag},
        json={"content": "v2"},
    )
    assert stale.status_code == 409

    current = client.get(url, params={"path": "a.txt"}, headers=headers).json()["etag"]
    ok = client.put(
        url,
        params={"path": "a.txt"},
        headers={**headers, "If-Match": current},
        json={"content": "v2"},
    )
    assert ok.status_code == 200
    assert (session_obj.workspace / "a.txt").read_text() == "v2"


def test_file_path_confinement(client, session):
    sid, headers, _ = session
    for path in ("../escape.txt", "/etc/passwd"):
        response = client.get(
            f"{PREFIX}/sessions/{sid}/file", params={"path": path}, headers=headers
        )
        assert response.status_code == 400, path


def test_file_delete(client, session):
    sid, headers, session_obj = session
    client.put(
        f"{PREFIX}/sessions/{sid}/file",
        params={"path": "gone.txt"},
        headers=headers,
        json={"content": "x"},
    )
    response = client.request(
        "DELETE",
        f"{PREFIX}/sessions/{sid}/file",
        params={"path": "gone.txt"},
        headers=headers,
    )
    assert response.status_code == 204
    assert not (session_obj.workspace / "gone.txt").exists()
    response = client.request(
        "DELETE",
        f"{PREFIX}/sessions/{sid}/file",
        params={"path": "gone.txt"},
        headers=headers,
    )
    assert response.status_code == 404
    response = client.request(
        "DELETE",
        f"{PREFIX}/sessions/{sid}/file",
        params={"path": "."},
        headers=headers,
    )
    assert response.status_code == 400
