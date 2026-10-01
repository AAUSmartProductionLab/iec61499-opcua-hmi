"""The OPC UA link against the simulator, where it differs from a lenient server."""

from __future__ import annotations

import asyncio
import time

import pytest
from asyncua import Client, ua

from hmi import link as link_module
from hmi import profiles as prof
from hmi.link import OpcuaLink

from test_integration import Simulator, wait_for


@pytest.fixture(scope="module")
def quiet():
    """The stoppering module alone: nothing changes while it stands still."""
    simulator = Simulator(["stoppering"])
    simulator.start()
    yield simulator
    simulator.stop()


def test_a_method_answers_only_on_its_own_object(quiet):
    """open62541 (the controller) refuses a call whose object is not the method's owner."""

    async def calls():
        async with Client(quiet.endpoint) as client:
            root = await client.nodes.objects.get_child(["1:Stoppering"])
            owner = await root.get_child(["1:Occupation"])
            method = await owner.get_child(["1:Occupy"])
            argument = ua.Variant("probe-session", ua.VariantType.String)
            with pytest.raises(ua.UaStatusCodeError) as refused:
                await method.call_method(method.nodeid, argument)
            assert refused.value.code == ua.StatusCodes.BadNodeClassInvalid
            assert await owner.call_method(method.nodeid, argument) == [True, 0]
            assert await owner.call_method("1:Release", argument) == [True, 0]

    asyncio.run(calls())


def test_the_link_calls_methods_on_their_object(quiet):
    link = OpcuaLink(quiet.endpoint, [prof.STOPPERING], sampling_ms=100)
    link.start()
    try:
        assert wait_for(lambda: link.state.connected, timeout=20)
        result = link.call("stoppering", "Occupation/Occupy", ["link-session"])
        assert result.ok and result.accepted, result
        assert link.call("stoppering", "Occupation/Release", ["link-session"]).accepted
    finally:
        link.stop()


def test_a_quiet_module_keeps_its_connection(quiet, monkeypatch):
    """No data change for a while is normal; the link probes instead of reconnecting."""
    monkeypatch.setattr(link_module, "SILENCE_LIMIT", 1.0)
    link = OpcuaLink(quiet.endpoint, [prof.STOPPERING], sampling_ms=100)
    link.start()
    try:
        assert wait_for(lambda: link.state.connected, timeout=20)
        since = link.state.since
        time.sleep(5.0)
        assert link.state.connected
        assert link.state.since == since, link.log_lines()
        assert not any("disconnected" in line for line in link.log_lines()), link.log_lines()
    finally:
        link.stop()
