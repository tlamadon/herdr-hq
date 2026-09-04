"""Running the single-file stdlib scripts on a host, locally or over ssh.

Every remote helper (collector, attach, eventsub) is delivered as source text
on stdin of a bare python interpreter — nothing is ever installed on the
target machine. Two delivery modes:

* one-shot (`run`): `python3 - <args>` reads the whole of stdin as the
  program, so we write the source and close stdin. Used by the collector and
  the event bridge, which never read stdin themselves.

* interactive (`spawn`): `python -` eats all of stdin as the program, which
  leaves no channel for keystrokes. So the process is started as
  `python3 -c '<BOOTSTRAP>'` and handed a length-prefixed script instead:
  whatever follows those bytes on stdin still belongs to the script. Used by
  attach.py, whose stdin carries terminal input for the mirrored pane.
"""

from __future__ import annotations

import asyncio
import shlex
import sys

import asyncssh

from .pool import SSHPool

BOOTSTRAP = (
    "import sys;n=int(sys.stdin.buffer.readline());"
    "exec(compile(sys.stdin.buffer.read(n),'herdr-hq','exec'))"
)


class Proc:
    """A running script, local or remote, with byte streams either way."""

    def __init__(self, stdin, stdout, stderr, terminate):
        self.stdin = stdin  # has .write(bytes) and async .drain()
        self.stdout = stdout  # has async .readline() -> bytes
        self.stderr = stderr
        self._terminate = terminate

    async def send(self, data: bytes) -> None:
        self.stdin.write(data)
        await self.stdin.drain()

    async def send_eof(self) -> None:
        self.stdin.write_eof()
        await self.stdin.drain()

    async def readline(self) -> bytes:
        line = await self.stdout.readline()
        return line if isinstance(line, bytes) else line.encode()

    async def read_stderr(self, limit: int = 4096) -> bytes:
        try:
            data = await asyncio.wait_for(self.stderr.read(limit), 2)
            return data if isinstance(data, bytes) else data.encode()
        except (asyncio.TimeoutError, OSError, asyncssh.Error):
            return b""

    def close(self) -> None:
        try:
            self._terminate()
        except (OSError, ProcessLookupError):
            pass


class LocalTransport:
    """Runs scripts on this machine with asyncio subprocesses."""

    def __init__(self, python: str | None = None):
        self.python = python or sys.executable

    async def run(self, source: str, args: list[str], timeout: float) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            self.python, "-", *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(source.encode()), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def spawn(self, source: str, args: list[str], keep_stdin: bool = True) -> Proc:
        entry = ["-c", BOOTSTRAP] if keep_stdin else ["-"]
        proc = await asyncio.create_subprocess_exec(
            self.python, *entry, *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        p = Proc(proc.stdin, proc.stdout, proc.stderr, proc.terminate)
        await _handshake(p, source, keep_stdin)
        return p


class SSHTransport:
    """Runs scripts on a remote host through the shared connection pool."""

    def __init__(self, pool: SSHPool, target: str, python: str | None = None):
        self.pool = pool
        self.target = target
        self.python = python or "python3"

    async def run(self, source: str, args: list[str], timeout: float) -> tuple[int, str, str]:
        st = await asyncio.wait_for(self.pool.get(self.target), timeout)
        cmd = f"{self.python} - {shlex.join(args)}" if args else f"{self.python} -"
        result = await asyncio.wait_for(st.conn.run(cmd, input=source), timeout)
        return result.exit_status or 0, result.stdout or "", result.stderr or ""

    async def spawn(self, source: str, args: list[str], keep_stdin: bool = True) -> Proc:
        st = await self.pool.get(self.target)
        # ssh joins its command words and the remote shell re-splits them, so
        # the bootstrap has to survive one round of shell quoting
        entry = f"-c {shlex.quote(BOOTSTRAP)}" if keep_stdin else "-"
        cmd = f"{self.python} {entry} {shlex.join(args)}".rstrip()
        proc = await st.conn.create_process(cmd, encoding=None)
        p = Proc(proc.stdin, proc.stdout, proc.stderr, proc.terminate)
        await _handshake(p, source, keep_stdin)
        return p


async def _handshake(p: Proc, source: str, keep_stdin: bool) -> None:
    blob = source.encode()
    if keep_stdin:
        # hand the script to the bootstrap: byte count, then the source itself
        await p.send(f"{len(blob)}\n".encode() + blob)
    else:
        await p.send(blob)
        await p.send_eof()


def make_transport(pool: SSHPool, transport: str, target: str, python: str | None):
    if transport == "local":
        return LocalTransport(python)
    return SSHTransport(pool, target, python)
