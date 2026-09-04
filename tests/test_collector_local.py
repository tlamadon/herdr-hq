"""Run the real collector on this machine through the transport layer.

Works whether or not herdr is installed: the envelope shape is the contract.
"""

import asyncio
import json

from herdrhq.fleet import _script
from herdrhq.transport import LocalTransport


def test_collector_envelope():
    async def go():
        t = LocalTransport()
        rc, out, err = await t.run(_script("collector.py"), ["--interval", "0.2"], timeout=60)
        assert rc == 0, err
        data = json.loads(out)
        assert set(data) >= {"schema", "collected_at", "machine", "herdr", "panes", "warnings"}
        assert data["schema"] == 1
        assert "cpu_pct" in data["machine"]
        return True

    assert asyncio.run(go())
