"""The stdin bootstrap handshake, end to end, without ssh or herdr."""

import asyncio
import json

import pytest

from herdrhq.term import TerminalSession
from herdrhq.transport import LocalTransport

# A stand-in for attach.py: prints one frame, then echoes input ops back.
FAKE_PANE = """
import sys, json
print(json.dumps({"t": "screen", "argv": sys.argv[1:]}), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("t") == "input":
        print(json.dumps({"t": "echo", "ops": msg["ops"]}), flush=True)
"""

ONESHOT = """
import sys, json
print(json.dumps({"argv": sys.argv[1:], "stdin_rest": sys.stdin.read()}))
"""


def test_run_oneshot():
    async def go():
        t = LocalTransport()
        rc, out, err = await t.run(ONESHOT, ["--flag", "value"], timeout=10)
        assert rc == 0
        data = json.loads(out)
        # `python -` consumed the whole of stdin as the program
        assert data["argv"] == ["--flag", "value"]
        assert data["stdin_rest"] == ""
        return True

    assert asyncio.run(go())


def test_spawn_keeps_stdin_for_input():
    async def go():
        t = LocalTransport()
        proc = await t.spawn(FAKE_PANE, ["--pane", "w:p1"], keep_stdin=True)
        first = json.loads(await proc.readline())
        assert first == {"t": "screen", "argv": ["--pane", "w:p1"]}
        await proc.send(json.dumps({"t": "input", "ops": [{"text": "ls"}]}).encode() + b"\n")
        echo = json.loads(await proc.readline())
        assert echo == {"t": "echo", "ops": [{"text": "ls"}]}
        proc.close()
        return True

    assert asyncio.run(go())


# A stand-in for the observe stream: emits one full frame, then echoes input.
FAKE_FRAMES = """
import sys, json, base64
print(json.dumps({"t": "frame", "full": True, "cols": 80, "rows": 24, "seq": 1,
                  "bytes": base64.b64encode(b"hi").decode()}), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("t") == "input":
        print(json.dumps({"t": "echo", "ops": msg["ops"]}), flush=True)
"""


def test_terminal_session_pump_and_input():
    async def go():
        t = LocalTransport()
        session = await TerminalSession.open("local", t, "w:p1", FAKE_FRAMES)
        q = session.subscribe()
        msg = await asyncio.wait_for(q.get(), 5)
        assert msg["t"] == "frame" and msg["full"] is True
        await session.send_input([{"key": "Enter"}])
        msg = await asyncio.wait_for(q.get(), 5)
        assert msg == {"t": "echo", "ops": [{"key": "Enter"}]}
        session.close()
        return True

    assert asyncio.run(go())


def test_run_timeout():
    async def go():
        t = LocalTransport()
        with pytest.raises(asyncio.TimeoutError):
            await t.run("import time; time.sleep(30)", [], timeout=0.5)
        return True

    assert asyncio.run(go())
