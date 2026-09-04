#!/usr/bin/env python3
"""Stream herdr events over stdout, one JSON object per line.

Runs on the machine that owns the herdr session; the dashboard pipes it there
over ssh exactly like collector.py. It holds one long-lived connection to
herdr's unix socket: unlike plain requests (where herdr hangs up after
answering), an events.subscribe connection stays open and streams.

Only broadcast (type-only) subscriptions are used — per-pane kinds like
pane.agent_status_changed need a pane_id, but broadcast pane.updated carries
the full pane info including agent_status, which covers status flips.

Protocol out:

    {"t":"ready"}                                   subscription acknowledged
    {"t":"event","event":"pane_updated","data":{}}  one herdr event envelope
    {"t":"idle"}                                    heartbeat (quiet ~20s)
    {"t":"error","message":"..."}                   fatal; the server respawns
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time

SOCKET_CANDIDATES = [
    "~/.config/herdr/herdr.sock",
    "~/.herdr/herdr.sock",
]

SUBSCRIPTIONS = [
    "pane.created", "pane.closed", "pane.updated", "pane.exited",
    "pane.agent_detected",
    "tab.created", "tab.closed", "tab.renamed",
    "workspace.created", "workspace.closed", "workspace.renamed",
    "layout.updated",
]

HEARTBEAT = 20.0
# pane.updated fires on every output revision; one update per pane per this
# window is plenty for a dashboard.
COALESCE = 0.25


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def socket_path(explicit: str | None) -> str | None:
    for cand in filter(None, [explicit, os.environ.get("HERDR_SOCKET_PATH"), *SOCKET_CANDIDATES]):
        path = os.path.expanduser(cand)
        if os.path.exists(path):
            return path
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--socket", default=None, help="path to herdr.sock")
    args = ap.parse_args()

    path = socket_path(args.socket)
    if not path:
        emit({"t": "error", "message": "herdr socket not found on this host"})
        return 1

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(HEARTBEAT)
    try:
        sock.connect(path)
        fh = sock.makefile("rwb")
        request = {
            "id": "hq-events",
            "method": "events.subscribe",
            "params": {"subscriptions": [{"type": t} for t in SUBSCRIPTIONS]},
        }
        fh.write((json.dumps(request) + "\n").encode())
        fh.flush()
    except OSError as exc:
        emit({"t": "error", "message": f"cannot reach herdr: {exc}"})
        return 1

    ready = False
    last_pane_update: dict[str, float] = {}  # pane_id -> last emit time
    try:
        while True:
            try:
                line = fh.readline()
            except socket.timeout:
                emit({"t": "idle"})
                continue
            if not line:
                emit({"t": "error", "message": "herdr closed the event stream"})
                return 1
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not ready:
                if msg.get("id") == "hq-events":
                    if "error" in msg:
                        detail = msg["error"].get("message", msg["error"])
                        emit({"t": "error", "message": f"subscribe refused: {detail}"})
                        return 1
                    ready = True
                    emit({"t": "ready"})
                continue
            kind = msg.get("event")
            if not kind:
                continue
            if kind == "pane_updated":
                pane = (msg.get("data") or {}).get("pane") or {}
                pid = pane.get("pane_id")
                now = time.monotonic()
                if pid and now - last_pane_update.get(pid, 0) < COALESCE:
                    continue
                if pid:
                    last_pane_update[pid] = now
            emit({"t": "event", "event": kind, "data": msg.get("data")})
    except (OSError, KeyboardInterrupt) as exc:
        emit({"t": "error", "message": f"event stream failed: {exc}"})
        return 1
    finally:
        try:
            sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
