#!/usr/bin/env python3
"""Mirror one herdr pane over stdout, and forward keystrokes from stdin.

Runs on the machine that owns the pane; the dashboard pipes it there over ssh
exactly like collector.py.

Output uses herdr's own per-pane terminal stream: `herdr terminal session
observe <pane> --cols --rows` emits terminal frames (raw ANSI, full repaints +
diffs) on stdout as one JSON object per line. We relay each frame; the browser
writes its bytes straight into xterm.js, so the cursor, colours and
synchronized-output all render correctly and updates arrive as herdr produces
them — no polling, no screen scraping. Input is still sent through the socket
API with `pane.send_input` (observe is read-only, and this avoids taking an
exclusive control lock on a pane the user may be driving themselves).

Protocol, one JSON object per line each way:

    out  {"t":"frame","full":bool,"cols":N,"rows":N,"seq":N,"bytes":<base64 ANSI>}
    out  {"t":"closed","message":"..."} / {"t":"error","message":"..."}
    in   {"t":"input","ops":[{"text":"ls"},{"key":"Enter"}]}
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time

SOCKET_CANDIDATES = [
    "~/.config/herdr/herdr.sock",
    "~/.herdr/herdr.sock",
]
BIN_CANDIDATES = [
    "/opt/homebrew/bin/herdr",
    "/usr/local/bin/herdr",
    "~/.local/bin/herdr",
    "~/.nix-profile/bin/herdr",
    "/run/current-system/sw/bin/herdr",
    "/nix/var/nix/profiles/default/bin/herdr",
]


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def socket_path(explicit: str | None) -> str | None:
    for cand in filter(None, [explicit, os.environ.get("HERDR_SOCKET_PATH"), *SOCKET_CANDIDATES]):
        path = os.path.expanduser(cand)
        if os.path.exists(path):
            return path
    return None


def find_herdr(explicit: str | None) -> str | None:
    import shutil

    env_bin = os.environ.get("HERDR_BIN_PATH")
    for path in filter(None, [explicit, env_bin, "herdr", *BIN_CANDIDATES]):
        path = os.path.expanduser(path)
        found = shutil.which(path) if os.sep not in path else (path if os.access(path, os.X_OK) else None)
        if found:
            return found
    return None


class ApiError(RuntimeError):
    """herdr understood the request and refused it — retrying won't help."""


class HerdrClient:
    """One connection per call: herdr hangs up after answering a request."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.seq = 0

    def call(self, method: str, params: dict, timeout: float = 15.0) -> dict:
        with self.lock:
            self.seq += 1
            rid = f"hq{self.seq}"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self.path)
            fh = sock.makefile("rwb")
            fh.write((json.dumps({"id": rid, "method": method, "params": params}) + "\n").encode())
            fh.flush()
            while True:
                line = fh.readline()
                if not line:
                    raise ConnectionError("herdr closed the connection")
                msg = json.loads(line)
                if msg.get("id") != rid:
                    continue  # ignore anything we didn't ask for
                if "error" in msg:
                    raise ApiError(str(msg["error"].get("message", msg["error"])))
                return msg.get("result", {})
        finally:
            try:
                sock.close()
            except OSError:
                pass


def observe_loop(binary: str, pane_id: str, cols: int, rows: int,
                 env: dict, stop: threading.Event) -> None:
    """Relay `herdr terminal session observe` frames to stdout, restarting the
    stream if it drops (the pane closing ends us for good)."""
    cmd = [binary, "terminal", "session", "observe", pane_id,
           "--cols", str(cols), "--rows", str(rows)]
    failures = 0
    while not stop.is_set():
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            )
        except OSError as exc:
            emit({"t": "error", "message": f"cannot start herdr observe: {exc}"})
            stop.set()
            return
        try:
            for raw in proc.stdout:
                if stop.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = frame.get("type")
                if kind == "terminal.frame":
                    failures = 0
                    emit({
                        "t": "frame",
                        "full": bool(frame.get("full")),
                        "cols": frame.get("width"),
                        "rows": frame.get("height"),
                        "seq": frame.get("seq"),
                        "bytes": frame.get("bytes", ""),
                    })
                elif kind == "terminal.closed":
                    emit({"t": "closed", "message": frame.get("reason") or "pane closed"})
                    stop.set()
                    return
        finally:
            try:
                proc.terminate()
            except OSError:
                pass
        if stop.is_set():
            return
        # the stream ended without a terminal.closed — herdr restarted, network
        # blipped, etc. Retry with backoff; give up only after persistent failure.
        detail = ""
        try:
            detail = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()[:300]
        except (OSError, ValueError):
            pass
        failures += 1
        if failures in (3, 20):
            emit({"t": "error", "message": f"observe stream dropped: {detail or 'no output'}"})
        if failures > 40:
            emit({"t": "error", "message": f"giving up on observe stream: {detail or 'repeated drops'}"})
            stop.set()
            return
        stop.wait(min(2.0, 0.2 * failures))


def input_loop(client: HerdrClient, pane_id: str, stop: threading.Event) -> None:
    for line in sys.stdin:
        if stop.is_set():
            return
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("t") != "input":
            continue
        for params in input_batches(msg.get("ops") or []):
            try:
                client.call("pane.send_input", {"pane_id": pane_id, **params})
            except Exception as exc:  # noqa: BLE001 - a rejected keystroke shouldn't kill the mirror
                emit({"t": "error", "message": f"input failed: {exc}"})
                break
    stop.set()


def input_batches(ops: list) -> list[dict]:
    """Order matters, so text and key runs stay separate — but runs of keys coalesce."""
    batches: list[dict] = []
    keys: list[str] = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        if op.get("key"):
            keys.append(str(op["key"]))
            continue
        if keys:
            batches.append({"keys": keys})
            keys = []
        if op.get("text"):
            batches.append({"text": str(op["text"])})
    if keys:
        batches.append({"keys": keys})
    return batches


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pane", required=True, help="herdr pane id, e.g. wC:p1")
    ap.add_argument("--cols", type=int, default=200, help="observe stream width")
    ap.add_argument("--rows", type=int, default=50, help="observe stream height")
    ap.add_argument("--socket", default=None, help="path to herdr.sock")
    ap.add_argument("--bin", default=None, help="path to the herdr binary")
    ap.add_argument("--interval", type=float, default=0.0, help="(ignored; kept for compatibility)")
    args = ap.parse_args()

    path = socket_path(args.socket)
    if not path:
        emit({"t": "error", "message": "herdr socket not found on this host"})
        return 1
    binary = find_herdr(args.bin)
    if not binary:
        emit({"t": "error", "message": "herdr binary not found on this host"})
        return 1

    client = HerdrClient(path)
    try:
        client.call("pane.get", {"pane_id": args.pane})
    except Exception as exc:  # noqa: BLE001 - fail fast with a clear reason
        emit({"t": "error", "message": f"cannot reach pane {args.pane}: {exc}"})
        return 1

    env = dict(os.environ)
    if args.socket or os.environ.get("HERDR_SOCKET_PATH"):
        env["HERDR_SOCKET_PATH"] = path

    stop = threading.Event()
    reader = threading.Thread(
        target=observe_loop,
        args=(binary, args.pane, max(20, args.cols), max(4, args.rows), env, stop),
        daemon=True,
    )
    reader.start()

    try:
        input_loop(client, args.pane, stop)
    except (KeyboardInterrupt, OSError):
        pass
    finally:
        stop.set()
        reader.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
