"""One attach.py process mirroring one herdr pane, fanned out to SSE viewers.

The attach script speaks newline-delimited JSON on both streams: screen frames
and errors come out of stdout; `{"t": "input", "ops": [...]}` lines go into
stdin (which stays open thanks to the bootstrap trick in transport.py).
"""

from __future__ import annotations

import asyncio
import json
import time

import asyncssh

from .transport import Proc


class TerminalSession:
    def __init__(self, host_name: str, pane_id: str, proc: Proc):
        self.host_name = host_name
        self.pane_id = pane_id
        self.key = (host_name, pane_id)
        self.proc = proc
        self.subscribers: set[asyncio.Queue] = set()
        self.latest: dict | None = None
        self.error: str | None = None
        self.empty_since: float | None = time.time()
        self.closed = False
        self._pump_task: asyncio.Task | None = None

    @classmethod
    async def open(
        cls, host_name: str, transport, pane_id: str, source: str, interval: float,
    ) -> "TerminalSession":
        proc = await transport.spawn(
            source, ["--pane", pane_id, "--interval", str(interval)], keep_stdin=True,
        )
        session = cls(host_name, pane_id, proc)
        session._pump_task = asyncio.create_task(
            session._pump(), name=f"term-{host_name}-{pane_id}",
        )
        return session

    async def _pump(self) -> None:
        try:
            while True:
                raw = await self.proc.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("t") == "screen":
                    self.latest = msg
                    self.error = None  # a good frame supersedes any earlier complaint
                elif msg.get("t") == "error":
                    self.error = msg.get("message")
                self.broadcast(msg)
        except (OSError, ValueError, asyncssh.Error):
            pass
        finally:
            detail = ""
            if not self.closed:  # closed means close() cancelled us on purpose
                try:
                    detail = (await self.proc.read_stderr()).decode("utf-8", "replace").strip()[:300]
                except asyncio.CancelledError:
                    pass
            self.broadcast({"t": "closed", "message": detail or "mirror ended"})
            self.closed = True

    def broadcast(self, msg: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.subscribers.add(q)
        self.empty_since = None
        if self.latest:
            q.put_nowait(self.latest)
        if self.error:
            q.put_nowait({"t": "error", "message": self.error})
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)
        if not self.subscribers:
            self.empty_since = time.time()

    async def send_input(self, ops: list) -> None:
        if self.closed or not ops:
            return
        try:
            await self.proc.send((json.dumps({"t": "input", "ops": ops}) + "\n").encode())
        except (OSError, ValueError, asyncssh.Error) as exc:
            raise RuntimeError(f"terminal not writable: {exc}") from exc

    def close(self) -> None:
        self.closed = True
        if self._pump_task:
            self._pump_task.cancel()
        self.proc.close()
