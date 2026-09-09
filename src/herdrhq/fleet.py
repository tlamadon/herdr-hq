"""Fleet polling: one task per host runs the collector and keeps history.

Ported from the original threaded server.py; the collector contract and the
/api/state shape are unchanged. Everything lives on one event loop, so the
per-poller locks are gone.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from importlib import resources

import asyncssh

from .config import Config, HostSpec
from .pool import SSHPool
from .term import TerminalSession
from .transport import make_transport

log = logging.getLogger("herdrhq.fleet")

# a mirror with no viewers is torn down after this long
SESSION_GRACE = 20.0


def _script(name: str) -> str:
    return (resources.files("herdrhq.remote") / name).read_text()


class HostPoller:
    """Polls one host on a loop, keeping the latest snapshot and a short history."""

    def __init__(self, spec: HostSpec, cfg: Config, source: str, pool: SSHPool,
                 usage_source: str = ""):
        self.spec = spec
        self.cfg = cfg
        self.source = source
        self.usage_source = usage_source
        self.name = spec.name
        self.transport = make_transport(pool, spec.transport, spec.ssh_target, spec.python)
        self.wake = asyncio.Event()
        self.usage_wake = asyncio.Event()
        self.stopping = asyncio.Event()
        self.events_status: str | None = None  # set by the event bridge
        self.hub = None  # EventHub when push is enabled; polls announce themselves
        self.enricher = None  # async callable stapling transcript summaries on

        depth = cfg.history
        self.hist_t: deque[float] = deque(maxlen=depth)
        self.hist_cpu: deque[float] = deque(maxlen=depth)
        self.hist_mem: deque[float] = deque(maxlen=depth)
        self.agent_hist: dict[str, deque[float]] = {}

        self.state: dict = {
            "name": spec.name,
            "transport": spec.transport,
            "target": spec.target,
            "status": "pending",
            "error": None,
            "latency_ms": None,
            "last_ok": None,
            "last_try": None,
            "data": None,
        }

    # -- one poll ------------------------------------------------------------

    async def poll_once(self) -> None:
        started = time.monotonic()
        args = ["--interval", str(self.cfg.sample_interval)]
        try:
            rc, out, err = await self.transport.run(self.source, args, self.cfg.poll_timeout)
        except asyncio.TimeoutError:
            self.fail("timed out", started)
            return
        except (OSError, asyncssh.Error) as exc:
            self.fail(str(exc) or type(exc).__name__, started)
            return

        out = out.strip()
        if not out:
            self.fail(err.strip()[:400] or f"no output (exit {rc})", started)
            return
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            self.fail(f"unparseable output: {out[:200]}", started)
            return
        if data.get("fatal"):
            self.fail(str(data["fatal"]), started)
            return

        latency = int((time.monotonic() - started) * 1000)
        now = time.time()
        self.state.update(
            {
                "status": "ok",
                "error": None,
                "latency_ms": latency,
                "last_ok": now,
                "last_try": now,
                "data": data,
            }
        )
        machine = data.get("machine", {})
        self.hist_t.append(round(now, 1))
        self.hist_cpu.append(machine.get("cpu_pct", 0.0))
        self.hist_mem.append(machine.get("mem_pct", 0.0))

        live = set()
        for pane in data.get("panes", []):
            if not pane.get("is_agent"):
                continue
            key = pane["pane_id"]
            live.add(key)
            buf = self.agent_hist.setdefault(key, deque(maxlen=self.hist_t.maxlen))
            buf.append(pane.get("usage", {}).get("cpu_pct", 0.0))
        for gone in set(self.agent_hist) - live:
            del self.agent_hist[gone]

        if self.enricher is not None:
            try:
                await self.enricher(self)
            except Exception as exc:  # noqa: BLE001 - enrichment never fails a poll
                log.debug("%s: enrich failed: %s", self.name, exc)

        if self.hub is not None:
            self.hub.publish({"type": "host", "host": self.name})

    def fail(self, error: str, started: float) -> None:
        self.state.update(
            {
                "status": "error",
                "error": error,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "last_try": time.time(),
            }
        )
        log.warning("%s: %s", self.name, error)

    # -- claude usage limits -------------------------------------------------

    async def usage_once(self) -> None:
        """Probe the host's Claude account limits; failures land in the payload."""
        try:
            rc, out, err = await self.transport.run(
                self.usage_source, [], self.cfg.usage_timeout,
            )
            out = out.strip()
            if not out:
                data = {"error": err.strip()[:400] or f"no output (exit {rc})"}
            else:
                data = json.loads(out)
                if data.get("fatal"):
                    data = {"error": str(data["fatal"])}
        except asyncio.TimeoutError:
            data = {"error": "timed out"}
        except (OSError, asyncssh.Error, json.JSONDecodeError) as exc:
            data = {"error": str(exc) or type(exc).__name__}
        data["checked_at"] = time.time()
        self.state["agent_usage"] = data
        if data.get("error"):
            log.debug("%s: usage probe: %s", self.name, data["error"])
        if self.hub is not None:
            self.hub.publish({"type": "host", "host": self.name})

    async def run_usage(self) -> None:
        while not self.stopping.is_set():
            try:
                await self.usage_once()
            except Exception as exc:  # noqa: BLE001 - a poller must never die
                log.warning("%s: usage probe unexpected %r", self.name, exc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.usage_wake.wait(), self.cfg.usage_interval)
            self.usage_wake.clear()

    # -- loop ----------------------------------------------------------------

    def interval(self) -> float:
        if self.events_status == "live" and self.cfg.interval_when_live > 0:
            return self.cfg.interval_when_live
        return self.cfg.poll_interval

    async def run(self) -> None:
        while not self.stopping.is_set():
            try:
                await self.poll_once()
            except Exception as exc:  # noqa: BLE001 - a poller must never die
                log.warning("%s: unexpected %r", self.name, exc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), self.interval())
            self.wake.clear()

    def snapshot(self) -> dict:
        out = dict(self.state)
        if self.events_status is not None:
            out["events"] = self.events_status
        out["history"] = {
            "t": list(self.hist_t),
            "cpu": list(self.hist_cpu),
            "mem": list(self.hist_mem),
        }
        out["agent_history"] = {k: list(v) for k, v in self.agent_hist.items()}
        return out


