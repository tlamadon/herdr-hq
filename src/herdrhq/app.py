"""HTTP API + static UI.

Endpoints
---------
GET  /                      UI: the fleet dashboard
GET  /browse                UI: file browser, services, tunnels, live views
GET  /view?host=&path=      UI: live PDF/image/markdown viewer
GET  /api/state             full fleet JSON (same shape as herdr-hq 0.1)
POST /api/refresh           wake every poller now
GET  /api/term/stream?host=&pane=   SSE mirror of one pane
POST /api/term/input        {"host", "pane", "ops": [{"text"}|{"key"}]}
POST /api/term/close        {"host", "pane"}
GET  /api/hosts             configured + connected hosts (for the browse view)
GET  /api/fs/ls?host=&path=     directory listing (path defaults to remote home)
GET  /api/fs/file?host=&path=   stream file bytes (&dl=1 forces download)
GET  /api/fs/watch?host=&path=  SSE: fires when (mtime, size) settles on a new value
GET  /api/views             open live views (one per /api/fs/watch stream)
DELETE /api/views/{id}      detach a live view (its tab closes itself)
GET  /api/forwards          list raw TCP tunnels
POST /api/forwards          {"host", "port", "remote_host"?, "local"?}
DELETE /api/forwards/{id}   drop a tunnel
GET  /api/services          declared HTTP services with their proxy URLs
POST /api/preview           {"host", "port"} -> ad-hoc proxied preview URL
GET  /__bless?to=           redirect to a service URL with ?hq_token= appended

Plus the reverse proxy itself: http://<service>.<host>.localhost:<port>/ and
http://p<port>.<host>.localhost:<port>/ relay to the remote app (see proxy.py).

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
from urllib.parse import urlsplit

import asyncssh

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .auth import AuthGate
from .config import Config
from .files import FileRoutes
from .fleet import Fleet
from .pool import SSHPool
from .proxy import TOKEN_PARAM, ProxyRouter, match_service, service_fid

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
    files = FileRoutes(cfg, pool)

    # ---------------------------------------------------------------- pages

    async def index(request: Request) -> FileResponse:
        return FileResponse(STATIC / "index.html")

    async def browse(request: Request) -> FileResponse:
        return FileResponse(STATIC / "browse.html")

    async def view(request: Request) -> FileResponse:
        return FileResponse(STATIC / "viewer.html")

    async def favicon(request: Request) -> FileResponse:
        return FileResponse(STATIC / "favicon.svg")

    async def bless(request: Request) -> Response:
        """Grant this visitor's auth to a service origin.

        Service origins (<service>.<host>.localhost) keep their own cookie
        jars, so the proxy bounces unauthenticated browser visits here.
        Reaching this handler means the visitor got past AuthGate on the main
        origin; send them back with ?hq_token=... so the proxy sets that
        origin's cookie. The target must be one of our own service URLs --
        the token must not leak.
        """
        to = request.query_params.get("to") or ""
        u = urlsplit(to)
        if not (
            u.scheme == "http"
            and u.port == cfg.listen_port
            and u.hostname
            and match_service(cfg, u.hostname)
        ):
            return err("not a service URL of this herdr-hq", 400)
        if secret:
            to += ("&" if u.query else "?") + f"{TOKEN_PARAM}={secret}"
        return RedirectResponse(to, status_code=303)

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

    def _ssh_target(host: str) -> str:
        """Resolve a UI host name to the pool's ssh destination."""
        spec = cfg.hosts.get(host)
        if spec is not None and spec.transport == "local":
            raise ValueError(f"{host} is this machine — no tunnel needed")
        return spec.ssh_target if spec is not None else host

    async def forwards_get(request: Request) -> JSONResponse:
        out = []
        for f, up in pool.all_forwards():
            if f.internal:
                continue  # HTTP service backings show up under /api/services
            out.append({
                "id": f.id,
                "host": f.host,
                "remote_host": f.remote_host,
                "remote_port": f.remote_port,
                "local_port": f.local_port,
                "declared": f.declared,
                "up": up,
            })
        return JSONResponse(out)

    async def forwards_post(request: Request) -> JSONResponse:
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return err("body must be JSON", 400)
        host = data.get("host")
        port = data.get("port")
        remote_host = data.get("remote_host") or "localhost"
        local = data.get("local") or 0
        if not host or not port:
            return err('need {"host": ..., "port": ...}', 400)
        try:
            target = _ssh_target(host)
            f = await pool.add_forward(target, int(port), remote_host, desired_local=int(local))
        except (OSError, asyncssh.Error, ValueError, asyncio.TimeoutError) as e:
            return err(e)
        return JSONResponse(
            {"id": f.id, "local_port": f.local_port, "url": f"http://127.0.0.1:{f.local_port}"}
        )

    async def forwards_delete(request: Request) -> JSONResponse:
        fid = request.path_params["fid"]
        host = fid.split(":", 1)[0]
        ok = await pool.remove_forward(host, fid)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    def _service_url(host_name: str, service: str) -> str:
        url = f"http://{service}.{host_name}.localhost:{cfg.listen_port}/"
        if secret:
            # Service origins have their own cookie jars; the token in the
            # link blesses each one on first visit (proxy sets the cookie
            # and redirects to the clean URL).
            url += f"?{TOKEN_PARAM}={secret}"
        return url

    async def services(request: Request) -> JSONResponse:
        out = []
        for hs in cfg.hosts.values():
            for s in hs.http:
                if hs.transport == "local":
                    up = True  # no forward needed; the port is already here
                else:
                    st = pool.peek_state(hs.ssh_target)
                    f = st.forwards.get(service_fid(hs.name, s.name)) if st else None
                    up = bool(st and not st.closed and f and f.local_port)
                out.append({
                    "name": s.name,
                    "host": hs.name,
                    "target": f"{s.remote_host}:{s.remote_port}",
                    "url": _service_url(hs.name, s.name),
                    "up": up,
                })
        return JSONResponse(out)

    async def preview(request: Request) -> JSONResponse:
        """Turn a discovered listening port into a proxied preview URL."""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return err("body must be JSON", 400)
        host = data.get("host")
        try:
            port = int(data.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        hs = cfg.hosts.get(host or "")
        if hs is None:
            return err(f"unknown host {host!r}", 400)
        if not 0 < port < 65536:
            return err("port must be 1-65535", 400)
        service = f"p{port}"
        local_port = port if hs.transport == "local" else None
        if hs.transport != "local":
            try:  # warm the forward so the first click is instant
                f = await pool.add_forward(
                    hs.ssh_target, port, "localhost",
                    fid=service_fid(hs.name, service), internal=True,
                )
                local_port = f.local_port
            except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
                return err(e)
        return JSONResponse({
            "url": _service_url(hs.name, service),
            "service": service,
            "host": hs.name,
            "local_port": local_port,
        })

    async def term_close(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        fleet.close_terminal(body.get("host", ""), body.get("pane", ""))
        return JSONResponse({"ok": True})

    routes = [
        Route("/", index),
        Route("/browse", browse),
        Route("/view", view),
        Route("/favicon.svg", favicon),
        Route("/__bless", bless),
        Route("/api/services", services),
        Route("/api/preview", preview, methods=["POST"]),
        Route("/api/state", state),
        Route("/api/refresh", refresh, methods=["POST"]),
        Route("/api/term/stream", term_stream),
        Route("/api/term/input", term_input, methods=["POST"]),
        Route("/api/term/close", term_close, methods=["POST"]),
        Route("/api/hosts", files.hosts),
        Route("/api/fs/ls", files.ls),
        Route("/api/fs/file", files.file),
        Route("/api/fs/watch", files.watch),
        Route("/api/views", files.views_get),
        Route("/api/views/{vid:int}", files.views_delete, methods=["DELETE"]),
        Route("/api/forwards", forwards_get, methods=["GET"]),
        Route("/api/forwards", forwards_post, methods=["POST"]),
        Route("/api/forwards/{fid:path}", forwards_delete, methods=["DELETE"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]

    async def _ensure_host(hs) -> None:
        for t in hs.tunnels:
            try:
                await pool.add_forward(
                    hs.ssh_target, t.remote_port, t.remote_host,
                    desired_local=t.local, declared=True,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("tunnel %s:%s on %s: %s", t.remote_host, t.remote_port, hs.name, e)
        for s in hs.http:
            try:
                await pool.add_forward(
                    hs.ssh_target, s.remote_port, s.remote_host,
                    fid=service_fid(hs.name, s.name), internal=True, declared=True,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("service %s on %s: %s", s.name, hs.name, e)

    async def _ensure_loop() -> None:
        """Keep declared tunnels and services alive; self-heals after drops."""
        while True:
            targets = [
                hs for hs in cfg.hosts.values()
                if hs.transport != "local" and (hs.tunnels or hs.http)
            ]
            if targets:
                await asyncio.gather(*(_ensure_host(hs) for hs in targets),
                                     return_exceptions=True)
            await asyncio.sleep(20)

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
        ensure_task = asyncio.create_task(_ensure_loop())
        yield
        ensure_task.cancel()
        await fleet.stop()
        await app.client.aclose()
        await pool.close()

    inner = Starlette(routes=routes, lifespan=lifespan)
    inner.state.cfg = cfg
    inner.state.pool = pool
    inner.state.fleet = fleet
    app = ProxyRouter(AuthGate(inner, secret), cfg, pool, secret=secret)
    app.fleet = fleet  # reachable from tests and __main__ regardless of wrapping
    app.cfg = cfg
    app.pool = pool
    app.secret = secret
    return app
