#!/usr/bin/env python3
"""Mirror one herdr pane over stdout, and forward keystrokes from stdin.

Runs on the machine that owns the pane; the dashboard pipes it there over ssh
exactly like collector.py. It speaks herdr's unix socket directly (newline
delimited JSON) because the CLI has no streaming mode.

herdr publishes no raw output stream, so this polls `pane.read` for the visible
screen (ANSI intact) and emits a frame whenever the screen changes. The poll is
adaptive — fast while output is moving, relaxed when the screen is still —
because pane.read answers in under a millisecond and the buffer reflects output
within a few ms, far quicker than herdr's ~10 Hz change *events*. Input is
forwarded verbatim with `pane.send_input`.

Protocol, one JSON object per line each way:

    out  {"t":"screen","text":<base64 of the ANSI screen>,"cols":N,"rows":N}
    out  {"t":"error","message":"..."}
    in   {"t":"input","text":"ls\\r"}
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import sys
import threading
import time

SOCKET_CANDIDATES = [
    "~/.config/herdr/herdr.sock",
    "~/.herdr/herdr.sock",
]

ANSI_RE = re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def socket_path(explicit: str | None) -> str | None:
    for cand in filter(None, [explicit, os.environ.get("HERDR_SOCKET_PATH"), *SOCKET_CANDIDATES]):
        path = os.path.expanduser(cand)
        if os.path.exists(path):
            return path
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

    def close(self) -> None:
        return None


def screen_size(text: str) -> tuple[int, int]:
    """Visible dimensions of the frame, ignoring escape sequences."""
    lines = text.split("\n")
    cols = max((len(ANSI_RE.sub("", line).rstrip("\r")) for line in lines), default=80)
    return max(20, cols), max(4, len(lines))


def read_loop(client: HerdrClient, pane_id: str, active_interval: float,
              stop: threading.Event) -> None:
    """Adaptive-polling pane mirror.

    herdr's pane.read is essentially free — it answers in <1 ms and its
    screen buffer reflects output within ~6 ms — but its pane_updated *events*
    are throttled to ~10 Hz. So we don't wait for events: we poll pane.read
    fast while output is moving (echo lands within one fast tick, near a real
    ssh terminal) and back off when the screen is quiet, so an idle pane costs
    almost nothing. A frame is emitted only when the screen text changes.
    """
    active_interval = max(0.015, active_interval)  # fast tick while typing/output
    idle_interval = 0.15                            # relaxed when the screen is still
    hot_window = 0.6                                # stay fast this long after a change
    idle_keepalive = 20.0

    last = None
    failures = 0
    hot_until = 0.0
    last_change = time.monotonic()

    while not stop.is_set():
        now = time.monotonic()
        try:
            result = client.call(
                "pane.read",
                {"pane_id": pane_id, "source": "visible",
                 "format": "ansi", "strip_ansi": False},
            )
            failures = 0
        except ApiError as exc:  # unknown/closed pane — won't fix itself
            emit({"t": "error", "message": str(exc)})
            stop.set()
            return
        except (OSError, ValueError, ConnectionError) as exc:
            failures += 1
            if failures in (3, 30):
                emit({"t": "error", "message": f"read failed: {exc}"})
            if failures > 90:
                emit({"t": "error", "message": f"giving up after {failures} failed reads: {exc}"})
                stop.set()
                return
            stop.wait(min(2.0, idle_interval * failures))
            continue

        text = (result.get("read") or {}).get("text", "")
        if text != last:
            last = text
            last_change = now
            hot_until = now + hot_window   # a change keeps us polling fast
            cols, rows = screen_size(text)
            emit({
                "t": "screen",
                "text": base64.b64encode(text.encode("utf-8", "replace")).decode(),
                "cols": cols,
                "rows": rows,
            })
        elif now - last_change >= idle_keepalive:
            last_change = now
            emit({"t": "idle"})  # keeps the SSE pipe warm through proxies

        interval = active_interval if now < hot_until else idle_interval
        stop.wait(interval)


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
    ap.add_argument("--interval", type=float, default=0.25, help="seconds between screen reads")
    ap.add_argument("--socket", default=None, help="path to herdr.sock")
    args = ap.parse_args()

    path = socket_path(args.socket)
    if not path:
        emit({"t": "error", "message": "herdr socket not found on this host"})
        return 1

    client = HerdrClient(path)
    try:
        client.call("pane.get", {"pane_id": args.pane})
    except Exception as exc:  # noqa: BLE001 - fail fast with a clear reason
        emit({"t": "error", "message": f"cannot reach pane {args.pane}: {exc}"})
        return 1

    stop = threading.Event()
    reader = threading.Thread(target=read_loop, args=(client, args.pane, args.interval, stop))
    reader.daemon = True
    reader.start()

    try:
        input_loop(client, args.pane, stop)
    except (KeyboardInterrupt, OSError):
        pass
    finally:
        stop.set()
        reader.join(timeout=2)
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
