"""Cookie/token authentication for the UI and API.

Loopback TCP ports have no unix permissions: without auth, every local
user (and, via DNS rebinding, hostile web pages) could reach the API —
which mirrors terminals and can type into them. So every run is
protected by a secret: `listen.password` from herdr-hq.yaml if set
(survives restarts, so browser cookies stay valid), otherwise a fresh
random token printed with the startup URL. Set `listen.auth: none` to
opt out entirely.

Requests authenticate with the herdrhq_auth cookie (set on first visit
via ?token=... or the login form), `Authorization: Bearer <secret>`
(for curl), or a stateless ?token=... on /api/ calls.

Raw TCP tunnels bind their own ports and cannot carry HTTP auth; they
remain reachable by any local process.
"""

from __future__ import annotations

import hmac
import html
from http.cookies import SimpleCookie
from urllib.parse import parse_qsl, urlencode

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE = "herdrhq_auth"
COOKIE_MAX_AGE = 30 * 24 * 3600

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>herdr HQ · sign in</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
  :root {{ --plane: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b;
           --border: rgba(11, 11, 11, 0.10); --accent: #2a78d6; --serious: #d03b3b; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --plane: #0d0d0d; --surface: #1a1a19; --ink: #ffffff;
             --border: rgba(255, 255, 255, 0.10); --accent: #3987e5; --serious: #d03b3b; }}
  }}
  body {{ display: flex; align-items: center; justify-content: center; height: 100vh;
         margin: 0; background: var(--plane); color: var(--ink);
         font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }}
  form {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
          padding: 26px 28px; display: flex; flex-direction: column; gap: 12px; }}
  .wordmark {{ font-size: 18px; font-weight: 700; }} .wordmark .hq {{ color: var(--accent); }}
  input, button {{ font: inherit; font-size: 13px; padding: 6px 10px; color: inherit;
                   background: var(--plane); border: 1px solid var(--border); border-radius: 6px; }}
  button {{ background: var(--accent); color: #fff; border-color: var(--accent); cursor: pointer; }}
  .err {{ color: var(--serious); font-size: 12px; }}
</style></head><body>
<form method="post" action="/auth">
  <span class="wordmark">herdr <span class="hq">HQ</span></span>
  {err}
  <input type="password" name="token" placeholder="password / token" autofocus>
  <input type="hidden" name="next" value="{next}">
  <button type="submit">sign in</button>
</form></body></html>"""


def cookie_secret(scope) -> str | None:
    for k, v in scope.get("headers") or []:
        if k == b"cookie":
            try:
                jar = SimpleCookie()
                jar.load(v.decode())
                m = jar.get(COOKIE)
                return m.value if m else None
            except Exception:  # noqa: BLE001 - malformed cookie header
                return None
    return None


def matches(secret: str | None, supplied: str | None) -> bool:
    return bool(secret) and bool(supplied) and hmac.compare_digest(secret, supplied)


def set_cookie(response, secret: str) -> None:
    response.set_cookie(
        COOKIE, secret, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax"
    )


def _login(scope, status: int = 401, err: str = "") -> HTMLResponse:
    nxt = scope["path"]
    if scope.get("query_string"):
        nxt += "?" + scope["query_string"].decode()
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    body = LOGIN_PAGE.format(
        next=html.escape(nxt, quote=True),
        err=f'<span class="err">{html.escape(err)}</span>' if err else "",
    )
    return HTMLResponse(body, status_code=status)


class AuthGate:
    """ASGI middleware protecting everything served by the inner app."""

    def __init__(self, app, secret: str | None) -> None:
        self.app = app
        self.secret = secret

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not self.secret:
            return await self.app(scope, receive, send)
        if scope["path"] == "/favicon.svg":  # public: shown on the login page
            return await self.app(scope, receive, send)
        if matches(self.secret, cookie_secret(scope)):
            return await self.app(scope, receive, send)

        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            return await send({"type": "websocket.close", "code": 4401})

        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer ") and matches(self.secret, auth[7:]):
            return await self.app(scope, receive, send)

        query = parse_qsl(scope.get("query_string", b"").decode())
        qtok = dict(query).get("token")
        if matches(self.secret, qtok):
            if scope["path"].startswith("/api/"):
                return await self.app(scope, receive, send)  # stateless
            # Page visit with ?token=...: set the cookie, clean the URL.
            rest = urlencode([(k, v) for k, v in query if k != "token"])
            resp = RedirectResponse(
                scope["path"] + (f"?{rest}" if rest else ""), status_code=303
            )
            set_cookie(resp, self.secret)
            return await resp(scope, receive, send)

        if scope["method"] == "POST" and scope["path"] == "/auth":
            # Tiny urlencoded body; parsed by hand to avoid python-multipart.
            body = b""
            while True:
                msg = await receive()
                body += msg.get("body", b"")
                if not msg.get("more_body"):
                    break
            form = dict(parse_qsl(body.decode()))
            nxt = str(form.get("next") or "/")
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = "/"
            if matches(self.secret, str(form.get("token") or "")):
                resp = RedirectResponse(nxt, status_code=303)
                set_cookie(resp, self.secret)
            else:
                resp = _login(scope, err="wrong password or token")
            return await resp(scope, receive, send)

        wants_html = "text/html" in headers.get("accept", "")
        if scope["method"] == "GET" and wants_html:
            resp = _login(scope)
        else:
            resp = JSONResponse({"error": "authentication required"}, status_code=401)
        await resp(scope, receive, send)
