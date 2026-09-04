import asyncio

from herdrhq.config import Config, HostSpec
from herdrhq.events import EventBridge, EventHub
from herdrhq.fleet import HostPoller
from herdrhq.pool import SSHPool


def make_poller():
    cfg = Config()
    spec = HostSpec(name="local", transport="local")
    poller = HostPoller(spec, cfg, source="", pool=SSHPool())
    poller.state["data"] = {"panes": [
        {"pane_id": "w1:p1", "agent_status": "working", "title": "old",
         "is_agent": True, "state_change_seq": 4},
    ]}
    return poller


def test_pane_patch_publishes_and_updates_cache():
    async def go():
        poller = make_poller()
        hub = EventHub()
        q = hub.subscribe()
        bridge = EventBridge(poller, hub)
        bridge.handle("pane_updated", {"pane": {
            "pane_id": "w1:p1", "agent_status": "done", "focused": True,
        }})
        msg = q.get_nowait()
        assert msg["type"] == "pane" and msg["host"] == "local"
        assert msg["agent_status"] == "done"
        pane = poller.state["data"]["panes"][0]
        assert pane["agent_status"] == "done" and pane["focused"] is True
        assert pane["title"] == "old"  # untouched: the event carried no title
        # an identical event changes nothing and stays silent
        bridge.handle("pane_updated", {"pane": {"pane_id": "w1:p1", "agent_status": "done"}})
        assert q.empty()
        return True

    assert asyncio.run(go())


def test_unknown_pane_and_structural_wake():
    async def go():
        poller = make_poller()
        hub = EventHub()
        q = hub.subscribe()
        bridge = EventBridge(poller, hub)
        # a pane we never polled: schedule a re-poll instead of patching
        bridge.handle("pane_updated", {"pane": {"pane_id": "w9:p9", "agent_status": "idle"}})
        assert bridge._wake_handle is not None
        assert q.empty()
        # structural events debounce into the same pending wake and announce
        bridge.handle("pane_created", {"pane": {"pane_id": "w9:p9"}})
        assert q.get_nowait() == {"type": "structure", "host": "local"}
        assert not poller.wake.is_set()  # not yet: debounced
        bridge._wake_handle.cancel()  # don't leak the timer into other tests
        return True

    assert asyncio.run(go())


def test_bridge_status_dedup():
    async def go():
        poller = make_poller()
        hub = EventHub()
        q = hub.subscribe()
        bridge = EventBridge(poller, hub)
        bridge.set_status("live")
        bridge.set_status("live")
        assert q.get_nowait()["status"] == "live"
        assert q.empty()
        assert poller.snapshot()["events"] == "live"
        return True

    assert asyncio.run(go())


def test_hub_drops_when_full():
    hub = EventHub()
    q = hub.subscribe()
    for i in range(300):  # queue maxsize is 256; overflow must not raise
        hub.publish({"n": i})
    assert q.qsize() == 256
