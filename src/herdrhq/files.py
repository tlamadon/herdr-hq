"""Remote file browsing, streaming and change-watching.

Ported from sshpeek. Configured ssh hosts use the pooled SFTP client; hosts
declared `transport: local` read this machine's filesystem directly (sshpeek
had no local mode). A host name that isn't configured at all is treated as an
ad-hoc ssh alias, sshpeek-style — type any ~/.ssh/config name into the UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import stat as statmod
import time
from pathlib import Path, PurePosixPath

import asyncssh
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .config import Config
from .pool import SSHPool

log = logging.getLogger("herdrhq.files")

CHUNK = 256 * 1024

# Extensions we serve as text/plain when mimetypes has no (browser-friendly)
# answer, so "peeking" at them renders in the browser instead of downloading.
# .md is deliberately absent: the viewer fetches it raw and renders it itself.
TEXT_EXT = {
    ".log", ".out", ".err", ".toml", ".nix", ".yml", ".yaml", ".jl", ".tex",
    ".bib", ".sty", ".cls", ".sh", ".zsh", ".conf", ".ini", ".service",
    ".gitignore", ".sbatch", ".slurm", ".env", ".lock", ".sql", ".r", ".R",
}


def guess_type(path: str, dl: bool) -> str:
    name = PurePosixPath(path).name
    suffix = PurePosixPath(path).suffix.lower()
    ctype = mimetypes.guess_type(name)[0]
    if not dl and (suffix in TEXT_EXT or (ctype is None and "." not in name)):
        return "text/plain; charset=utf-8"
    return ctype or "application/octet-stream"


def err(e: object, code: int = 502) -> JSONResponse:
    return JSONResponse({"error": str(e)}, status_code=code)


def _local_path(path: str) -> Path:
    """Match SFTP semantics: relative paths hang off the home directory."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else Path.home() / p


class LocalFs:
    """The `transport: local` backend: this machine's filesystem, thread-offloaded."""

    async def realpath(self, path: str) -> str:
        return str(await asyncio.to_thread(lambda: _local_path(path).resolve()))

    async def listdir(self, path: str) -> list[dict]:
        def scan():
            out = []
            with os.scandir(_local_path(path)) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=True)
                        is_dir = e.is_dir(follow_symlinks=True)
                    except OSError:
                        st, is_dir = None, False
                    out.append({
                        "name": e.name,
                        "dir": is_dir,
                        "size": st.st_size if st else None,
                        "mtime": int(st.st_mtime) if st else None,
                    })
            return out

        return await asyncio.to_thread(scan)

    async def stat(self, path: str) -> tuple[int | None, int | None]:
        st = await asyncio.to_thread(os.stat, _local_path(path))
        return int(st.st_mtime), st.st_size

    async def read_tail(self, path: str, n: int) -> bytes:
        def go():
            p = _local_path(path)
            size = p.stat().st_size
            with open(p, "rb") as fh:
                fh.seek(max(0, size - n))
                return fh.read(n)

        return await asyncio.to_thread(go)

    async def read_head(self, path: str, n: int) -> bytes:
        def go():
            with open(_local_path(path), "rb") as fh:
                return fh.read(n)

        return await asyncio.to_thread(go)

    async def open_stream(self, path: str):
        p = _local_path(path)
        st = await asyncio.to_thread(os.stat, p)
        fh = await asyncio.to_thread(open, p, "rb")

        async def body():
            try:
                while True:
                    chunk = await asyncio.to_thread(fh.read, CHUNK)
                    if not chunk:
                        break
                    yield chunk
            finally:
                with contextlib.suppress(OSError):
                    fh.close()

        return st.st_size, body()


class SftpFs:
    """SFTP through the shared pool, for ssh hosts (configured or ad-hoc)."""

    def __init__(self, pool: SSHPool, target: str):
        self.pool = pool
        self.target = target

    async def _sftp(self):
        st = await self.pool.get(self.target)
        return st.sftp

    async def realpath(self, path: str) -> str:
        sftp = await self._sftp()
        return await sftp.realpath(path)

    async def listdir(self, path: str) -> list[dict]:
        sftp = await self._sftp()
        entries = []
        for name in await sftp.readdir(path):
            if name.filename in (".", ".."):
                continue
            a = name.attrs
            is_dir = a.permissions is not None and statmod.S_ISDIR(a.permissions)
            entries.append({
                "name": name.filename,
                "dir": is_dir,
                "size": a.size,
                "mtime": a.mtime,
            })
        return entries

    async def stat(self, path: str) -> tuple[int | None, int | None]:
        sftp = await self._sftp()
        a = await sftp.stat(path)
        return a.mtime, a.size

    async def read_tail(self, path: str, n: int) -> bytes:
        sftp = await self._sftp()
        attrs = await sftp.stat(path)
        f = await sftp.open(path, "rb")
        try:
            if attrs.size and attrs.size > n:
                await f.seek(attrs.size - n)
            return await f.read(n)
        finally:
            with contextlib.suppress(Exception):
                await f.close()

    async def read_head(self, path: str, n: int) -> bytes:
        sftp = await self._sftp()
        f = await sftp.open(path, "rb")
        try:
            return await f.read(n)
        finally:
            with contextlib.suppress(Exception):
                await f.close()

    async def open_stream(self, path: str):
        sftp = await self._sftp()
        attrs = await sftp.stat(path)
        f = await sftp.open(path, "rb")

        async def body():
            try:
                while True:
                    chunk = await f.read(CHUNK)
                    if not chunk:
                        break
                    yield chunk
            except (OSError, asyncssh.Error) as e:  # connection died mid-stream
                log.warning("stream of %s aborted: %s", path, e)
            finally:
                with contextlib.suppress(Exception):
                    await f.close()

        return attrs.size, body()


