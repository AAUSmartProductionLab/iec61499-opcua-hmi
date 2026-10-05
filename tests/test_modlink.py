"""modlink against the simulated controller: discovery, link and module."""

from __future__ import annotations

import asyncio

import pytest
from asyncua import Client, ua

import modlink.link as link_module
from hmi import profiles as prof
from modlink import Interface, Link, Module, ModuleState, Refused, SkillState, discover
from modlink.codes import ErrorId

from test_integration import Simulator


@pytest.fixture(scope="module")
def filling():
    simulator = Simulator(["filling"])
    simulator.start()
    yield simulator
    simulator.stop()


def run(coroutine):
    return asyncio.run(coroutine)


async def connected(endpoint: str, interface: Interface, **options) -> Link:
    link = Link(endpoint, [interface], sampling_ms=100, **options)
    await link.start()
    assert await link.wait_connected(20)
    return link


def test_discovery_finds_the_address_space_the_profile_describes(filling):
    found = run(discover(filling.endpoint, "Filling"))
    described = prof.interface(prof.FILLING)
    assert set(described.variables) <= set(found.variables)
    assert set(described.methods) <= set(found.methods)
    assert found.skills() == ["MoveNeedleUp", "MoveNeedleDown", "AttachNeedle", "Tare", "Weigh", "Dispensing"]


def test_a_method_answers_only_on_its_own_object(filling):
    """open62541 (the controller) refuses a call whose object is not the method's owner."""

    async def calls():
        async with Client(filling.endpoint) as client:
            owner = await client.nodes.objects.get_child(["1:Filling", "1:Occupation"])
            method = await owner.get_child(["1:Occupy"])
            argument = ua.Variant("probe-session", ua.VariantType.String)
            with pytest.raises(ua.UaStatusCodeError) as refused:
                await method.call_method(method.nodeid, argument)
            assert refused.value.code == ua.StatusCodes.BadNodeClassInvalid
            assert await owner.call_method(method.nodeid, argument) == [True, 0]
            assert await owner.call_method("1:Release", argument) == [True, 0]

    run(calls())


def test_the_link_calls_methods_on_their_object_and_sees_the_change(filling):
    async def scenario():
        link = await connected(filling.endpoint, prof.interface(prof.FILLING))
        try:
            answer = await link.call("Filling", "Occupation/Occupy", "link-session")
            assert answer.ok and answer.accepted, answer
            assert await link.wait_for("Filling", "Occupation/Occupied", bool, timeout=5)
            refused = await link.call("Filling", "Occupation/Occupy", "someone-else")
            assert (refused.accepted, refused.error_id) == (False, ErrorId.NOT_PERMITTED)
            assert (await link.call("Filling", "Occupation/Release", "link-session")).accepted
            missing = await link.call("Filling", "Skills/Ghost/Start", "link-session")
            assert not missing.ok and "not resolved" in missing.transport_error
        finally:
            await link.stop()
        assert not link.connected
        assert (await link.call("Filling", "Occupation/Occupy", "x")).transport_error

    run(scenario())


def test_a_quiet_module_keeps_its_connection(filling, monkeypatch):
    """No data change for a while is normal; the link probes instead of reconnecting."""
    monkeypatch.setattr(link_module, "SILENCE_LIMIT", 1.0)

    async def scenario():
        link = await connected(filling.endpoint, prof.interface(prof.FILLING))
        try:
            since = link.state.since
            await asyncio.sleep(4.0)
            assert link.connected and link.state.since == since, list(link.log)
            assert not any("disconnected" in line for line in link.log), list(link.log)
        finally:
            await link.stop()

    run(scenario())


def test_a_module_runs_a_skill_to_its_end_for_an_agent(filling):
    async def scenario():
        link = await connected(filling.endpoint, prof.interface(prof.FILLING))
        module = Module(link, "Filling", session="agent")
        changes = []
        link.add_listener(lambda change: changes.append(change.path))
        try:
            with pytest.raises(Refused):
                await module.command("Reset")             # not occupied yet
            await module.occupy()
            await module.bring_to_execute()
            assert module.state == ModuleState.EXECUTE
            tare = await module.run("Tare", timeout=10)
            assert tare.succeeded and tare.error_id == 0
            weigh = await module.run("Weigh", timeout=10)
            assert weigh.state == SkillState.SUCCEEDED and weigh.results == {"Weight": 0.0}
            with pytest.raises(Refused) as busy:
                await module.skill("Dispensing", "Start", 1.0)
                await module.skill("Dispensing", "Start", 1.0)
            assert busy.value.answer.error_id == ErrorId.BUSY
            await module.skill("Dispensing", "Stop")
            stopped = await module.wait_skill_end("Dispensing", timeout=10)
            assert (stopped.state, stopped.error_id) == (SkillState.FAILED, ErrorId.INTERRUPTED)
            assert "Skills/Dispensing/Execute/MoveNeedleDown/State" in changes
            await module.command("Stop")
            await module.wait_state(ModuleState.STOPPED, timeout=30)
            await module.release()
        finally:
            await link.stop()

    run(scenario())