class Fleet:
    def __init__(self, cfg: Config, pool: SSHPool):
        self.cfg = cfg
        self.pool = pool
        source = _script("collector.py")
        usage_source = _script("usage.py")
        self.attach_source = _script("attach.py")
        self.pollers = [
            HostPoller(spec, cfg, source, pool, usage_source=usage_source)
            for spec in cfg.hosts.values()
        ]
        self.by_name = {p.name: p for p in self.pollers}
        self.sessions: dict[tuple[str, str], TerminalSession] = {}
        self.session_lock = asyncio.Lock()
        self.tasks: list[asyncio.Task] = []

    def start(self) -> None:
        for poller in self.pollers:
            self.tasks.append(asyncio.create_task(poller.run(), name=f"poll-{poller.name}"))
            if self.cfg.usage_enabled:
                self.tasks.append(
                    asyncio.create_task(poller.run_usage(), name=f"usage-{poller.name}")
                )
        self.tasks.append(asyncio.create_task(self._reap_sessions(), name="term-reaper"))

    async def stop(self) -> None:
        for poller in self.pollers:
            poller.stopping.set()
        for task in self.tasks:
            task.cancel()
        for session in list(self.sessions.values()):
            session.close()
        self.sessions.clear()

    # -- terminal mirrors ----------------------------------------------------

    async def terminal(self, host_name: str, pane_id: str,
                       cols: int | None = None, rows: int | None = None) -> TerminalSession:
        if not self.cfg.terminal_enabled:
            raise PermissionError("terminals are disabled in the config")
        poller = self.by_name.get(host_name)
        if not poller:
            raise KeyError(f"unknown host {host_name!r}")
        key = (host_name, pane_id)
        async with self.session_lock:
            existing = self.sessions.get(key)
            if existing and not existing.closed:
                # input calls pass no size and always reuse; a viewer passing a
                # materially different size (a resize) needs a fresh stream,
                # since herdr renders the pane at the observed size
                if cols is None or (abs(existing.cols - cols) <= 1 and abs(existing.rows - rows) <= 1):
                    return existing
                existing.close()
                self.sessions.pop(key, None)
            limit = self.cfg.terminal_max_sessions
            live = [s for s in self.sessions.values() if not s.closed]
            if len(live) >= limit:
                raise RuntimeError(f"too many open terminals (limit {limit})")
            c, r = cols or 200, rows or 50
            session = await TerminalSession.open(
                host_name, poller.transport, pane_id, self.attach_source, c, r,
            )
            self.sessions[key] = session
            log.info("terminal opened: %s %s (%dx%d)", host_name, pane_id, c, r)
            return session

    def close_terminal(self, host_name: str, pane_id: str) -> None:
        session = self.sessions.pop((host_name, pane_id), None)
        if session:
            session.close()
            log.info("terminal closed: %s %s", host_name, pane_id)

    async def _reap_sessions(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = time.time()
            stale = [
                key for key, s in self.sessions.items()
                if s.closed or (s.empty_since and now - s.empty_since > SESSION_GRACE)
            ]
            for key in stale:
                session = self.sessions.pop(key)
                session.close()
                log.info("terminal reaped: %s %s", key[0], key[1])

    def refresh(self) -> None:
        for poller in self.pollers:
            poller.wake.set()
            poller.usage_wake.set()

    def state(self) -> dict:
        return {
            "generated_at": time.time(),
            "poll_interval": self.cfg.poll_interval,
            "terminal": {
                "enabled": self.cfg.terminal_enabled,
                "input": self.cfg.terminal_input,
            },
            "hosts": [p.snapshot() for p in self.pollers],
        }
