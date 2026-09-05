"""One attach.py process mirroring one herdr pane, fanned out to SSE viewers.

The attach script speaks newline-delimited JSON on both streams: terminal
frames (raw ANSI, full repaints + diffs) and errors come out of stdout;
`{"t": "input", "ops": [...]}` lines go into stdin (which stays open thanks to
the bootstrap trick in transport.py). A late-joining viewer is replayed the
last full frame plus the diffs since, so it starts from the correct screen.
"""

from __future__ import annotations

import asyncio
import json
import time

import asyncssh

from .transport import Proc


class TerminalSession:
    def __init__(self, host_name: str, pane_id: str, proc: Proc, cols: int, rows: int):
        self.host_name = host_name
        self.pane_id = pane_id
        self.cols = cols
        self.rows = rows
        self.key = (host_name, pane_id)
        self.proc = proc
        self.subscribers: set[asyncio.Queue] = set()
        self.full_frame: dict | None = None   # last full repaint (for late joiners)
        self.diffs: list[dict] = []           # diffs since that full frame
        self.error: str | None = None
        self.empty_since: float | None = time.time()
        self.closed = False
        self._pump_task: asyncio.Task | None = None

    @classmethod
    async def open(
        cls, host_name: str, transport, pane_id: str, source: str,
        cols: int = 200, rows: int = 50,
    ) -> "TerminalSession":
        proc = await transport.spawn(
            source, ["--pane", pane_id, "--cols", str(cols), "--rows", str(rows)],
            keep_stdin=True,
        )
        session = cls(host_name, pane_id, proc, cols, rows)
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
                t = msg.get("t")
                if t == "frame":
                    self.error = None  # a good frame supersedes any earlier complaint
                    if msg.get("full"):
                        self.full_frame = msg
                        self.diffs = []
                        if msg.get("cols"):
                            self.cols, self.rows = msg["cols"], msg["rows"]
                    elif self.full_frame is not None:
                        self.diffs.append(msg)
                elif t == "closed":
                    self.error = msg.get("message")
                elif t == "error":
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
        # frames can be large and bursty; a deep queue lets a briefly-busy
        # browser catch up without dropping diffs (a drop desyncs until the
        # next full frame)
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.subscribers.add(q)
        self.empty_since = None
        # replay the current screen so a late joiner starts correct
        if self.full_frame is not None:
            q.put_nowait(self.full_frame)
            for diff in self.diffs:
                try:
                    q.put_nowait(diff)
                except asyncio.QueueFull:
                    break
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

    async def send_scroll(self, direction: str, lines: int) -> None:
        """Scroll the pane's own viewport (its real scrollback) via the control
        stream; the browser keeps no local scroll log, so this is the history."""
        if self.closed:
            return
        msg = {"t": "scroll", "dir": "up" if direction == "up" else "down", "lines": lines}
        try:
            await self.proc.send((json.dumps(msg) + "\n").encode())
        except (OSError, ValueError, asyncssh.Error) as exc:
            raise RuntimeError(f"terminal not writable: {exc}") from exc

    def close(self) -> None:
        self.closed = True
        if self._pump_task:
            self._pump_task.cancel()
        self.proc.close()
