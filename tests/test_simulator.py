"""The simulated modules against cell/modules/filling.yaml and stoppering.yaml.

Runs a SimulatedModule on its own clock, without an OPC UA server.
"""

from __future__ import annotations

import asyncio

import pytest

from hmi import model
from hmi import profiles as prof
from sim.fake_module import FAULT_INVARIANT, FAULT_SENSOR, SimulatedModule

SESSION = "sim-test"
DT = 0.01


def module(key: str, state: int = model.EXECUTE) -> SimulatedModule:
    sim = SimulatedModule(prof.get_profile(key))
    sim.holder = SESSION
    sim.module_state = state
    return sim


def run(coroutine):
    return asyncio.run(coroutine)


async def advance(sim: SimulatedModule, seconds: float) -> None:
    for _ in range(int(round(seconds / DT))):
        for work in sim.tick(DT):
            await work


async def until(sim: SimulatedModule, done, limit: float = 30.0) -> float:
    """Simulated seconds until done() holds."""
    start = sim.clock
    while not done():
        assert sim.clock - start < limit, "timed out"
        await advance(sim, DT)
    return sim.clock - start


async def start(sim: SimulatedModule, skill: str, *params: float) -> None:
    accepted, error = await sim.on_skill_command(skill, "Start", SESSION, list(params))
    assert (accepted.Value, error.Value) == (True, 0), skill


def state(sim: SimulatedModule, skill: str) -> int:
    return sim.skills[skill].state


def test_the_needle_travels_in_two_seconds_with_its_start_boost():
    """travel_s 2.0, dead_s 0.15, 190 instead of 140 for the first 200 ms."""
    sim = module("filling")

    async def scenario():
        await start(sim, "MoveNeedleDown")
        took = await until(sim, lambda: state(sim, "MoveNeedleDown") != model.SK_RUNNING)
        assert state(sim, "MoveNeedleDown") == model.SK_SUCCEEDED
        assert 2.05 < took < 2.25, took
        assert sim.inputs["NeedleAxis/AtBottom"] and not sim.inputs["NeedleAxis/AtTop"]
        await start(sim, "AttachNeedle")      # already at the bottom: done at once
        assert state(sim, "AttachNeedle") == model.SK_SUCCEEDED

    run(scenario())


def test_tare_zeroes_the_vial_and_dispensing_fills_about_three_grams():
    """3 mL per second during the 1 s dwell, at a density of 0.98..1.06 g/mL."""
    sim = module("filling")

    async def scenario():
        assert sim.inputs["Scale/Weight"] == pytest.approx(9.75)
        await start(sim, "Tare")
        took = await until(sim, lambda: state(sim, "Tare") == model.SK_SUCCEEDED)
        assert took == pytest.approx(2.0, abs=0.05)
        assert sim.inputs["Scale/Weight"] == 0.0
        await start(sim, "Dispensing")
        took = await until(sim, lambda: state(sim, "Dispensing") == model.SK_SUCCEEDED)
        # down 2.1 s, dwell 1.0 s, up 2.1 s, weigh 0.2 s
        assert 5.2 < took < 5.8, took
        weight = sim.skills["Dispensing"].results["Weight"]
        assert 2.9 <= weight <= 3.2, weight
        assert sim.inputs["Scale/Weight"] == weight
        assert sim.inputs["NeedleAxis/AtTop"]
        await start(sim, "Weigh")
        took = await until(sim, lambda: state(sim, "Weigh") == model.SK_SUCCEEDED)
        assert took == pytest.approx(0.2, abs=0.05)

    run(scenario())


def test_nothing_flows_unless_the_needle_is_down():
    sim = module("filling")

    async def scenario():
        await start(sim, "MoveNeedleDown")
        await until(sim, lambda: state(sim, "MoveNeedleDown") == model.SK_SUCCEEDED)
        await start(sim, "MoveNeedleUp")
        await until(sim, lambda: state(sim, "MoveNeedleUp") == model.SK_SUCCEEDED)
        assert sim.inputs["Scale/Weight"] == pytest.approx(9.75)

    run(scenario())


