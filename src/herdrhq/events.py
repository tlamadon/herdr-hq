"""Push channel: herdr events bridged per host, fanned out to the browser.

One EventBridge task per host holds a remote eventsub.py process (delivered
over ssh like the collector). Cheap pane updates patch the poller's cached
state in place and go straight to the browser as SSE messages — status flips
land in under a second without an SSH round-trip. Structural events (panes or
workspaces appearing/disappearing) instead wake the poller for a full
re-poll, debounced so a burst costs one collector run.

Patches are overlay-only: the next full snapshot replaces them wholesale, so
the poll remains authoritative and there is no sequencing bookkeeping.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from importlib import resources

import asyncssh

from .fleet import HostPoller

log = logging.getLogger("herdrhq.events")

STRUCTURAL = {
    "pane_created", "pane_closed", "pane_exited", "pane_agent_detected",
    "tab_created", "tab_closed", "tab_renamed",
    "workspace_created", "workspace_closed", "workspace_renamed",
    "layout_updated",
}

DEBOUNCE = 2.0  # structural bursts collapse into one re-poll
READ_TIMEOUT = 75.0  # eventsub heartbeats every ~20s; silence this long is death
HEALTHY_AFTER = 60.0  # a stream this old resets the reconnect backoff


class EventHub:
    """Fan-out of push messages to /api/state/stream subscribers."""

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue] = set()

    def publish(self, msg: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # a slow browser drops updates, never blocks the bridge

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)


class EventBridge:
    """Holds one eventsub.py stream for one host, forever, with backoff."""

    def __init__(self, poller: HostPoller, hub: EventHub):
        self.poller = poller
        self.hub = hub
        self.source = (resources.files("herdrhq.remote") / "eventsub.py").read_text()
        self._wake_handle: asyncio.TimerHandle | None = None

    def set_status(self, status: str) -> None:
        if self.poller.events_status != status:
            self.poller.events_status = status
            self.hub.publish({"type": "events", "host": self.poller.name, "status": status})

    # -- event reactions -----------------------------------------------------

    def _debounced_wake(self) -> None:
        if self._wake_handle is not None:
            return
        loop = asyncio.get_running_loop()

        def fire() -> None:
            self._wake_handle = None
            self.poller.wake.set()

        self._wake_handle = loop.call_later(DEBOUNCE, fire)

    def handle(self, kind: str, data: dict) -> None:
        if kind == "pane_updated":
            pane = (data or {}).get("pane") or {}
            pane_id = pane.get("pane_id")
            cached = ((self.poller.state.get("data") or {}).get("panes")) or []
            target = next((p for p in cached if p.get("pane_id") == pane_id), None)
            if target is None:
                self._debounced_wake()  # a pane we have never polled: structural
                return
            patch = {}
            for src, dst in (
                ("agent_status", "agent_status"), ("agent", "agent"),
                ("cwd", "cwd"), ("focused", "focused"),
                ("terminal_title_stripped", "title"),
            ):
                if pane.get(src) is not None:
                    patch[dst] = pane[src]
            changed = {k: v for k, v in patch.items() if target.get(k) != v}
            target.update(patch)
            if changed:
                self.hub.publish({
                    "type": "pane",
                    "host": self.poller.name,
                    "pane_id": pane_id,
                    "at": time.time(),
                    **{k: target.get(k) for k in ("agent_status", "title", "agent", "focused")},
                })
        elif kind in STRUCTURAL:
            self._debounced_wake()
            self.hub.publish({"type": "structure", "host": self.poller.name})

    # -- the stream loop -----------------------------------------------------

    async def run(self) -> None:
        backoff = 1.0
        while True:
            self.set_status("connecting")
            started = time.monotonic()
            try:
                await self._stream_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the bridge must never die
                log.warning("%s: event bridge: %r", self.poller.name, exc)
            if time.monotonic() - started > HEALTHY_AFTER:
                backoff = 1.0
            else:
                backoff = min(30.0, backoff * 2)
            await asyncio.sleep(backoff)

    async def _stream_once(self) -> None:
        try:
            proc = await self.poller.transport.spawn(self.source, [], keep_stdin=False)
        except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
            self.set_status(f"error: {exc}" if str(exc) else "error: unreachable")
            return
        try:
            while True:
                raw = await asyncio.wait_for(proc.readline(), READ_TIMEOUT)
                if not raw:
                    self.set_status("error: stream closed")
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = msg.get("t")
                if t == "ready":
                    self.set_status("live")
                elif t == "idle":
                    pass
                elif t == "event":
                    self.handle(msg.get("event") or "", msg.get("data") or {})
                elif t == "error":
                    self.set_status(f"error: {msg.get('message')}")
                    return
        except asyncio.TimeoutError:
            self.set_status("error: stream went quiet")
        finally:
            with contextlib.suppress(Exception):
                proc.close()