FS_ERRORS = (OSError, asyncssh.Error, asyncio.TimeoutError)


class FileRoutes:
    """The /api/fs/* handlers plus the live-view registry."""

    def __init__(self, cfg: Config, pool: SSHPool):
        self.cfg = cfg
        self.pool = pool
        self.watchers: dict[int, dict] = {}
        self._watcher_seq = 0

    def backend(self, host: str) -> LocalFs | SftpFs:
        spec = self.cfg.hosts.get(host)
        if spec is not None and spec.transport == "local":
            return LocalFs()
        target = spec.ssh_target if spec is not None else host
        return SftpFs(self.pool, target)

    # ------------------------------------------------------------------ api

    async def hosts(self, request: Request) -> JSONResponse:
        live = self.pool.hosts()
        agents: dict[str, int] = {}
        fleet = getattr(request.app.state, "fleet", None)
        if fleet is not None:
            for poller in fleet.pollers:
                panes = (poller.state.get("data") or {}).get("panes") or []
                agents[poller.name] = sum(1 for p in panes if p.get("is_agent"))
        names = list(dict.fromkeys(list(self.cfg.hosts) + list(live)))
        out = []
        for n in names:
            spec = self.cfg.hosts.get(n)
            local = spec is not None and spec.transport == "local"
            out.append({
                "name": n,
                "kind": "local" if local else "ssh",
                "configured": spec is not None,
                "connected": True if local else live.get(spec.ssh_target if spec else n, False),
                "agents": agents.get(n, 0),
            })
        return JSONResponse(out)

    async def ls(self, request: Request) -> JSONResponse:
        host = request.query_params.get("host")
        path = request.query_params.get("path") or "."
        if not host:
            return err("missing ?host=", 400)
        fs = self.backend(host)
        try:
            cwd = await fs.realpath(path)
            entries = await fs.listdir(cwd)
        except FS_ERRORS as e:
            return err(e)
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return JSONResponse({"host": host, "path": cwd, "entries": entries})

    async def file(self, request: Request) -> Response:
        host = request.query_params.get("host")
        path = request.query_params.get("path")
        dl = bool(request.query_params.get("dl"))
        if not host or not path:
            return err("missing ?host= or ?path=", 400)
        fs = self.backend(host)
        try:
            size, body = await fs.open_stream(path)
        except FS_ERRORS as e:
            return err(e)
        headers = {}
        if size is not None:
            headers["Content-Length"] = str(size)
        if dl:
            headers["Content-Disposition"] = (
                f'attachment; filename="{PurePosixPath(path).name}"'
            )
        return StreamingResponse(body, media_type=guess_type(path, dl), headers=headers)

    async def watch(self, request: Request) -> Response:
        """SSE stream of change events for one file.

        A change is only emitted once (mtime, size) differs from the last
        emitted version AND is identical across two consecutive polls — the
        stable-size debounce that avoids firing while latexmk & co. are
        mid-write.
        """
        host = request.query_params.get("host")
        path = request.query_params.get("path")
        try:
            interval = min(max(float(request.query_params.get("interval", "2")), 0.5), 60)
        except ValueError:
            interval = 2.0
        if not host or not path:
            return err("missing ?host= or ?path=", 400)
        fs = self.backend(host)

        async def gen():
            self._watcher_seq += 1
            wid = self._watcher_seq
            close = asyncio.Event()
            self.watchers[wid] = {"id": wid, "host": host, "path": path,
                                  "started": time.time(), "close": close}
            try:
                last_emit: tuple | None = None
                prev: tuple | None = None
                first = True
                while True:
                    cur: tuple | None = None
                    try:
                        cur = await fs.stat(path)
                    except FS_ERRORS as e:
                        log.debug("stat %s:%s failed: %s", host, path, e)
                    if cur is not None and first:
                        first = False
                        last_emit = cur
                        yield f"data: {json.dumps({'type': 'init', 'mtime': cur[0], 'size': cur[1]})}\n\n"
                    elif cur is not None and cur == prev and cur != last_emit:
                        last_emit = cur
                        yield f"data: {json.dumps({'type': 'change', 'mtime': cur[0], 'size': cur[1]})}\n\n"
                    else:
                        # Comment line: ignored by EventSource, but forces a write
                        # so a vanished client cancels this generator promptly.
                        yield ": ping\n\n"
                    prev = cur
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(close.wait(), interval)
                    if close.is_set():
                        yield 'data: {"type": "bye"}\n\n'
                        return
            finally:
                self.watchers.pop(wid, None)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def views_get(self, request: Request) -> JSONResponse:
        return JSONResponse([
            {"id": w["id"], "host": w["host"], "path": w["path"], "started": w["started"]}
            for w in self.watchers.values()
        ])

    async def views_delete(self, request: Request) -> JSONResponse:
        w = self.watchers.get(request.path_params["vid"])
        if w is None:
            return err("no such live view", 404)
        w["close"].set()
        return JSONResponse({"ok": True})
