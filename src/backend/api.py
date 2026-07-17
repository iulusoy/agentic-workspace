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

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend import client_loop as cl
from backend.service import Session, SessionManager, SessionStartupError

# Seconds between SSE heartbeat comments (keeps proxies from closing the
# stream while the agent is idle).
HEARTBEAT_SECONDS = 15


class KeyIn(BaseModel):
    api_key: str | None = None
    auth_token: str | None = None


class MessageIn(BaseModel):
    content: str


class FileIn(BaseModel):
    content: str


def _etag(text: str) -> str:
    return f'"{hashlib.sha256(text.encode()).hexdigest()[:32]}"'


def create_app(manager: SessionManager | None = None) -> FastAPI:
    mgr = manager if manager is not None else SessionManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await mgr.shutdown()

    app = FastAPI(title="BioCypher Agentic Workspace API", lifespan=lifespan)
    app.state.manager = mgr
    prefix = os.getenv("AGENT_API_PREFIX", "/agent/api/v1")

    def session_auth(
        session_id: str,
        authorization: str | None = Header(None),
        token: str | None = Query(None),
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

    # ------------------------------------------------------ session routes

    @app.post(f"{prefix}/sessions", status_code=201)
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

    @app.get(f"{prefix}/sessions/{{session_id}}")
    async def get_session(session: Session = Depends(session_auth)):
        return {
            "session_id": session.id,
            "model": cl.MODEL,
            "has_key": session.has_key,
            "busy": session.busy,
            "error": session.error,
            "tools": session.tool_defs,
        }

    @app.delete(f"{prefix}/sessions/{{session_id}}", status_code=204)
    async def delete_session(session: Session = Depends(session_auth)):
        await mgr.delete(session.id)

    @app.post(f"{prefix}/sessions/{{session_id}}/key", status_code=204)
    async def set_key(body: KeyIn, session: Session = Depends(session_auth)):
        if not body.api_key and not body.auth_token:
            raise HTTPException(400, "provide api_key or auth_token")
        session.set_key(body.api_key, body.auth_token)

    # --------------------------------------------------------- chat routes

    @app.post(f"{prefix}/sessions/{{session_id}}/messages", status_code=202)
    async def post_message(body: MessageIn, session: Session = Depends(session_auth)):
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

    @app.post(f"{prefix}/sessions/{{session_id}}/interrupt", status_code=202)
    async def interrupt(session: Session = Depends(session_auth)):
        if not session.interrupt():
            raise HTTPException(409, "no turn is running")
        return {"status": "interrupting"}

    @app.get(f"{prefix}/sessions/{{session_id}}/events")
    async def events(session: Session = Depends(session_auth)):
        queue = session.subscribe()

        async def stream():
            try:
                snapshot = {
                    "has_key": session.has_key,
                    "busy": session.busy,
                    "error": session.error,
                }
                yield f"event: session_state\ndata: {json.dumps(snapshot)}\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=HEARTBEAT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    yield (
                        f"event: {event['type']}\n"
                        f"id: {event['seq']}\n"
                        f"data: {json.dumps(event['data'])}\n\n"
                    )
            finally:
                session.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # --------------------------------------------------------- file routes

    def resolve(session: Session, path: str):
        try:
            return cl.resolve_in_root(path, session.workspace)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get(f"{prefix}/sessions/{{session_id}}/files")
    async def list_files(path: str = "", session: Session = Depends(session_auth)):
        target = resolve(session, path or ".")
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

    @app.get(f"{prefix}/sessions/{{session_id}}/file")
    async def read_file(path: str, session: Session = Depends(session_auth)):
        target = resolve(session, path)
        if not target.is_file():
            raise HTTPException(404, f"no such file: {path}")
        try:
            content = target.read_text()
        except UnicodeDecodeError:
            raise HTTPException(415, f"not a text file: {path}")
        etag = _etag(content)
        return {"path": path, "content": content, "etag": etag}

    @app.put(f"{prefix}/sessions/{{session_id}}/file")
    async def write_file(
        path: str,
        body: FileIn,
        session: Session = Depends(session_auth),
        if_match: str | None = Header(None),
    ):
        target = resolve(session, path)
        if target.is_dir():
            raise HTTPException(409, f"is a directory: {path}")
        if if_match is not None:
            if not target.is_file():
                raise HTTPException(409, "file no longer exists")
            current = _etag(target.read_text())
            if current != if_match:
                # The agent (or another editor) changed the file since it was
                # loaded; the frontend re-fetches and shows a conflict banner.
                raise HTTPException(409, "file changed since it was loaded")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.content)
        session.publish("fs_changed", paths=[path])
        return {"path": path, "etag": _etag(body.content)}

    @app.delete(f"{prefix}/sessions/{{session_id}}/file", status_code=204)
    async def delete_file(path: str, session: Session = Depends(session_auth)):
        target = resolve(session, path)
        if target == session.workspace:
            raise HTTPException(400, "refusing to delete the workspace root")
        if target.is_dir():
            shutil.rmtree(target)
        elif target.is_file():
            target.unlink()
        else:
            raise HTTPException(404, f"no such file or directory: {path}")
        session.publish("fs_changed", paths=[path])

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.api:create_app",
        factory=True,
        host=os.getenv("AGENT_API_HOST", "127.0.0.1"),
        port=int(os.getenv("AGENT_API_PORT", "8100")),
    )
