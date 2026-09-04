"""SSH connection pool.

One multiplexed connection per host, created lazily and reconnected on
demand. Authentication, host keys, proxies etc. all come from the normal
OpenSSH machinery: asyncssh reads ~/.ssh/config, ~/.ssh/known_hosts and
talks to the ssh-agent (SSH_AUTH_SOCK), so anything you can reach with
plain `ssh <host>` works here too.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import asyncssh

log = logging.getLogger("herdrhq.pool")


@dataclass
class Forward:
    """A local port forward: 127.0.0.1:<local> -> <host> ssh -> remote_host:remote_port."""

    id: str
    host: str
    remote_host: str
    remote_port: int
    listener: asyncssh.SSHListener | None = None
    desired_local: int = 0  # try to rebind the same local port after a reconnect
    internal: bool = False  # backing an HTTP service; hidden from the tunnels UI
    declared: bool = False  # comes from herdr-hq.yaml; re-ensured periodically

    @property
    def local_port(self) -> int | None:
        try:
            return self.listener.get_port() if self.listener else None
        except Exception:
            return None


@dataclass
class HostState:
    name: str
    conn: asyncssh.SSHClientConnection
    sftp: asyncssh.SFTPClient | None = None
    forwards: dict[str, Forward] = field(default_factory=dict)
    closed: bool = False
    watcher: asyncio.Task | None = None


class SSHPool:
    def __init__(self) -> None:
        self._states: dict[str, HostState] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, host: str) -> asyncio.Lock:
        return self._locks.setdefault(host, asyncio.Lock())

    async def get(self, host: str) -> HostState:
        """Return a live HostState, connecting or reconnecting if needed."""
        async with self._lock(host):
            st = self._states.get(host)
            if st is not None and not st.closed:
                return st
            return await self._connect(host, previous=st)

    async def _connect(self, host: str, previous: HostState | None) -> HostState:
        log.info("connecting to %s ...", host)
        conn = await asyncio.wait_for(
            asyncssh.connect(
                host,
                keepalive_interval=30,
                keepalive_count_max=3,
            ),
            timeout=20,
        )
        st = HostState(name=host, conn=conn)
        st.sftp = await conn.start_sftp_client()

        async def _watch() -> None:
            await conn.wait_closed()
            st.closed = True
            log.warning("connection to %s closed", host)

        st.watcher = asyncio.create_task(_watch())
        self._states[host] = st
        log.info("connected to %s", host)

        # Re-establish forwards that existed on the previous connection.
        if previous is not None:
            for f in list(previous.forwards.values()):
                try:
                    await self._open_forward(st, f)
                    log.info(
                        "restored forward %s on 127.0.0.1:%s", f.id, f.local_port
                    )
                except Exception as e:  # noqa: BLE001 - best effort restore
                    log.warning("could not restore forward %s: %s", f.id, e)
        return st

    async def _open_forward(self, st: HostState, f: Forward) -> Forward:
        want = f.desired_local or 0
        try:
            f.listener = await st.conn.forward_local_port(
                "127.0.0.1", want, f.remote_host, f.remote_port
            )
        except OSError:
            # Preferred local port taken; let the OS pick one.
            f.listener = await st.conn.forward_local_port(
                "127.0.0.1", 0, f.remote_host, f.remote_port
            )
        f.desired_local = f.listener.get_port()
        st.forwards[f.id] = f
        return f

    async def add_forward(
        self,
        host: str,
        remote_port: int,
        remote_host: str = "localhost",
        desired_local: int = 0,
        fid: str | None = None,
        internal: bool = False,
        declared: bool = False,
    ) -> Forward:
        st = await self.get(host)
        fid = fid or f"{host}:{remote_host}:{remote_port}"
        existing = st.forwards.get(fid)
        if existing is not None and existing.listener is not None:
            return existing
        f = Forward(
            id=fid,
            host=host,
            remote_host=remote_host,
            remote_port=remote_port,
            desired_local=desired_local,
            internal=internal,
            declared=declared,
        )
        return await self._open_forward(st, f)

    def peek_state(self, host: str) -> HostState | None:
        """Current state without connecting."""
        return self._states.get(host)

    async def remove_forward(self, host: str, fid: str) -> bool:
        st = self._states.get(host)
        if st is None:
            return False
        f = st.forwards.pop(fid, None)
        if f is None:
            return False
        if f.listener is not None:
            f.listener.close()
        return True

    def all_forwards(self) -> list[tuple[Forward, bool]]:
        """All registered forwards as (forward, up) pairs."""
        out: list[tuple[Forward, bool]] = []
        for st in self._states.values():
            for f in st.forwards.values():
                out.append((f, not st.closed))
        return out

    def hosts(self) -> dict[str, bool]:
        """Hosts we have talked to, mapped to whether the connection is up."""
        return {name: not st.closed for name, st in self._states.items()}

    async def close(self) -> None:
        for st in self._states.values():
            try:
                st.conn.close()
            except Exception:  # noqa: BLE001
                pass
