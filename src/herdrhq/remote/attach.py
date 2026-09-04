#!/usr/bin/env python3
"""Mirror one herdr pane over stdout, and forward keystrokes from stdin.

Runs on the machine that owns the pane; the dashboard pipes it there over ssh
exactly like collector.py. It speaks herdr's unix socket directly (newline
delimited JSON) because the CLI has no streaming mode.

herdr publishes no raw output stream, so this polls `pane.read` for the visible
screen (ANSI intact) and emits a frame whenever the screen changes. Input is
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


def open_subscription(path: str) -> tuple[socket.socket, "io.BufferedRWPair"]:
    """A held connection streaming pane_updated events (revision bumps on
    output change). Broadcast: fires for every pane; we filter to ours."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(0.3)
    sock.connect(path)
    fh = sock.makefile("rwb")
    req = {"id": "hqsub", "method": "events.subscribe",
           "params": {"subscriptions": [{"type": "pane.updated"}]}}
    fh.write((json.dumps(req) + "\n").encode())
    fh.flush()
    # first framed reply is the subscription_started ack
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        line = fh.readline()
        if not line:
            raise ConnectionError("herdr closed before ack")
        msg = json.loads(line)
        if msg.get("id") == "hqsub":
            if "error" in msg:
                raise ApiError(str(msg["error"].get("message", msg["error"])))
            return sock, fh
    raise ConnectionError("no subscription ack")


class _Reader:
    """Reads the pane on demand, emitting a frame only when the screen text
    changes. Keeps the last text so callers can just say 'read now'."""

    def __init__(self, client: HerdrClient, pane_id: str):
        self.client = client
        self.pane_id = pane_id
        self.last = None

    def read_and_emit(self) -> bool | None:
        """True on success, None on a transient failure, False on a fatal one."""
        try:
            result = self.client.call(
                "pane.read",
                {"pane_id": self.pane_id, "source": "visible",
                 "format": "ansi", "strip_ansi": False},
            )
        except ApiError as exc:  # unknown/closed pane — won't fix itself
            emit({"t": "error", "message": str(exc)})
            return False
        except (OSError, ValueError, ConnectionError):
            return None
        text = (result.get("read") or {}).get("text", "")
        if text != self.last:
            self.last = text
            cols, rows = screen_size(text)
            emit({
                "t": "screen",
                "text": base64.b64encode(text.encode("utf-8", "replace")).decode(),
                "cols": cols,
                "rows": rows,
            })
        return True


def read_loop(client: HerdrClient, pane_id: str, min_interval: float,
              stop: threading.Event) -> None:
    """Event-driven pane mirror: read the moment herdr reports output changed,
    not on a fixed timer. herdr coalesces output to ~10 Hz internally, so this
    tracks its true cadence (and idles silently) instead of the old 4 Hz poll,
    which both lagged and wasted reads. A slow safety poll covers missed
    events, dropped subscriptions, and pane exit; a keepalive idle frame keeps
    the SSE pipe warm."""
    reader = _Reader(client, pane_id)
    min_interval = max(0.03, min_interval)  # cap the read rate; ~herdr's cadence
    safety = 1.5   # forced read when quiet (missed event / liveness)
    idle_after = 20.0

    if reader.read_and_emit() is False:
        stop.set()
        return
    last_read = time.monotonic()
    last_change = last_read
    dirty = False
    failures = 0
    sub_sock = sub_fh = None
    backoff = 0.5

    def do_read() -> bool:
        nonlocal last_read, dirty, failures, last_change
        r = reader.read_and_emit()
        last_read = time.monotonic()
        if r is False:
            stop.set()
            return False
        if r is None:
            failures += 1
            if failures in (3, 30):
                emit({"t": "error", "message": "read failed"})
            if failures > 60:
                emit({"t": "error", "message": "giving up after repeated read failures"})
                stop.set()
                return False
        else:
            failures = 0
            last_change = last_read
        dirty = False
        return True

    try:
        while not stop.is_set():
            if sub_fh is None:
                try:
                    sub_sock, sub_fh = open_subscription(client.path)
                    backoff = 0.5
                except ApiError as exc:  # server refused the subscription
                    emit({"t": "error", "message": f"event subscription refused: {exc}"})
                    # degrade to timed polling rather than give up
                    sub_fh = "poll"
                except (OSError, ValueError, ConnectionError):
                    stop.wait(min(backoff, 2.0))
                    backoff = min(5.0, backoff * 2)
                    continue

            now = time.monotonic()
            # read when dirty (rate-capped) or on the safety interval
            if dirty and now - last_read >= min_interval:
                if not do_read():
                    return
            elif now - last_read >= safety:
                if not do_read():
                    return
                if now - last_change >= idle_after:
                    emit({"t": "idle"})  # keeps the SSE pipe warm through proxies

            if sub_fh == "poll":  # no subscription: fall back to timed reads
                dirty = True
                stop.wait(max(min_interval, 0.15))  # gentler without events
                continue

            try:
                line = sub_fh.readline()
            except socket.timeout:
                continue  # quiet moment; loop re-checks the safety timer
            except (OSError, ValueError):
                sub_fh = None
                continue
            if not line:  # subscription closed
                sub_fh = None
                stop.wait(min(backoff, 2.0))
                backoff = min(5.0, backoff * 2)
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg:
                continue  # the ack, already handled
            data = msg.get("data") or {}
            kind = msg.get("event")
            if kind == "pane_updated" and (data.get("pane") or {}).get("pane_id") == pane_id:
                dirty = True
            elif kind in ("pane_exited", "pane_closed") and data.get("pane_id") == pane_id:
                emit({"t": "error", "message": "pane exited"})
                stop.set()
                return
    finally:
        if sub_sock is not None:
            try:
                sub_sock.close()
            except OSError:
                pass


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
    reader = threading.Thread(target=read_loop, args=(client, args.pane, max(0.05, args.interval), stop))
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