def test_the_piston_leaves_its_limit_switch_when_raised():
    """travel_s 3.0, starts half way; RaisePiston is open loop for Duration."""
    sim = module("stoppering")

    async def scenario():
        assert not sim.inputs["Piston/AtLimit"]
        await start(sim, "LowerPiston")
        took = await until(sim, lambda: state(sim, "LowerPiston") == model.SK_SUCCEEDED)
        assert took == pytest.approx(1.5, abs=0.05)
        assert sim.inputs["Piston/AtLimit"]
        await start(sim, "RaisePiston", 1.0)
        took = await until(sim, lambda: state(sim, "RaisePiston") == model.SK_SUCCEEDED)
        assert took == pytest.approx(1.0, abs=0.05)
        assert not sim.inputs["Piston/AtLimit"]
        await start(sim, "LowerPiston")
        took = await until(sim, lambda: state(sim, "LowerPiston") == model.SK_SUCCEEDED)
        assert took == pytest.approx(1.0, abs=0.05)

    run(scenario())


def test_open_loop_skills_take_exactly_their_duration():
    sim = module("stoppering")

    async def scenario():
        for skill, params, seconds in (
            ("ExtendPlunger", (3.0,), 3.0),
            ("RetractPlunger", (2.5,), 2.5),
            ("MoveArm", (45.0, 0.5), 0.5),
        ):
            await start(sim, skill, *params)
            took = await until(sim, lambda s=skill: state(sim, s) == model.SK_SUCCEEDED)
            assert took == pytest.approx(seconds, abs=0.05), skill
        assert sim.angles["StopperArm"] == 45.0

    run(scenario())


def test_a_stuck_open_switch_fails_with_timeout_after_eight_seconds():
    sim = module("filling")
    sim.faults.add(FAULT_SENSOR)

    async def scenario():
        await start(sim, "MoveNeedleDown")
        took = await until(sim, lambda: state(sim, "MoveNeedleDown") == model.SK_FAILED)
        assert took == pytest.approx(8.0, abs=0.05)
        assert sim.skills["MoveNeedleDown"].error_id == model.ERR_TIMEOUT

    run(scenario())


def test_a_stuck_closed_switch_breaks_the_needle_invariant():
    sim = module("filling")
    sim.faults.add(FAULT_INVARIANT)

    async def scenario():
        await start(sim, "MoveNeedleDown")
        await until(sim, lambda: state(sim, "MoveNeedleDown") == model.SK_FAILED)
        assert sim.skills["MoveNeedleDown"].error_id == model.ERR_INVARIANT
        assert sim.inputs["NeedleAxis/AtTop"] and sim.inputs["NeedleAxis/AtBottom"]

    run(scenario())


def test_stop_while_resetting_waits_for_the_resetting_procedure():
    """Steps started by a parent do not halt on module Stop (iec61499-mgmt-py da9c4a2)."""
    sim = module("filling", model.STOPPED)

    async def scenario():
        await sim.place("NeedleAxis", 1.0)
        await sim.on_module_command("Reset", SESSION)
        await advance(sim, 0.5)
        await sim.on_module_command("Stop", SESSION)
        assert sim.module_state == model.STOPPING
        assert sim.procedures["Resetting"].active
        await until(sim, lambda: sim.module_state == model.STOPPED)
        assert sim.steps["Procedures/Resetting/MoveNeedleUp"].state == model.SK_SUCCEEDED
        assert sim.steps["Procedures/Stopping/MoveNeedleUp"].state == model.SK_SUCCEEDED

    run(scenario())


def test_a_failed_resetting_procedure_aborts_the_module():
    sim = module("filling", model.STOPPED)
    sim.faults.add(FAULT_SENSOR)

    async def scenario():
        await sim.place("NeedleAxis", 1.0)
        await sim.on_module_command("Reset", SESSION)
        await until(sim, lambda: sim.module_state == model.ABORTED)
        assert sim.steps["Procedures/Resetting/MoveNeedleUp"].error_id == model.ERR_TIMEOUT

    run(scenario())
