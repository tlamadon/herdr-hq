"""HTTP API + static UI.

Endpoints
---------
GET  /                      UI: the fleet dashboard
GET  /api/state             full fleet JSON (same shape as herdr-hq 0.1)
POST /api/refresh           wake every poller now
GET  /api/term/stream?host=&pane=   SSE mirror of one pane
POST /api/term/input        {"host", "pane", "ops": [{"text"}|{"key"}]}
POST /api/term/close        {"host", "pane"}

Everything is behind cookie/token auth (see auth.py) unless listen.auth is
set to none.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import secrets
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .auth import AuthGate
from .config import Config
from .fleet import Fleet
from .pool import SSHPool

log = logging.getLogger("herdrhq.app")

STATIC = Path(__file__).parent / "static"

mimetypes.add_type("text/javascript", ".mjs")  # pdf.js modules

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def err(e: object, code: int = 502) -> JSONResponse:
    return JSONResponse({"error": str(e)}, status_code=code)


def create_app(cfg: Config, pool: SSHPool | None = None, secret: str | None = None):
    """Build the ASGI app. `secret=None` derives one from the config; pass
    a value (tests) to pin it, or set cfg.auth_enabled=False to disable."""
    pool = pool or SSHPool()
    if secret is None and cfg.auth_enabled:
        secret = cfg.password or secrets.token_urlsafe(24)
    if not cfg.auth_enabled:
        secret = None
    fleet = Fleet(cfg, pool)

    # ---------------------------------------------------------------- pages

    async def index(request: Request) -> FileResponse:
        return FileResponse(STATIC / "index.html")

    # ------------------------------------------------------------------ api

    async def state(request: Request) -> JSONResponse:
        return JSONResponse(fleet.state())

    async def refresh(request: Request) -> JSONResponse:
        fleet.refresh()
        return JSONResponse({"ok": True})

    async def term_stream(request: Request) -> Response:
        host = request.query_params.get("host") or ""
        pane = request.query_params.get("pane") or ""
        if not host or not pane:
            return err("host and pane are required", 400)
        try:
            session = await fleet.terminal(host, pane)
        except (PermissionError, KeyError, RuntimeError, OSError, asyncio.TimeoutError) as exc:
            return err(str(exc), 400)

        async def gen():
            q = session.subscribe()
            try:
                yield ": open\n\n"
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), 15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"  # keep the connection alive
                        continue
                    yield f"data: {json.dumps(msg)}\n\n"
                    if msg.get("t") == "closed":
                        return
            finally:
                session.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)

    async def term_input(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return err("body must be JSON", 400)
        if not cfg.terminal_input:
            return err("terminal input is disabled in the config", 403)
        ops = body.get("ops")
        if not isinstance(ops, list):
            return err("ops must be a list", 400)
        try:
            session = await fleet.terminal(body.get("host", ""), body.get("pane", ""))
            await session.send_input(ops)
        except (PermissionError, KeyError, RuntimeError, OSError, asyncio.TimeoutError) as exc:
            return err(str(exc), 400)
        return JSONResponse({"ok": True})

    async def term_close(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        fleet.close_terminal(body.get("host", ""), body.get("pane", ""))
        return JSONResponse({"ok": True})

    routes = [
        Route("/", index),
        Route("/api/state", state),
        Route("/api/refresh", refresh, methods=["POST"]),
        Route("/api/term/stream", term_stream),
        Route("/api/term/input", term_input, methods=["POST"]),
        Route("/api/term/close", term_close, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        if secret and not cfg.password:
            print(
                f"herdr-hq: open http://{cfg.listen_host}:{cfg.listen_port}/?token={secret}",
                flush=True,
            )
        elif secret:
            print("herdr-hq: password auth on (listen.password in herdr-hq.yaml)", flush=True)
        else:
            print("herdr-hq: auth disabled (listen.auth: none)", flush=True)
        fleet.start()
        yield
        await fleet.stop()
        await pool.close()

    inner = Starlette(routes=routes, lifespan=lifespan)
    inner.state.cfg = cfg
    inner.state.pool = pool
    inner.state.fleet = fleet
    app = AuthGate(inner, secret)
    app.fleet = fleet  # reachable from tests and __main__ regardless of wrapping
    app.cfg = cfg
    app.pool = pool
    app.secret = secret
    return app
