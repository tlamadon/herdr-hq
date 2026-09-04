"""Reverse proxy for remote web apps, declared or discovered.

Each HTTP service in herdr-hq.yaml gets a stable URL:

    http://<service>.<host>.localhost:<herdr-hq port>/

and any listening port the collector discovers on a known host can be proxied
ad hoc, no declaration needed:

    http://p8877.<host>.localhost:<herdr-hq port>/

Browsers resolve *.localhost to 127.0.0.1 natively, so no /etc/hosts edits.
Under the hood every service is backed by an (internal, ephemeral) SSH local
forward from the pool; the proxy just relays to 127.0.0.1:<forwarded port>.
For `transport: local` hosts the port is already on this machine's loopback
and the relay goes straight to it. Proxying to a real local port keeps things
simple and gives WebSockets and streaming for free -- no hand-rolled HTTP
over SSH channels.

Path rewriting is deliberately absent: because every service lives on its own
(sub)domain at path /, apps that generate absolute paths (Jupyter, Grafana,
...) work unmodified, and cookies/origins stay isolated per service.

Ported from sshpeek; the dynamic p<port> origins are new. A declared service
whose name matches p<digits> shadows the dynamic form.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import parse_qsl, urlencode

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, StreamingResponse
from starlette.websockets import WebSocket
from websockets.asyncio.client import connect as ws_connect

from . import auth
from .config import Config, HostSpec, HttpService
from .pool import SSHPool

log = logging.getLogger("herdrhq.proxy")

TOKEN_PARAM = "hq_token"

# Hop-by-hop headers are between us and each peer, never forwarded.
HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host",
}

DYNAMIC_RE = re.compile(r"^p(\d{1,5})$")


def service_fid(host: str, service: str) -> str:
    return f"http:{host}:{service}"


def match_service(cfg: Config, hostname: str) -> tuple[HostSpec, HttpService] | None:
    """Resolve a <service>.<host>.localhost hostname to its config entries."""
    if not hostname.endswith(".localhost"):
        return None
    labels = hostname[: -len(".localhost")]
    if "." not in labels:
        return None
    service, halias = labels.split(".", 1)
    hs = cfg.hosts.get(halias)
    if hs is None:
        return None
    for s in hs.http:
        if s.name == service:
            return hs, s
    m = DYNAMIC_RE.match(service)
    if m and 0 < int(m.group(1)) < 65536:
        # a discovered port on a known host: synthesize the service on the fly
        return hs, HttpService(name=service, remote_host="localhost",
                               remote_port=int(m.group(1)))
    return None


class ProxyRouter:
    """ASGI wrapper: routes *.localhost hosts to the proxy, everything else
    to the inner app."""

    def __init__(self, app, cfg: Config, pool: SSHPool, secret: str | None = None) -> None:
        self.app = app
        self.cfg = cfg
        self.pool = pool
        self.secret = secret
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=10), follow_redirects=False
        )

    # -- routing -----------------------------------------------------------

    @staticmethod
    def _hostname(scope) -> str:
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        host = (headers.get("host") or "").lower()
        if host.startswith("["):  # [::1]:8787
            return host.partition("]")[0] + "]"
        return host.split(":")[0]

    def _host_allowed(self, hostname: str) -> bool:
        """Reject foreign Host headers: the DNS-rebinding guard."""
        return (
            hostname in ("localhost", "127.0.0.1", "[::1]", "::1", self.cfg.listen_host)
            or hostname.endswith(".localhost")
        )

    def _match(self, scope) -> tuple[HostSpec, HttpService] | None:
        return match_service(self.cfg, self._hostname(scope))

    async def local_port(self, hs: HostSpec, s: HttpService) -> int:
        if hs.transport == "local":
            if s.remote_host in ("localhost", "127.0.0.1"):
                return s.remote_port  # already on this machine's loopback
            raise OSError(f"{hs.name} is local; cannot forward to {s.remote_host}")
        f = await self.pool.add_forward(
            hs.ssh_target, s.remote_port, s.remote_host,
            fid=service_fid(hs.name, s.name), internal=True,
            declared=not DYNAMIC_RE.match(s.name),
        )
        if f.local_port is None:
            raise OSError("forward has no bound port")
        return f.local_port

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            if not self._host_allowed(self._hostname(scope)):
                if scope["type"] == "websocket":
                    await receive()
                    return await send({"type": "websocket.close", "code": 4421})
                resp = PlainTextResponse(
                    "herdr-hq: unrecognized Host header", status_code=421
                )
                return await resp(scope, receive, send)
            m = self._match(scope)
            if m is not None:
                if self.secret and not auth.matches(self.secret, auth.cookie_secret(scope)):
                    return await self._proxy_auth(scope, receive, send)
                if scope["type"] == "http":
                    return await self._http(scope, receive, send, *m)
                return await self._ws(scope, receive, send, *m)
        await self.app(scope, receive, send)

    async def _proxy_auth(self, scope, receive, send):
        """Service origins are their own cookie jars: bless each one via the
        ?hq_token=... the UI appends to service links, then redirect to the
        clean URL so the app never sees the parameter. Browser visits without
        a token (typed or bookmarked URLs) bounce through /__bless on the
        main origin, which appends it -- after a login there if needed."""
        if scope["type"] == "websocket":
            await receive()
            return await send({"type": "websocket.close", "code": 4401})
        query = parse_qsl(scope.get("query_string", b"").decode())
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        if auth.matches(self.secret, dict(query).get(TOKEN_PARAM)):
            rest = urlencode([(k, v) for k, v in query if k != TOKEN_PARAM])
            resp = RedirectResponse(
                scope["path"] + (f"?{rest}" if rest else ""), status_code=303
            )
            auth.set_cookie(resp, self.secret)
        elif scope["method"] == "GET" and "text/html" in headers.get("accept", ""):
            rest = urlencode([(k, v) for k, v in query if k != TOKEN_PARAM])
            back = f"http://{headers.get('host', '')}{scope['path']}"
            back += f"?{rest}" if rest else ""
            resp = RedirectResponse(
                f"http://localhost:{self.cfg.listen_port}/__bless?"
                + urlencode({"to": back}),
                status_code=302,
            )
        else:
            resp = PlainTextResponse(
                "herdr-hq: authentication required -- open this service from the herdr HQ UI",
                status_code=401,
            )
        await resp(scope, receive, send)

    # -- http --------------------------------------------------------------

    async def _http(self, scope, receive, send, hs: HostSpec, s: HttpService):
        request = Request(scope, receive)
        try:
            port = await self.local_port(hs, s)
        except Exception as e:  # noqa: BLE001
            resp = PlainTextResponse(
                f"herdr-hq: cannot reach {s.name} on {hs.name}: {e}", status_code=502
            )
            return await resp(scope, receive, send)

        url = f"http://127.0.0.1:{port}{scope['path']}"
        if scope.get("query_string"):
            url += "?" + scope["query_string"].decode()
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP]
        req = self.client.build_request(
            request.method, url, headers=headers, content=request.stream()
        )
        try:
            r = await self.client.send(req, stream=True)
        except httpx.HTTPError as e:
            resp = PlainTextResponse(
                f"herdr-hq: {s.name} on {hs.name} not responding: {e}", status_code=502
            )
            return await resp(scope, receive, send)

        resp = StreamingResponse(
            r.aiter_raw(), status_code=r.status_code, background=BackgroundTask(r.aclose)
        )
        # Bypass Starlette's dict headers to preserve duplicates (Set-Cookie)
        # and pass raw bytes through untouched (Content-Encoding intact).
        resp.raw_headers = [
            (k.encode(), v.encode())
            for k, v in r.headers.multi_items()
            if k.lower() not in HOP
        ]
        await resp(scope, receive, send)

    # -- websocket ---------------------------------------------------------

    async def _ws(self, scope, receive, send, hs: HostSpec, s: HttpService):
        ws = WebSocket(scope, receive, send)
        try:
            port = await self.local_port(hs, s)
        except Exception as e:  # noqa: BLE001
            log.warning("ws: cannot reach %s on %s: %s", s.name, hs.name, e)
            await ws.close(code=1011)
            return

        url = f"ws://127.0.0.1:{port}{scope['path']}"
        if scope.get("query_string"):
            url += "?" + scope["query_string"].decode()
        fwd_headers = {}
        for k, v in scope.get("headers") or []:
            if k.decode().lower() in ("cookie", "authorization"):
                fwd_headers[k.decode()] = v.decode()

        try:
            upstream = await ws_connect(
                url,
                subprotocols=scope.get("subprotocols") or None,
                additional_headers=fwd_headers,
                max_size=None,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("ws upstream connect failed for %s: %s", url, e)
            await ws.close(code=1011)
            return

        await ws.accept(subprotocol=upstream.subprotocol)

        async def client_to_upstream():
            try:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    if msg.get("text") is not None:
                        await upstream.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await upstream.send(msg["bytes"])
            except Exception:  # noqa: BLE001
                return

        async def upstream_to_client():
            try:
                async for m in upstream:
                    if isinstance(m, str):
                        await ws.send_text(m)
                    else:
                        await ws.send_bytes(m)
            except Exception:  # noqa: BLE001
                return

        t1 = asyncio.create_task(client_to_upstream())
        t2 = asyncio.create_task(upstream_to_client())
        _, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        for closer in (upstream.close(), ws.close()):
            try:
                await closer
            except Exception:  # noqa: BLE001
                pass
