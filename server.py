#!/usr/bin/env python3
"""herdr HQ — a small web dashboard for herdr agents across several machines.

Polls each configured host (locally or over ssh) by piping `collector.py` to a
remote python interpreter, then serves the merged state as JSON plus a static
dashboard. Standard library only.

    ./server.py                 # uses ./config.json, serves on 127.0.0.1:8787
    ./server.py --port 9000 --config other.json
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.join(ROOT, "collector.py")
ATTACH = os.path.join(ROOT, "attach.py")
STATIC = os.path.join(ROOT, "static")

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8787,
    "poll_interval": 6.0,
    "sample_interval": 0.7,
    "ssh_timeout": 30.0,
    "history": 120,
    "terminal_enabled": True,
    "terminal_input": True,
    "terminal_interval": 0.25,
    "terminal_max_sessions": 6,
    "hosts": [{"name": "localhost", "transport": "local"}],
}

# a mirror with no viewers is torn down after this long
SESSION_GRACE = 20.0

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# polling
# --------------------------------------------------------------------------


# `python -` eats all of stdin as the program, which leaves no channel for
# keystrokes. This reads a length-prefixed script instead, so whatever follows on
# stdin still belongs to the script. sys.argv[1:] is the args after -c, as usual.
BOOTSTRAP = (
    "import sys;n=int(sys.stdin.buffer.readline());"
    "exec(compile(sys.stdin.buffer.read(n),'herdr-hq','exec'))"
)


def host_command(spec: dict, args: list[str], keep_stdin: bool = False) -> list[str]:
    """How to run a stdlib script on `spec` — locally, or piped to a remote python."""
    local = spec.get("transport", "ssh" if spec.get("target") else "local") == "local"
    if local:
        python = spec.get("python", sys.executable)
        entry = ["-c", BOOTSTRAP] if keep_stdin else ["-"]
        return [python, *entry, *args]

    ctrl = spec.get("control_path", "/tmp/herdr-hq-%C")
    ssh = [
        spec.get("ssh", "ssh"),
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={int(spec.get('connect_timeout', 8))}",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={ctrl}",
        "-o", "ControlPersist=180",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=2",
    ]
    ssh += list(spec.get("ssh_args", []))
    # ssh joins its command words and the remote shell re-splits them, so the
    # bootstrap has to survive one round of shell quoting
    entry = ["-c", shlex.quote(BOOTSTRAP)] if keep_stdin else ["-"]
    ssh += [spec["target"], spec.get("python", "python3"), *entry, *args]
    return ssh


class HostPoller(threading.Thread):
    """Polls one host on a loop, keeping the latest snapshot and a short history."""

    def __init__(self, spec: dict, cfg: dict, source: str):
        super().__init__(daemon=True, name=f"poll-{spec.get('name')}")
        self.spec = spec
        self.cfg = cfg
        self.source = source
        self.name_ = spec.get("name") or spec.get("target") or "host"
        self.transport = spec.get("transport", "ssh" if spec.get("target") else "local")
        self.target = spec.get("target")
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()

        depth = int(cfg.get("history", 120))
        self.hist_t: deque[float] = deque(maxlen=depth)
        self.hist_cpu: deque[float] = deque(maxlen=depth)
        self.hist_mem: deque[float] = deque(maxlen=depth)
        self.agent_hist: dict[str, deque[float]] = {}

        self.state: dict = {
            "name": self.name_,
            "transport": self.transport,
            "target": self.target,
            "status": "pending",
            "error": None,
            "latency_ms": None,
            "last_ok": None,
            "last_try": None,
            "data": None,
        }

    # -- command construction ------------------------------------------------

    def command(self) -> list[str]:
        return host_command(self.spec, ["--interval", str(self.cfg.get("sample_interval", 0.7))])

    # -- one poll ------------------------------------------------------------

    def poll_once(self) -> None:
        started = time.monotonic()
        cmd = self.command()
        try:
            proc = subprocess.run(
                cmd,
                input=self.source,
                capture_output=True,
                text=True,
                timeout=float(self.cfg.get("ssh_timeout", 30.0)),
            )
        except subprocess.TimeoutExpired:
            self.fail("timed out", started)
            return
        except OSError as exc:
            self.fail(str(exc), started)
            return

        out = proc.stdout.strip()
        if not out:
            self.fail(proc.stderr.strip()[:400] or f"no output (exit {proc.returncode})", started)
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
        with self.lock:
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

    def fail(self, error: str, started: float) -> None:
        with self.lock:
            self.state.update(
                {
                    "status": "error",
                    "error": error,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "last_try": time.time(),
                }
            )
        log(f"{self.name_}: {error}")

    # -- loop ----------------------------------------------------------------

    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - a poller must never die
                log(f"{self.name_}: unexpected {exc!r}")
            self.wake.wait(float(self.cfg.get("poll_interval", 6.0)))
            self.wake.clear()

    def snapshot(self) -> dict:
        with self.lock:
            out = dict(self.state)
            out["history"] = {
                "t": list(self.hist_t),
                "cpu": list(self.hist_cpu),
                "mem": list(self.hist_mem),
            }
            out["agent_history"] = {k: list(v) for k, v in self.agent_hist.items()}
            return out


class TerminalSession:
    """One `attach.py` process mirroring one pane, fanned out to SSE viewers."""

    def __init__(self, host_name: str, spec: dict, pane_id: str, source: str, interval: float):
        self.host_name = host_name
        self.pane_id = pane_id
        self.key = (host_name, pane_id)
        self.lock = threading.Lock()
        self.subscribers: set[queue.Queue] = set()
        self.latest: dict | None = None
        self.error: str | None = None
        self.empty_since: float | None = time.time()
        self.closed = False

        cmd = host_command(spec, ["--pane", pane_id, "--interval", str(interval)], keep_stdin=True)
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # hand the script to the bootstrap: byte count, then the source itself
        blob = source.encode()
        self.proc.stdin.write(f"{len(blob)}\n".encode() + blob)
        self.proc.stdin.flush()
        threading.Thread(target=self._pump, daemon=True, name=f"term-{host_name}-{pane_id}").start()

    def _pump(self) -> None:
        try:
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("t") == "screen":
                    with self.lock:
                        self.latest = msg
                        self.error = None  # a good frame supersedes any earlier complaint
                elif msg.get("t") == "error":
                    with self.lock:
                        self.error = msg.get("message")
                self.broadcast(msg)
        except (OSError, ValueError):
            pass
        finally:
            detail = ""
            try:
                detail = (self.proc.stderr.read() or b"").decode("utf-8", "replace").strip()[:300]
            except (OSError, ValueError):
                pass
            self.broadcast({"t": "closed", "message": detail or "mirror ended"})
            self.closed = True

    def broadcast(self, msg: dict) -> None:
        with self.lock:
            targets = list(self.subscribers)
        for q in targets:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self.lock:
            self.subscribers.add(q)
            self.empty_since = None
            if self.latest:
                q.put_nowait(self.latest)
            if self.error:
                q.put_nowait({"t": "error", "message": self.error})
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.subscribers.discard(q)
            if not self.subscribers:
                self.empty_since = time.time()

    def send_input(self, ops: list) -> None:
        if self.closed or not ops:
            return
        try:
            self.proc.stdin.write((json.dumps({"t": "input", "ops": ops}) + "\n").encode())
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"terminal not writable: {exc}") from exc

    def close(self) -> None:
        self.closed = True
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        try:
            self.proc.terminate()
        except OSError:
            pass


class Fleet:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        with open(COLLECTOR) as fh:
            source = fh.read()
        self.pollers = [HostPoller(spec, cfg, source) for spec in cfg["hosts"]]
        self.specs = {spec["name"]: spec for spec in cfg["hosts"]}
        with open(ATTACH) as fh:
            self.attach_source = fh.read()
        self.sessions: dict[tuple[str, str], TerminalSession] = {}
        self.session_lock = threading.Lock()

    def start(self) -> None:
        for poller in self.pollers:
            poller.start()
        threading.Thread(target=self._reap_sessions, daemon=True, name="term-reaper").start()

    # -- terminal mirrors ----------------------------------------------------

    def terminal(self, host_name: str, pane_id: str) -> TerminalSession:
        if not self.cfg.get("terminal_enabled", True):
            raise PermissionError("terminals are disabled in config.json")
        spec = self.specs.get(host_name)
        if not spec:
            raise KeyError(f"unknown host {host_name!r}")
        key = (host_name, pane_id)
        with self.session_lock:
            existing = self.sessions.get(key)
            if existing and not existing.closed:
                return existing
            limit = int(self.cfg.get("terminal_max_sessions", 6))
            live = [s for s in self.sessions.values() if not s.closed]
            if len(live) >= limit:
                raise RuntimeError(f"too many open terminals (limit {limit})")
            session = TerminalSession(
                host_name, spec, pane_id, self.attach_source,
                float(self.cfg.get("terminal_interval", 0.25)),
            )
            self.sessions[key] = session
            log(f"terminal opened: {host_name} {pane_id}")
            return session

    def close_terminal(self, host_name: str, pane_id: str) -> None:
        with self.session_lock:
            session = self.sessions.pop((host_name, pane_id), None)
        if session:
            session.close()
            log(f"terminal closed: {host_name} {pane_id}")

    def _reap_sessions(self) -> None:
        while True:
            time.sleep(5)
            now = time.time()
            with self.session_lock:
                stale = [
                    key for key, s in self.sessions.items()
                    if s.closed or (s.empty_since and now - s.empty_since > SESSION_GRACE)
                ]
                doomed = [self.sessions.pop(key) for key in stale]
            for key, session in zip(stale, doomed):
                session.close()
                log(f"terminal reaped: {key[0]} {key[1]}")

    def refresh(self) -> None:
        for poller in self.pollers:
            poller.wake.set()

    def state(self) -> dict:
        return {
            "generated_at": time.time(),
            "poll_interval": self.cfg.get("poll_interval", 6.0),
            "terminal": {
                "enabled": bool(self.cfg.get("terminal_enabled", True)),
                "input": bool(self.cfg.get("terminal_input", True)),
            },
            "hosts": [p.snapshot() for p in self.pollers],
        }


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    fleet: Fleet = None  # type: ignore[assignment]
    server_version = "herdr-hq"

    def log_message(self, fmt, *args):  # quieter than the default
        return

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, relpath: str) -> None:
        safe = posixpath.normpath("/" + relpath).lstrip("/")
        path = os.path.abspath(os.path.join(STATIC, safe))
        if not path.startswith(STATIC + os.sep) or not os.path.isfile(path):
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        ctype = MIME.get(os.path.splitext(path)[1], "application/octet-stream")
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError):
            return {}

    def stream_terminal(self, host_name: str, pane_id: str) -> None:
        try:
            session = self.fleet.terminal(host_name, pane_id)
        except (PermissionError, KeyError, RuntimeError, OSError) as exc:
            self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        q = session.subscribe()
        try:
            self.wfile.write(b": open\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # keep the connection alive
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                self.wfile.flush()
                if msg.get("t") == "closed":
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            session.unsubscribe(q)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/state":
            self.send_json(self.fleet.state())
        elif path == "/api/term/stream":
            params = parse_qs(parsed.query)
            host_name = (params.get("host") or [""])[0]
            pane_id = (params.get("pane") or [""])[0]
            if not host_name or not pane_id:
                self.send_json({"error": "host and pane are required"}, status=HTTPStatus.BAD_REQUEST)
            else:
                self.stream_terminal(host_name, pane_id)
        elif path in ("/", "/index.html"):
            self.send_file("index.html")
        elif path.startswith("/static/"):
            self.send_file(path[len("/static/"):])
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        if path == "/api/refresh":
            self.fleet.refresh()
            self.send_json({"ok": True})
            return
        if path == "/api/term/input":
            body = self.read_body()
            if not self.fleet.cfg.get("terminal_input", True):
                self.send_json({"error": "terminal input is disabled in config.json"},
                               status=HTTPStatus.FORBIDDEN)
                return
            ops = body.get("ops")
            if not isinstance(ops, list):
                self.send_json({"error": "ops must be a list"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                session = self.fleet.terminal(body.get("host", ""), body.get("pane", ""))
                session.send_input(ops)
            except (PermissionError, KeyError, RuntimeError, OSError) as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"ok": True})
            return
        if path == "/api/term/close":
            body = self.read_body()
            self.fleet.close_terminal(body.get("host", ""), body.get("pane", ""))
            self.send_json({"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "not found")


# --------------------------------------------------------------------------
# config + entry point
# --------------------------------------------------------------------------


def load_config(path: str) -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path) as fh:
            cfg.update(json.load(fh))
    else:
        example = os.path.join(ROOT, "config.example.json")
        if os.path.exists(example):
            with open(example) as fh:
                cfg.update(json.load(fh))
            try:
                shutil.copyfile(example, path)
                log(f"created {path} from config.example.json — edit it to add your machines")
            except OSError as exc:
                # read-only install (a container, say): run from the example as-is
                log(f"running from config.example.json — cannot write {path}: {exc}")
    if not cfg.get("hosts"):
        cfg["hosts"] = list(DEFAULTS["hosts"])
    seen = set()
    for i, spec in enumerate(cfg["hosts"]):
        name = spec.get("name") or spec.get("target") or f"host{i}"
        while name in seen:
            name += "'"
        spec["name"] = name
        seen.add(name)
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--host", default=None, help="bind address (default from config)")
    ap.add_argument("--port", type=int, default=None, help="bind port (default from config)")
    ap.add_argument("--once", action="store_true", help="poll every host once, print JSON, exit")
    args = ap.parse_args()

    cfg = load_config(args.config)
    fleet = Fleet(cfg)

    if args.once:
        threads = [threading.Thread(target=p.poll_once) for p in fleet.pollers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        json.dump(fleet.state(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    fleet.start()
    Handler.fleet = fleet
    bind = args.host or cfg.get("host", "127.0.0.1")
    port = args.port or int(cfg.get("port", 8787))
    httpd = ThreadingHTTPServer((bind, port), Handler)
    hosts = ", ".join(p.name_ for p in fleet.pollers)
    log(f"watching {len(fleet.pollers)} host(s): {hosts}")
    log(f"serving http://{bind}:{port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
