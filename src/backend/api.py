"""FastAPI face over the session service — the API from PLAN.md 3.4.

Mounted under AGENT_API_PREFIX (default: /agent/api/v1) so it can sit behind
the registry's nginx unchanged and match the registry's versioned-prefix
convention. Designed for later merge into biocypher-components-registry;
compatibility notes live in PLAN.md 3.7.

Auth: every /sessions/{id}/... route requires the session token returned by
POST /sessions, either as `Authorization: Bearer <token>` or as a `?token=`
query parameter (the query form exists for browser-native EventSource, which
cannot set headers; prefer the header).

Run:
    uvicorn backend.api:create_app --factory --port 8100
"""

import asyncio
import hashlib
import json
import os
import secrets as pysecrets
import shutil
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend import client_loop as cl
from backend.service import Session, SessionManager, SessionStartupError

# Seconds between SSE heartbeat comments (keeps proxies from closing the
# stream while the agent is idle).
HEARTBEAT_SECONDS = 15

_ERROR_DESCRIPTIONS = {
    400: "Invalid input: bad path, empty content, or missing key field",
    401: "Unknown session or invalid session token",
    404: "No such file or directory",
    409: "Conflict: turn running, stale If-Match, filesystem error, "
    "or nothing to interrupt",
    415: "Not a text file",
    428: "No API key set for this session yet",
    502: "MCP server unreachable or the session's MCP connection died",
}


def _responses(*codes: int) -> dict:
    """OpenAPI documentation for a route's error status codes."""
    return {code: {"description": _ERROR_DESCRIPTIONS[code]} for code in codes}


class KeyIn(BaseModel):
    api_key: str | None = None
    auth_token: str | None = None


class MessageIn(BaseModel):
    content: str


class FileIn(BaseModel):
    content: str


def _etag(text: str) -> str:
    return f'"{hashlib.sha256(text.encode()).hexdigest()[:32]}"'


def _resolve(session: Session, path: str):
    try:
        return cl.resolve_in_root(path, session.workspace)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _add_session_routes(router: APIRouter, mgr: SessionManager, session_dep) -> None:
    @router.post("/sessions", status_code=201, responses=_responses(502))
    async def create_session():
        try:
            session = await mgr.create()
        except SessionStartupError as e:
            raise HTTPException(502, f"could not connect to MCP server: {e}")
        return {
            "session_id": session.id,
            "session_token": session.token,
            "model": cl.MODEL,
            "tools": session.tool_defs,
        }

    @router.get("/sessions/{session_id}", responses=_responses(401))
    async def get_session(session: session_dep):
        return {
            "session_id": session.id,
            "model": cl.MODEL,
            "has_key": session.has_key,
            "busy": session.busy,
            "error": session.error,
            "tools": session.tool_defs,
        }

    @router.delete("/sessions/{session_id}", status_code=204, responses=_responses(401))
    async def delete_session(session: session_dep):
        await mgr.delete(session.id)

    @router.post(
        "/sessions/{session_id}/key",
        status_code=204,
        responses=_responses(400, 401),
    )
    async def set_key(body: KeyIn, session: session_dep):
        if not body.api_key and not body.auth_token:
            raise HTTPException(400, "provide api_key or auth_token")
        session.set_key(body.api_key, body.auth_token)


def _add_chat_routes(router: APIRouter, session_dep) -> None:
    @router.post(
        "/sessions/{session_id}/messages",
        status_code=202,
        responses=_responses(400, 401, 409, 428, 502),
    )
    async def post_message(body: MessageIn, session: session_dep):
        if session.error:
            raise HTTPException(502, f"session is unusable: {session.error}")
        if not session.has_key:
            raise HTTPException(
                428, "no API key set for this session; POST .../key first"
            )
        if not body.content.strip():
            raise HTTPException(400, "content must not be empty")
        if session.busy:
            raise HTTPException(409, "a turn is already running")
        session.busy = True
        turn_id = uuid.uuid4().hex
        session.inbox.put_nowait((turn_id, body.content))
        return {"turn_id": turn_id}

    @router.post(
        "/sessions/{session_id}/interrupt",
        status_code=202,
        responses=_responses(401, 409),
    )
    async def interrupt(session: session_dep):
        if not session.interrupt():
            raise HTTPException(409, "no turn is running")
        return {"status": "interrupting"}


def _add_event_routes(router: APIRouter, mgr: SessionManager, session_dep) -> None:
    @router.get("/sessions/{session_id}/events", responses=_responses(401))
    async def events(session: session_dep):
        queue = session.subscribe()

        async def stream():
            try:
                # The session can be deleted between auth and the generator
                # starting; its teardown already published session_closed to
                # the subscribers it knew about, which excludes this one.
                if mgr.get(session.id) is not session:
                    yield "event: session_closed\ndata: {}\n\n"
                    return
                snapshot = {
                    "has_key": session.has_key,
                    "busy": session.busy,
                    "error": session.error,
                }
                yield f"event: session_state\ndata: {json.dumps(snapshot)}\n\n"
                while True:
                    # asyncio.timeout rather than wait_for: wait_for's cancel
                    # of Queue.get can drop a just-delivered event.
                    try:
                        async with asyncio.timeout(HEARTBEAT_SECONDS):
                            event = await queue.get()
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    yield (
                        f"event: {event['type']}\n"
                        f"id: {event['seq']}\n"
                        f"data: {json.dumps(event['data'])}\n\n"
                    )
                    if event["type"] == "session_closed":
                        return
            finally:
                session.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )


# File IO in the routes below is synchronous inside async handlers: it blocks
# the event loop for the duration of one read/write. Fine at workspace scale
# (text files, single user per session); revisit with anyio.to_thread if the
# workspace ever holds large files.


def _add_file_read_routes(router: APIRouter, session_dep) -> None:
    @router.get("/sessions/{session_id}/files", responses=_responses(400, 401, 404))
    async def list_files(session: session_dep, path: str = ""):
        target = _resolve(session, path or ".")
        if not target.is_dir():
            raise HTTPException(404, f"no such directory: {path}")
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        return {
            "path": path,
            "entries": [
                {
                    "name": e.name,
                    "path": str(e.relative_to(session.workspace)),
                    "is_dir": e.is_dir(),
                }
                for e in entries
            ],
        }

    @router.get(
        "/sessions/{session_id}/file",
        responses=_responses(400, 401, 404, 409, 415),
    )
    async def read_file(path: str, session: session_dep):
        target = _resolve(session, path)
        if not target.is_file():
            raise HTTPException(404, f"no such file: {path}")
        try:
            content = target.read_text()
        except UnicodeDecodeError:
            raise HTTPException(415, f"not a text file: {path}")
        except OSError as e:
            raise HTTPException(409, f"filesystem error: {e}")
        return {"path": path, "content": content, "etag": _etag(content)}


def _check_if_match(target, if_match: str | None) -> None:
    """409 when the file changed (or vanished) since the editor loaded it.

    The check is atomic against the agent's file tools (same event loop, no
    await in between) but not against a concurrent run_command subprocess
    writing the same file.
    """
    if if_match is None:
        return
    if not target.is_file():
        raise HTTPException(409, "file no longer exists")
    if _etag(target.read_text()) != if_match:
        # The agent (or another editor) changed the file since it was loaded;
        # the frontend re-fetches and shows a conflict banner.
        raise HTTPException(409, "file changed since it was loaded")


def _add_file_put_route(router: APIRouter, session_dep) -> None:
    @router.put("/sessions/{session_id}/file", responses=_responses(400, 401, 409))
    async def write_file(
        path: str,
        body: FileIn,
        session: session_dep,
        if_match: Annotated[str | None, Header()] = None,
    ):
        target = _resolve(session, path)
        if target.is_dir():
            raise HTTPException(409, f"is a directory: {path}")
        try:
            _check_if_match(target, if_match)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body.content)
        except OSError as e:
            # e.g. a parent component is an existing file, or permissions
            raise HTTPException(409, f"filesystem error: {e}")
        session.publish("fs_changed", paths=[path])
        return {"path": path, "etag": _etag(body.content)}


def _add_file_delete_route(router: APIRouter, session_dep) -> None:
    @router.delete(
        "/sessions/{session_id}/file",
        status_code=204,
        responses=_responses(400, 401, 404, 409),
    )
    async def delete_file(path: str, session: session_dep):
        target = _resolve(session, path)
        if target == session.workspace:
            raise HTTPException(400, "refusing to delete the workspace root")
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.is_file():
                target.unlink()
            else:
                raise HTTPException(404, f"no such file or directory: {path}")
        except OSError as e:
            raise HTTPException(409, f"filesystem error: {e}")
        session.publish("fs_changed", paths=[path])


def _build_router(mgr: SessionManager) -> APIRouter:
    router = APIRouter()

    def session_auth(
        session_id: str,
        authorization: Annotated[str | None, Header()] = None,
        token: Annotated[str | None, Query()] = None,
    ) -> Session:
        session = mgr.get(session_id)
        supplied = token or ""
        if not supplied and authorization and authorization.startswith("Bearer "):
            supplied = authorization[len("Bearer ") :].strip()
        # Same 401 for unknown id and bad token: don't leak which ids exist.
        if (
            session is None
            or not supplied
            or not pysecrets.compare_digest(supplied, session.token)
        ):
            raise HTTPException(401, "unknown session or invalid session token")
        return session

    session_dep = Annotated[Session, Depends(session_auth)]

    _add_session_routes(router, mgr, session_dep)
    _add_chat_routes(router, session_dep)
    _add_event_routes(router, mgr, session_dep)
    _add_file_read_routes(router, session_dep)
    _add_file_put_route(router, session_dep)
    _add_file_delete_route(router, session_dep)
    return router


def create_app(manager: SessionManager | None = None) -> FastAPI:
    mgr = manager if manager is not None else SessionManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await mgr.shutdown()

    app = FastAPI(title="BioCypher Agentic Workspace API", lifespan=lifespan)
    app.state.manager = mgr
    app.include_router(
        _build_router(mgr), prefix=os.getenv("AGENT_API_PREFIX", "/agent/api/v1")
    )
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.api:create_app",
        factory=True,
        host=os.getenv("AGENT_API_HOST", "127.0.0.1"),
        port=int(os.getenv("AGENT_API_PORT", "8100")),
    )
